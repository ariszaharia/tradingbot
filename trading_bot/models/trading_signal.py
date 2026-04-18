from __future__ import annotations
import time
from enum import Enum
from pydantic import BaseModel, Field, field_validator


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"    # no actionable condition met
    EXIT = "EXIT"    # close existing position


class TradingSignal(BaseModel):
    signal_id: str = Field(description="Unique ID; Execution Agent uses this for idempotency")
    direction: Direction
    strategy_name: str

    # 0.0 = no conviction, 1.0 = all conditions met + HTF confirmation
    confidence_score: float = Field(ge=0.0, le=1.0)

    entry_price: float = Field(gt=0)

    # ATR-based: entry ± 1.5 * ATR(14)
    suggested_stop_loss: float = Field(gt=0)

    # ATR-based: entry ± 3.0 * ATR(14)  → R:R = 2:1
    suggested_take_profit: float = Field(gt=0)

    timeframe: str
    reasoning: list[str] = Field(default_factory=list, description="Human-readable list of conditions met")

    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("suggested_stop_loss")
    @classmethod
    def stop_must_differ_from_entry(cls, v: float, info) -> float:
        entry = info.data.get("entry_price")
        if entry is not None and abs(v - entry) < 1e-8:
            raise ValueError("stop_loss must differ from entry_price")
        return v
