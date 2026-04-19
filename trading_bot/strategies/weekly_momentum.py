from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, ExitLevel, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy


class WeeklyMomentumStrategy(BaseStrategy):
    """
    Strategy 3 — Weekly Momentum Continuation (tertiary).

    Closest to the old trend-following but gated on a weekly uptrend.
    Trades pullbacks to 4H EMA21 only when the weekly timeframe shows a
    confirmed directional trend — reducing noise from 1H chop.

    Weekly uptrend (ALL required):
      - Weekly EMA9 > EMA21 > EMA50
      - 2 consecutive weekly closes above EMA9
      - Weekly ADX(14) > 25

    Entry trigger (4H timeframe) — ALL required:
      - Price pulled back to within 0.5% of 4H EMA21
      - 4H RSI(14) ∈ [40, 55]  (pulled back but not collapsed)
      - Current 4H candle is bullish (close > open)  (confirmation)
      - 4H MACD histogram > 0  (momentum resuming)
      - 4H volume > volume_SMA(20) × 1.3

    Exit plan:
      TP1 (50%): previous swing high (max high of last 20 4H candles) → SL to breakeven
      TP2 (50%): trailing at 1.5 × ATR(14) on 4H

    Invalidation exits (strategy exits):
      - 4H EMA21 crosses below EMA50 → EXIT ALL
      - Weekly close below EMA21 (checked via weekly_indicators) → EXIT ALL
    """

    _PULLBACK_ZONE_PCT: float = 0.5   # price within 0.5% of EMA21 counts as pullback
    _RSI_LOW: float = 40.0
    _RSI_HIGH: float = 55.0
    _VOL_MULT: float = 1.3
    _WEEKLY_ADX_MIN: float = 25.0

    @property
    def name(self) -> str:
        return "weekly_momentum"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        ind    = snapshot.indicators
        htf    = snapshot.htf_indicators   # 4H
        weekly = snapshot.weekly_indicators
        pos    = snapshot.current_position_direction
        price  = snapshot.price

        # --- 4H indicators ---------------------------------------------------
        htf_ema21   = htf.get("ema_21",        float("nan"))
        htf_ema50   = htf.get("ema_50",        float("nan"))
        htf_rsi14   = htf.get("rsi_14",        float("nan"))
        htf_vol     = htf.get("volume",         float("nan"))
        htf_vol_sma = htf.get("volume_sma_20",  float("nan"))
        htf_open    = htf.get("open",           float("nan"))
        htf_close   = htf.get("close",          float("nan"))
        htf_macd_h  = htf.get("macd_hist",      float("nan"))
        htf_atr14   = htf.get("atr_14",         ind.get("atr_14", float("nan")))
        swing_high  = htf.get("prev_swing_high_60", float("nan"))  # 4H swing high (60 bars ≈ 10 days)
        swing_low   = htf.get("prev_swing_low_60",  float("nan"))

        # --- Weekly indicators -----------------------------------------------
        w_ema9   = weekly.get("ema_9",    float("nan"))
        w_ema21  = weekly.get("ema_21",   float("nan"))
        w_ema50  = weekly.get("ema_50",   float("nan"))
        w_adx    = weekly.get("adx_14",   float("nan"))
        w_close  = weekly.get("close",    float("nan"))
        w_prev   = weekly.get("prev_close", float("nan"))

        # --- Strategy EXIT: structural invalidation -------------------------
        if pos == "LONG":
            # 4H EMA21 crossed below EMA50
            if htf_ema21 == htf_ema21 and htf_ema50 == htf_ema50 and htf_ema21 < htf_ema50:
                return self._exit(snapshot, price,
                    f"4H EMA21({htf_ema21:.0f}) crossed below EMA50({htf_ema50:.0f}) — invalidated")
            # Weekly close below EMA21
            if w_close == w_close and w_ema21 == w_ema21 and w_close < w_ema21:
                return self._exit(snapshot, price,
                    f"Weekly close({w_close:.0f}) below EMA21({w_ema21:.0f}) — weekly trend broken")

        if pos == "SHORT":
            if htf_ema21 == htf_ema21 and htf_ema50 == htf_ema50 and htf_ema21 > htf_ema50:
                return self._exit(snapshot, price,
                    f"4H EMA21({htf_ema21:.0f}) crossed above EMA50({htf_ema50:.0f}) — invalidated")
            if w_close == w_close and w_ema21 == w_ema21 and w_close > w_ema21:
                return self._exit(snapshot, price,
                    f"Weekly close({w_close:.0f}) above EMA21({w_ema21:.0f}) — weekly trend broken")

        # Skip new entries if already in a position
        if pos is not None:
            return self._flat(snapshot, ["Position open — waiting for SL/TP/invalidation"])

        # --- Data check -------------------------------------------------------
        required = [htf_ema21, htf_ema50, htf_rsi14, htf_vol, htf_vol_sma,
                    htf_open, htf_close, htf_macd_h, htf_atr14,
                    w_ema9, w_ema21, w_ema50, w_adx, w_close, w_prev]
        if any(v != v for v in required):
            return self._flat(snapshot, ["Insufficient indicator data"])

        # --- Weekly trend gate -----------------------------------------------
        weekly_uptrend = (
            w_ema9 > w_ema21 > w_ema50
            and w_adx >= self._WEEKLY_ADX_MIN
            and w_close > w_ema9
            and w_prev > w_ema9   # 2 consecutive closes above EMA9
        )
        weekly_downtrend = (
            w_ema9 < w_ema21 < w_ema50
            and w_adx >= self._WEEKLY_ADX_MIN
            and w_close < w_ema9
            and w_prev < w_ema9
        )

        if not weekly_uptrend and not weekly_downtrend:
            return self._flat(snapshot, [
                f"No clear weekly trend: ADX={w_adx:.1f}, EMA9={w_ema9:.0f}, "
                f"EMA21={w_ema21:.0f}, EMA50={w_ema50:.0f}"])

        is_bullish_4h = htf_close > htf_open
        is_bearish_4h = htf_close < htf_open

        # --- LONG: pullback to 4H EMA21 in weekly uptrend -------------------
        if weekly_uptrend:
            dist_pct = abs(price - htf_ema21) / htf_ema21 * 100 if htf_ema21 > 0 else 100.0
            # 4H structure must still be bullish: EMA21 above EMA50.
            # Without this gate, entries can happen when EMA21 < EMA50, causing the
            # invalidation exit to fire immediately on the next bar.
            if htf_ema21 < htf_ema50:
                return self._flat(snapshot, [
                    f"4H EMA21({htf_ema21:.0f}) < EMA50({htf_ema50:.0f}) — 4H structure broken, skip LONG"])
            checks = {
                f"Price({price:.0f}) within {self._PULLBACK_ZONE_PCT}% of 4H EMA21({htf_ema21:.0f})": (
                    dist_pct <= self._PULLBACK_ZONE_PCT
                ),
                f"RSI14={htf_rsi14:.1f} ∈ [{self._RSI_LOW:.0f},{self._RSI_HIGH:.0f}]": (
                    self._RSI_LOW <= htf_rsi14 <= self._RSI_HIGH
                ),
                f"4H bullish confirmation candle": is_bullish_4h,
                f"MACD hist > 0 (momentum resuming)": htf_macd_h > 0,
                f"Volume({htf_vol:.0f}) > {self._VOL_MULT}×SMA({htf_vol_sma:.0f})": (
                    htf_vol_sma > 0 and htf_vol > htf_vol_sma * self._VOL_MULT
                ),
            }
            met = [r for r, ok in checks.items() if ok]

            if all(checks.values()):
                sl = min(htf_ema50, price - 2.0 * htf_atr14)
                hard_sl = price * (1.0 - 0.025)
                sl = max(sl, hard_sl, 1e-6)

                tp1 = swing_high if swing_high == swing_high and swing_high > price else price + 2.0 * htf_atr14
                score = self._conviction_long(w_adx, htf_macd_h, htf_vol, htf_vol_sma)

                exit_levels = [
                    ExitLevel(price=tp1, fraction=0.50),
                    ExitLevel(price=0.0, fraction=0.50, trailing=True, trailing_atr_mult=1.5),
                ]
                met += [f"Weekly uptrend: EMA9({w_ema9:.0f})>EMA21({w_ema21:.0f})>EMA50({w_ema50:.0f}), ADX={w_adx:.1f}"]
                return TradingSignal(
                    signal_id=str(uuid.uuid4()),
                    direction=Direction.LONG,
                    strategy_name=self.name,
                    confidence_score=round(score, 4),
                    entry_price=price,
                    suggested_stop_loss=sl,
                    suggested_take_profit=tp1,
                    exit_levels=exit_levels,
                    timeframe="4h",
                    reasoning=met,
                    timestamp=int(time.time() * 1000),
                )

        # --- SHORT: bounce to 4H EMA21 in weekly downtrend ------------------
        if weekly_downtrend:
            dist_pct = abs(price - htf_ema21) / htf_ema21 * 100 if htf_ema21 > 0 else 100.0
            if htf_ema21 > htf_ema50:
                return self._flat(snapshot, [
                    f"4H EMA21({htf_ema21:.0f}) > EMA50({htf_ema50:.0f}) — 4H structure not broken, skip SHORT"])
            checks = {
                f"Price({price:.0f}) within {self._PULLBACK_ZONE_PCT}% of 4H EMA21({htf_ema21:.0f})": (
                    dist_pct <= self._PULLBACK_ZONE_PCT
                ),
                f"RSI14={htf_rsi14:.1f} ∈ [45,60]": 45 <= htf_rsi14 <= 60,
                f"4H bearish confirmation candle": is_bearish_4h,
                f"MACD hist < 0 (momentum resuming down)": htf_macd_h < 0,
                f"Volume({htf_vol:.0f}) > {self._VOL_MULT}×SMA({htf_vol_sma:.0f})": (
                    htf_vol_sma > 0 and htf_vol > htf_vol_sma * self._VOL_MULT
                ),
            }
            met = [r for r, ok in checks.items() if ok]

            if all(checks.values()):
                sl = max(htf_ema50, price + 2.0 * htf_atr14)
                hard_sl = price * (1.0 + 0.025)
                sl = min(sl, hard_sl)

                tp1 = swing_low if swing_low == swing_low and swing_low < price else price - 2.0 * htf_atr14
                tp1 = max(tp1, 1e-6)
                score = self._conviction_long(w_adx, abs(htf_macd_h), htf_vol, htf_vol_sma)

                exit_levels = [
                    ExitLevel(price=tp1, fraction=0.50),
                    ExitLevel(price=0.0, fraction=0.50, trailing=True, trailing_atr_mult=1.5),
                ]
                met += [f"Weekly downtrend: EMA9({w_ema9:.0f})<EMA21({w_ema21:.0f})<EMA50({w_ema50:.0f}), ADX={w_adx:.1f}"]
                return TradingSignal(
                    signal_id=str(uuid.uuid4()),
                    direction=Direction.SHORT,
                    strategy_name=self.name,
                    confidence_score=round(score, 4),
                    entry_price=price,
                    suggested_stop_loss=sl,
                    suggested_take_profit=tp1,
                    exit_levels=exit_levels,
                    timeframe="4h",
                    reasoning=met,
                    timestamp=int(time.time() * 1000),
                )

        return self._flat(snapshot, ["No weekly momentum setup found"])

    # -------------------------------------------------------------------------

    def _conviction_long(
        self, adx: float, macd_h: float, vol: float, vol_sma: float
    ) -> float:
        score = 0.6  # base when all entry conditions met
        if adx > 35:
            score += 0.1
        if vol_sma > 0 and vol > vol_sma * 2.0:
            score += 0.1
        if abs(macd_h) > 0:  # any positive histogram adds a tiny bit
            score += 0.05
        return min(score, 1.0)

    def _exit(self, snapshot: DataSnapshot, price: float, reason: str) -> TradingSignal:
        atr = snapshot.htf_indicators.get("atr_14", snapshot.indicators.get("atr_14", price * 0.01))
        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=Direction.EXIT,
            strategy_name=self.name,
            confidence_score=1.0,
            entry_price=price,
            suggested_stop_loss=max(price - atr, 1e-6),
            suggested_take_profit=price + atr,
            timeframe="4h",
            reasoning=[reason],
            timestamp=int(time.time() * 1000),
        )

    def _flat(self, snapshot: DataSnapshot, reasoning: list[str]) -> TradingSignal:
        price = snapshot.price or 1.0
        atr   = snapshot.htf_indicators.get("atr_14", snapshot.indicators.get("atr_14", price * 0.01))
        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=Direction.FLAT,
            strategy_name=self.name,
            confidence_score=0.0,
            entry_price=price,
            suggested_stop_loss=max(price - atr, 1e-6),
            suggested_take_profit=price + atr,
            timeframe="4h",
            reasoning=reasoning,
            timestamp=int(time.time() * 1000),
        )
