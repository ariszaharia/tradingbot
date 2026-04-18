from __future__ import annotations
import time
from enum import Enum
from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class CloseReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"
    TRAILING_STOP = "TRAILING_STOP"


class ExecutionReport(BaseModel):
    signal_id: str = Field(description="Matches TradingSignal.signal_id for idempotency")
    order_id: str

    status: OrderStatus

    executed_price: float = Field(default=0.0)
    executed_quantity: float = Field(default=0.0, description="In asset units")

    # (executed_price - signal_price) / signal_price * 100
    slippage_pct: float = Field(default=0.0)
    fees_paid: float = Field(default=0.0, description="In quote currency (USDT)")

    stop_loss_order_id: str | None = None
    take_profit_order_id: str | None = None

    timestamp_open: int = Field(default_factory=lambda: int(time.time() * 1000))
    error_message: str | None = None


class PositionClose(BaseModel):
    signal_id: str
    order_id: str
    close_reason: CloseReason

    entry_price: float
    exit_price: float
    quantity: float

    pnl_gross: float = Field(description="Before fees")
    pnl_net: float = Field(description="After fees")
    pnl_pct: float = Field(description="% return on notional")

    fees_total: float

    duration_minutes: int

    timestamp_open: int
    timestamp_close: int = Field(default_factory=lambda: int(time.time() * 1000))
