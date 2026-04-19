from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.market_regime import MarketRegime
from trading_bot.models.trading_signal import Direction, ExitLevel, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    """
    Strategy 1 — Regime-Gated Breakout (primary, highest expected edge).

    Entry logic (4H trigger):
      CONSOLIDATION detected on Daily (≥3 of 4 criteria) AND
      4H close above 14-day high (LONG) / below 14-day low (SHORT) AND
      Volume > 2× SMA(20) on 4H AND
      RSI(14) on 4H: 55–70 (LONG) / 30–45 (SHORT) AND
      Candle body > 60% of full range

    SL: opposite end of consolidation range (rejected if > 3% from entry).

    Exit plan (scaled):
      TP1 (40%): entry ± 1.5 × range_width → SL moves to breakeven
      TP2 (40%): entry ± 3.0 × range_width
      TP3 (20%): trailing at 1.5 × ATR(14) on 4H

    Conviction scoring (min 0.7 required, base 0.5 when all conditions met):
      +0.1 Weekly EMA21 aligned with trade direction
      +0.1 Consolidation lasted > 21 days (actually range_duration_days as set by detector)
      +0.1 Volume > 3× SMA on breakout candle
      +0.1 BB width percentile < 10 (very tight compression)

    Strategy exit: if price re-enters consolidation range → EXIT (failed breakout).
    """

    @property
    def name(self) -> str:
        return "breakout"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        regime = snapshot.regime
        pos    = snapshot.current_position_direction
        price  = snapshot.price
        htf    = snapshot.htf_indicators
        ind    = snapshot.indicators
        weekly = snapshot.weekly_indicators

        # --- Strategy EXIT: failed breakout ----------------------------------
        if pos == "LONG" and regime is not None and regime.consolidation_range_high is not None:
            if price < regime.consolidation_range_high:
                return self._exit(snapshot, price,
                    f"Failed breakout: price {price:.0f} re-entered consolidation "
                    f"(< range high {regime.consolidation_range_high:.0f})")

        if pos == "SHORT" and regime is not None and regime.consolidation_range_low is not None:
            if price > regime.consolidation_range_low:
                return self._exit(snapshot, price,
                    f"Failed breakout: price {price:.0f} re-entered consolidation "
                    f"(> range low {regime.consolidation_range_low:.0f})")

        # --- Regime gate -----------------------------------------------------
        if regime is None or regime.regime != "CONSOLIDATION":
            return self._flat(snapshot, ["No consolidation regime on daily"])

        range_high = regime.consolidation_range_high
        range_low  = regime.consolidation_range_low
        if range_high is None or range_low is None:
            return self._flat(snapshot, ["Consolidation range not set"])

        range_width = range_high - range_low

        # --- 4H indicator data -----------------------------------------------
        htf_rsi14   = htf.get("rsi_14",        float("nan"))
        htf_vol     = htf.get("volume",         float("nan"))
        htf_vol_sma = htf.get("volume_sma_20",  float("nan"))
        htf_open    = htf.get("open",           float("nan"))
        htf_high    = htf.get("high",           float("nan"))
        htf_low     = htf.get("low",            float("nan"))
        htf_close   = htf.get("close",          float("nan"))
        htf_atr14   = htf.get("atr_14",         ind.get("atr_14", float("nan")))

        required = [htf_rsi14, htf_vol, htf_vol_sma, htf_open, htf_high, htf_low, htf_close]
        if any(v != v for v in required):
            return self._flat(snapshot, ["Insufficient 4H indicator data"])

        candle_range = htf_high - htf_low
        body_pct = abs(htf_close - htf_open) / candle_range if candle_range > 0 else 0.0

        # --- Weekly alignment ------------------------------------------------
        w_ema21 = weekly.get("ema_21", float("nan"))
        w_ema50 = weekly.get("ema_50", float("nan"))
        weekly_aligned_long  = w_ema21 == w_ema21 and w_ema50 == w_ema50 and w_ema21 > w_ema50
        weekly_aligned_short = w_ema21 == w_ema21 and w_ema50 == w_ema50 and w_ema21 < w_ema50

        # --- LONG BREAKOUT ---------------------------------------------------
        if htf_close > range_high:
            sl_price    = range_low
            sl_dist_pct = (price - sl_price) / price * 100
            if sl_dist_pct > 3.0:
                return self._flat(snapshot, [
                    f"Range too wide: SL {sl_dist_pct:.1f}% away (max 3%)"])

            checks = {
                f"4H close({htf_close:.0f}) > range high({range_high:.0f})": htf_close > range_high,
                f"Volume({htf_vol:.0f}) > 2×SMA({htf_vol_sma:.0f})": htf_vol > htf_vol_sma * 2.0,
                f"RSI14={htf_rsi14:.1f} ∈ [55,70]": 55 <= htf_rsi14 <= 70,
                f"Body pct {body_pct:.0%} > 60%": body_pct > 0.60,
            }
            met = [r for r, ok in checks.items() if ok]

            if all(checks.values()):
                score = self._conviction(regime, weekly_aligned_long, htf_vol, htf_vol_sma)
                if score < 0.7:
                    return self._flat(snapshot, [
                        f"Breakout conditions met but conviction {score:.2f} < 0.70"])
                return self._signal(
                    snapshot, Direction.LONG, met, htf_atr14, price, score,
                    sl_price, range_width,
                )

        # --- SHORT BREAKOUT --------------------------------------------------
        if htf_close < range_low:
            sl_price    = range_high
            sl_dist_pct = (sl_price - price) / price * 100
            if sl_dist_pct > 3.0:
                return self._flat(snapshot, [
                    f"Range too wide: SL {sl_dist_pct:.1f}% away (max 3%)"])

            checks = {
                f"4H close({htf_close:.0f}) < range low({range_low:.0f})": htf_close < range_low,
                f"Volume({htf_vol:.0f}) > 2×SMA({htf_vol_sma:.0f})": htf_vol > htf_vol_sma * 2.0,
                f"RSI14={htf_rsi14:.1f} ∈ [30,45]": 30 <= htf_rsi14 <= 45,
                f"Body pct {body_pct:.0%} > 60%": body_pct > 0.60,
            }
            met = [r for r, ok in checks.items() if ok]

            if all(checks.values()):
                score = self._conviction(regime, weekly_aligned_short, htf_vol, htf_vol_sma)
                if score < 0.7:
                    return self._flat(snapshot, [
                        f"Breakout conditions met but conviction {score:.2f} < 0.70"])
                return self._signal(
                    snapshot, Direction.SHORT, met, htf_atr14, price, score,
                    sl_price, range_width,
                )

        return self._flat(snapshot, ["No breakout condition met"])

    # -------------------------------------------------------------------------

    def _conviction(
        self,
        regime: MarketRegime,
        weekly_aligned: bool,
        vol: float,
        vol_sma: float,
    ) -> float:
        score = 0.5
        if weekly_aligned:
            score += 0.1
        if regime.range_duration_days >= 21:
            score += 0.1
        if vol_sma > 0 and vol > vol_sma * 3.0:
            score += 0.1
        if regime.bb_width_pct == regime.bb_width_pct and regime.bb_width_pct < 10.0:
            score += 0.1
        return min(score, 1.0)

    def _signal(
        self,
        snapshot: DataSnapshot,
        direction: Direction,
        reasoning: list[str],
        atr: float,
        price: float,
        confidence: float,
        sl_price: float,
        range_width: float,
    ) -> TradingSignal:
        if direction == Direction.LONG:
            tp1 = price + 1.5 * range_width
            tp2 = price + 3.0 * range_width
        else:
            tp1 = price - 1.5 * range_width
            tp2 = price - 3.0 * range_width

        exit_levels = [
            ExitLevel(price=tp1, fraction=0.40),
            ExitLevel(price=tp2, fraction=0.40),
            ExitLevel(price=0.0, fraction=0.20, trailing=True, trailing_atr_mult=1.5),
        ]

        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=direction,
            strategy_name=self.name,
            confidence_score=round(confidence, 4),
            entry_price=price,
            suggested_stop_loss=max(sl_price, 1e-6),
            suggested_take_profit=tp1,
            exit_levels=exit_levels,
            timeframe="4h",
            reasoning=reasoning,
            timestamp=int(time.time() * 1000),
        )

    def _exit(self, snapshot: DataSnapshot, price: float, reason: str) -> TradingSignal:
        atr = snapshot.indicators.get("atr_14", price * 0.01)
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
        atr   = snapshot.indicators.get("atr_14", price * 0.01)
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
