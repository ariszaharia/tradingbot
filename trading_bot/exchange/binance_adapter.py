from __future__ import annotations
import time
import uuid

import ccxt.async_support as ccxt

from trading_bot.exchange.base_exchange import (
    BaseExchange,
    Balance,
    ExchangeOrder,
    ExchangeOrderStatus,
    OrderSide,
    OrderType,
    PriceFeedCallback,
)
from trading_bot.utils.logger import AgentLogger

_STATUS_MAP = {
    "open": ExchangeOrderStatus.OPEN,
    "partially_filled": ExchangeOrderStatus.PARTIALLY_FILLED,
    "closed": ExchangeOrderStatus.FILLED,
    "canceled": ExchangeOrderStatus.CANCELED,
    "rejected": ExchangeOrderStatus.REJECTED,
    "expired": ExchangeOrderStatus.EXPIRED,
}


class BinanceAdapter(BaseExchange):
    """Live Binance adapter. Only activated when config mode = 'live'."""

    def __init__(self, config: dict) -> None:
        creds = config.get("exchange_credentials", {})
        self._exchange = ccxt.binance({
            "apiKey": creds.get("api_key", ""),
            "secret": creds.get("api_secret", ""),
            "enableRateLimit": True,
        })
        self._symbol = config["trading"]["symbol"]
        self.log = AgentLogger("BINANCE_ADAPTER")
        self._price_callbacks: list[PriceFeedCallback] = []
        self._feed_task = None

    async def connect(self) -> None:
        await self._exchange.load_markets()
        self.log.info("Binance adapter connected")

    async def disconnect(self) -> None:
        if self._feed_task:
            self._feed_task.cancel()
        await self._exchange.close()
        self.log.info("Binance adapter disconnected")

    async def get_balance(self) -> dict[str, Balance]:
        raw = await self._exchange.fetch_balance()
        return {
            asset: Balance(asset=asset, free=info["free"], locked=info["used"])
            for asset, info in raw["total"].items()
            if info != 0 or asset in (self._symbol.split("/"))
        }

    async def get_current_price(self, symbol: str) -> tuple[float, float, float]:
        ticker = await self._exchange.fetch_ticker(symbol)
        return float(ticker["last"]), float(ticker["bid"]), float(ticker["ask"])

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
        ccxt_type = {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP_MARKET: "stop_market",
            OrderType.STOP_LIMIT: "stop_limit",
        }[order_type]
        params = {}
        if stop_price:
            params["stopPrice"] = stop_price
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        raw = await self._exchange.create_order(
            symbol,
            ccxt_type,
            side.value.lower(),
            quantity,
            price,
            params,
        )
        return self._parse_order(raw)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self._exchange.cancel_order(order_id, symbol)
            return True
        except Exception:
            return False

    async def get_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        raw = await self._exchange.fetch_order(order_id, symbol)
        return self._parse_order(raw)

    async def get_open_orders(self, symbol: str) -> list[ExchangeOrder]:
        raw = await self._exchange.fetch_open_orders(symbol)
        return [self._parse_order(o) for o in raw]

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: int | None = None,
    ) -> list[dict]:
        raw = await self._exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return [
            {"timestamp": c[0], "open": c[1], "high": c[2],
             "low": c[3], "close": c[4], "volume": c[5]}
            for c in raw
        ]

    async def subscribe_price_feed(self, symbol: str, callback: PriceFeedCallback) -> None:
        self._price_callbacks.append(callback)

    def _parse_order(self, raw: dict) -> ExchangeOrder:
        return ExchangeOrder(
            order_id=str(raw["id"]),
            client_order_id=str(raw.get("clientOrderId", "")),
            symbol=raw["symbol"],
            side=OrderSide.BUY if raw["side"] == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if raw["type"] == "market" else OrderType.LIMIT,
            quantity=float(raw["amount"]),
            filled_quantity=float(raw.get("filled", 0)),
            price=float(raw["price"]) if raw.get("price") else None,
            status=_STATUS_MAP.get(raw["status"], ExchangeOrderStatus.OPEN),
            executed_price=float(raw.get("average") or 0),
            fees_paid=float(raw.get("fee", {}).get("cost", 0)),
            timestamp_created=int(raw.get("timestamp") or time.time() * 1000),
            timestamp_updated=int(raw.get("lastTradeTimestamp") or time.time() * 1000),
        )
