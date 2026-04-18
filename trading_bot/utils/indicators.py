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

    # Latest candle raw values (convenient for strategy logic)
    result["open"] = float(opens[-1])
    result["high"] = float(highs[-1])
    result["low"] = float(lows[-1])
    result["close"] = float(closes[-1])
    result["volume"] = float(volumes[-1])

    return result
