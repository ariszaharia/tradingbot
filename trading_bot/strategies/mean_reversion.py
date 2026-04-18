from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """
    Strategy B — Bollinger Bands + RSI(7) mean reversion.

    LONG : price ≤ BB_lower, RSI7 < 25, close > open (bullish candle)
    SHORT: price ≥ BB_upper, RSI7 > 75, close < open (bearish candle)
    EXIT : price returns to BB_middle OR RSI7 returns to 50

    Confidence = (conditions_met / total) + 0.15 HTF bonus.
    No signal if spread > 0.1% or anomaly_flag is set (enforced in StrategyAgent).
    """

    @property
    def name(self) -> str:
        return "mean_reversion"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        ind = snapshot.indicators
        htf = snapshot.htf_indicators
        price = snapshot.price

        rsi7 = ind.get("rsi_7", float("nan"))
        bb_upper = ind.get("bb_upper", float("nan"))
        bb_middle = ind.get("bb_middle", float("nan"))
        bb_lower = ind.get("bb_lower", float("nan"))
        atr14 = ind.get("atr_14", float("nan"))
        candle_open = ind.get("open", float("nan"))
        candle_close = ind.get("close", float("nan"))

        required = [rsi7, bb_upper, bb_middle, bb_lower, atr14, candle_open, candle_close]
        if any(v != v for v in required):
            return self._flat(snapshot, ["Insufficient indicator data"])

        is_bullish_candle = candle_close > candle_open
        is_bearish_candle = candle_close < candle_open

        # ── EXIT (only when in a position) ───────────────────────────────
        pos = snapshot.current_position_direction
        if pos in ("LONG", "SHORT"):
            if abs(price - bb_middle) / bb_middle < 0.001:
                return self._signal(
                    snapshot, Direction.EXIT,
                    [f"Price {price:.2f} returned to BB midline {bb_middle:.2f}"],
                    atr14, price, confidence=1.0,
                )
            if 48 <= rsi7 <= 52:
                return self._signal(
                    snapshot, Direction.EXIT,
                    [f"RSI7={rsi7:.1f} returned to neutral ~50"],
                    atr14, price, confidence=1.0,
                )

        # ── LONG ─────────────────────────────────────────────────────────
        long_checks = {
            f"Price({price:.0f}) ≤ BB_lower({bb_lower:.0f})": price <= bb_lower,
            f"RSI7={rsi7:.1f} < 25": rsi7 < 25,
            f"Bullish candle (close {candle_close:.0f} > open {candle_open:.0f})": is_bullish_candle,
        }
        long_met = [r for r, ok in long_checks.items() if ok]
        long_score = len(long_met) / len(long_checks)
        if self._htf_is_bullish(htf):
            long_score = min(long_score + 0.15, 1.0)
            long_met.append("HTF 4H bullish confirmation")

        if len([ok for ok in long_checks.values() if ok]) == len(long_checks):
            return self._signal(snapshot, Direction.LONG, long_met, atr14, price, long_score)

        # ── SHORT ────────────────────────────────────────────────────────
        short_checks = {
            f"Price({price:.0f}) ≥ BB_upper({bb_upper:.0f})": price >= bb_upper,
            f"RSI7={rsi7:.1f} > 75": rsi7 > 75,
            f"Bearish candle (close {candle_close:.0f} < open {candle_open:.0f})": is_bearish_candle,
        }
        short_met = [r for r, ok in short_checks.items() if ok]
        short_score = len(short_met) / len(short_checks)
        if self._htf_is_bearish(htf):
            short_score = min(short_score + 0.15, 1.0)
            short_met.append("HTF 4H bearish confirmation")

        if len([ok for ok in short_checks.values() if ok]) == len(short_checks):
            return self._signal(snapshot, Direction.SHORT, short_met, atr14, price, short_score)

        return self._flat(snapshot, ["No mean-reversion condition fully met"])

    # ------------------------------------------------------------------ #

    def _signal(
        self,
        snapshot: DataSnapshot,
        direction: Direction,
        reasoning: list[str],
        atr: float,
        price: float,
        confidence: float,
    ) -> TradingSignal:
        sl, tp = self._atr_stop_and_tp(price, direction.value, atr)
        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=direction,
            strategy_name=self.name,
            confidence_score=round(confidence, 4),
            entry_price=price,
            suggested_stop_loss=sl,
            suggested_take_profit=tp,
            timeframe=snapshot.indicators.get("timeframe", "1h"),
            reasoning=reasoning,
            timestamp=int(time.time() * 1000),
        )

    def _flat(self, snapshot: DataSnapshot, reasoning: list[str]) -> TradingSignal:
        price = snapshot.price or 1.0
        atr = snapshot.indicators.get("atr_14", price * 0.01)
        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=Direction.FLAT,
            strategy_name=self.name,
            confidence_score=0.0,
            entry_price=price,
            suggested_stop_loss=price - atr,
            suggested_take_profit=price + atr,
            timeframe="1h",
            reasoning=reasoning,
            timestamp=int(time.time() * 1000),
        )
