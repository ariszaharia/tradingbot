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
        """4H EMA21 > EMA50 → bullish higher-timeframe bias."""
        ema21 = htf.get("ema_21", float("nan"))
        ema50 = htf.get("ema_50", float("nan"))
        return ema21 > ema50

    def _htf_is_bearish(self, htf: dict[str, float]) -> bool:
        ema21 = htf.get("ema_21", float("nan"))
        ema50 = htf.get("ema_50", float("nan"))
        return ema21 < ema50
