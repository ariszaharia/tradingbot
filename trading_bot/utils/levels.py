from __future__ import annotations
import numpy as np


def fibonacci_retracement(cascade_high: float, cascade_low: float) -> dict[str, float]:
    """Fibonacci retracement levels for a liquidation cascade move."""
    diff = cascade_high - cascade_low
    return {
        "0.0":   cascade_low,
        "23.6":  cascade_low + diff * 0.236,
        "38.2":  cascade_low + diff * 0.382,
        "50.0":  cascade_low + diff * 0.500,
        "61.8":  cascade_low + diff * 0.618,
        "78.6":  cascade_low + diff * 0.786,
        "100.0": cascade_high,
    }


def nearest_round_number(price: float, tolerance_pct: float = 2.0) -> float | None:
    """Returns the nearest BTC round number within tolerance_pct of price, or None."""
    if price >= 50_000:
        interval = 5_000
    elif price >= 10_000:
        interval = 1_000
    else:
        interval = 500
    nearest = round(price / interval) * interval
    if abs(nearest - price) / price * 100 <= tolerance_pct:
        return float(nearest)
    return None


def find_support_levels(
    ema_200: float,
    current_price: float,
    tolerance_pct: float = 2.0,
    daily_lows: np.ndarray | None = None,
) -> list[float]:
    """
    Returns significant support levels within tolerance_pct of current price.
    Sources: daily EMA200, approximate weekly lows (7-day min windows), round numbers.
    """
    levels: list[float] = []

    if not np.isnan(ema_200):
        if abs(ema_200 - current_price) / current_price * 100 <= tolerance_pct:
            levels.append(float(ema_200))

    if daily_lows is not None and len(daily_lows) >= 7:
        n = len(daily_lows)
        for i in range(max(0, n - 70), n - 7, 7):
            week_low = float(np.min(daily_lows[i: i + 7]))
            if abs(week_low - current_price) / current_price * 100 <= tolerance_pct:
                levels.append(week_low)

    rn = nearest_round_number(current_price, tolerance_pct)
    if rn is not None:
        levels.append(rn)

    return levels
