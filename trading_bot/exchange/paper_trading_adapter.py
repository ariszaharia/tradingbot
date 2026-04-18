from __future__ import annotations
import asyncio
import random
import time
import uuid
from typing import Callable, Awaitable

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


# Binance spot standard fee
MAKER_FEE = 0.001
TAKER_FEE = 0.001


class PaperTradingAdapter(BaseExchange):
    """
    Faithful simulation of Binance spot for paper trading.

    Simulates:
      - Network latency: 50–200 ms per call
      - Slippage on MARKET orders: 0–0.1 % random
      - Fees: 0.1 % maker/taker (Binance standard)
      - Partial fills: LIMIT orders < 5 % chance of partial fill on first check
      - Stop-market triggers: monitored against live price feed
      - Real OHLCV data via CCXT (no real orders placed)
    """

    def __init__(self, config: dict, initial_capital: float) -> None:
        self._config = config
        self._symbol: str = config["trading"]["symbol"]
        self.log = AgentLogger("PAPER_ADAPTER")

        # Capital accounts
        quote_asset = self._symbol.split("/")[1]   # USDT
        base_asset = self._symbol.split("/")[0]    # BTC
        self._balances: dict[str, Balance] = {
            quote_asset: Balance(asset=quote_asset, free=initial_capital, locked=0.0),
            base_asset: Balance(asset=base_asset, free=0.0, locked=0.0),
        }

        self._orders: dict[str, ExchangeOrder] = {}
        self._current_price: float = 0.0
        self._price_callbacks: list[PriceFeedCallback] = []

        # CCXT exchange (data-only, no API keys needed for public endpoints)
        self._ccxt = ccxt.binance({"enableRateLimit": True})

        self._feed_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        await self._ccxt.load_markets()
        # Bootstrap price
        ticker = await self._ccxt.fetch_ticker(self._symbol)
        self._current_price = float(ticker["last"])
        self.log.info("Paper adapter connected", price=self._current_price)

        self._monitor_task = asyncio.create_task(self._order_monitor_loop())

    async def disconnect(self) -> None:
        if self._feed_task:
            self._feed_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()
        await self._ccxt.close()
        self.log.info("Paper adapter disconnected")

    # ------------------------------------------------------------------ #
    # Simulation helpers                                                   #
    # ------------------------------------------------------------------ #

    async def _simulate_latency(self) -> None:
        await asyncio.sleep(random.uniform(0.050, 0.200))

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        slippage_pct = random.uniform(0.0, 0.001)   # 0–0.1 %
        if side == OrderSide.BUY:
            return price * (1 + slippage_pct)
        return price * (1 - slippage_pct)

    def _calc_fee(self, notional: float, order_type: OrderType) -> float:
        rate = MAKER_FEE if order_type == OrderType.LIMIT else TAKER_FEE
        return notional * rate

    def _new_order_id(self) -> str:
        return f"PAPER-{uuid.uuid4().hex[:12].upper()}"

    # ------------------------------------------------------------------ #
    # Balance management (internal)                                        #
    # ------------------------------------------------------------------ #

    def _lock_quote(self, amount: float) -> bool:
        quote = self._symbol.split("/")[1]
        if self._balances[quote].free < amount:
            return False
        self._balances[quote].free -= amount
        self._balances[quote].locked += amount
        return True

    def _lock_base(self, amount: float) -> bool:
        base = self._symbol.split("/")[0]
        if self._balances[base].free < amount:
            return False
        self._balances[base].free -= amount
        self._balances[base].locked += amount
        return True

    def _settle_buy(self, order: ExchangeOrder) -> None:
        base = self._symbol.split("/")[0]
        quote = self._symbol.split("/")[1]
        cost = order.filled_quantity * order.executed_price + order.fees_paid
        # Release locked quote and debit actual cost
        self._balances[quote].locked -= cost
        # Credit base
        self._balances[base].free += order.filled_quantity

    def _settle_sell(self, order: ExchangeOrder) -> None:
        base = self._symbol.split("/")[0]
        quote = self._symbol.split("/")[1]
        proceeds = order.filled_quantity * order.executed_price - order.fees_paid
        self._balances[base].locked -= order.filled_quantity
        self._balances[quote].free += proceeds

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def get_balance(self) -> dict[str, Balance]:
        await self._simulate_latency()
        return dict(self._balances)

    async def get_current_price(self, symbol: str) -> tuple[float, float, float]:
        await self._simulate_latency()
        spread = self._current_price * 0.0001  # 0.01 % spread simulation
        return self._current_price, self._current_price - spread / 2, self._current_price + spread / 2

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
        await self._simulate_latency()

        now = int(time.time() * 1000)
        order = ExchangeOrder(
            order_id=self._new_order_id(),
            client_order_id=client_order_id or str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            timestamp_created=now,
            timestamp_updated=now,
        )

        # Idempotency: reject duplicate client_order_id
        if client_order_id:
            for existing in self._orders.values():
                if existing.client_order_id == client_order_id:
                    self.log.warning("Duplicate client_order_id rejected", cid=client_order_id)
                    return existing

        if order_type == OrderType.MARKET:
            exec_price = self._apply_slippage(self._current_price, side)
            notional = quantity * exec_price
            fee = self._calc_fee(notional, order_type)

            # Reserve funds
            if side == OrderSide.BUY:
                if not self._lock_quote(notional + fee):
                    order.status = ExchangeOrderStatus.REJECTED
                    order.error_message = "Insufficient quote balance"
                    self.log.warning("Order rejected: insufficient balance", side=side.value)
                    return order
            else:
                if not self._lock_base(quantity):
                    order.status = ExchangeOrderStatus.REJECTED
                    order.error_message = "Insufficient base balance"
                    return order

            order.executed_price = exec_price
            order.filled_quantity = quantity
            order.fees_paid = fee
            order.status = ExchangeOrderStatus.FILLED
            order.timestamp_updated = int(time.time() * 1000)

            if side == OrderSide.BUY:
                self._settle_buy(order)
            else:
                self._settle_sell(order)

            self.log.info(
                "MARKET order filled",
                side=side.value,
                qty=quantity,
                price=exec_price,
                fee=fee,
            )

        elif order_type == OrderType.LIMIT:
            if price is None:
                order.status = ExchangeOrderStatus.REJECTED
                order.error_message = "LIMIT order requires price"
                return order

            notional = quantity * price
            fee = self._calc_fee(notional, order_type)
            if side == OrderSide.BUY:
                if not self._lock_quote(notional + fee):
                    order.status = ExchangeOrderStatus.REJECTED
                    order.error_message = "Insufficient quote balance"
                    return order
            else:
                if not self._lock_base(quantity):
                    order.status = ExchangeOrderStatus.REJECTED
                    order.error_message = "Insufficient base balance"
                    return order

            order.status = ExchangeOrderStatus.OPEN
            self.log.info("LIMIT order placed", side=side.value, qty=quantity, price=price)

        elif order_type == OrderType.STOP_MARKET:
            if stop_price is None:
                order.status = ExchangeOrderStatus.REJECTED
                order.error_message = "STOP_MARKET requires stop_price"
                return order
            order.status = ExchangeOrderStatus.OPEN
            self.log.info("STOP_MARKET order placed", side=side.value, stop=stop_price)

        self._orders[order.order_id] = order
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._simulate_latency()
        order = self._orders.get(order_id)
        if not order or order.status not in (
            ExchangeOrderStatus.OPEN,
            ExchangeOrderStatus.PARTIALLY_FILLED,
        ):
            return False

        # Release locked funds
        if order.side == OrderSide.BUY:
            remaining = order.quantity - order.filled_quantity
            notional = remaining * (order.price or self._current_price)
            fee = self._calc_fee(notional, order.order_type)
            quote = self._symbol.split("/")[1]
            self._balances[quote].locked -= notional + fee
            self._balances[quote].free += notional + fee
        else:
            remaining = order.quantity - order.filled_quantity
            base = self._symbol.split("/")[0]
            self._balances[base].locked -= remaining
            self._balances[base].free += remaining

        order.status = ExchangeOrderStatus.CANCELED
        order.timestamp_updated = int(time.time() * 1000)
        self.log.info("Order cancelled", order_id=order_id)
        return True

    async def get_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        await self._simulate_latency()
        if order_id not in self._orders:
            raise KeyError(f"Order {order_id} not found")
        return self._orders[order_id]

    async def get_open_orders(self, symbol: str) -> list[ExchangeOrder]:
        await self._simulate_latency()
        return [
            o for o in self._orders.values()
            if o.symbol == symbol and o.status in (
                ExchangeOrderStatus.OPEN,
                ExchangeOrderStatus.PARTIALLY_FILLED,
            )
        ]

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: int | None = None,
    ) -> list[dict]:
        raw = await self._ccxt.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return [
            {
                "timestamp": c[0],
                "open": c[1],
                "high": c[2],
                "low": c[3],
                "close": c[4],
                "volume": c[5],
            }
            for c in raw
        ]

    async def subscribe_price_feed(
        self,
        symbol: str,
        callback: PriceFeedCallback,
    ) -> None:
        self._price_callbacks.append(callback)
        if self._feed_task is None or self._feed_task.done():
            self._feed_task = asyncio.create_task(self._price_feed_loop(symbol))

    # ------------------------------------------------------------------ #
    # Background loops                                                     #
    # ------------------------------------------------------------------ #

    async def _price_feed_loop(self, symbol: str) -> None:
        """
        Polls the real Binance ticker every 5 s to update _current_price,
        then fires all registered callbacks. Uses real market data but places
        no real orders.
        """
        while True:
            try:
                ticker = await self._ccxt.fetch_ticker(symbol)
                self._current_price = float(ticker["last"])
                for cb in self._price_callbacks:
                    await cb(symbol, self._current_price)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Price feed error", error=str(exc))
            await asyncio.sleep(5)

    async def _order_monitor_loop(self) -> None:
        """
        Checks open LIMIT and STOP_MARKET orders against the current price
        every second and fills/triggers them as appropriate.
        """
        while True:
            try:
                await asyncio.sleep(1)
                price = self._current_price
                if price == 0:
                    continue

                for order in list(self._orders.values()):
                    if order.status not in (
                        ExchangeOrderStatus.OPEN,
                        ExchangeOrderStatus.PARTIALLY_FILLED,
                    ):
                        continue

                    if order.order_type == OrderType.LIMIT:
                        await self._try_fill_limit(order, price)

                    elif order.order_type == OrderType.STOP_MARKET:
                        await self._try_trigger_stop(order, price)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Order monitor error", error=str(exc))

    async def _try_fill_limit(self, order: ExchangeOrder, price: float) -> None:
        assert order.price is not None
        triggered = (
            (order.side == OrderSide.BUY and price <= order.price) or
            (order.side == OrderSide.SELL and price >= order.price)
        )
        if not triggered:
            return

        # ~5 % chance of partial fill on first trigger (realistic simulation)
        fill_pct = random.choices([1.0, random.uniform(0.5, 0.9)], weights=[95, 5])[0]
        filled = (order.quantity - order.filled_quantity) * fill_pct
        exec_price = order.price  # LIMIT fills at exact price (no slippage)
        fee = self._calc_fee(filled * exec_price, order.order_type)

        order.filled_quantity += filled
        order.executed_price = exec_price
        order.fees_paid += fee

        if abs(order.filled_quantity - order.quantity) < 1e-9:
            order.status = ExchangeOrderStatus.FILLED
            if order.side == OrderSide.BUY:
                self._settle_buy(order)
            else:
                self._settle_sell(order)
            self.log.info("LIMIT order filled", order_id=order.order_id, price=exec_price)
        else:
            order.status = ExchangeOrderStatus.PARTIALLY_FILLED
            self.log.info(
                "LIMIT order partially filled",
                order_id=order.order_id,
                filled_pct=round(fill_pct * 100, 1),
            )

        order.timestamp_updated = int(time.time() * 1000)

    async def _try_trigger_stop(self, order: ExchangeOrder, price: float) -> None:
        assert order.stop_price is not None
        triggered = (
            (order.side == OrderSide.SELL and price <= order.stop_price) or
            (order.side == OrderSide.BUY and price >= order.stop_price)
        )
        if not triggered:
            return

        exec_price = self._apply_slippage(price, order.side)
        fee = self._calc_fee(order.quantity * exec_price, order.order_type)

        order.executed_price = exec_price
        order.filled_quantity = order.quantity
        order.fees_paid = fee
        order.status = ExchangeOrderStatus.FILLED
        order.timestamp_updated = int(time.time() * 1000)

        if order.side == OrderSide.SELL:
            self._settle_sell(order)
        else:
            self._settle_buy(order)

        self.log.info(
            "STOP_MARKET triggered",
            order_id=order.order_id,
            stop=order.stop_price,
            exec_price=exec_price,
        )

    # ------------------------------------------------------------------ #
    # Convenience                                                          #
    # ------------------------------------------------------------------ #

    def get_portfolio_value(self) -> float:
        """Approximate total value in quote currency at current price."""
        quote = self._symbol.split("/")[1]
        base = self._symbol.split("/")[0]
        return (
            self._balances[quote].total +
            self._balances[base].total * self._current_price
        )

    def set_price(self, price: float) -> None:
        """Inject price externally — used by backtests and unit tests."""
        self._current_price = price
