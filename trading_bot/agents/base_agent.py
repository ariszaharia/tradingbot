from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.utils.logger import AgentLogger

if TYPE_CHECKING:
    pass


class BaseAgent(ABC):
    """
    Abstract base for all 6 agents.

    Each agent owns an inbound asyncio.Queue.  The Orchestrator (or any
    sender) puts AgentMessage objects onto that queue.  The agent's run()
    loop pulls messages and dispatches them to handle_message().

    Agents that need to send messages back call self._send(), which puts
    the message onto the recipient's queue via the shared bus dict that is
    injected at construction time.
    """

    def __init__(
        self,
        name: AgentName,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
        log_level: str = "INFO",
    ) -> None:
        self.name = name
        self._bus = bus
        self._config = config
        self.log = AgentLogger(name.value, log_level)

        # Register this agent's inbound queue on the shared bus
        self._inbox: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._bus[self.name] = self._inbox

        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        self.log.info("Agent starting")
        await self._on_start()
        self._tasks.append(asyncio.create_task(self._run_loop(), name=f"{self.name.value}-loop"))

    async def stop(self) -> None:
        self._running = False
        self.log.info("Agent stopping")
        await self._on_stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.log.info("Agent stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbox.get(), timeout=1.0)
                self.log.debug(
                    "Received message",
                    msg_id=msg.msg_id,
                    msg_type=msg.msg_type.value,
                    sender=msg.sender.value,
                )
                try:
                    await self.handle_message(msg)
                except Exception as exc:
                    self.log.error(
                        "Error handling message",
                        msg_type=msg.msg_type.value,
                        error=str(exc),
                    )
                finally:
                    self._inbox.task_done()

                    if msg.requires_ack:
                        await self._send(AgentMessage(
                            sender=self.name,
                            recipient=msg.sender,
                            msg_type=MessageType.ACK,
                            payload={"ack_for": msg.msg_id},
                        ))

            except asyncio.TimeoutError:
                # Heartbeat tick — subclasses may override _on_tick()
                await self._on_tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Unexpected error in run loop", error=str(exc))

    # ------------------------------------------------------------------ #
    # Messaging helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _send(self, msg: AgentMessage) -> None:
        target = msg.recipient
        if target == AgentName.BROADCAST:
            for name, queue in self._bus.items():
                if name != self.name:
                    await queue.put(msg)
        elif target in self._bus:
            await self._bus[target].put(msg)
        else:
            self.log.warning("No queue for recipient", recipient=target.value)

    async def _send_to_journal(self, msg: AgentMessage) -> None:
        """Convenience: forward any message to the Journal Agent."""
        journal_msg = msg.model_copy(update={"recipient": AgentName.JOURNAL})
        await self._send(journal_msg)

    # ------------------------------------------------------------------ #
    # Abstract interface — subclasses must implement                       #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def handle_message(self, msg: AgentMessage) -> None:
        """Process one inbound message."""
        ...

    async def _on_start(self) -> None:
        """Called once before the run loop begins. Override for setup."""

    async def _on_stop(self) -> None:
        """Called once after the run loop ends. Override for teardown."""

    async def _on_tick(self) -> None:
        """Called on every 1-second timeout in the run loop. Override for periodic work."""

    # ------------------------------------------------------------------ #
    # Utilities                                                            #
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running

    def queue_size(self) -> int:
        return self._inbox.qsize()
