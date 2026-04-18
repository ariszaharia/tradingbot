"""
Unit tests for RiskAgent._evaluate() — pure synchronous logic, no asyncio needed.

Signal geometry used throughout:
  entry=50_000, sl=47_000 (distance=3_000), tp=56_000 (distance=6_000, R:R=2)
  notional/capital = risk_pct * entry / distance = 0.01 * 50_000 / 3_000 = 16.7 % < 20% cap.
"""
import uuid
import time
import pytest

from trading_bot.agents.risk_agent import RiskAgent
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.models.system_state import OpenPosition, SystemState


# ─────────────────────────────────────────────────────────────────────────────
# Config & helpers
# ─────────────────────────────────────────────────────────────────────────────

_CFG = {
    "capital": {
        "risk_per_trade_pct": 1.0,
        "max_drawdown_daily_pct": 3.0,
        "max_drawdown_total_pct": 10.0,
        "max_positions": 3,
        "max_position_size_pct": 20.0,
    },
    "strategy": {
        "min_confidence_score": 0.6,
        "cooldown_after_losses": 2,
    },
}


def _agent() -> RiskAgent:
    return RiskAgent(bus={}, config=_CFG)


def _signal(
    direction: Direction = Direction.LONG,
    confidence: float = 0.75,
    entry: float = 50_000.0,
    sl: float = 47_000.0,    # distance 3_000 → notional = 16.7% of capital
    tp: float = 56_000.0,    # distance 6_000 → R:R = 2.0
) -> TradingSignal:
    return TradingSignal(
        signal_id=str(uuid.uuid4()),
        direction=direction,
        strategy_name="trend_following",
        confidence_score=confidence,
        entry_price=entry,
        suggested_stop_loss=sl,
        suggested_take_profit=tp,
        timeframe="1h",
        reasoning=["test"],
        timestamp=int(time.time() * 1000),
    )


def _state(
    capital: float = 10_000.0,
    initial_capital: float = 10_000.0,
    daily_dd_pct: float = 0.0,
    total_dd_pct: float = 0.0,
    open_positions: int = 0,
    cooldown: int = 0,
) -> SystemState:
    positions = [
        OpenPosition(
            signal_id=str(uuid.uuid4()),
            order_id=f"order_{i}",
            symbol="BTC/USDT",
            direction="LONG",
            entry_price=50_000.0,
            quantity=0.01,
            stop_loss=47_000.0,
            take_profit=56_000.0,
        )
        for i in range(open_positions)
    ]
    return SystemState(
        initial_capital=initial_capital,
        current_capital=capital,
        daily_drawdown_pct=daily_dd_pct,
        total_drawdown_pct=total_dd_pct,
        open_positions=positions,
        cooldown_candles_remaining=cooldown,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskAgentApproved:
    def setup_method(self):
        self.agent = _agent()

    def test_approved_standard_long(self):
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.approved is True
        assert decision.rejection_reason is None

    def test_position_size_calculation(self):
        # capital=10_000, risk_pct=1% → risk_amount=100
        # distance = 3_000, units = 100/3_000 ≈ 0.03333
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.position_size == pytest.approx(100 / 3_000, rel=1e-4)
        assert decision.position_size_usd == pytest.approx(decision.position_size * 50_000, rel=1e-3)

    def test_risk_pct_of_capital_is_1_pct(self):
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.risk_pct_of_capital == pytest.approx(1.0, abs=0.01)

    def test_rr_ratio_reported_correctly(self):
        # reward = 6_000, risk = 3_000 → R:R = 2.0
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.reward_risk_ratio == pytest.approx(2.0, abs=0.01)

    def test_exit_signal_always_approved_even_during_dd_breach(self):
        sig = _signal(direction=Direction.EXIT)
        decision = self.agent._evaluate(sig, _state(daily_dd_pct=5.0))
        assert decision.approved is True

    def test_all_rule_checks_present_on_approval(self):
        decision = self.agent._evaluate(_signal(), _state())
        for rule in ["rule_1_daily_dd", "rule_2_total_dd", "rule_3_max_exposure",
                     "rule_4_max_positions", "rule_5_correlation",
                     "rule_6_confidence", "rule_7_cooldown"]:
            assert rule in decision.rule_checks, f"Missing check: {rule}"
            assert decision.rule_checks[rule].startswith("PASS"), (
                f"{rule} should PASS, got: {decision.rule_checks[rule]}"
            )

    def test_short_signal_approved(self):
        # SHORT: sl above entry, tp below entry
        sig = _signal(direction=Direction.SHORT, sl=53_000.0, tp=44_000.0)
        decision = self.agent._evaluate(sig, _state())
        assert decision.approved is True
        assert decision.position_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1 — Daily drawdown
# ─────────────────────────────────────────────────────────────────────────────

class TestRule1DailyDrawdown:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_at_limit(self):
        decision = self.agent._evaluate(_signal(), _state(daily_dd_pct=3.0))
        assert decision.approved is False
        assert "daily drawdown" in decision.rejection_reason.lower()
        assert decision.rule_checks["rule_1_daily_dd"].startswith("FAIL")

    def test_rejected_above_limit(self):
        decision = self.agent._evaluate(_signal(), _state(daily_dd_pct=4.5))
        assert decision.approved is False

    def test_approved_just_below_limit(self):
        decision = self.agent._evaluate(_signal(), _state(daily_dd_pct=2.99))
        assert decision.approved is True
        assert decision.rule_checks["rule_1_daily_dd"] == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2 — Total drawdown
# ─────────────────────────────────────────────────────────────────────────────

class TestRule2TotalDrawdown:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_at_limit(self):
        decision = self.agent._evaluate(_signal(), _state(total_dd_pct=10.0))
        assert decision.approved is False
        assert "total drawdown" in decision.rejection_reason.lower()

    def test_approved_just_below_limit(self):
        decision = self.agent._evaluate(_signal(), _state(total_dd_pct=9.99))
        assert decision.approved is True
        assert decision.rule_checks["rule_2_total_dd"] == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 3 — Max position size (20 % of capital)
# ─────────────────────────────────────────────────────────────────────────────

class TestRule3MaxExposure:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_when_position_exceeds_20pct(self):
        # distance=5 → units=100/5=20, notional=20*50_000=1_000_000 >> 2_000 (20% of 10k)
        sig = _signal(sl=49_995.0, tp=50_015.0)
        decision = self.agent._evaluate(sig, _state())
        assert decision.approved is False
        assert "position size" in decision.rejection_reason.lower()

    def test_approved_when_within_20pct(self):
        # distance=3_000 → notional = 100/3_000 * 50_000 = 1_667 = 16.7% < 20% ✓
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4 — Max simultaneous positions
# ─────────────────────────────────────────────────────────────────────────────

class TestRule4MaxPositions:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_when_max_positions_reached(self):
        decision = self.agent._evaluate(_signal(), _state(open_positions=3))
        assert decision.approved is False
        assert "3" in decision.rejection_reason

    def test_approved_with_two_open_positions(self):
        decision = self.agent._evaluate(_signal(), _state(open_positions=2))
        assert decision.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Rule 6 — Confidence filter
# ─────────────────────────────────────────────────────────────────────────────

class TestRule6Confidence:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_below_threshold(self):
        decision = self.agent._evaluate(_signal(confidence=0.55), _state())
        assert decision.approved is False
        assert "confidence" in decision.rejection_reason.lower()

    def test_approved_at_exact_threshold(self):
        decision = self.agent._evaluate(_signal(confidence=0.60), _state())
        assert decision.approved is True

    def test_approved_above_threshold(self):
        decision = self.agent._evaluate(_signal(confidence=0.85), _state())
        assert decision.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Rule 7 — Cooldown
# ─────────────────────────────────────────────────────────────────────────────

class TestRule7Cooldown:
    def setup_method(self):
        self.agent = _agent()

    def test_rejected_during_cooldown(self):
        decision = self.agent._evaluate(_signal(), _state(cooldown=1))
        assert decision.approved is False
        assert "cooldown" in decision.rejection_reason.lower()

    def test_approved_after_cooldown_expires(self):
        decision = self.agent._evaluate(_signal(), _state(cooldown=0))
        assert decision.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# R:R adjustment
# ─────────────────────────────────────────────────────────────────────────────

class TestRRAdjustment:
    def setup_method(self):
        self.agent = _agent()

    def test_tp_adjusted_when_rr_below_minimum(self):
        # risk=3_000, reward=3_000 → R:R=1.0 < 1.5 → TP adjusted to entry + 1.5*3_000 = 54_500
        sig = _signal(sl=47_000.0, tp=53_000.0)
        decision = self.agent._evaluate(sig, _state())
        assert decision.approved is True
        assert "rr_adjustment" in decision.rule_checks
        assert decision.final_take_profit == pytest.approx(50_000.0 + 1.5 * 3_000, rel=1e-6)
        assert decision.reward_risk_ratio == pytest.approx(1.5, abs=0.01)

    def test_tp_not_adjusted_when_rr_sufficient(self):
        # risk=3_000, reward=6_000 → R:R=2.0 ≥ 1.5 → no adjustment
        decision = self.agent._evaluate(_signal(), _state())
        assert decision.approved is True
        assert "rr_adjustment" not in decision.rule_checks
        assert decision.final_take_profit == pytest.approx(56_000.0)

    def test_short_tp_adjusted_downward(self):
        # SHORT: entry=50_000, sl=53_000 (above, risk=3_000), tp=49_000 (reward=1_000, R:R=0.33)
        # Expected adjusted tp = 50_000 - 1.5*3_000 = 45_500
        sig = _signal(direction=Direction.SHORT, sl=53_000.0, tp=49_000.0)
        decision = self.agent._evaluate(sig, _state())
        assert decision.approved is True
        assert decision.final_take_profit == pytest.approx(45_500.0, rel=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# FLAT signal
# ─────────────────────────────────────────────────────────────────────────────

class TestFlatSignal:
    def setup_method(self):
        self.agent = _agent()

    def test_flat_always_rejected(self):
        sig = _signal(direction=Direction.FLAT)
        decision = self.agent._evaluate(sig, _state())
        assert decision.approved is False
        assert "FLAT" in decision.rejection_reason


# ─────────────────────────────────────────────────────────────────────────────
# Rule ordering — first failure short-circuits
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleOrdering:
    def setup_method(self):
        self.agent = _agent()

    def test_rule1_fires_before_rule6(self):
        """Daily DD breach fires before confidence check."""
        sig = _signal(confidence=0.3)
        decision = self.agent._evaluate(sig, _state(daily_dd_pct=5.0))
        assert "daily drawdown" in decision.rejection_reason.lower()
        assert "rule_2_total_dd" not in decision.rule_checks

    def test_rule2_fires_before_rule3(self):
        """Total DD breach fires before position-size check."""
        sig = _signal(sl=49_995.0, tp=50_015.0)   # would also breach rule 3
        decision = self.agent._evaluate(sig, _state(total_dd_pct=10.0))
        assert "total drawdown" in decision.rejection_reason.lower()
        assert "rule_3_max_exposure" not in decision.rule_checks
