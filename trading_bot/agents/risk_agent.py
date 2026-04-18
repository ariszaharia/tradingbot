from __future__ import annotations
import asyncio

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.risk_decision import RiskDecision
from trading_bot.models.system_state import SystemState
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.utils.risk_calculator import (
    adjust_take_profit,
    calc_position_size,
    calc_reward_risk,
)


class RiskAgent(BaseAgent):
    """
    Capital guardian — validates every signal and sizes every position.

    Seven rules are evaluated in order; the first failure short-circuits
    and returns a rejected RiskDecision. All decisions (pass and fail)
    are forwarded to the Journal Agent.
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
    ) -> None:
        super().__init__(AgentName.RISK, bus, config)
        cap = config.get("capital", {})
        self._risk_pct: float = cap.get("risk_per_trade_pct", 1.0)
        self._max_dd_daily_pct: float = cap.get("max_drawdown_daily_pct", 3.0)
        self._max_dd_total_pct: float = cap.get("max_drawdown_total_pct", 10.0)
        self._max_positions: int = cap.get("max_positions", 3)
        self._max_pos_size_pct: float = cap.get("max_position_size_pct", 20.0)
        strat = config.get("strategy", {})
        self._min_confidence: float = strat.get("min_confidence_score", 0.6)
        self._cooldown_candles: int = strat.get("cooldown_after_losses", 2)
        self._min_rr: float = 1.5

    # ------------------------------------------------------------------ #
    # Message handler                                                      #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        if msg.msg_type != MessageType.REQUEST_RISK_DECISION:
            return

        signal = TradingSignal(**msg.payload["signal"])
        state = SystemState(**msg.payload["state"])

        decision = self._evaluate(signal, state)

        self.log.info(
            "Risk decision",
            signal_id=signal.signal_id,
            approved=decision.approved,
            rejection=decision.rejection_reason,
            size_usd=round(decision.position_size_usd, 2),
            rr=round(decision.reward_risk_ratio, 2),
        )

        reply = AgentMessage(
            sender=AgentName.RISK,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.RISK_DECISION,
            payload=decision.model_dump(),
        )
        await self._send(reply)

        journal_msg = AgentMessage(
            sender=AgentName.RISK,
            recipient=AgentName.JOURNAL,
            msg_type=MessageType.RISK_DECISION,
            payload=decision.model_dump(),
        )
        await self._send(journal_msg)

    # ------------------------------------------------------------------ #
    # Core evaluation (pure, synchronous — easy to unit-test)             #
    # ------------------------------------------------------------------ #

    def _evaluate(self, signal: TradingSignal, state: SystemState) -> RiskDecision:
        checks: dict[str, str] = {}

        # EXIT signals bypass all sizing rules — always approved
        if signal.direction == Direction.EXIT:
            return RiskDecision(
                signal_id=signal.signal_id,
                approved=True,
                position_size=0.0,
                position_size_usd=0.0,
                final_stop_loss=signal.suggested_stop_loss,
                final_take_profit=signal.suggested_take_profit,
                risk_pct_of_capital=0.0,
                reward_risk_ratio=0.0,
                rule_checks={"EXIT_BYPASS": "PASS"},
            )

        # FLAT signals are always rejected (nothing to do)
        if signal.direction == Direction.FLAT:
            return self._reject(signal.signal_id, "FLAT signal — no action", checks)

        capital = state.current_capital

        # ── RULE 1: Daily drawdown ────────────────────────────────────────
        if state.daily_drawdown_pct >= self._max_dd_daily_pct:
            checks["rule_1_daily_dd"] = (
                f"FAIL: daily drawdown {state.daily_drawdown_pct:.2f}% "
                f">= limit {self._max_dd_daily_pct}%"
            )
            return self._reject(
                signal.signal_id,
                f"Daily drawdown limit reached ({state.daily_drawdown_pct:.2f}%)",
                checks,
            )
        checks["rule_1_daily_dd"] = "PASS"

        # ── RULE 2: Total drawdown ────────────────────────────────────────
        if state.total_drawdown_pct >= self._max_dd_total_pct:
            checks["rule_2_total_dd"] = (
                f"FAIL: total drawdown {state.total_drawdown_pct:.2f}% "
                f">= limit {self._max_dd_total_pct}%"
            )
            return self._reject(
                signal.signal_id,
                f"Total drawdown limit reached ({state.total_drawdown_pct:.2f}%)",
                checks,
            )
        checks["rule_2_total_dd"] = "PASS"

        # ── Position sizing ───────────────────────────────────────────────
        try:
            units, notional = calc_position_size(
                capital,
                self._risk_pct,
                signal.entry_price,
                signal.suggested_stop_loss,
            )
        except ValueError as e:
            return self._reject(signal.signal_id, str(e), checks)

        actual_risk_pct = (units * abs(signal.entry_price - signal.suggested_stop_loss)) / capital * 100

        # ── RULE 3: Max exposure per trade ────────────────────────────────
        pos_size_pct = notional / capital * 100
        if pos_size_pct > self._max_pos_size_pct:
            checks["rule_3_max_exposure"] = (
                f"FAIL: position {pos_size_pct:.1f}% > limit {self._max_pos_size_pct}%"
            )
            return self._reject(
                signal.signal_id,
                f"Position size {pos_size_pct:.1f}% exceeds max {self._max_pos_size_pct}%",
                checks,
            )
        checks["rule_3_max_exposure"] = f"PASS ({pos_size_pct:.1f}%)"

        # ── RULE 4: Max simultaneous positions ───────────────────────────
        if len(state.open_positions) >= self._max_positions:
            checks["rule_4_max_positions"] = (
                f"FAIL: {len(state.open_positions)} open >= limit {self._max_positions}"
            )
            return self._reject(
                signal.signal_id,
                f"Max {self._max_positions} simultaneous positions reached",
                checks,
            )
        checks["rule_4_max_positions"] = f"PASS ({len(state.open_positions)} open)"

        # ── RULE 5: Correlation (single-symbol bot — always passes) ───────
        # In a multi-symbol extension, check pairwise correlation here.
        checks["rule_5_correlation"] = "PASS (single symbol)"

        # ── RULE 6: Confidence filter ─────────────────────────────────────
        if signal.confidence_score < self._min_confidence:
            checks["rule_6_confidence"] = (
                f"FAIL: confidence {signal.confidence_score:.2f} < {self._min_confidence}"
            )
            return self._reject(
                signal.signal_id,
                f"Low confidence ({signal.confidence_score:.2f} < {self._min_confidence})",
                checks,
            )
        checks["rule_6_confidence"] = f"PASS ({signal.confidence_score:.2f})"

        # ── RULE 7: Cooldown after consecutive losses ─────────────────────
        if state.cooldown_candles_remaining > 0:
            checks["rule_7_cooldown"] = (
                f"FAIL: {state.cooldown_candles_remaining} cooldown candle(s) remaining"
            )
            return self._reject(
                signal.signal_id,
                f"Cooldown active ({state.cooldown_candles_remaining} candle(s) remaining)",
                checks,
            )
        checks["rule_7_cooldown"] = "PASS"

        # ── R:R check & potential TP adjustment ───────────────────────────
        rr = calc_reward_risk(
            signal.entry_price,
            signal.suggested_stop_loss,
            signal.suggested_take_profit,
        )
        final_sl = signal.suggested_stop_loss
        final_tp = signal.suggested_take_profit

        if rr < self._min_rr:
            final_tp = adjust_take_profit(
                signal.entry_price,
                signal.suggested_stop_loss,
                signal.direction.value,
                self._min_rr,
            )
            rr = self._min_rr
            checks["rr_adjustment"] = (
                f"TP adjusted from {signal.suggested_take_profit:.2f} "
                f"to {final_tp:.2f} to meet min R:R {self._min_rr}"
            )
        else:
            checks["rr_check"] = f"PASS (R:R={rr:.2f})"

        return RiskDecision(
            signal_id=signal.signal_id,
            approved=True,
            position_size=round(units, 8),
            position_size_usd=round(notional, 2),
            final_stop_loss=final_sl,
            final_take_profit=final_tp,
            risk_pct_of_capital=round(actual_risk_pct, 4),
            reward_risk_ratio=round(rr, 4),
            rule_checks=checks,
        )

    # ------------------------------------------------------------------ #
    # Helper                                                               #
    # ------------------------------------------------------------------ #

    def _reject(
        self,
        signal_id: str,
        reason: str,
        checks: dict[str, str],
    ) -> RiskDecision:
        return RiskDecision(
            signal_id=signal_id,
            approved=False,
            rejection_reason=reason,
            rule_checks=checks,
        )
