"""
Unit tests for StrategyAgent, TrendFollowingStrategy, and MeanReversionStrategy.

All tests are synchronous — strategies are pure functions with no I/O.
"""
import pytest
from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction
from trading_bot.strategies.trend_following import TrendFollowingStrategy
from trading_bot.strategies.mean_reversion import MeanReversionStrategy


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_CFG: dict = {}


def _snapshot(
    price: float = 50000.0,
    spread_pct: float = 0.05,
    indicators: dict | None = None,
    htf_indicators: dict | None = None,
    anomaly_flag: bool = False,
    anomaly_reason: str | None = None,
) -> DataSnapshot:
    base_ind: dict = {
        "ema_9": 50200.0,
        "ema_21": 49900.0,
        "ema_50": 49500.0,
        "ema_200": 48000.0,
        "rsi_14": 55.0,
        "rsi_7": 45.0,
        "atr_14": 500.0,
        "macd_line": 100.0,
        "macd_signal": 80.0,
        "macd_hist": 20.0,
        "bb_upper": 52000.0,
        "bb_middle": 50000.0,
        "bb_lower": 48000.0,
        "volume_sma_20": 1000.0,
        "volume": 1300.0,
        "open": 49800.0,
        "high": 50500.0,
        "low": 49400.0,
        "close": 50000.0,
    }
    base_htf: dict = {"ema_21": 49800.0, "ema_50": 49000.0}

    if indicators:
        base_ind.update(indicators)
    if htf_indicators:
        base_htf.update(htf_indicators)

    return DataSnapshot(
        symbol="BTC/USDT",
        price=price,
        bid=price * 0.9999,
        ask=price * 1.0001,
        spread_pct=spread_pct,
        ohlcv={},
        indicators=base_ind,
        htf_indicators=base_htf,
        anomaly_flag=anomaly_flag,
        anomaly_reason=anomaly_reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TrendFollowingStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestTrendFollowing:
    def setup_method(self):
        self.strategy = TrendFollowingStrategy(_CFG)

    def test_long_all_conditions_met(self):
        snap = _snapshot(
            price=50100.0,
            indicators={
                "ema_9": 50200.0,
                "ema_21": 49900.0,
                "ema_50": 49500.0,
                "rsi_14": 55.0,
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 50100.0,
                "open": 49800.0,
            },
            htf_indicators={"ema_21": 49800.0, "ema_50": 49000.0},
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.LONG
        assert sig.confidence_score > 0.9  # 5/5 + HTF bonus
        assert sig.suggested_stop_loss < snap.price
        assert sig.suggested_take_profit > snap.price

    def test_short_all_conditions_met(self):
        snap = _snapshot(
            price=49300.0,           # must be < ema_21 (49400)
            indicators={
                "ema_9": 49000.0,
                "ema_21": 49400.0,
                "ema_50": 49800.0,
                "rsi_14": 45.0,
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 49500.0,
                "open": 49700.0,
            },
            htf_indicators={"ema_21": 49000.0, "ema_50": 49800.0},
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.SHORT
        assert sig.confidence_score > 0.9
        assert sig.suggested_stop_loss > snap.price
        assert sig.suggested_take_profit < snap.price

    def test_flat_when_rsi_out_of_range(self):
        snap = _snapshot(
            price=50100.0,
            indicators={
                "ema_9": 50200.0,
                "ema_21": 49900.0,
                "ema_50": 49500.0,
                "rsi_14": 70.0,   # outside [45,65]
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 50100.0,
                "open": 49800.0,
            },
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT

    def test_exit_long_on_high_rsi(self):
        snap = _snapshot(
            price=52000.0,
            indicators={
                "ema_9": 51000.0,
                "ema_21": 50500.0,
                "ema_50": 50000.0,
                "rsi_14": 78.0,   # > 75 → exit long
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 52000.0,
                "open": 51500.0,
            },
        )
        snap.current_position_direction = "LONG"
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.EXIT
        assert sig.confidence_score == 1.0

    def test_exit_long_when_price_below_ema50(self):
        snap = _snapshot(
            price=48000.0,
            indicators={
                "ema_9": 49000.0,
                "ema_21": 49200.0,
                "ema_50": 49500.0,  # price < ema50
                "rsi_14": 40.0,
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 48000.0,
                "open": 49000.0,
            },
        )
        snap.current_position_direction = "LONG"
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.EXIT

    def test_flat_when_volume_insufficient(self):
        snap = _snapshot(
            price=50100.0,
            indicators={
                "ema_9": 50200.0,
                "ema_21": 49900.0,
                "ema_50": 49500.0,
                "rsi_14": 55.0,
                "volume": 800.0,   # < sma * 1.2 = 1200
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 50100.0,
                "open": 49800.0,
            },
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT

    def test_flat_when_indicators_nan(self):
        snap = _snapshot(indicators={"ema_9": float("nan")})
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT

    def test_long_confidence_without_htf(self):
        snap = _snapshot(
            price=50100.0,
            indicators={
                "ema_9": 50200.0,
                "ema_21": 49900.0,
                "ema_50": 49500.0,
                "rsi_14": 55.0,
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 50100.0,
                "open": 49800.0,
            },
            htf_indicators={"ema_21": 49000.0, "ema_50": 49800.0},  # HTF bearish
        )
        sig = self.strategy.evaluate(snap)
        # All 5 primary conditions met but HTF doesn't confirm → score = 1.0 with no bonus
        assert sig.direction == Direction.LONG
        assert sig.confidence_score == pytest.approx(1.0, abs=0.01)

    def test_rr_ratio_at_least_2_to_1(self):
        snap = _snapshot(
            price=50000.0,
            indicators={
                "ema_9": 50200.0,
                "ema_21": 49900.0,
                "ema_50": 49500.0,
                "rsi_14": 55.0,
                "volume": 1300.0,
                "volume_sma_20": 1000.0,
                "atr_14": 500.0,
                "close": 50000.0,
                "open": 49800.0,
            },
        )
        sig = self.strategy.evaluate(snap)
        if sig.direction == Direction.LONG:
            risk = sig.entry_price - sig.suggested_stop_loss
            reward = sig.suggested_take_profit - sig.entry_price
            assert reward / risk == pytest.approx(2.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# MeanReversionStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestMeanReversion:
    def setup_method(self):
        self.strategy = MeanReversionStrategy(_CFG)

    def test_long_at_lower_band(self):
        snap = _snapshot(
            price=47950.0,
            indicators={
                "rsi_7": 22.0,
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 48200.0,
                "close": 47950.0,   # close < open → bearish candle
            },
        )
        # close < open means bearish candle — should be FLAT for LONG
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT

    def test_long_bullish_candle_at_lower_band(self):
        snap = _snapshot(
            price=47950.0,
            indicators={
                "rsi_7": 22.0,
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 47700.0,
                "close": 47950.0,   # bullish candle
            },
            htf_indicators={"ema_21": 49800.0, "ema_50": 49000.0},  # HTF bullish
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.LONG
        assert sig.confidence_score > 0.9  # 3/3 + HTF bonus

    def test_short_at_upper_band(self):
        snap = _snapshot(
            price=52100.0,
            indicators={
                "rsi_7": 78.0,
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 52300.0,
                "close": 52100.0,   # bearish candle
            },
            htf_indicators={"ema_21": 49000.0, "ema_50": 49800.0},  # HTF bearish
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.SHORT
        assert sig.confidence_score > 0.9

    def test_exit_at_midline(self):
        snap = _snapshot(
            price=50001.0,   # within 0.1% of bb_middle=50000
            indicators={
                "rsi_7": 45.0,
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 49900.0,
                "close": 50001.0,
            },
        )
        snap.current_position_direction = "LONG"
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.EXIT

    def test_exit_when_rsi_neutral(self):
        snap = _snapshot(
            price=49000.0,
            indicators={
                "rsi_7": 50.0,   # neutral → exit
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 49200.0,
                "close": 49000.0,
            },
        )
        snap.current_position_direction = "LONG"
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.EXIT

    def test_flat_when_rsi7_not_extreme(self):
        snap = _snapshot(
            price=47950.0,
            indicators={
                "rsi_7": 30.0,   # not < 25
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 47700.0,
                "close": 47950.0,
            },
        )
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT

    def test_short_stop_above_entry(self):
        snap = _snapshot(
            price=52100.0,
            indicators={
                "rsi_7": 78.0,
                "bb_upper": 52000.0,
                "bb_middle": 50000.0,
                "bb_lower": 48000.0,
                "atr_14": 500.0,
                "open": 52300.0,
                "close": 52100.0,
            },
        )
        sig = self.strategy.evaluate(snap)
        if sig.direction == Direction.SHORT:
            assert sig.suggested_stop_loss > sig.entry_price
            assert sig.suggested_take_profit < sig.entry_price

    def test_flat_with_nan_indicators(self):
        snap = _snapshot(indicators={"rsi_7": float("nan")})
        sig = self.strategy.evaluate(snap)
        assert sig.direction == Direction.FLAT


# ─────────────────────────────────────────────────────────────────────────────
# StrategyAgent (integration-style, no asyncio needed for logic layer)
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyAgentEvaluate:
    """Tests the _evaluate logic directly without spawning the asyncio loop."""

    def setup_method(self):
        from trading_bot.agents.strategy_agent import StrategyAgent
        import asyncio
        self.bus = {}
        cfg = {"strategy": {"active": ["trend_following", "mean_reversion"]}}
        self.agent = StrategyAgent(self.bus, cfg)

    def _eval(self, snapshot: DataSnapshot):
        return self.agent._evaluate(snapshot)

    def test_suppressed_by_spread(self):
        snap = _snapshot(spread_pct=0.15)
        sig = self._eval(snap)
        assert sig.direction == Direction.FLAT
        assert "Spread" in sig.reasoning[0]

    def test_suppressed_by_anomaly(self):
        snap = _snapshot(anomaly_flag=True, anomaly_reason="Price spike")
        sig = self._eval(snap)
        assert sig.direction == Direction.FLAT
        assert "Anomaly" in sig.reasoning[0]

    def test_conflict_returns_flat(self):
        """Manually patch strategies to return conflicting signals."""
        from trading_bot.models.trading_signal import TradingSignal
        import uuid, time

        def make_sig(direction, score):
            return TradingSignal(
                signal_id=str(uuid.uuid4()),
                direction=direction,
                strategy_name="test",
                confidence_score=score,
                entry_price=50000.0,
                suggested_stop_loss=49000.0,
                suggested_take_profit=52000.0,
                timeframe="1h",
                reasoning=["test"],
                timestamp=int(time.time() * 1000),
            )

        snap = _snapshot()
        # Override evaluate on both strategies
        self.agent._strategies[0].evaluate = lambda s: make_sig(Direction.LONG, 0.8)
        self.agent._strategies[1].evaluate = lambda s: make_sig(Direction.SHORT, 0.7)
        sig = self._eval(snap)
        assert sig.direction == Direction.FLAT

    def test_highest_confidence_wins_same_direction(self):
        from trading_bot.models.trading_signal import TradingSignal
        import uuid, time

        def make_sig(direction, score):
            return TradingSignal(
                signal_id=str(uuid.uuid4()),
                direction=direction,
                strategy_name="test",
                confidence_score=score,
                entry_price=50000.0,
                suggested_stop_loss=49000.0,
                suggested_take_profit=52000.0,
                timeframe="1h",
                reasoning=["test"],
                timestamp=int(time.time() * 1000),
            )

        snap = _snapshot()
        self.agent._strategies[0].evaluate = lambda s: make_sig(Direction.LONG, 0.75)
        self.agent._strategies[1].evaluate = lambda s: make_sig(Direction.LONG, 0.90)
        sig = self._eval(snap)
        assert sig.direction == Direction.LONG
        assert sig.confidence_score == pytest.approx(0.90)

    def test_exit_takes_priority_over_directional(self):
        from trading_bot.models.trading_signal import TradingSignal
        import uuid, time

        def make_sig(direction, score):
            return TradingSignal(
                signal_id=str(uuid.uuid4()),
                direction=direction,
                strategy_name="test",
                confidence_score=score,
                entry_price=50000.0,
                suggested_stop_loss=49000.0,
                suggested_take_profit=52000.0,
                timeframe="1h",
                reasoning=["test"],
                timestamp=int(time.time() * 1000),
            )

        snap = _snapshot()
        self.agent._strategies[0].evaluate = lambda s: make_sig(Direction.EXIT, 1.0)
        self.agent._strategies[1].evaluate = lambda s: make_sig(Direction.LONG, 0.85)
        sig = self._eval(snap)
        assert sig.direction == Direction.EXIT
