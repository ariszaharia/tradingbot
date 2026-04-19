from __future__ import annotations
import numpy as np

from trading_bot.models.market_regime import MarketRegime
from trading_bot.utils.indicators import atr, adx_di, bb_width_percentile


def detect_regime(
    daily_highs: np.ndarray,
    daily_lows: np.ndarray,
    daily_closes: np.ndarray,
    timestamp: int,
    lookback_days: int = 14,
    lag_days: int = 4,
) -> MarketRegime | None:
    """
    Detect market regime from daily OHLCV arrays (oldest→newest).

    The consolidation zone is evaluated on the window ending `lag_days` ago
    so that a current-day breakout is correctly identified as leaving a
    recently-completed consolidation (not a current one).

    CONSOLIDATION (need ≥ 3 of 4, evaluated on lagged window):
      1. ATR(14) < ATR(50)  — volatility contracting
      2. BB width percentile (period=20, lookback=100) < 20th  — tight band
      3. ADX(14) < 20  — no directional trend
      4. (high − low) / low < 8% over the lookback window

    consolidation_range_high / _low are set to the HIGH/LOW of that lagged
    window, so the current 4H price can be above range_high (breakout) while
    the regime still reports CONSOLIDATION.

    Returns None when there is insufficient data.
    """
    n = len(daily_closes)
    # Need: ATR(50) lookback + bb_width_percentile(100) lookback + lag
    # Minimum sensible: 165 candles (≈5.5 months)
    if n < 165:
        return None

    # Determine the endpoint of the lagged evaluation window.
    # We want to assess consolidation quality as of `lag_days` ago, so all
    # condition arrays are sliced to [:eval_end].
    eval_end = n - lag_days  # positive index; exclusive slice endpoint

    # Range window: lookback_days ending at eval_end
    zone_highs  = daily_highs[eval_end - lookback_days: eval_end]
    zone_lows   = daily_lows[eval_end - lookback_days:  eval_end]
    zone_high   = float(np.max(zone_highs))
    zone_low    = float(np.min(zone_lows))
    range_pct   = (zone_high - zone_low) / zone_low * 100 if zone_low > 0 else 100.0
    range_tight = range_pct < 8.0

    # ATR(14) vs ATR(50) at eval_end
    atr14_arr  = atr(daily_highs[:eval_end], daily_lows[:eval_end], daily_closes[:eval_end], 14)
    atr50_arr  = atr(daily_highs[:eval_end], daily_lows[:eval_end], daily_closes[:eval_end], 50)
    last_atr14 = float(atr14_arr[-1])
    last_atr50 = float(atr50_arr[-1])
    atr_contracting = (
        last_atr14 == last_atr14 and last_atr50 == last_atr50
        and last_atr14 < last_atr50
    )

    # BB width percentile at eval_end (uses closes up to eval_end)
    bb_pct_arr  = bb_width_percentile(daily_closes[:eval_end], period=20, lookback=100)
    last_bb_pct = float(bb_pct_arr[-1])
    bb_tight    = last_bb_pct == last_bb_pct and last_bb_pct < 20.0

    # ADX at eval_end
    adx_arr, di_plus_arr, di_minus_arr = adx_di(
        daily_highs[:eval_end], daily_lows[:eval_end], daily_closes[:eval_end], 14
    )
    last_adx      = float(adx_arr[-1])
    last_di_plus  = float(di_plus_arr[-1])
    last_di_minus = float(di_minus_arr[-1])
    adx_ok  = last_adx == last_adx  # not NaN
    adx_low = adx_ok and last_adx < 20.0

    conditions_met = sum([atr_contracting, bb_tight, adx_low, range_tight])

    if conditions_met >= 3:
        return MarketRegime(
            regime="CONSOLIDATION",
            confidence=conditions_met / 4.0,
            consolidation_range_high=zone_high,
            consolidation_range_low=zone_low,
            range_duration_days=lookback_days,
            bb_width_pct=last_bb_pct if last_bb_pct == last_bb_pct else 50.0,
            adx_value=last_adx if adx_ok else 0.0,
            timestamp=timestamp,
        )

    # Trending regimes — use current (non-lagged) ADX for classification
    # (we want to know current trend direction, not historical)
    adx_cur_arr, di_plus_cur, di_minus_cur = adx_di(
        daily_highs, daily_lows, daily_closes, 14
    )
    cur_adx      = float(adx_cur_arr[-1])
    cur_di_plus  = float(di_plus_cur[-1])
    cur_di_minus = float(di_minus_cur[-1])
    cur_adx_ok   = cur_adx == cur_adx

    if cur_adx_ok and cur_adx >= 25.0:
        di_ok = cur_di_plus == cur_di_plus and cur_di_minus == cur_di_minus
        up = di_ok and cur_di_plus > cur_di_minus
        return MarketRegime(
            regime="TRENDING_UP" if up else "TRENDING_DOWN",
            confidence=min(cur_adx / 50.0, 1.0),
            bb_width_pct=last_bb_pct if last_bb_pct == last_bb_pct else 50.0,
            adx_value=cur_adx,
            timestamp=timestamp,
        )

    return MarketRegime(
        regime="VOLATILE",
        confidence=0.4,
        bb_width_pct=last_bb_pct if last_bb_pct == last_bb_pct else 50.0,
        adx_value=cur_adx if cur_adx_ok else 0.0,
        timestamp=timestamp,
    )
