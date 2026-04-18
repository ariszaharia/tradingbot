from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"


class ExchangeOrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class ExchangeOrder:
    order_id: str
    client_order_id: str        # signal_id used for idempotency
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float             # requested
    filled_quantity: float = 0.0
    price: float | None = None  # for LIMIT orders
    stop_price: float | None = None
    status: ExchangeOrderStatus = ExchangeOrderStatus.OPEN
    executed_price: float = 0.0
    fees_paid: float = 0.0
    timestamp_created: int = 0
    timestamp_updated: int = 0
    error_message: str | None = None


@dataclass
class Balance:
    asset: str
    free: float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


# Callback type: receives the current price every tick
PriceFeedCallback = Callable[[str, float], Awaitable[None]]


class BaseExchange(ABC):
    """
    Defines the interface all exchange adapters must implement.
    The Execution Agent talks only to this interface — never to a concrete adapter.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection / load markets."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean shutdown."""

    @abstractmethod
    async def get_balance(self) -> dict[str, Balance]:
        """Return balances keyed by asset symbol."""

    @abstractmethod
    async def get_current_price(self, symbol: str) -> tuple[float, float, float]:
        """Return (last_price, bid, ask)."""

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        """Place a new order. Returns the order with its exchange-assigned ID."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""

    @abstractmethod
    async def get_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        """Fetch current state of an order."""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[ExchangeOrder]:
        """Return all open orders for a symbol."""

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: int | None = None,
    ) -> list[dict]:
        """
        Return OHLCV candles as list of dicts:
        [{"timestamp": ms, "open": f, "high": f, "low": f, "close": f, "volume": f}, ...]
        """

    @abstractmethod
    async def subscribe_price_feed(
        self,
        symbol: str,
        callback: PriceFeedCallback,
    ) -> None:
        """Subscribe to real-time price updates. Calls callback(symbol, price) on each tick."""
