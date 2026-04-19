from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy


class TrendFollowingStrategy(BaseStrategy):
    """
    Strategy A — Two-mode trend following.

    MODE 1 — Pullback to EMA21 (primary, ADX > 25):
      Entry when price pulls back to EMA21 zone in a confirmed trend.
      Requires: ADX > 25, EMA9 > EMA21 > EMA50, price > EMA200, HTF bullish,
               low touched EMA21 zone (within 0.3%), close > EMA21,
               bullish candle, RSI14 in [38, 62], DI+ > DI-.
      SL: 2.0 ATR | TP: 5.0 ATR → R:R 2.5:1 | breakeven WR: 28.6%

    MODE 2 — Momentum continuation (ADX > 35, very strong trend):
      Entry when all EMAs stacked and price is in a healthy position above EMA21.
      No strict pullback touch required — captures sustained trend moves that
      never pull back cleanly to EMA21.
      Requires: ADX > 35, EMA9 > EMA21 > EMA50 > EMA200, HTF bullish,
               bullish candle, RSI14 in [45, 72], DI+ > DI-,
               price within 0.5% above EMA21 (not too extended).
      SL: 2.0 ATR | TP: 5.0 ATR → same R:R

    Exit LONG: price < EMA50 after 8+ candles (structural break)
               OR stalled: candles_in >= 20 AND price < EMA21

    Exit SHORT: mirror image.
    """

    _ADX_MIN: float = 25.0
    _ADX_MOMENTUM: float = 35.0   # momentum mode threshold
    _TIME_EXIT_CANDLES: int = 20  # extended from 12 → more room for trend to play out

    @property
    def name(self) -> str:
        return "trend_following"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        ind = snapshot.indicators
        htf = snapshot.htf_indicators
        price = snapshot.price

        ema9    = ind.get("ema_9",         float("nan"))
        ema21   = ind.get("ema_21",        float("nan"))
        ema50   = ind.get("ema_50",        float("nan"))
        ema200  = ind.get("ema_200",       float("nan"))
        rsi14   = ind.get("rsi_14",        float("nan"))
        adx14   = ind.get("adx_14",        float("nan"))
        di_plus = ind.get("di_plus_14",    float("nan"))
        di_minus= ind.get("di_minus_14",   float("nan"))
        vol     = ind.get("volume",        float("nan"))
        vol_sma = ind.get("volume_sma_20", float("nan"))
        atr14   = ind.get("atr_14",        float("nan"))
        macd_h  = ind.get("macd_hist",     float("nan"))
        candle_low   = ind.get("low",   float("nan"))
        candle_high  = ind.get("high",  float("nan"))
        candle_open  = ind.get("open",  float("nan"))
        candle_close = ind.get("close", float("nan"))

        required = [ema9, ema21, ema50, ema200, rsi14, adx14, atr14,
                    candle_low, candle_open, candle_close]
        if any(v != v for v in required):
            return self._flat(snapshot, ["Insufficient indicator data"])

        candles_open = snapshot.candles_in_position
        pos = snapshot.current_position_direction

        # ── EXIT checks (structural breaks only — SL/TP are primary exits) ───
        if pos == "LONG":
            exit_reasons: list[str] = []
            if candles_open >= 8 and price < ema50:
                exit_reasons.append(
                    f"Structural break after {candles_open} candles: "
                    f"price {price:.2f} < EMA50 {ema50:.2f}"
                )
            if candles_open >= self._TIME_EXIT_CANDLES and price < ema21:
                exit_reasons.append(
                    f"Stalled: {candles_open} candles, price {price:.2f} < EMA21 {ema21:.2f}"
                )
            if exit_reasons:
                return self._signal(snapshot, Direction.EXIT, exit_reasons, atr14, price, 1.0)

        elif pos == "SHORT":
            exit_short_reasons: list[str] = []
            if candles_open >= 8 and price > ema50:
                exit_short_reasons.append(
                    f"Structural break after {candles_open} candles: "
                    f"price {price:.2f} > EMA50 {ema50:.2f}"
                )
            if candles_open >= self._TIME_EXIT_CANDLES and price > ema21:
                exit_short_reasons.append(
                    f"Stalled: {candles_open} candles, price {price:.2f} > EMA21 {ema21:.2f}"
                )
            if exit_short_reasons:
                return self._signal(snapshot, Direction.EXIT, exit_short_reasons, atr14, price, 1.0)

        # ── Hard regime gate ─────────────────────────────────────────────────
        if adx14 <= self._ADX_MIN:
            return self._flat(snapshot, [
                f"ADX({adx14:.1f}) ≤ {self._ADX_MIN} — trend too weak for entry"
            ])

        is_bullish_candle = candle_close > candle_open
        is_bearish_candle = candle_close < candle_open
        strong_trend = adx14 > 30
        volume_active = vol > vol_sma * 1.2 if vol_sma > 0 else False
        macd_bullish = macd_h > 0 if macd_h == macd_h else False
        macd_bearish = macd_h < 0 if macd_h == macd_h else False
        di_long_ok   = di_plus > di_minus if (di_plus == di_plus and di_minus == di_minus) else True
        di_short_ok  = di_minus > di_plus if (di_plus == di_plus and di_minus == di_minus) else True

        htf_bullish = self._htf_is_bullish(htf)
        htf_bearish = self._htf_is_bearish(htf)

        # ── MODE 1: Pullback to EMA21 in uptrend ────────────────────────────
        if price > ema200 and htf_bullish and ema9 > ema21 > ema50 and di_long_ok:
            # Pullback zone: low dipped to EMA21 area (within 0.3%)
            # Close must be above EMA9 (not just EMA21) to confirm a strong bounce
            touched_ema21 = candle_low <= ema21 * 1.003
            long_checks = {
                f"Low({candle_low:.0f}) ≤ EMA21({ema21:.0f})×1.003": touched_ema21,
                f"Close({candle_close:.0f}) > EMA9({ema9:.0f}) (strong bounce)": candle_close > ema9,
                f"Bullish candle at EMA21": is_bullish_candle,
                f"RSI14={rsi14:.1f} ∈ [38,62]": 38 <= rsi14 <= 62,
                f"MACD hist > 0 (momentum aligned)": macd_bullish,
            }
            long_met = [r for r, ok in long_checks.items() if ok]
            long_score = len(long_met) / len(long_checks)
            if strong_trend:
                long_score = min(long_score + 0.10, 1.0)
                long_met.append(f"Strong trend ADX={adx14:.1f} > 30")
            if volume_active:
                long_score = min(long_score + 0.10, 1.0)
                long_met.append(f"Volume ({vol:.0f}) > VolSMA×1.2")
            long_met.append(f"HTF 4H bullish | DI+({di_plus:.1f}) > DI-({di_minus:.1f})")

            if all(long_checks.values()):
                return self._signal(snapshot, Direction.LONG, long_met, atr14, price,
                                    long_score, mode="pullback")

        # ── MODE 2: Momentum continuation (ADX > 35, price near EMA21) ──────
        # Catches sustained trends where price never pulls all the way back to EMA21.
        if (adx14 > self._ADX_MOMENTUM and price > ema200
                and htf_bullish and ema9 > ema21 > ema50 > ema200
                and di_long_ok):
            # Price above EMA21 and not more than 1% above EMA9 (not too extended)
            near_ema21 = ema21 < price <= ema9 * 1.01
            momentum_checks = {
                f"Price({price:.0f}) above EMA21, within EMA9×1.01": near_ema21,
                f"Bullish candle": is_bullish_candle,
                f"RSI14={rsi14:.1f} ∈ [45,72] (healthy momentum)": 45 <= rsi14 <= 72,
            }
            momentum_met = [r for r, ok in momentum_checks.items() if ok]
            momentum_score = len(momentum_met) / len(momentum_checks)
            if strong_trend:
                momentum_score = min(momentum_score + 0.10, 1.0)
                momentum_met.append(f"Very strong trend ADX={adx14:.1f} > {self._ADX_MOMENTUM}")
            if volume_active:
                momentum_score = min(momentum_score + 0.10, 1.0)
                momentum_met.append(f"Volume ({vol:.0f}) > VolSMA×1.2")
            if macd_bullish:
                momentum_score = min(momentum_score + 0.05, 1.0)
                momentum_met.append("MACD hist positive")
            momentum_met.append(f"HTF 4H bullish | DI+({di_plus:.1f}) > DI-({di_minus:.1f})")

            if all(momentum_checks.values()):
                return self._signal(snapshot, Direction.LONG, momentum_met, atr14, price,
                                    momentum_score, mode="momentum")

        # ── MODE 1 SHORT: Pullback to EMA21 in downtrend ────────────────────
        if price < ema200 and htf_bearish and ema9 < ema21 < ema50 and di_short_ok:
            touched_ema21_short = candle_high >= ema21 * 0.997
            short_checks = {
                f"High({candle_high:.0f}) ≥ EMA21({ema21:.0f})×0.997": touched_ema21_short,
                f"Close({candle_close:.0f}) < EMA9({ema9:.0f}) (strong rejection)": candle_close < ema9,
                f"Bearish candle at EMA21": is_bearish_candle,
                f"RSI14={rsi14:.1f} ∈ [38,62]": 38 <= rsi14 <= 62,
                f"MACD hist < 0 (downward momentum)": macd_bearish,
            }
            short_met = [r for r, ok in short_checks.items() if ok]
            short_score = len(short_met) / len(short_checks)
            if strong_trend:
                short_score = min(short_score + 0.10, 1.0)
                short_met.append(f"Strong trend ADX={adx14:.1f} > 30")
            if volume_active:
                short_score = min(short_score + 0.10, 1.0)
                short_met.append(f"Volume ({vol:.0f}) > VolSMA×1.2")
            short_met.append(f"HTF 4H bearish | DI-({di_minus:.1f}) > DI+({di_plus:.1f})")

            if all(short_checks.values()):
                return self._signal(snapshot, Direction.SHORT, short_met, atr14, price,
                                    short_score, mode="pullback")

        # ── MODE 2 SHORT: Momentum continuation downtrend ────────────────────
        if (adx14 > self._ADX_MOMENTUM and price < ema200
                and htf_bearish and ema9 < ema21 < ema50 < ema200
                and di_short_ok):
            near_ema21_short = ema9 * 0.99 <= price < ema21
            momentum_short_checks = {
                f"Price({price:.0f}) below EMA21, within EMA9×0.99": near_ema21_short,
                f"Bearish candle": is_bearish_candle,
                f"RSI14={rsi14:.1f} ∈ [28,55]": 28 <= rsi14 <= 55,
            }
            ms_met = [r for r, ok in momentum_short_checks.items() if ok]
            ms_score = len(ms_met) / len(momentum_short_checks)
            if strong_trend:
                ms_score = min(ms_score + 0.10, 1.0)
                ms_met.append(f"Very strong trend ADX={adx14:.1f} > {self._ADX_MOMENTUM}")
            if volume_active:
                ms_score = min(ms_score + 0.10, 1.0)
                ms_met.append(f"Volume ({vol:.0f}) > VolSMA×1.2")
            if macd_bearish:
                ms_score = min(ms_score + 0.05, 1.0)
                ms_met.append("MACD hist negative")
            ms_met.append(f"HTF 4H bearish | DI-({di_minus:.1f}) > DI+({di_plus:.1f})")

            if all(momentum_short_checks.values()):
                return self._signal(snapshot, Direction.SHORT, ms_met, atr14, price,
                                    ms_score, mode="momentum")

        return self._flat(snapshot, ["No trend setup found"])

    # ─────────────────────────────────────────────────────────────────────── #

    def _signal(
        self,
        snapshot: DataSnapshot,
        direction: Direction,
        reasoning: list[str],
        atr: float,
        price: float,
        confidence: float,
        mode: str = "pullback",
    ) -> TradingSignal:
        # Wider SL (2.0 ATR) gives trades more room to breathe.
        # Wider TP (5.0 ATR) → R:R = 2.5:1, breakeven WR = 28.6%.
        sl, tp = self._atr_stop_and_tp(price, direction.value, atr, sl_mult=2.0, tp_mult=5.0)
        name = f"{self.name}_{mode}"
        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=direction,
            strategy_name=name,
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
