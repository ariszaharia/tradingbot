from __future__ import annotations
import asyncio

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.models.trading_signal import Direction, TradingSignal
from trading_bot.strategies.base_strategy import BaseStrategy
from trading_bot.strategies.mean_reversion import MeanReversionStrategy
from trading_bot.strategies.trend_following import TrendFollowingStrategy

_STRATEGY_MAP = {
    "trend_following": TrendFollowingStrategy,
    "mean_reversion": MeanReversionStrategy,
}


class StrategyAgent(BaseAgent):
    """
    Runs one or more strategies against each DataSnapshot.

    Rules enforced here (before calling strategy.evaluate):
      - No signal if spread_pct > 0.1 %
      - No signal if anomaly_flag is True

    When multiple strategies are active and both produce a non-FLAT signal,
    the one with the higher confidence_score wins. If they conflict in
    direction, FLAT is emitted (no forced trades in ambiguous conditions).
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
    ) -> None:
        super().__init__(AgentName.STRATEGY, bus, config)

        active_names: list[str] = config.get("strategy", {}).get(
            "active", ["trend_following", "mean_reversion"]
        )
        self._strategies: list[BaseStrategy] = [
            _STRATEGY_MAP[name](config)
            for name in active_names
            if name in _STRATEGY_MAP
        ]
        self.log.info("Strategies loaded", strategies=active_names)

    # ------------------------------------------------------------------ #
    # Message handler                                                      #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        if msg.msg_type != MessageType.REQUEST_SIGNAL:
            return

        snapshot = DataSnapshot(**msg.payload)
        signal = self._evaluate(snapshot)

        self.log.info(
            "Signal generated",
            direction=signal.direction.value,
            strategy=signal.strategy_name,
            confidence=signal.confidence_score,
            reasoning=signal.reasoning,
        )

        reply = AgentMessage(
            sender=AgentName.STRATEGY,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.TRADING_SIGNAL,
            payload=signal.model_dump(),
        )
        await self._send(reply)

        # Journal always gets a copy
        journal_msg = AgentMessage(
            sender=AgentName.STRATEGY,
            recipient=AgentName.JOURNAL,
            msg_type=MessageType.TRADING_SIGNAL,
            payload=signal.model_dump(),
        )
        await self._send(journal_msg)

    # ------------------------------------------------------------------ #
    # Core evaluation                                                      #
    # ------------------------------------------------------------------ #

    def _evaluate(self, snapshot: DataSnapshot) -> TradingSignal:
        # Gate 1: spread filter
        if snapshot.spread_pct > 0.1:
            self.log.warning(
                "Signal suppressed: spread too wide",
                spread_pct=snapshot.spread_pct,
            )
            return self._flat_signal(snapshot, f"Spread {snapshot.spread_pct:.3f}% > 0.1%")

        # Gate 2: anomaly filter
        if snapshot.anomaly_flag:
            self.log.warning(
                "Signal suppressed: anomaly flag set",
                reason=snapshot.anomaly_reason,
            )
            return self._flat_signal(snapshot, f"Anomaly: {snapshot.anomaly_reason}")

        signals = [s.evaluate(snapshot) for s in self._strategies]

        # Filter out FLAT signals for arbitration
        actionable = [s for s in signals if s.direction not in (Direction.FLAT,)]

        # EXIT signals always take priority — if any strategy says EXIT, EXIT
        exits = [s for s in actionable if s.direction == Direction.EXIT]
        if exits:
            best_exit = max(exits, key=lambda s: s.confidence_score)
            return best_exit

        non_flat = [s for s in actionable if s.direction != Direction.EXIT]

        if not non_flat:
            return self._flat_signal(snapshot, "All strategies returned FLAT")

        if len(non_flat) == 1:
            return non_flat[0]

        # Multiple actionable signals: check for directional conflict
        directions = {s.direction for s in non_flat}
        if len(directions) > 1:
            self.log.info(
                "Strategy conflict — emitting FLAT",
                directions=[d.value for d in directions],
            )
            return self._flat_signal(snapshot, "Strategy directional conflict")

        # Same direction — return highest confidence
        return max(non_flat, key=lambda s: s.confidence_score)

    def _flat_signal(self, snapshot: DataSnapshot, reason: str) -> TradingSignal:
        # Delegate to the first strategy's _flat helper for a consistent object
        return self._strategies[0]._flat(snapshot, [reason])
