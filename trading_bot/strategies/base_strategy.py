from __future__ import annotations
from abc import ABC, abstractmethod

from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import TradingSignal


class BaseStrategy(ABC):
    """
    All strategies receive a DataSnapshot and return a TradingSignal.
    They have no knowledge of risk, execution, or account state.
    """

    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        """
        Pure synchronous evaluation — no I/O, no side effects.
        Must always return a TradingSignal (FLAT when nothing actionable).
        """
        ...

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _atr_stop_and_tp(
        self,
        entry: float,
        direction: str,
        atr: float,
        sl_mult: float = 1.5,
        tp_mult: float = 3.0,
    ) -> tuple[float, float]:
        """Returns (stop_loss, take_profit) using ATR multiples."""
        if direction == "LONG":
            sl = entry - sl_mult * atr
            tp = entry + tp_mult * atr
        else:
            sl = entry + sl_mult * atr
            tp = entry - tp_mult * atr
        return max(sl, 1e-6), max(tp, 1e-6)

    def _htf_is_bullish(self, htf: dict[str, float]) -> bool:
        """Three-layer 4H bullish confirmation:
        EMA21 > EMA50 (near-term), EMA50 > EMA200 (medium-term), ADX > 20 (active trend)."""
        ema21  = htf.get("ema_21",  float("nan"))
        ema50  = htf.get("ema_50",  float("nan"))
        ema200 = htf.get("ema_200", float("nan"))
        adx14  = htf.get("adx_14",  float("nan"))
        ema_near = ema21 > ema50
        ema_mid  = ema50 > ema200 if ema200 == ema200 else True  # pass if unavailable
        adx_ok   = adx14 > 20    if adx14  == adx14  else True
        return ema_near and ema_mid and adx_ok

    def _htf_is_bearish(self, htf: dict[str, float]) -> bool:
        """Three-layer 4H bearish confirmation:
        EMA21 < EMA50, EMA50 < EMA200, ADX > 20."""
        ema21  = htf.get("ema_21",  float("nan"))
        ema50  = htf.get("ema_50",  float("nan"))
        ema200 = htf.get("ema_200", float("nan"))
        adx14  = htf.get("adx_14",  float("nan"))
        ema_near = ema21 < ema50
        ema_mid  = ema50 < ema200 if ema200 == ema200 else True
        adx_ok   = adx14 > 20    if adx14  == adx14  else True
        return ema_near and ema_mid and adx_ok

    def _is_trending(self, indicators: dict[str, float], threshold: float = 20.0) -> bool:
        """True when ADX > threshold — directional/trending market."""
        v = indicators.get("adx_14", float("nan"))
        return v > threshold if v == v else False

    def _is_ranging(self, indicators: dict[str, float], threshold: float = 25.0) -> bool:
        """True when ADX < threshold — choppy/ranging market."""
        v = indicators.get("adx_14", float("nan"))
        return v < threshold if v == v else False

    def _is_high_volatility(self, indicators: dict[str, float], threshold: float = 75.0) -> bool:
        """True when ATR percentile > threshold — elevated volatility regime."""
        v = indicators.get("atr_pct_50", float("nan"))
        return v > threshold if v == v else False
