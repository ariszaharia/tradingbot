from __future__ import annotations
import asyncio
import signal
import time
from typing import Callable, Awaitable

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.execution_report import ExecutionReport, PositionClose
from trading_bot.models.risk_decision import RiskDecision
from trading_bot.models.system_state import OpenPosition, SystemMode, SystemState
from trading_bot.models.trading_signal import Direction, TradingSignal


StatusCallback = Callable[[SystemState], Awaitable[None]]


class OrchestratorAgent(BaseAgent):
    """
    Central brain — coordinates the full trading cycle.

    Cycle (triggered by each DATA_SNAPSHOT from MarketDataAgent):
      1. DATA_SNAPSHOT received
      2. Forward snapshot to StrategyAgent as REQUEST_SIGNAL
      3. TRADING_SIGNAL received
         - FLAT  → discard, log
         - EXIT  → send EXECUTE_ORDER directly (skip risk for exits)
         - LONG/SHORT → send REQUEST_RISK_DECISION to RiskAgent
      4. RISK_DECISION received
         - rejected → log and discard
         - approved → send EXECUTE_ORDER to ExecutionAgent
      5. EXECUTION_REPORT received → update SystemState
      6. POSITION_CLOSED received → settle PnL, update drawdown counters
      7. Repeat from 1

    Circuit breaker: checked after every POSITION_CLOSED and after daily_drawdown
    update. When tripped, mode → PAUSED (no new orders placed).
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
    ) -> None:
        super().__init__(AgentName.ORCHESTRATOR, bus, config)

        cap = config.get("capital", {})
        initial_capital: float = cap.get("initial_capital", 10_000.0)

        self._state = SystemState(
            initial_capital=initial_capital,
            current_capital=initial_capital,
            risk_budget_remaining_pct=cap.get("max_drawdown_daily_pct", 3.0),
        )

        self._max_dd_daily: float = cap.get("max_drawdown_daily_pct", 3.0)
        self._max_dd_total: float = cap.get("max_drawdown_total_pct", 10.0)
        self._cooldown_candles: int = config.get("strategy", {}).get("cooldown_after_losses", 2)

        # Pending signal waiting for risk decision (signal_id → TradingSignal)
        self._pending_signals: dict[str, TradingSignal] = {}

        # Daily reset tracking
        self._trading_day_start_utc: int = self._start_of_day_ms()

        # Optional external status callback (used by tests / UI)
        self._status_callbacks: list[StatusCallback] = []

    # ------------------------------------------------------------------ #
    # Public control interface                                             #
    # ------------------------------------------------------------------ #

    async def start_trading(self) -> None:
        self._state.mode = SystemMode.RUNNING
        self.log.info("Trading started", capital=self._state.current_capital)

    async def stop_trading(self) -> None:
        self._state.mode = SystemMode.STOPPED
        self.log.info("Trading stopped")

    async def pause_trading(self, reason: str) -> None:
        self._state.mode = SystemMode.PAUSED
        self.log.warning("Trading paused", reason=reason)

    async def resume_trading(self) -> None:
        if self._state.mode == SystemMode.PAUSED:
            self._state.mode = SystemMode.RUNNING
            self.log.info("Trading resumed")

    def get_system_state(self) -> SystemState:
        return self._state.model_copy(deep=True)

    async def force_close_all(self) -> None:
        """Close every open position immediately (sends EXIT order per position)."""
        if not self._state.open_positions:
            self.log.info("force_close_all: no open positions")
            return
        for pos in list(self._state.open_positions):
            self.log.info("Force closing position", signal_id=pos.signal_id)
            await self._send(AgentMessage(
                sender=AgentName.ORCHESTRATOR,
                recipient=AgentName.EXECUTION,
                msg_type=MessageType.EXECUTE_ORDER,
                payload={
                    "signal": {
                        "signal_id": pos.signal_id,
                        "direction": "EXIT",
                        "strategy_name": "force_close",
                        "confidence_score": 1.0,
                        "entry_price": 0.0,
                        "suggested_stop_loss": 0.0,
                        "suggested_take_profit": 0.0,
                        "timeframe": "N/A",
                        "reasoning": ["force_close_all"],
                        "timestamp": int(time.time() * 1000),
                    },
                    "decision": {"approved": True, "signal_id": pos.signal_id},
                    "position": pos.model_dump(),
                },
            ))

    def add_status_callback(self, cb: StatusCallback) -> None:
        self._status_callbacks.append(cb)

    # ------------------------------------------------------------------ #
    # Message handler (dispatch table)                                     #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        match msg.msg_type:
            case MessageType.DATA_SNAPSHOT:
                await self._on_data_snapshot(msg)
            case MessageType.ANOMALY_DETECTED:
                await self._on_anomaly(msg)
            case MessageType.TRADING_SIGNAL:
                await self._on_trading_signal(msg)
            case MessageType.RISK_DECISION:
                await self._on_risk_decision(msg)
            case MessageType.EXECUTION_REPORT:
                await self._on_execution_report(msg)
            case MessageType.POSITION_CLOSED:
                await self._on_position_closed(msg)
            case MessageType.HIGH_SLIPPAGE:
                self.log.warning("High slippage reported", **msg.payload)
            case MessageType.PAUSE:
                await self.pause_trading(msg.payload.get("reason", "external PAUSE"))
            case MessageType.STOP:
                await self.stop_trading()
            case MessageType.RESUME:
                await self.resume_trading()
            case MessageType.STATUS:
                await self._reply_status(msg)
            case _:
                self.log.debug("Unhandled message type", msg_type=msg.msg_type.value)

    # ------------------------------------------------------------------ #
    # Cycle step 1 — DATA_SNAPSHOT                                        #
    # ------------------------------------------------------------------ #

    async def _on_data_snapshot(self, msg: AgentMessage) -> None:
        if self._state.mode != SystemMode.RUNNING:
            self.log.debug("Skipping snapshot — system not RUNNING", mode=self._state.mode.value)
            return

        await self._check_daily_reset()
        await self._decrement_cooldown()

        snapshot = DataSnapshot(**msg.payload)

        # Inject current position direction so strategies can check EXIT conditions
        if self._state.open_positions:
            snapshot.current_position_direction = self._state.open_positions[0].direction

        request = AgentMessage(
            sender=AgentName.ORCHESTRATOR,
            recipient=AgentName.STRATEGY,
            msg_type=MessageType.REQUEST_SIGNAL,
            payload=snapshot.model_dump(),
        )
        await self._send(request)
        self._state.updated_at = int(time.time() * 1000)

    # ------------------------------------------------------------------ #
    # Cycle step 3 — TRADING_SIGNAL                                       #
    # ------------------------------------------------------------------ #

    async def _on_trading_signal(self, msg: AgentMessage) -> None:
        signal = TradingSignal(**msg.payload)
        self._state.last_signal_id = signal.signal_id
        self._state.last_signal_direction = signal.direction.value

        if signal.direction == Direction.FLAT:
            self.log.debug("Signal FLAT — no action", strategy=signal.strategy_name)
            return

        if signal.direction == Direction.EXIT:
            if not self._state.open_positions:
                self.log.debug("EXIT signal but no open position — ignored")
                return
            # Match EXIT to the relevant position
            pos = self._find_position_for_exit(signal)
            if pos is None:
                return
            await self._send_execute(signal, None, pos)
            return

        # LONG or SHORT — route through Risk
        if self._state.cooldown_candles_remaining > 0:
            self.log.info(
                "Signal blocked by cooldown",
                candles_remaining=self._state.cooldown_candles_remaining,
            )
            return

        self._pending_signals[signal.signal_id] = signal

        await self._send(AgentMessage(
            sender=AgentName.ORCHESTRATOR,
            recipient=AgentName.RISK,
            msg_type=MessageType.REQUEST_RISK_DECISION,
            payload={"signal": signal.model_dump(), "state": self._state.model_dump()},
        ))

    # ------------------------------------------------------------------ #
    # Cycle step 4 — RISK_DECISION                                        #
    # ------------------------------------------------------------------ #

    async def _on_risk_decision(self, msg: AgentMessage) -> None:
        decision = RiskDecision(**msg.payload)
        signal = self._pending_signals.pop(decision.signal_id, None)

        if signal is None:
            self.log.warning("RiskDecision for unknown signal_id", signal_id=decision.signal_id)
            return

        if not decision.approved:
            self.log.info(
                "Signal rejected by Risk",
                reason=decision.rejection_reason,
                signal_id=decision.signal_id,
            )
            # If rejection was due to drawdown limits, trip the circuit breaker
            if decision.rejection_reason and (
                "daily drawdown" in decision.rejection_reason.lower()
            ):
                await self.trigger_circuit_breaker(decision.rejection_reason)
            elif decision.rejection_reason and (
                "total drawdown" in decision.rejection_reason.lower()
            ):
                await self.trigger_circuit_breaker(decision.rejection_reason, hard_stop=True)
            return

        pos = None  # new entry, no existing position to close
        await self._send_execute(signal, decision, pos)

    # ------------------------------------------------------------------ #
    # Cycle step 5 — EXECUTION_REPORT                                     #
    # ------------------------------------------------------------------ #

    async def _on_execution_report(self, msg: AgentMessage) -> None:
        report = ExecutionReport(**msg.payload)

        if report.status.value in ("REJECTED", "ERROR"):
            self.log.error(
                "Order failed",
                signal_id=report.signal_id,
                status=report.status.value,
                error=report.error_message,
            )
            self._add_error(f"Order {report.order_id} {report.status.value}: {report.error_message}")
            return

        # Register the new position in system state
        signal = self._pending_signals.get(report.signal_id)
        direction = msg.payload.get("direction", "LONG")

        # Find the original signal direction from context passed by ExecutionAgent
        direction_str = msg.payload.get("direction", "LONG")

        pos = OpenPosition(
            signal_id=report.signal_id,
            order_id=report.order_id,
            symbol=self._config["trading"]["symbol"],
            direction=direction_str,
            entry_price=report.executed_price,
            quantity=report.executed_quantity,
            stop_loss=msg.payload.get("stop_loss", 0.0),
            take_profit=msg.payload.get("take_profit", 0.0),
        )
        self._state.open_positions.append(pos)
        self.log.info(
            "Position opened",
            signal_id=report.signal_id,
            direction=direction_str,
            price=report.executed_price,
            qty=report.executed_quantity,
            open_positions=len(self._state.open_positions),
        )
        await self._notify_status()

    # ------------------------------------------------------------------ #
    # Cycle step 6 — POSITION_CLOSED                                      #
    # ------------------------------------------------------------------ #

    async def _on_position_closed(self, msg: AgentMessage) -> None:
        close = PositionClose(**msg.payload)

        # Remove from open positions
        self._state.open_positions = [
            p for p in self._state.open_positions
            if p.signal_id != close.signal_id
        ]

        # Update PnL
        self._state.daily_pnl += close.pnl_net
        self._state.total_pnl += close.pnl_net
        self._state.current_capital += close.pnl_net

        # Drawdown tracking
        self._state.daily_drawdown_pct = max(
            0.0,
            -self._state.daily_pnl / self._state.initial_capital * 100,
        )
        self._state.total_drawdown_pct = max(
            0.0,
            -self._state.total_pnl / self._state.initial_capital * 100,
        )
        self._state.risk_budget_remaining_pct = max(
            0.0,
            self._max_dd_daily - self._state.daily_drawdown_pct,
        )

        # Win / loss tracking
        self._state.total_trades += 1
        if close.pnl_net > 0:
            self._state.winning_trades += 1
            self._state.consecutive_losses = 0
        else:
            self._state.consecutive_losses += 1
            if self._state.consecutive_losses >= 2:
                self._state.cooldown_candles_remaining = self._cooldown_candles
                self.log.info(
                    "Cooldown activated",
                    consecutive_losses=self._state.consecutive_losses,
                    candles=self._cooldown_candles,
                )

        self.log.info(
            "Position closed",
            signal_id=close.signal_id,
            pnl_net=round(close.pnl_net, 2),
            pnl_pct=round(close.pnl_pct, 3),
            reason=close.close_reason.value,
            capital=round(self._state.current_capital, 2),
            daily_dd_pct=round(self._state.daily_drawdown_pct, 3),
        )

        await self._check_circuit_breaker()
        await self._notify_status()

    # ------------------------------------------------------------------ #
    # Anomaly handler                                                      #
    # ------------------------------------------------------------------ #

    async def _on_anomaly(self, msg: AgentMessage) -> None:
        reason = msg.payload.get("reason", "unknown")
        self.log.warning("Market anomaly detected", reason=reason)
        self._add_error(f"ANOMALY: {reason}")
        # Anomaly flag is set on the DataSnapshot by MarketDataAgent;
        # StrategyAgent will suppress signals automatically.

    # ------------------------------------------------------------------ #
    # Circuit breaker                                                      #
    # ------------------------------------------------------------------ #

    async def trigger_circuit_breaker(self, reason: str, hard_stop: bool = False) -> None:
        if hard_stop:
            self._state.mode = SystemMode.STOPPED
            self.log.critical("Circuit breaker — HARD STOP", reason=reason)
        else:
            self._state.mode = SystemMode.PAUSED
            self.log.warning("Circuit breaker — PAUSED", reason=reason)

        self._add_error(f"CIRCUIT_BREAKER: {reason}")
        await self._notify_status()

        # Broadcast so all agents are aware
        await self._send(AgentMessage(
            sender=AgentName.ORCHESTRATOR,
            recipient=AgentName.BROADCAST,
            msg_type=MessageType.PAUSE if not hard_stop else MessageType.STOP,
            payload={"reason": reason},
        ))

    async def _check_circuit_breaker(self) -> None:
        if self._state.daily_drawdown_pct >= self._max_dd_daily:
            await self.trigger_circuit_breaker(
                f"Daily drawdown {self._state.daily_drawdown_pct:.2f}% >= limit {self._max_dd_daily}%"
            )
        elif self._state.total_drawdown_pct >= self._max_dd_total:
            await self.trigger_circuit_breaker(
                f"Total drawdown {self._state.total_drawdown_pct:.2f}% >= limit {self._max_dd_total}%",
                hard_stop=True,
            )

    # ------------------------------------------------------------------ #
    # Execution helper                                                     #
    # ------------------------------------------------------------------ #

    async def _send_execute(
        self,
        signal: TradingSignal,
        decision: RiskDecision | None,
        position: OpenPosition | None,
    ) -> None:
        payload: dict = {
            "signal": signal.model_dump(),
            "direction": signal.direction.value,
        }
        if decision:
            payload["decision"] = decision.model_dump()
            payload["stop_loss"] = decision.final_stop_loss
            payload["take_profit"] = decision.final_take_profit
        if position:
            payload["position"] = position.model_dump()

        await self._send(AgentMessage(
            sender=AgentName.ORCHESTRATOR,
            recipient=AgentName.EXECUTION,
            msg_type=MessageType.EXECUTE_ORDER,
            payload=payload,
        ))

    # ------------------------------------------------------------------ #
    # Status reply                                                         #
    # ------------------------------------------------------------------ #

    async def _reply_status(self, msg: AgentMessage) -> None:
        reply = AgentMessage(
            sender=AgentName.ORCHESTRATOR,
            recipient=msg.sender,
            msg_type=MessageType.ACK,
            payload=self._state.model_dump(),
        )
        await self._send(reply)

    async def _notify_status(self) -> None:
        for cb in self._status_callbacks:
            try:
                await cb(self._state)
            except Exception as exc:
                self.log.error("Status callback error", error=str(exc))

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _find_position_for_exit(self, signal: TradingSignal) -> OpenPosition | None:
        if not self._state.open_positions:
            return None
        # Return the first open position (single-symbol bot has at most a few)
        return self._state.open_positions[0]

    async def _check_daily_reset(self) -> None:
        now_ms = int(time.time() * 1000)
        day_start = self._start_of_day_ms()
        if day_start > self._trading_day_start_utc:
            self._trading_day_start_utc = day_start
            prev_pnl = self._state.daily_pnl
            self._state.daily_pnl = 0.0
            self._state.daily_drawdown_pct = 0.0
            self._state.risk_budget_remaining_pct = self._max_dd_daily
            # Re-enable trading if paused only due to daily limit
            if self._state.mode == SystemMode.PAUSED:
                self._state.mode = SystemMode.RUNNING
                self.log.info("New trading day — daily limits reset, trading resumed", prev_daily_pnl=prev_pnl)
            else:
                self.log.info("New trading day — daily limits reset", prev_daily_pnl=prev_pnl)

    async def _decrement_cooldown(self) -> None:
        if self._state.cooldown_candles_remaining > 0:
            self._state.cooldown_candles_remaining -= 1
            self.log.debug(
                "Cooldown tick",
                remaining=self._state.cooldown_candles_remaining,
            )

    def _add_error(self, msg: str) -> None:
        self._state.errors.append(f"{int(time.time())}|{msg}")
        # Keep only the last 50 errors
        if len(self._state.errors) > 50:
            self._state.errors = self._state.errors[-50:]

    @staticmethod
    def _start_of_day_ms() -> int:
        import datetime
        today = datetime.datetime.utcnow().date()
        midnight = datetime.datetime(today.year, today.month, today.day)
        return int(midnight.timestamp() * 1000)
