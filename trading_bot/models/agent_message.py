from __future__ import annotations
import uuid
import time
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class AgentName(str, Enum):
    ORCHESTRATOR = "ORCHESTRATOR"
    MARKET_DATA = "MARKET_DATA"
    STRATEGY = "STRATEGY"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    JOURNAL = "JOURNAL"
    BROADCAST = "BROADCAST"


class MessageType(str, Enum):
    # Market Data → Orchestrator
    DATA_SNAPSHOT = "DATA_SNAPSHOT"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"

    # Orchestrator → Strategy
    REQUEST_SIGNAL = "REQUEST_SIGNAL"

    # Strategy → Orchestrator
    TRADING_SIGNAL = "TRADING_SIGNAL"

    # Orchestrator → Risk
    REQUEST_RISK_DECISION = "REQUEST_RISK_DECISION"

    # Risk → Orchestrator
    RISK_DECISION = "RISK_DECISION"

    # Orchestrator → Execution
    EXECUTE_ORDER = "EXECUTE_ORDER"

    # Execution → Orchestrator
    EXECUTION_REPORT = "EXECUTION_REPORT"
    POSITION_CLOSED = "POSITION_CLOSED"
    HIGH_SLIPPAGE = "HIGH_SLIPPAGE"

    # System control
    PAUSE = "PAUSE"
    STOP = "STOP"
    RESUME = "RESUME"
    STATUS = "STATUS"
    ACK = "ACK"

    # Journal
    DAILY_REPORT = "DAILY_REPORT"
    PERFORMANCE_ALERT = "PERFORMANCE_ALERT"


class AgentMessage(BaseModel):
    msg_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: AgentName
    recipient: AgentName
    msg_type: MessageType
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    requires_ack: bool = False
