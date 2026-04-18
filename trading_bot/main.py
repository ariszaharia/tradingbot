"""
Entry point for the trading bot.

Usage:
    python -m trading_bot.main [--config path/to/config.yaml]

Starts agents in dependency order, runs until SIGINT/SIGTERM,
then performs a graceful shutdown in reverse order.
"""
from __future__ import annotations
import asyncio
import signal
import sys
from pathlib import Path

import yaml

from trading_bot.models.agent_message import AgentMessage, AgentName
from trading_bot.agents.orchestrator_agent import OrchestratorAgent
from trading_bot.agents.market_data_agent import MarketDataAgent
from trading_bot.agents.strategy_agent import StrategyAgent
from trading_bot.agents.risk_agent import RiskAgent
from trading_bot.agents.journal_agent import JournalAgent
from trading_bot.exchange.paper_trading_adapter import PaperTradingAdapter
from trading_bot.exchange.binance_adapter import BinanceAdapter
from trading_bot.utils.logger import AgentLogger


log = AgentLogger("MAIN")


def load_config(path: str = "trading_bot/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def run(config: dict) -> None:
    bus: dict[AgentName, asyncio.Queue[AgentMessage]] = {}
    mode = config["trading"].get("mode", "paper")

    # Build exchange adapter
    if mode == "live":
        exchange = BinanceAdapter(config)
        log.warning("LIVE MODE — real orders will be placed")
    else:
        exchange = PaperTradingAdapter(config, config["capital"]["initial_capital"])
        log.info("PAPER MODE — no real orders")

    # Instantiate agents (order matters: bus populated during __init__)
    orchestrator = OrchestratorAgent(bus, config)
    market_data = MarketDataAgent(bus, config, exchange)
    strategy = StrategyAgent(bus, config)
    risk = RiskAgent(bus, config)
    journal = JournalAgent(bus, config)

    agents = [orchestrator, market_data, strategy, risk, journal]

    # Graceful shutdown flag
    shutdown_event = asyncio.Event()

    def _handle_signal(*_):
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            signal.signal(sig, _handle_signal)

    # Start agents in dependency order
    log.info("Starting agents", mode=mode)
    for agent in agents:
        await agent.start()

    await orchestrator.start_trading()
    log.info("System running — press Ctrl+C to stop")

    # Wait for shutdown
    await shutdown_event.wait()

    # Graceful shutdown: stop new positions, then stop agents in reverse
    log.info("Initiating graceful shutdown")
    await orchestrator.pause_trading("shutdown requested")

    # Give in-flight messages a moment to drain
    await asyncio.sleep(2)

    for agent in reversed(agents):
        await agent.stop()

    log.info("All agents stopped")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bot")
    parser.add_argument("--config", default="trading_bot/config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
