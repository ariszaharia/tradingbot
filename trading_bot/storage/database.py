from __future__ import annotations
import json
import time
from pathlib import Path

import aiosqlite


_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT    NOT NULL UNIQUE,
    direction       TEXT    NOT NULL,
    strategy_name   TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    quantity        REAL    NOT NULL,
    pnl_gross       REAL    NOT NULL,
    pnl_net         REAL    NOT NULL,
    pnl_pct         REAL    NOT NULL,
    fees_total      REAL    NOT NULL,
    duration_minutes INTEGER NOT NULL,
    close_reason    TEXT    NOT NULL,
    timestamp_open  INTEGER NOT NULL,
    timestamp_close INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT    NOT NULL UNIQUE,
    direction       TEXT    NOT NULL,
    strategy_name   TEXT    NOT NULL,
    confidence_score REAL   NOT NULL,
    entry_price     REAL    NOT NULL,
    timeframe       TEXT    NOT NULL,
    reasoning       TEXT    NOT NULL,
    timestamp       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT    NOT NULL,
    approved        INTEGER NOT NULL,
    rejection_reason TEXT,
    position_size   REAL    NOT NULL,
    position_size_usd REAL  NOT NULL,
    reward_risk_ratio REAL  NOT NULL,
    rule_checks     TEXT    NOT NULL,
    timestamp       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT    NOT NULL,
    message         TEXT    NOT NULL,
    timestamp       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_json   TEXT    NOT NULL,
    timestamp       INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str = "trading_bot/journal.db") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------ #
    # Writes                                                               #
    # ------------------------------------------------------------------ #

    async def insert_trade(self, close: dict, strategy_name: str = "") -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO trades
               (signal_id, direction, strategy_name, entry_price, exit_price,
                quantity, pnl_gross, pnl_net, pnl_pct, fees_total,
                duration_minutes, close_reason, timestamp_open, timestamp_close)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                close["signal_id"],
                close.get("direction", "LONG"),
                strategy_name,
                close["entry_price"],
                close["exit_price"],
                close["quantity"],
                close["pnl_gross"],
                close["pnl_net"],
                close["pnl_pct"],
                close["fees_total"],
                close["duration_minutes"],
                close["close_reason"],
                close["timestamp_open"],
                close["timestamp_close"],
            ),
        )
        await self._db.commit()

    async def insert_signal(self, signal: dict) -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO signals
               (signal_id, direction, strategy_name, confidence_score,
                entry_price, timeframe, reasoning, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                signal["signal_id"],
                signal["direction"],
                signal["strategy_name"],
                signal["confidence_score"],
                signal["entry_price"],
                signal["timeframe"],
                json.dumps(signal.get("reasoning", [])),
                signal["timestamp"],
            ),
        )
        await self._db.commit()

    async def insert_risk_decision(self, decision: dict) -> None:
        await self._db.execute(
            """INSERT INTO risk_decisions
               (signal_id, approved, rejection_reason, position_size,
                position_size_usd, reward_risk_ratio, rule_checks, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                decision["signal_id"],
                int(decision["approved"]),
                decision.get("rejection_reason"),
                decision.get("position_size", 0.0),
                decision.get("position_size_usd", 0.0),
                decision.get("reward_risk_ratio", 0.0),
                json.dumps(decision.get("rule_checks", {})),
                decision.get("timestamp", int(time.time() * 1000)),
            ),
        )
        await self._db.commit()

    async def insert_error(self, agent: str, message: str) -> None:
        await self._db.execute(
            "INSERT INTO errors (agent, message, timestamp) VALUES (?,?,?)",
            (agent, message, int(time.time() * 1000)),
        )
        await self._db.commit()

    async def insert_snapshot(self, state_json: str) -> None:
        await self._db.execute(
            "INSERT INTO state_snapshots (snapshot_json, timestamp) VALUES (?,?)",
            (state_json, int(time.time() * 1000)),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Reads                                                                #
    # ------------------------------------------------------------------ #

    async def get_trades(
        self,
        since_ms: int = 0,
        until_ms: int | None = None,
        strategy: str | None = None,
    ) -> list[dict]:
        until_ms = until_ms or int(time.time() * 1000)
        params: list = [since_ms, until_ms]
        where = "WHERE timestamp_close BETWEEN ? AND ?"
        if strategy:
            where += " AND strategy_name = ?"
            params.append(strategy)
        async with self._db.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp_close ASC", params
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_trades(self, n: int = 20) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM trades ORDER BY timestamp_close DESC LIMIT ?", (n,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_trade_count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM trades") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0
