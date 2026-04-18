from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.exchange.base_exchange import (
    BaseExchange,
    ExchangeOrder,
    ExchangeOrderStatus,
    OrderSide,
    OrderType,
)
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.execution_report import (
    CloseReason,
    ExecutionReport,
    OrderStatus,
    PositionClose,
)
from trading_bot.models.trading_signal import Direction


# ── Internal position tracker ─────────────────────────────────────────────────

@dataclass
class _PositionTracker:
    signal_id: str
    direction: str               # "LONG" | "SHORT"
    entry_order_id: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    sl_order_id: str | None = None
    tp_order_id: str | None = None
    trailing_active: bool = False
    breakeven_moved: bool = False
    timestamp_open: int = field(default_factory=lambda: int(time.time() * 1000))
    fees_paid: float = 0.0
    atr: float = 0.0             # used for trailing stop trigger


class ExecutionAgent(BaseAgent):
    """
    The only agent that interacts with the exchange for placing orders.

    Entry flow:
      1. MARKET (or LIMIT) main order
      2. After fill confirmed → STOP_MARKET stop-loss order
      3. LIMIT take-profit order
      4. Partial-fill watchdog: if < 90% filled in 60s → cancel remainder

    Position monitoring (every 30s):
      - Polls order status for each tracked position
      - Detects SL or TP hit and emits POSITION_CLOSED
      - Checks trailing stop: if price moved > 1×ATR favourably, move SL to breakeven

    Retry: up to 3 attempts with backoff [1s, 2s, 4s] on network errors.
    Idempotency: `client_order_id` = signal_id prevents duplicate orders.
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
        exchange: BaseExchange,
    ) -> None:
        super().__init__(AgentName.EXECUTION, bus, config)
        self._exchange = exchange
        exec_cfg = config.get("execution", {})
        self._order_type_str: str = exec_cfg.get("order_type", "market").upper()
        self._max_slippage_pct: float = exec_cfg.get("max_slippage_pct", 0.15)
        self._retry_attempts: int = exec_cfg.get("retry_attempts", 3)
        self._retry_backoff: list[float] = exec_cfg.get("retry_backoff_seconds", [1, 2, 4])
        self._partial_timeout: float = exec_cfg.get("partial_fill_timeout_seconds", 60)
        self._partial_min_pct: float = exec_cfg.get("partial_fill_min_pct", 90.0)
        self._trailing_enabled: bool = exec_cfg.get("trailing_stop_enabled", True)
        self._trailing_trigger_atr: float = exec_cfg.get("trailing_stop_trigger_atr", 1.0)
        self._monitor_interval: float = exec_cfg.get("position_monitor_interval_seconds", 30)
        self._symbol: str = config["trading"]["symbol"]

        # signal_id → tracker
        self._positions: dict[str, _PositionTracker] = {}
        self._monitor_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def _on_start(self) -> None:
        await self._exchange.connect()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.log.info("ExecutionAgent started")

    async def _on_stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        await self._exchange.disconnect()

    # ------------------------------------------------------------------ #
    # Message handler                                                      #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        if msg.msg_type != MessageType.EXECUTE_ORDER:
            return

        direction = msg.payload.get("direction", "")
        signal_data = msg.payload.get("signal", {})
        signal_id = signal_data.get("signal_id", str(uuid.uuid4()))

        if direction == "EXIT":
            position_data = msg.payload.get("position")
            if position_data:
                await self._close_position_market(
                    signal_id=position_data.get("signal_id", signal_id),
                    reason=CloseReason.MANUAL,
                )
            return

        decision = msg.payload.get("decision", {})
        stop_loss = msg.payload.get("stop_loss", signal_data.get("suggested_stop_loss", 0.0))
        take_profit = msg.payload.get("take_profit", signal_data.get("suggested_take_profit", 0.0))
        entry_price = signal_data.get("entry_price", 0.0)
        position_size = decision.get("position_size", 0.0)
        atr = signal_data.get("atr", entry_price * 0.005)

        await self._open_position(
            signal_id=signal_id,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=position_size,
            atr=float(atr),
        )

    # ------------------------------------------------------------------ #
    # Open position                                                        #
    # ------------------------------------------------------------------ #

    async def _open_position(
        self,
        signal_id: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: float,
        atr: float,
    ) -> None:
        if signal_id in self._positions:
            self.log.warning("Duplicate signal_id — order skipped", signal_id=signal_id)
            return

        side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
        order_type = (
            OrderType.MARKET if self._order_type_str == "MARKET" else OrderType.LIMIT
        )
        limit_price = entry_price if order_type == OrderType.LIMIT else None

        # ── Place main order with retry ───────────────────────────────────
        main_order = await self._place_with_retry(
            symbol=self._symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=limit_price,
            client_order_id=signal_id,
        )

        if main_order is None:
            await self._emit_report(signal_id, None, OrderStatus.ERROR, "All retry attempts failed")
            return

        if main_order.status == ExchangeOrderStatus.REJECTED:
            await self._emit_report(
                signal_id, main_order, OrderStatus.REJECTED, main_order.error_message
            )
            return

        # ── Partial fill watchdog for LIMIT orders ────────────────────────
        if order_type == OrderType.LIMIT:
            main_order = await self._wait_for_fill(main_order)

        if main_order.filled_quantity < 1e-9:
            await self._emit_report(signal_id, main_order, OrderStatus.ERROR, "Zero fill")
            return

        actual_qty = main_order.filled_quantity
        exec_price = main_order.executed_price

        # ── Slippage check ────────────────────────────────────────────────
        if entry_price > 0:
            slippage_pct = abs(exec_price - entry_price) / entry_price * 100
        else:
            slippage_pct = 0.0

        if slippage_pct > self._max_slippage_pct:
            self.log.warning(
                "High slippage", slippage_pct=round(slippage_pct, 4), signal_id=signal_id
            )
            await self._send(AgentMessage(
                sender=AgentName.EXECUTION,
                recipient=AgentName.ORCHESTRATOR,
                msg_type=MessageType.HIGH_SLIPPAGE,
                payload={"signal_id": signal_id, "slippage_pct": slippage_pct},
            ))

        # ── Place SL order (stop-market) ──────────────────────────────────
        sl_side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
        sl_order = await self._place_with_retry(
            symbol=self._symbol,
            side=sl_side,
            order_type=OrderType.STOP_MARKET,
            quantity=actual_qty,
            stop_price=stop_loss,
            client_order_id=f"{signal_id}-SL",
        )

        # ── Place TP order (limit) ────────────────────────────────────────
        tp_side = sl_side  # same side as SL (closing the position)
        tp_order = await self._place_with_retry(
            symbol=self._symbol,
            side=tp_side,
            order_type=OrderType.LIMIT,
            quantity=actual_qty,
            price=take_profit,
            client_order_id=f"{signal_id}-TP",
        )

        # ── Register position tracker ─────────────────────────────────────
        tracker = _PositionTracker(
            signal_id=signal_id,
            direction=direction,
            entry_order_id=main_order.order_id,
            entry_price=exec_price,
            quantity=actual_qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            sl_order_id=sl_order.order_id if sl_order else None,
            tp_order_id=tp_order.order_id if tp_order else None,
            fees_paid=main_order.fees_paid,
            atr=atr,
        )
        self._positions[signal_id] = tracker

        # ── Emit ExecutionReport ──────────────────────────────────────────
        report = ExecutionReport(
            signal_id=signal_id,
            order_id=main_order.order_id,
            status=OrderStatus.FILLED if main_order.status == ExchangeOrderStatus.FILLED
                   else OrderStatus.PARTIAL,
            executed_price=exec_price,
            executed_quantity=actual_qty,
            slippage_pct=round(slippage_pct, 6),
            fees_paid=main_order.fees_paid,
            stop_loss_order_id=tracker.sl_order_id,
            take_profit_order_id=tracker.tp_order_id,
        )
        await self._send(AgentMessage(
            sender=AgentName.EXECUTION,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.EXECUTION_REPORT,
            payload={
                **report.model_dump(),
                "direction": direction,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            },
        ))
        await self._send(AgentMessage(
            sender=AgentName.EXECUTION,
            recipient=AgentName.JOURNAL,
            msg_type=MessageType.EXECUTION_REPORT,
            payload=report.model_dump(),
        ))

        self.log.info(
            "Position opened",
            signal_id=signal_id,
            direction=direction,
            price=round(exec_price, 2),
            qty=round(actual_qty, 6),
            slippage_pct=round(slippage_pct, 4),
        )

    # ------------------------------------------------------------------ #
    # Close position (market)                                             #
    # ------------------------------------------------------------------ #

    async def _close_position_market(
        self,
        signal_id: str,
        reason: CloseReason,
        exit_price: float = 0.0,
    ) -> None:
        tracker = self._positions.get(signal_id)
        if tracker is None:
            self.log.warning("close_position: unknown signal_id", signal_id=signal_id)
            return

        # Cancel any remaining SL / TP orders
        for oid in (tracker.sl_order_id, tracker.tp_order_id):
            if oid:
                try:
                    await self._exchange.cancel_order(oid, self._symbol)
                except Exception:
                    pass

        # If exit_price already known (SL/TP fill), skip market order
        if exit_price == 0.0:
            close_side = OrderSide.SELL if tracker.direction == "LONG" else OrderSide.BUY
            close_order = await self._place_with_retry(
                symbol=self._symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                quantity=tracker.quantity,
                client_order_id=f"{signal_id}-CLOSE",
            )
            if close_order and close_order.executed_price > 0:
                exit_price = close_order.executed_price
                tracker.fees_paid += close_order.fees_paid
            else:
                # Fallback to last known price
                price_tuple = await self._exchange.get_current_price(self._symbol)
                exit_price = price_tuple[0]

        # ── PnL calculation ───────────────────────────────────────────────
        if tracker.direction == "LONG":
            pnl_gross = (exit_price - tracker.entry_price) * tracker.quantity
        else:
            pnl_gross = (tracker.entry_price - exit_price) * tracker.quantity
        pnl_net = pnl_gross - tracker.fees_paid
        pnl_pct = pnl_gross / (tracker.entry_price * tracker.quantity) * 100 if tracker.entry_price else 0.0
        duration = int((int(time.time() * 1000) - tracker.timestamp_open) / 60_000)

        close = PositionClose(
            signal_id=signal_id,
            order_id=tracker.entry_order_id,
            close_reason=reason,
            entry_price=tracker.entry_price,
            exit_price=exit_price,
            quantity=tracker.quantity,
            pnl_gross=round(pnl_gross, 4),
            pnl_net=round(pnl_net, 4),
            pnl_pct=round(pnl_pct, 4),
            fees_total=round(tracker.fees_paid, 4),
            duration_minutes=duration,
            timestamp_open=tracker.timestamp_open,
        )

        del self._positions[signal_id]

        for recipient in (AgentName.ORCHESTRATOR, AgentName.JOURNAL):
            await self._send(AgentMessage(
                sender=AgentName.EXECUTION,
                recipient=recipient,
                msg_type=MessageType.POSITION_CLOSED,
                payload=close.model_dump(),
            ))

        self.log.info(
            "Position closed",
            signal_id=signal_id,
            reason=reason.value,
            pnl_net=round(pnl_net, 2),
            duration_minutes=duration,
        )

    # ------------------------------------------------------------------ #
    # Position monitor loop                                                #
    # ------------------------------------------------------------------ #

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._monitor_interval)
                for signal_id, tracker in list(self._positions.items()):
                    await self._check_position(signal_id, tracker)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Monitor loop error", error=str(exc))

    async def _check_position(self, signal_id: str, tracker: _PositionTracker) -> None:
        # Check SL order
        if tracker.sl_order_id:
            try:
                sl_order = await self._exchange.get_order(tracker.sl_order_id, self._symbol)
                if sl_order.status == ExchangeOrderStatus.FILLED:
                    await self._close_position_market(
                        signal_id, CloseReason.STOP_LOSS, sl_order.executed_price
                    )
                    return
            except Exception as exc:
                self.log.error("SL order check failed", error=str(exc))

        # Check TP order
        if tracker.tp_order_id:
            try:
                tp_order = await self._exchange.get_order(tracker.tp_order_id, self._symbol)
                if tp_order.status == ExchangeOrderStatus.FILLED:
                    await self._close_position_market(
                        signal_id, CloseReason.TAKE_PROFIT, tp_order.executed_price
                    )
                    return
            except Exception as exc:
                self.log.error("TP order check failed", error=str(exc))

        # Trailing stop check
        if self._trailing_enabled and not tracker.breakeven_moved:
            await self._check_trailing_stop(signal_id, tracker)

    async def _check_trailing_stop(self, signal_id: str, tracker: _PositionTracker) -> None:
        try:
            price, _, _ = await self._exchange.get_current_price(self._symbol)
        except Exception:
            return

        trigger = tracker.atr * self._trailing_trigger_atr
        if tracker.direction == "LONG":
            moved = price - tracker.entry_price
        else:
            moved = tracker.entry_price - price

        if moved < trigger:
            return

        # Move SL to breakeven
        new_sl = tracker.entry_price
        self.log.info(
            "Trailing stop: moving SL to breakeven",
            signal_id=signal_id,
            entry=tracker.entry_price,
            price=price,
            moved=round(moved, 2),
        )

        # Cancel old SL and place new one at breakeven
        if tracker.sl_order_id:
            try:
                await self._exchange.cancel_order(tracker.sl_order_id, self._symbol)
            except Exception:
                pass

        sl_side = OrderSide.SELL if tracker.direction == "LONG" else OrderSide.BUY
        new_sl_order = await self._place_with_retry(
            symbol=self._symbol,
            side=sl_side,
            order_type=OrderType.STOP_MARKET,
            quantity=tracker.quantity,
            stop_price=new_sl,
            client_order_id=f"{signal_id}-TSL",
        )
        if new_sl_order:
            tracker.sl_order_id = new_sl_order.order_id
            tracker.stop_loss = new_sl
            tracker.breakeven_moved = True
            tracker.trailing_active = True

            await self._send(AgentMessage(
                sender=AgentName.EXECUTION,
                recipient=AgentName.JOURNAL,
                msg_type=MessageType.EXECUTION_REPORT,
                payload={
                    "event": "trailing_stop_moved",
                    "signal_id": signal_id,
                    "new_sl": new_sl,
                    "price": price,
                },
            ))

    # ------------------------------------------------------------------ #
    # Partial fill watchdog                                                #
    # ------------------------------------------------------------------ #

    async def _wait_for_fill(self, order: ExchangeOrder) -> ExchangeOrder:
        deadline = time.time() + self._partial_timeout
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                order = await self._exchange.get_order(order.order_id, self._symbol)
            except Exception:
                continue
            if order.status == ExchangeOrderStatus.FILLED:
                return order
            if order.status in (ExchangeOrderStatus.CANCELED, ExchangeOrderStatus.REJECTED):
                return order

        # Timeout: check fill percentage
        fill_pct = order.filled_quantity / order.quantity * 100 if order.quantity else 0
        if fill_pct < self._partial_min_pct:
            self.log.warning(
                "Partial fill below threshold — cancelling remainder",
                fill_pct=round(fill_pct, 1),
                order_id=order.order_id,
            )
            await self._exchange.cancel_order(order.order_id, self._symbol)
            order = await self._exchange.get_order(order.order_id, self._symbol)

        return order

    # ------------------------------------------------------------------ #
    # Retry wrapper                                                        #
    # ------------------------------------------------------------------ #

    async def _place_with_retry(self, **kwargs) -> ExchangeOrder | None:
        last_exc: Exception | None = None
        backoff = list(self._retry_backoff)

        for attempt in range(self._retry_attempts):
            try:
                order = await self._exchange.place_order(**kwargs)
                return order
            except Exception as exc:
                last_exc = exc
                delay = backoff[attempt] if attempt < len(backoff) else backoff[-1]
                self.log.warning(
                    "Order attempt failed",
                    attempt=attempt + 1,
                    error=str(exc),
                    retry_in=delay,
                )
                await asyncio.sleep(delay)

        self.log.error(
            "All retry attempts exhausted",
            attempts=self._retry_attempts,
            error=str(last_exc),
        )
        return None

    # ------------------------------------------------------------------ #
    # Emit helper                                                          #
    # ------------------------------------------------------------------ #

    async def _emit_report(
        self,
        signal_id: str,
        order: ExchangeOrder | None,
        status: OrderStatus,
        error: str | None,
    ) -> None:
        report = ExecutionReport(
            signal_id=signal_id,
            order_id=order.order_id if order else "NONE",
            status=status,
            error_message=error,
        )
        for recipient in (AgentName.ORCHESTRATOR, AgentName.JOURNAL):
            await self._send(AgentMessage(
                sender=AgentName.EXECUTION,
                recipient=recipient,
                msg_type=MessageType.EXECUTION_REPORT,
                payload=report.model_dump(),
            ))
        self.log.error("Execution failed", signal_id=signal_id, status=status.value, error=error)
