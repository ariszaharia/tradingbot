from __future__ import annotations
import asyncio
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.storage.database import Database


# ── Performance snapshot returned by queries ─────────────────────────────────

@dataclass
class Metrics:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_duration_minutes: float = 0.0
    total_pnl_net: float = 0.0
    total_fees: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0


@dataclass
class PerformanceSummary:
    period: str
    metrics: Metrics
    per_strategy: dict[str, Metrics] = field(default_factory=dict)
    hourly_pnl: dict[int, float] = field(default_factory=dict)   # hour → net PnL


class JournalAgent(BaseAgent):
    """
    Permanent memory and performance analytics engine.

    Listens to every message type on the bus, persists to SQLite, and
    maintains in-memory running metrics for zero-latency queries.

    Alert thresholds (checked on every POSITION_CLOSED):
      • > 5 consecutive losing trades
      • Win rate < 40 % over last 20 trades
      • Fees > 10 % of gross profit over the last 7 days
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
        db_path: str = "trading_bot/journal.db",
    ) -> None:
        super().__init__(AgentName.JOURNAL, bus, config)
        self._db = Database(db_path)

        log_cfg = config.get("logging", {})
        self._alert_consec_losses: int = log_cfg.get("alert_consecutive_losses", 5)
        self._alert_wr_threshold: float = log_cfg.get("alert_win_rate_threshold", 0.40)
        self._alert_wr_window: int = log_cfg.get("alert_win_rate_window", 20)
        self._alert_fees_ratio: float = log_cfg.get("alert_fees_profit_ratio", 0.10)
        self._alert_fees_window_days: int = log_cfg.get("alert_fees_window_days", 7)

        # In-memory state for O(1) metric updates
        self._trades: list[dict] = []       # all closed trades (chronological)
        self._equity_curve: list[float] = []  # running capital after each trade
        self._consecutive_losses: int = 0
        self._snapshot_task: asyncio.Task | None = None
        self._daily_report_task: asyncio.Task | None = None

        # Strategy-level trade lists
        self._strategy_trades: dict[str, list[dict]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def _on_start(self) -> None:
        await self._db.connect()
        # Reload persisted trades into memory on startup
        self._trades = await self._db.get_trades()
        for t in self._trades:
            self._strategy_trades[t.get("strategy_name", "unknown")].append(t)
        self._rebuild_equity_curve()
        self._snapshot_task = asyncio.create_task(self._hourly_snapshot_loop())
        self._daily_report_task = asyncio.create_task(self._daily_report_loop())
        self.log.info("JournalAgent started", loaded_trades=len(self._trades))

    async def _on_stop(self) -> None:
        if self._snapshot_task:
            self._snapshot_task.cancel()
        if self._daily_report_task:
            self._daily_report_task.cancel()
        await self._db.close()

    # ------------------------------------------------------------------ #
    # Message handler                                                      #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        match msg.msg_type:
            case MessageType.TRADING_SIGNAL:
                await self._db.insert_signal(msg.payload)

            case MessageType.RISK_DECISION:
                await self._db.insert_risk_decision(msg.payload)

            case MessageType.POSITION_CLOSED:
                await self._on_trade_closed(msg.payload)

            case MessageType.EXECUTION_REPORT:
                # Log errors and high-slippage events
                if msg.payload.get("status") in ("ERROR", "REJECTED"):
                    await self._db.insert_error(
                        "EXECUTION",
                        f"{msg.payload.get('status')}: {msg.payload.get('error_message')}",
                    )

            case MessageType.ANOMALY_DETECTED:
                await self._db.insert_error("MARKET_DATA", msg.payload.get("reason", ""))

            case MessageType.PAUSE | MessageType.STOP:
                await self._db.insert_error(
                    "ORCHESTRATOR",
                    f"{msg.msg_type.value}: {msg.payload.get('reason', '')}",
                )

    # ------------------------------------------------------------------ #
    # Trade closed — core analytics update                                 #
    # ------------------------------------------------------------------ #

    async def _on_trade_closed(self, payload: dict) -> None:
        # Retrieve strategy name from signals table (best-effort)
        signal_id = payload.get("signal_id", "")
        strategy_name = await self._lookup_strategy(signal_id)
        payload["strategy_name"] = strategy_name
        payload["direction"] = await self._lookup_direction(signal_id)

        await self._db.insert_trade(payload, strategy_name)

        self._trades.append(payload)
        self._strategy_trades[strategy_name].append(payload)
        self._equity_curve.append(
            (self._equity_curve[-1] if self._equity_curve else 0.0) + payload["pnl_net"]
        )

        is_win = payload["pnl_net"] > 0
        self._consecutive_losses = 0 if is_win else self._consecutive_losses + 1

        self.log.info(
            "Trade recorded",
            signal_id=signal_id,
            strategy=strategy_name,
            pnl_net=round(payload["pnl_net"], 2),
            reason=payload.get("close_reason"),
            total_trades=len(self._trades),
        )

        await self._check_alerts(payload)

    # ------------------------------------------------------------------ #
    # Alert engine                                                         #
    # ------------------------------------------------------------------ #

    async def _check_alerts(self, latest_trade: dict) -> None:
        alerts: list[str] = []

        # Alert 1: consecutive losses
        if self._consecutive_losses >= self._alert_consec_losses:
            alerts.append(
                f"ALERT: {self._consecutive_losses} consecutive losing trades"
            )

        # Alert 2: win rate over last N trades
        window = self._trades[-self._alert_wr_window:]
        if len(window) >= self._alert_wr_window:
            wr = sum(1 for t in window if t["pnl_net"] > 0) / len(window)
            if wr < self._alert_wr_threshold:
                alerts.append(
                    f"ALERT: win rate {wr:.1%} < {self._alert_wr_threshold:.0%} "
                    f"over last {self._alert_wr_window} trades"
                )

        # Alert 3: fees vs gross profit over last 7 days
        cutoff_ms = int(time.time() * 1000) - self._alert_fees_window_days * 86_400_000
        recent = [t for t in self._trades if t.get("timestamp_close", 0) >= cutoff_ms]
        if recent:
            gross_profit = sum(t["pnl_gross"] for t in recent if t["pnl_gross"] > 0)
            total_fees = sum(t["fees_total"] for t in recent)
            if gross_profit > 0 and total_fees / gross_profit > self._alert_fees_ratio:
                alerts.append(
                    f"ALERT: fees ({total_fees:.2f}) = "
                    f"{total_fees/gross_profit:.1%} of gross profit over {self._alert_fees_window_days}d"
                )

        for alert in alerts:
            self.log.warning(alert)
            await self._db.insert_error("JOURNAL_ALERT", alert)
            await self._send(AgentMessage(
                sender=AgentName.JOURNAL,
                recipient=AgentName.ORCHESTRATOR,
                msg_type=MessageType.PERFORMANCE_ALERT,
                payload={"alert": alert, "timestamp": int(time.time() * 1000)},
            ))

    # ------------------------------------------------------------------ #
    # Public query API                                                     #
    # ------------------------------------------------------------------ #

    def get_performance_summary(self, period: str = "all") -> PerformanceSummary:
        trades = self._filter_by_period(self._trades, period)
        metrics = self._compute_metrics(trades, self._equity_curve_for(trades))
        per_strategy = {
            name: self._compute_metrics(
                self._filter_by_period(strat_trades, period), []
            )
            for name, strat_trades in self._strategy_trades.items()
        }
        hourly = self._hourly_pnl(trades)
        return PerformanceSummary(
            period=period,
            metrics=metrics,
            per_strategy=per_strategy,
            hourly_pnl=hourly,
        )

    def get_strategy_comparison(self) -> dict[str, Metrics]:
        return {
            name: self._compute_metrics(trades, [])
            for name, trades in self._strategy_trades.items()
        }

    async def get_trade_history(
        self,
        start_ms: int = 0,
        end_ms: int | None = None,
    ) -> list[dict]:
        return await self._db.get_trades(since_ms=start_ms, until_ms=end_ms)

    # ------------------------------------------------------------------ #
    # Metrics computation                                                  #
    # ------------------------------------------------------------------ #

    def _compute_metrics(self, trades: list[dict], equity_curve: list[float]) -> Metrics:
        if not trades:
            return Metrics()

        wins = [t for t in trades if t["pnl_net"] > 0]
        losses = [t for t in trades if t["pnl_net"] <= 0]

        gross_profit = sum(t["pnl_gross"] for t in wins) if wins else 0.0
        gross_loss = abs(sum(t["pnl_gross"] for t in losses)) if losses else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_rr = 0.0
        rr_values = []
        for t in trades:
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            sl = t.get("stop_loss", 0)
            if entry and sl and abs(entry - sl) > 1e-6:
                reward = abs(exit_ - entry)
                risk = abs(entry - sl)
                rr_values.append(reward / risk)
        if rr_values:
            avg_rr = sum(rr_values) / len(rr_values)

        sharpe = 0.0
        if len(trades) >= 30:
            pnls = [t["pnl_pct"] for t in trades]
            mean = sum(pnls) / len(pnls)
            variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
            std = math.sqrt(variance) if variance > 0 else 0.0
            sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0

        max_dd = self._max_drawdown(equity_curve) if equity_curve else 0.0

        pnl_nets = [t["pnl_net"] for t in trades]
        best = max(pnl_nets)
        worst = min(pnl_nets)

        return Metrics(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades),
            avg_rr=round(avg_rr, 4),
            profit_factor=round(profit_factor, 4),
            sharpe_ratio=round(sharpe, 4),
            max_drawdown_pct=round(max_dd, 4),
            avg_duration_minutes=round(
                sum(t["duration_minutes"] for t in trades) / len(trades), 1
            ),
            total_pnl_net=round(sum(pnl_nets), 4),
            total_fees=round(sum(t["fees_total"] for t in trades), 4),
            best_trade_pnl=round(best, 4),
            worst_trade_pnl=round(worst, 4),
        )

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / abs(peak) * 100 if peak != 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    @staticmethod
    def _hourly_pnl(trades: list[dict]) -> dict[int, float]:
        result: dict[int, float] = defaultdict(float)
        for t in trades:
            close_ts = t.get("timestamp_close", 0)
            hour = (close_ts // 3_600_000) % 24
            result[hour] += t["pnl_net"]
        return dict(result)

    def _equity_curve_for(self, trades: list[dict]) -> list[float]:
        running = 0.0
        curve = []
        for t in trades:
            running += t["pnl_net"]
            curve.append(running)
        return curve

    def _rebuild_equity_curve(self) -> None:
        running = 0.0
        self._equity_curve = []
        for t in self._trades:
            running += t["pnl_net"]
            self._equity_curve.append(running)

    @staticmethod
    def _filter_by_period(trades: list[dict], period: str) -> list[dict]:
        if period == "all":
            return trades
        now_ms = int(time.time() * 1000)
        delta = {
            "today": 86_400_000,
            "week": 7 * 86_400_000,
            "month": 30 * 86_400_000,
        }.get(period, 0)
        if delta == 0:
            return trades
        cutoff = now_ms - delta
        return [t for t in trades if t.get("timestamp_close", 0) >= cutoff]

    # ------------------------------------------------------------------ #
    # DB lookup helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _lookup_strategy(self, signal_id: str) -> str:
        try:
            async with self._db._db.execute(
                "SELECT strategy_name FROM signals WHERE signal_id = ?", (signal_id,)
            ) as cur:
                row = await cur.fetchone()
            return row["strategy_name"] if row else "unknown"
        except Exception:
            return "unknown"

    async def _lookup_direction(self, signal_id: str) -> str:
        try:
            async with self._db._db.execute(
                "SELECT direction FROM signals WHERE signal_id = ?", (signal_id,)
            ) as cur:
                row = await cur.fetchone()
            return row["direction"] if row else "LONG"
        except Exception:
            return "LONG"

    # ------------------------------------------------------------------ #
    # Background tasks                                                     #
    # ------------------------------------------------------------------ #

    async def _hourly_snapshot_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3_600)
                summary = self.get_performance_summary("all")
                snapshot = {
                    "metrics": summary.metrics.__dict__,
                    "per_strategy": {k: v.__dict__ for k, v in summary.per_strategy.items()},
                }
                await self._db.insert_snapshot(json.dumps(snapshot))
                self.log.info(
                    "Hourly snapshot saved",
                    trades=summary.metrics.total_trades,
                    win_rate=round(summary.metrics.win_rate, 3),
                    pnl=round(summary.metrics.total_pnl_net, 2),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Snapshot error", error=str(exc))

    async def _daily_report_loop(self) -> None:
        """Fires at the next UTC midnight, then every 24h."""
        import datetime
        while True:
            try:
                now = datetime.datetime.utcnow()
                next_midnight = datetime.datetime(now.year, now.month, now.day) \
                    + datetime.timedelta(days=1)
                sleep_secs = (next_midnight - now).total_seconds()
                await asyncio.sleep(sleep_secs)
                await self._generate_daily_report()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Daily report error", error=str(exc))

    async def _generate_daily_report(self) -> None:
        summary = self.get_performance_summary("today")
        m = summary.metrics
        report = {
            "date": time.strftime("%Y-%m-%d", time.gmtime()),
            "trades": m.total_trades,
            "pnl_net": m.total_pnl_net,
            "win_rate": round(m.win_rate, 3),
            "profit_factor": m.profit_factor,
            "max_drawdown_pct": m.max_drawdown_pct,
            "best_trade": m.best_trade_pnl,
            "worst_trade": m.worst_trade_pnl,
            "fees": m.total_fees,
        }
        await self._db.insert_snapshot(json.dumps({"daily_report": report}))
        self.log.info("Daily report generated", **report)

        await self._send(AgentMessage(
            sender=AgentName.JOURNAL,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.DAILY_REPORT,
            payload=report,
        ))
