from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class MarketRegime(BaseModel):
    regime: Literal["CONSOLIDATION", "TRENDING_UP", "TRENDING_DOWN", "VOLATILE"]
    confidence: float
    consolidation_range_high: float | None = None
    consolidation_range_low: float | None = None
    range_duration_days: int = 0
    bb_width_pct: float = 50.0   # percentile (0=tightest, 100=widest)
    adx_value: float = 0.0
    timestamp: int = 0
