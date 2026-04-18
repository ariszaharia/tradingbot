from __future__ import annotations
import time
from enum import Enum
from pydantic import BaseModel, Field


class SystemMode(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"    # circuit breaker tripped or manual pause
    STOPPED = "STOPPED"  # clean shutdown


class OpenPosition(BaseModel):
    signal_id: str
    order_id: str
    symbol: str
    direction: str        # "LONG" | "SHORT"
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    trailing_stop_active: bool = False
    trailing_stop_price: float | None = None
    timestamp_open: int = Field(default_factory=lambda: int(time.time() * 1000))


class SystemState(BaseModel):
    mode: SystemMode = SystemMode.RUNNING

    # Capital tracking
    initial_capital: float
    current_capital: float
    daily_pnl: float = 0.0
    total_pnl: float = 0.0

    # Derived risk metrics
    daily_drawdown_pct: float = 0.0
    total_drawdown_pct: float = 0.0
    risk_budget_remaining_pct: float = Field(
        default=3.0,
        description="Remaining daily drawdown budget in %"
    )

    # Positions
    open_positions: list[OpenPosition] = Field(default_factory=list)

    # Trade history summary
    total_trades: int = 0
    winning_trades: int = 0
    consecutive_losses: int = 0
    cooldown_candles_remaining: int = 0

    # Last signal reference for display/logging
    last_signal_id: str | None = None
    last_signal_direction: str | None = None

    # Error log (rolling, last N entries)
    errors: list[str] = Field(default_factory=list)

    updated_at: int = Field(default_factory=lambda: int(time.time() * 1000))

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
