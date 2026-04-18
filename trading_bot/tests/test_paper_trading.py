"""
Integration tests for ExecutionAgent + PaperTradingAdapter.

Uses set_price() to control market price — no network access required.
"""
import asyncio
import pytest

from trading_bot.agents.execution_agent import ExecutionAgent
from trading_bot.exchange.paper_trading_adapter import PaperTradingAdapter
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.execution_report import CloseReason, PositionClose


# ─────────────────────────────────────────────────────────────────────────────
# Config / helpers
# ─────────────────────────────────────────────────────────────────────────────

_CFG = {
    "trading": {
        "symbol": "BTC/USDT", "mode": "paper",
        "primary_timeframe": "1h", "confirmation_timeframe": "4h",
    },
    "capital": {"initial_capital": 10_000.0},
    "execution": {
        "order_type": "market",
        "max_slippage_pct": 0.15,
        "retry_attempts": 2,
        "retry_backoff_seconds": [0.01, 0.02],
        "partial_fill_timeout_seconds": 0.3,
        "partial_fill_min_pct": 90.0,
        "trailing_stop_enabled": True,
        "trailing_stop_trigger_atr": 1.0,
        "position_monitor_interval_seconds": 9999,  # disable auto-monitoring in tests
    },
}


def _bus():
    return {name: asyncio.Queue() for name in AgentName}


def _exec_msg(
    signal_id: str = "sig-001",
    direction: str = "LONG",
    entry: float = 50_000.0,
    sl: float = 47_000.0,
    tp: float = 56_000.0,
    qty: float = 0.05,
    atr: float = 1_000.0,
) -> AgentMessage:
    return AgentMessage(
        sender=AgentName.ORCHESTRATOR,
        recipient=AgentName.EXECUTION,
        msg_type=MessageType.EXECUTE_ORDER,
        payload={
            "direction": direction,
            "stop_loss": sl,
            "take_profit": tp,
            "signal": {
                "signal_id": signal_id,
                "direction": direction,
                "strategy_name": "test",
                "confidence_score": 0.8,
                "entry_price": entry,
                "suggested_stop_loss": sl,
                "suggested_take_profit": tp,
                "timeframe": "1h",
                "reasoning": ["test"],
                "timestamp": 0,
                "atr": atr,
            },
            "decision": {
                "signal_id": signal_id,
                "approved": True,
                "position_size": qty,
                "position_size_usd": qty * entry,
                "final_stop_loss": sl,
                "final_take_profit": tp,
                "risk_pct_of_capital": 1.0,
                "reward_risk_ratio": 2.0,
                "rule_checks": {},
                "rejection_reason": None,
                "timestamp": 0,
            },
        },
    )


async def _make_agent(capital: float = 10_000.0) -> tuple[ExecutionAgent, PaperTradingAdapter, dict]:
    """Build agent with patched adapter (no CCXT network calls)."""
    bus = _bus()
    adapter = PaperTradingAdapter(_CFG, capital)
    adapter.set_price(50_000.0)

    async def _noop_connect():
        adapter._current_price = 50_000.0

    async def _noop_disconnect():
        pass

    adapter.connect = _noop_connect        # type: ignore[method-assign]
    adapter.disconnect = _noop_disconnect  # type: ignore[method-assign]

    agent = ExecutionAgent(bus, _CFG, adapter)
    agent._monitor_task = None   # prevent background monitor from firing
    await agent._on_start()
    return agent, adapter, bus


async def _get(q: asyncio.Queue, timeout: float = 3.0) -> AgentMessage:
    return await asyncio.wait_for(q.get(), timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

async def test_long_position_opens_and_emits_report():
    agent, adapter, bus = await _make_agent()

    await agent.handle_message(_exec_msg())

    msg = await _get(bus[AgentName.ORCHESTRATOR])
    assert msg.msg_type == MessageType.EXECUTION_REPORT
    assert msg.payload["status"] == "FILLED"
    assert msg.payload["executed_price"] > 0
    assert msg.payload["executed_quantity"] == pytest.approx(0.05, rel=0.01)

    await agent._on_stop()


async def test_sl_and_tp_orders_placed_after_entry():
    agent, adapter, bus = await _make_agent()
    await agent.handle_message(_exec_msg("sig-orders"))
    await _get(bus[AgentName.ORCHESTRATOR])   # drain EXECUTION_REPORT
    await _get(bus[AgentName.JOURNAL])

    tracker = agent._positions["sig-orders"]
    assert tracker.sl_order_id is not None, "SL order must be placed"
    assert tracker.tp_order_id is not None, "TP order must be placed"

    sl_order = adapter._orders.get(tracker.sl_order_id)
    tp_order = adapter._orders.get(tracker.tp_order_id)
    assert sl_order is not None
    assert tp_order is not None
    assert sl_order.stop_price == pytest.approx(47_000.0)
    assert tp_order.price == pytest.approx(56_000.0)

    await agent._on_stop()


async def test_position_tracked_after_open():
    agent, _, bus = await _make_agent()
    await agent.handle_message(_exec_msg("sig-track"))
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    assert "sig-track" in agent._positions
    t = agent._positions["sig-track"]
    assert t.direction == "LONG"
    assert t.quantity == pytest.approx(0.05, rel=0.01)

    await agent._on_stop()


async def test_idempotency_blocks_duplicate_signal():
    agent, _, bus = await _make_agent()
    msg = _exec_msg("dup-sig")

    await agent.handle_message(msg)
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    # Second call — should be silently dropped
    await agent.handle_message(msg)
    assert bus[AgentName.ORCHESTRATOR].empty(), "Duplicate must not emit a second report"

    await agent._on_stop()


async def test_stop_loss_triggers_position_close():
    agent, adapter, bus = await _make_agent()
    agent._trailing_enabled = False

    await agent.handle_message(_exec_msg("sl-test", sl=47_000.0))
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    tracker = agent._positions["sl-test"]

    # Manually trigger the SL stop-market order at 46_500
    sl_order = adapter._orders[tracker.sl_order_id]
    adapter.set_price(46_500.0)
    await adapter._try_trigger_stop(sl_order, 46_500.0)
    assert sl_order.status.value == "FILLED"

    # Let execution agent detect the fill
    await agent._check_position("sl-test", tracker)

    msg = await _get(bus[AgentName.ORCHESTRATOR])
    assert msg.msg_type == MessageType.POSITION_CLOSED
    close = PositionClose(**msg.payload)
    assert close.close_reason == CloseReason.STOP_LOSS
    assert close.pnl_gross < 0   # price dropped → loss

    await agent._on_stop()


async def test_take_profit_triggers_position_close():
    agent, adapter, bus = await _make_agent()
    agent._trailing_enabled = False

    await agent.handle_message(_exec_msg("tp-test", tp=56_000.0))
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    tracker = agent._positions["tp-test"]
    tp_order = adapter._orders[tracker.tp_order_id]

    adapter.set_price(56_500.0)
    await adapter._try_fill_limit(tp_order, 56_500.0)
    assert tp_order.status.value == "FILLED"

    await agent._check_position("tp-test", tracker)

    msg = await _get(bus[AgentName.ORCHESTRATOR])
    assert msg.msg_type == MessageType.POSITION_CLOSED
    close = PositionClose(**msg.payload)
    assert close.close_reason == CloseReason.TAKE_PROFIT
    assert close.pnl_gross > 0

    await agent._on_stop()


async def test_pnl_calculation_long_position():
    agent, _, bus = await _make_agent(capital=50_000.0)
    agent._trailing_enabled = False

    await agent.handle_message(_exec_msg("pnl-test", qty=0.1))
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    tracker = agent._positions["pnl-test"]
    entry = tracker.entry_price

    # Close at 55_000 → gross = (55000 - entry) * 0.1
    await agent._close_position_market("pnl-test", CloseReason.MANUAL, exit_price=55_000.0)

    msg = await _get(bus[AgentName.ORCHESTRATOR])
    close = PositionClose(**msg.payload)
    expected_gross = (55_000.0 - entry) * 0.1
    assert close.pnl_gross == pytest.approx(expected_gross, rel=0.01)
    assert close.pnl_net < close.pnl_gross  # fees deducted

    await agent._on_stop()


async def test_trailing_stop_moves_sl_to_breakeven():
    agent, adapter, bus = await _make_agent()

    await agent.handle_message(_exec_msg("trail-test", entry=50_000.0, atr=1_000.0))
    await _get(bus[AgentName.ORCHESTRATOR])
    await _get(bus[AgentName.JOURNAL])

    tracker = agent._positions["trail-test"]
    entry = tracker.entry_price

    # Price moves up by > 1×ATR (1000)
    adapter.set_price(entry + 1_100.0)
    await agent._check_trailing_stop("trail-test", tracker)

    assert tracker.breakeven_moved is True
    assert tracker.stop_loss == pytest.approx(entry, rel=1e-4)

    await agent._on_stop()
