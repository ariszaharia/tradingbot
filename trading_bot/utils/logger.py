from __future__ import annotations
import logging
import json
import time
from typing import Any


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx: dict[str, Any] = getattr(record, "ctx", {})
        entry = {
            "timestamp": int(time.time() * 1000),
            "agent": getattr(record, "agent_name", "SYSTEM"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if ctx:
            entry["context"] = ctx
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def get_logger(agent_name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(agent_name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


class AgentLogger:
    """Thin wrapper that injects agent_name and ctx into every log call."""

    def __init__(self, agent_name: str, level: str = "INFO") -> None:
        self._logger = get_logger(agent_name, level)
        self._agent_name = agent_name

    def _log(self, level: int, msg: str, ctx: dict[str, Any] | None = None) -> None:
        extra = {"agent_name": self._agent_name, "ctx": ctx or {}}
        self._logger.log(level, msg, extra=extra)

    def debug(self, msg: str, **ctx: Any) -> None:
        self._log(logging.DEBUG, msg, ctx)

    def info(self, msg: str, **ctx: Any) -> None:
        self._log(logging.INFO, msg, ctx)

    def warning(self, msg: str, **ctx: Any) -> None:
        self._log(logging.WARNING, msg, ctx)

    def error(self, msg: str, **ctx: Any) -> None:
        self._log(logging.ERROR, msg, ctx)

    def critical(self, msg: str, **ctx: Any) -> None:
        self._log(logging.CRITICAL, msg, ctx)
