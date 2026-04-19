from __future__ import annotations
import time
from typing import Any
from pydantic import BaseModel, Field

from trading_bot.models.market_regime import MarketRegime


class DataSnapshot(BaseModel):
    symbol: str
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))

    # Top-of-book
    price: float          # last trade price
    bid: float
    ask: float
    spread_pct: float     # (ask - bid) / mid * 100

    # OHLCV DataFrames are passed as serialised dict of records keyed by timeframe.
    # Structure: {"1h": [{"open":..,"high":..,"low":..,"close":..,"volume":..,"timestamp":..}, ...]}
    # Using Any here because pandas DataFrames are not JSON-serialisable; agents
    # that need a DataFrame reconstruct it from this dict.
    ohlcv: dict[str, Any] = Field(
        description="Keyed by timeframe string, value is list-of-dicts OHLCV records"
    )

    # All pre-calculated indicator values, flat dict.
    # Keys: "ema_9", "ema_21", "rsi_14", "atr_14", "macd_line", "macd_signal",
    # "macd_hist", "bb_upper", "bb_middle", "bb_lower", "volume_sma_20",
    # "cascade_high_4h", "drop_from_cascade_pct", "lower_wick_count_4h",
    # "prev_swing_high_20", "prev_swing_low_20"
    # Values are for the most recently CLOSED candle on the primary (1H) timeframe.
    indicators: dict[str, float] = Field(default_factory=dict)

    # 4H indicators (confirmation timeframe)
    htf_indicators: dict[str, float] = Field(default_factory=dict)

    # Daily indicators (regime detection, support levels)
    daily_indicators: dict[str, float] = Field(default_factory=dict)

    # Weekly indicators (weekly trend for Strategy 3)
    weekly_indicators: dict[str, float] = Field(default_factory=dict)

    # Daily regime detection result
    regime: MarketRegime | None = None

    anomaly_flag: bool = False
    anomaly_reason: str | None = None

    # Set by Orchestrator when routing to StrategyAgent so EXIT conditions
    # are only evaluated against the relevant open position direction.
    # None means no open position — EXIT checks are skipped entirely.
    current_position_direction: str | None = None  # "LONG" | "SHORT" | None

    # How many primary-timeframe candles the current position has been open.
    # Used by strategies for time-based exit logic.
    candles_in_position: int = 0
