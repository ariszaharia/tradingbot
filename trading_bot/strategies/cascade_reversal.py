from __future__ import annotations
import uuid
import time

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, ExitLevel, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy
from trading_bot.utils.levels import fibonacci_retracement, find_support_levels


class CascadeReversalStrategy(BaseStrategy):
    """
    Strategy 2 — Liquidation Cascade Reversal (secondary).

    Targets the snap-back after forced BTC liquidations overshoot fair value.
    This is NOT generic mean reversion — it requires specific derivative-driven
    cascade conditions.

    Entry (1H trigger) — ALL conditions required:
      - Drop > 5% from max high of last 4 completed 1H candles
      - 1H RSI(14) < 22  (extreme oversold)
      - Current 1H volume > 3× Volume_SMA(20)
      - At least 2 of last 4 candles have lower wick > 50% of body
      - Price within 2% of a support level (daily EMA200, weekly low, round number)
      - Spread < 0.2%  (market functional)

    Risk rules specific to this strategy:
      - Maximum 1 trade active (enforced by backtest engine)
      - Risk is 0.5% of capital (half normal) — signal carries lower min_confidence
        so the Risk Agent can apply a separate size cap; not implemented here,
        but the strategy returns confidence capped at 0.75 to signal higher risk.
      - Do NOT trade if drop_from_cascade_pct > 20% (third+ >5% drop = downtrend, not cascade)

    Exit plan:
      TP1 (50%): 50% Fibonacci retracement of cascade → SL moves to breakeven
      TP2 (30%): 78.6% Fibonacci retracement
      TP3 (20%): trailing at 1.0 × ATR(14) on 1H

    Time exit: candles_in_position >= 12 → EXIT (reversals that stall aren't coming back)
    """

    _DROP_THRESHOLD_PCT: float = 5.0
    _MAX_DROP_PCT: float = 20.0   # avoid trading into sustained downtrends
    _RSI_THRESHOLD: float = 22.0
    _VOLUME_MULT: float = 3.0
    _MIN_WICK_COUNT: int = 2
    _SUPPORT_TOLERANCE_PCT: float = 2.0
    _TIME_EXIT_CANDLES: int = 12

    @property
    def name(self) -> str:
        return "cascade_reversal"

    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        ind    = snapshot.indicators
        daily  = snapshot.daily_indicators
        pos    = snapshot.current_position_direction
        price  = snapshot.price

        rsi14       = ind.get("rsi_14",               float("nan"))
        vol         = ind.get("volume",                float("nan"))
        vol_sma     = ind.get("volume_sma_20",         float("nan"))
        drop_pct    = ind.get("drop_from_cascade_pct", float("nan"))
        cascade_h   = ind.get("cascade_high_4h",       float("nan"))
        cascade_l   = ind.get("cascade_low_4h",        float("nan"))
        wick_count  = ind.get("lower_wick_count_4h",   float("nan"))
        atr14       = ind.get("atr_14",                float("nan"))
        ema_200_d   = daily.get("ema_200",             float("nan"))
        daily_lows_key = "low"  # daily compute_all stores last daily low as "low"
        # We don't have the full daily lows array here — use EMA200 and round numbers only

        # --- Time exit (in position) ----------------------------------------
        if pos == "LONG" and snapshot.candles_in_position >= self._TIME_EXIT_CANDLES:
            return self._exit(snapshot, price,
                f"Time exit: {snapshot.candles_in_position} candles, reversal stalled")

        # --- No SHORT signals (cascade reversals are LONG only) -------------
        if pos is not None:
            return self._flat(snapshot, ["Position open — waiting for time exit or SL/TP"])

        # --- Data availability check ----------------------------------------
        required = [rsi14, vol, vol_sma, drop_pct, cascade_h, wick_count, atr14]
        if any(v != v for v in required):
            return self._flat(snapshot, ["Insufficient indicator data for cascade detection"])

        # --- Entry conditions -----------------------------------------------
        conditions: dict[str, bool] = {
            f"Drop {drop_pct:.1f}% > {self._DROP_THRESHOLD_PCT}% in 4h": (
                drop_pct >= self._DROP_THRESHOLD_PCT
            ),
            f"RSI14={rsi14:.1f} < {self._RSI_THRESHOLD}": rsi14 < self._RSI_THRESHOLD,
            f"Volume({vol:.0f}) > {self._VOLUME_MULT}×SMA({vol_sma:.0f})": (
                vol_sma > 0 and vol > vol_sma * self._VOLUME_MULT
            ),
            f"Lower wick count {wick_count:.0f} ≥ {self._MIN_WICK_COUNT}": (
                wick_count >= self._MIN_WICK_COUNT
            ),
        }

        # Support proximity check
        support_levels = find_support_levels(
            ema_200=ema_200_d,
            current_price=price,
            tolerance_pct=self._SUPPORT_TOLERANCE_PCT,
        )
        near_support = len(support_levels) > 0
        conditions[f"Near support level (EMA200 or round number, ±{self._SUPPORT_TOLERANCE_PCT}%)"] = near_support

        met = [r for r, ok in conditions.items() if ok]

        if not all(conditions.values()):
            return self._flat(snapshot, [f"Cascade conditions not met: {len(met)}/{len(conditions)}"])

        # Avoid sustained downtrends (≥ 3 cascades = trend, not event)
        if drop_pct > self._MAX_DROP_PCT:
            return self._flat(snapshot, [
                f"Drop {drop_pct:.1f}% > {self._MAX_DROP_PCT}% — likely sustained downtrend"])

        # Spread check (market must be functional)
        if snapshot.spread_pct > 0.2:
            return self._flat(snapshot, [f"Spread {snapshot.spread_pct:.3f}% > 0.2% — market impaired"])

        # --- Fibonacci TPs --------------------------------------------------
        # Cascade: from cascade_h (max high 4h ago) to cascade_l (min low 4h ago)
        fib = fibonacci_retracement(cascade_h, cascade_l)
        tp1 = fib["50.0"]
        tp2 = fib["78.6"]

        # SL: 1.5% below lowest wick in cascade; hard cap entry - 2.5%
        raw_sl  = cascade_l * (1.0 - 0.015)
        hard_sl = price * (1.0 - 0.025)
        sl = max(raw_sl, hard_sl)

        if sl >= price:
            return self._flat(snapshot, ["SL calculation degenerate (cascade_l >= entry)"])

        # Conviction (capped at 0.75 to signal elevated risk to Risk Agent)
        score = min(0.5 + 0.05 * int(near_support) + 0.1 * int(rsi14 < 18)
                    + 0.1 * int(vol > vol_sma * 5.0), 0.75)

        exit_levels = [
            ExitLevel(price=tp1, fraction=0.50),
            ExitLevel(price=tp2, fraction=0.30),
            ExitLevel(price=0.0, fraction=0.20, trailing=True, trailing_atr_mult=1.0),
        ]

        return TradingSignal(
            signal_id=str(uuid.uuid4()),
            direction=Direction.LONG,
            strategy_name=self.name,
            confidence_score=round(score, 4),
            entry_price=price,
            suggested_stop_loss=max(sl, 1e-6),
            suggested_take_profit=tp1,
            exit_levels=exit_levels,
            timeframe="1h",
            reasoning=met,
            timestamp=int(time.time() * 1000),
        )

    # -------------------------------------------------------------------------

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
            timeframe="1h",
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
            timeframe="1h",
            reasoning=reasoning,
            timestamp=int(time.time() * 1000),
        )
