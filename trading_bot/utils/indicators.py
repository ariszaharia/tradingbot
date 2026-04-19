from __future__ import annotations
import numpy as np


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average. Returns array same length as input (NaN-padded)."""
    if len(closes) < period:
        return np.full(len(closes), np.nan)
    result = np.full(len(closes), np.nan)
    k = 2.0 / (period + 1)
    # Seed with SMA of first `period` values
    result[period - 1] = np.mean(closes[:period])
    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI. Returns array same length as input (NaN-padded)."""
    if len(closes) < period + 1:
        return np.full(len(closes), np.nan)
    result = np.full(len(closes), np.nan)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed Wilder smoothing with simple average
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
        result[i] = 100 - (100 / (1 + rs))
    return result


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range (Wilder smoothing)."""
    if len(closes) < period + 1:
        return np.full(len(closes), np.nan)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(
        highs - lows,
        np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close))
    )
    result = np.full(len(closes), np.nan)
    result[period - 1] = np.mean(tr[:period])
    for i in range(period, len(closes)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = fast_ema - slow_ema
    # signal is EMA of macd_line; only compute where macd_line is valid
    valid_mask = ~np.isnan(macd_line)
    signal_line = np.full(len(closes), np.nan)
    if valid_mask.sum() >= signal:
        valid_idx = np.where(valid_mask)[0]
        sig = ema(macd_line[valid_mask], signal)
        signal_line[valid_idx] = sig
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    closes: np.ndarray,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (upper, middle, lower)."""
    if len(closes) < period:
        nan = np.full(len(closes), np.nan)
        return nan, nan, nan
    middle = np.full(len(closes), np.nan)
    upper = np.full(len(closes), np.nan)
    lower = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        m = np.mean(window)
        s = np.std(window, ddof=1)
        middle[i] = m
        upper[i] = m + num_std * s
        lower[i] = m - num_std * s
    return upper, middle, lower


def volume_sma(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    """Simple moving average of volume."""
    result = np.full(len(volumes), np.nan)
    for i in range(period - 1, len(volumes)):
        result[i] = np.mean(volumes[i - period + 1: i + 1])
    return result


def adx_di(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average Directional Index + Directional Indicators (Wilder method).
    Returns (adx, di_plus, di_minus) — all NaN-padded arrays."""
    n = len(closes)
    if n < period * 2 + 1:
        nan = np.full(n, np.nan)
        return nan, nan, nan

    prev_close = np.empty(n)
    prev_close[0] = closes[0]
    prev_close[1:] = closes[:-1]
    prev_high = np.empty(n)
    prev_high[0] = highs[0]
    prev_high[1:] = highs[:-1]
    prev_low = np.empty(n)
    prev_low[0] = lows[0]
    prev_low[1:] = lows[:-1]

    tr = np.maximum(
        highs - lows,
        np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)),
    )
    up_move = highs - prev_high
    down_move = prev_low - lows
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    s_tr = np.full(n, np.nan)
    s_plus = np.full(n, np.nan)
    s_minus = np.full(n, np.nan)

    if n > period:
        s_tr[period] = np.sum(tr[1:period + 1])
        s_plus[period] = np.sum(plus_dm[1:period + 1])
        s_minus[period] = np.sum(minus_dm[1:period + 1])
        for i in range(period + 1, n):
            s_tr[i] = s_tr[i - 1] - s_tr[i - 1] / period + tr[i]
            s_plus[i] = s_plus[i - 1] - s_plus[i - 1] / period + plus_dm[i]
            s_minus[i] = s_minus[i - 1] - s_minus[i - 1] / period + minus_dm[i]

    di_plus = np.full(n, np.nan)
    di_minus = np.full(n, np.nan)
    dx = np.full(n, np.nan)

    valid = s_tr > 0
    di_plus[valid] = 100.0 * s_plus[valid] / s_tr[valid]
    di_minus[valid] = 100.0 * s_minus[valid] / s_tr[valid]
    di_sum = di_plus + di_minus
    valid_sum = (di_sum > 0) & valid
    dx[valid_sum] = 100.0 * np.abs(
        di_plus[valid_sum] - di_minus[valid_sum]
    ) / di_sum[valid_sum]

    adx_arr = np.full(n, np.nan)
    first_dx = np.where(~np.isnan(dx))[0]
    if len(first_dx) >= period:
        seed = first_dx[period - 1]
        adx_arr[seed] = np.nanmean(dx[first_dx[0]:seed + 1])
        for i in range(seed + 1, n):
            if not np.isnan(dx[i]) and not np.isnan(adx_arr[i - 1]):
                adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period

    return adx_arr, di_plus, di_minus


def bb_width_percentile(
    closes: np.ndarray,
    period: int = 20,
    lookback: int = 100,
) -> np.ndarray:
    """Percentile rank of BB width (upper-lower)/middle vs past `lookback` values (0–100).
    0 = tightest (consolidating), 100 = widest (expanding)."""
    bb_u, bb_m, bb_l = bollinger_bands(closes, period)
    width = np.full(len(closes), np.nan)
    valid = bb_m > 0
    width[valid] = (bb_u[valid] - bb_l[valid]) / bb_m[valid]

    result = np.full(len(closes), np.nan)
    start = period + lookback
    for i in range(start, len(closes)):
        window = width[i - lookback: i + 1]
        v = window[~np.isnan(window)]
        if len(v) < 2:
            continue
        result[i] = float(np.sum(v[:-1] <= v[-1])) / (len(v) - 1) * 100.0
    return result


def atr_percentile(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
    lookback: int = 50,
) -> np.ndarray:
    """Percentile rank of current ATR vs past `lookback` ATR values (0–100).
    High values = elevated volatility regime."""
    atr_arr = atr(highs, lows, closes, period)
    result = np.full(len(closes), np.nan)
    start = period + lookback
    for i in range(start, len(closes)):
        window = atr_arr[i - lookback:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < 2:
            continue
        result[i] = float(np.sum(valid[:-1] <= valid[-1])) / (len(valid) - 1) * 100.0
    return result


def compute_all(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    cfg: dict,
) -> dict[str, float]:
    """
    Compute every indicator needed by the Strategy and Risk agents.
    Returns a flat dict of the LAST valid value for each indicator.
    All inputs are 1-D numpy arrays sorted oldest→newest.
    """
    def last(arr: np.ndarray) -> float:
        valid = arr[~np.isnan(arr)]
        return float(valid[-1]) if len(valid) else float("nan")

    result: dict[str, float] = {}

    for p in cfg.get("ema_periods", [9, 21, 50, 200]):
        result[f"ema_{p}"] = last(ema(closes, p))

    for p in cfg.get("rsi_periods", [7, 14]):
        result[f"rsi_{p}"] = last(rsi(closes, p))

    atr_p = cfg.get("atr_period", 14)
    result[f"atr_{atr_p}"] = last(atr(highs, lows, closes, atr_p))

    macd_l, macd_s, macd_h = macd(
        closes,
        cfg.get("macd_fast", 12),
        cfg.get("macd_slow", 26),
        cfg.get("macd_signal", 9),
    )
    result["macd_line"] = last(macd_l)
    result["macd_signal"] = last(macd_s)
    result["macd_hist"] = last(macd_h)

    bb_u, bb_m, bb_l = bollinger_bands(
        closes,
        cfg.get("bb_period", 20),
        cfg.get("bb_std", 2.0),
    )
    result["bb_upper"] = last(bb_u)
    result["bb_middle"] = last(bb_m)
    result["bb_lower"] = last(bb_l)

    result["volume_sma_20"] = last(volume_sma(volumes, cfg.get("volume_sma_period", 20)))

    adx_arr, di_plus_arr, di_minus_arr = adx_di(highs, lows, closes, period=14)
    result["adx_14"] = last(adx_arr)
    result["di_plus_14"] = last(di_plus_arr)
    result["di_minus_14"] = last(di_minus_arr)

    result["atr_pct_50"] = last(atr_percentile(highs, lows, closes, period=14, lookback=50))

    # BB width percentile (for regime detection on daily)
    result["bb_width_pct_100"] = last(bb_width_percentile(closes, period=20, lookback=100))

    # Latest candle raw values (convenient for strategy logic)
    result["open"] = float(opens[-1])
    result["high"] = float(highs[-1])
    result["low"] = float(lows[-1])
    result["close"] = float(closes[-1])
    result["volume"] = float(volumes[-1])
    result["prev_close"] = float(closes[-2]) if len(closes) >= 2 else float("nan")

    # Cascade detection helpers (used on 1H data by CascadeReversalStrategy)
    # cascade_high: max high of last 4 completed candles (excludes current)
    if len(highs) >= 5:
        result["cascade_high_4h"] = float(np.max(highs[-5:-1]))
        result["cascade_low_4h"]  = float(np.min(lows[-5:-1]))
        cascade_h = result["cascade_high_4h"]
        result["drop_from_cascade_pct"] = (
            (cascade_h - closes[-1]) / cascade_h * 100 if cascade_h > 0 else 0.0
        )
        # Count of last 4 completed candles with lower wick > 50% of body
        wick_count = 0
        for j in range(-5, -1):
            body = abs(closes[j] - opens[j])
            lower_wick = min(opens[j], closes[j]) - lows[j]
            if body > 0 and lower_wick > 0.5 * body:
                wick_count += 1
        result["lower_wick_count_4h"] = float(wick_count)
    else:
        result["cascade_high_4h"] = float("nan")
        result["cascade_low_4h"]  = float("nan")
        result["drop_from_cascade_pct"] = float("nan")
        result["lower_wick_count_4h"] = float("nan")

    # Swing high/low of last 60 completed candles (TP reference for weekly momentum).
    # 60 × 4H = 240 hours ≈ 10 days — captures a meaningful prior swing.
    if len(highs) >= 61:
        result["prev_swing_high_60"] = float(np.max(highs[-61:-1]))
        result["prev_swing_low_60"]  = float(np.min(lows[-61:-1]))
    else:
        result["prev_swing_high_60"] = float("nan")
        result["prev_swing_low_60"]  = float("nan")

    return result
