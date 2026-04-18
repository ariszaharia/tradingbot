from __future__ import annotations
import time
from pydantic import BaseModel, Field


class RiskDecision(BaseModel):
    signal_id: str = Field(description="Matches TradingSignal.signal_id")

    approved: bool

    # Populated when approved=False
    rejection_reason: str | None = None

    # Position sizing (populated when approved=True)
    position_size: float = Field(default=0.0, description="In asset units (e.g. BTC)")
    position_size_usd: float = Field(default=0.0, description="Notional USD value")

    # Possibly adjusted from strategy suggestion to meet min R:R
    final_stop_loss: float = Field(default=0.0)
    final_take_profit: float = Field(default=0.0)

    risk_pct_of_capital: float = Field(default=0.0, description="Actual % of capital at risk")
    reward_risk_ratio: float = Field(default=0.0)

    # Which rules were checked and their outcomes (always logged, even on approval)
    rule_checks: dict[str, str] = Field(
        default_factory=dict,
        description="rule_name → 'PASS' | 'FAIL: <reason>'"
    )

    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
