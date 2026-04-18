from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy


class TrendFollowingStrategy(BaseStrategy):
    """
    Strategy A — EMA stack + RSI + volume confirmation.

    LONG : EMA9 > EMA21 > EMA50, RSI14 ∈ [45,65], price > EMA21,
           volume > VolSMA20 * 1.2, HTF bullish (EMA21_4h > EMA50_4h)
    SHORT: EMA9 < EMA21 < EMA50, RSI14 ∈ [35,55], price < EMA21,
           volume > VolSMA20 * 1.2, HTF bearish
    EXIT LONG : RSI14 > 75 OR price < EMA50
    EXIT SHORT: RSI14 < 25 OR price > EMA50

    Confidence score = (conditions_met / total_conditions).
    HTF alignment adds 0.15 bonus (capped at 1.0).
    """

    @property
    def name(self) -> str:
        return "trend_following"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        ind = snapshot.indicators
        htf = snapshot.htf_indicators
        price = snapshot.price

        ema9 = ind.get("ema_9", float("nan"))
        ema21 = ind.get("ema_21", float("nan"))
        ema50 = ind.get("ema_50", float("nan"))
        rsi14 = ind.get("rsi_14", float("nan"))
        vol = ind.get("volume", float("nan"))
        vol_sma = ind.get("volume_sma_20", float("nan"))
        atr14 = ind.get("atr_14", float("nan"))

        # Guard: need all values to be finite
        required = [ema9, ema21, ema50, rsi14, vol, vol_sma, atr14]
        if any(v != v for v in required):  # NaN check
            return self._flat(snapshot, ["Insufficient indicator data"])

        volume_surge = vol > vol_sma * 1.2

        # ── EXIT checks (highest priority, only when in a position) ─────
        pos = snapshot.current_position_direction
        if pos == "LONG":
            exit_reasons: list[str] = []
            if rsi14 > 75:
                exit_reasons.append(f"RSI14={rsi14:.1f} > 75 (exit long)")
            if price < ema50:
                exit_reasons.append(f"Price {price:.2f} < EMA50 {ema50:.2f} (exit long)")
            if exit_reasons:
                return self._signal(snapshot, Direction.EXIT, exit_reasons, atr14, price, confidence=1.0)
        elif pos == "SHORT":
            exit_short_reasons: list[str] = []
            if rsi14 < 25:
                exit_short_reasons.append(f"RSI14={rsi14:.1f} < 25 (exit short)")
            if price > ema50:
                exit_short_reasons.append(f"Price {price:.2f} > EMA50 {ema50:.2f} (exit short)")
            if exit_short_reasons:
                return self._signal(snapshot, Direction.EXIT, exit_short_reasons, atr14, price, confidence=1.0)

        # ── LONG ─────────────────────────────────────────────────────────
        long_checks = {
            f"EMA9({ema9:.0f}) > EMA21({ema21:.0f})": ema9 > ema21,
            f"EMA21({ema21:.0f}) > EMA50({ema50:.0f})": ema21 > ema50,
            f"RSI14={rsi14:.1f} ∈ [45,65]": 45 <= rsi14 <= 65,
            f"Price({price:.0f}) > EMA21({ema21:.0f})": price > ema21,
            f"Volume({vol:.0f}) > VolSMA20*1.2({vol_sma*1.2:.0f})": volume_surge,
        }
        long_met = [reason for reason, ok in long_checks.items() if ok]
        long_score = len(long_met) / len(long_checks)
        if self._htf_is_bullish(htf):
            long_score = min(long_score + 0.15, 1.0)
            long_met.append("HTF 4H bullish (EMA21_4h > EMA50_4h)")

        if len([ok for ok in long_checks.values() if ok]) == len(long_checks):
            return self._signal(snapshot, Direction.LONG, long_met, atr14, price, long_score)

        # ── SHORT ────────────────────────────────────────────────────────
        short_checks = {
            f"EMA9({ema9:.0f}) < EMA21({ema21:.0f})": ema9 < ema21,
            f"EMA21({ema21:.0f}) < EMA50({ema50:.0f})": ema21 < ema50,
            f"RSI14={rsi14:.1f} ∈ [35,55]": 35 <= rsi14 <= 55,
            f"Price({price:.0f}) < EMA21({ema21:.0f})": price < ema21,
            f"Volume({vol:.0f}) > VolSMA20({vol_sma:.0f})": volume_surge,
        }
        short_met = [reason for reason, ok in short_checks.items() if ok]
        short_score = len(short_met) / len(short_checks)
        if self._htf_is_bearish(htf):
            short_score = min(short_score + 0.15, 1.0)
            short_met.append("HTF 4H bearish (EMA21_4h < EMA50_4h)")

        if len([ok for ok in short_checks.values() if ok]) == len(short_checks):
            return self._signal(snapshot, Direction.SHORT, short_met, atr14, price, short_score)

        return self._flat(snapshot, ["No trend condition fully met"])

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
