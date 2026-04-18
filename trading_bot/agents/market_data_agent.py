from __future__ import annotations
import asyncio
import time
from collections import deque

import numpy as np

from trading_bot.agents.base_agent import BaseAgent
from trading_bot.exchange.base_exchange import BaseExchange
from trading_bot.models.agent_message import AgentMessage, AgentName, MessageType
from trading_bot.models.data_snapshot import DataSnapshot
from trading_bot.utils.indicators import compute_all


# Canonical timeframe ordering (coarsest last)
_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

# Maps config string → CCXT string
_TF_ALIAS = {"1H": "1h", "4H": "4h", "1D": "1d"}


def _normalise_tf(tf: str) -> str:
    return _TF_ALIAS.get(tf, tf.lower())


class MarketDataAgent(BaseAgent):
    """
    Single source of truth for market data.

    On startup:
      - Fetches the last 500 candles for every required timeframe via CCXT.
      - Subscribes to a live price feed (polled or WebSocket).

    Every time a new candle closes on the primary timeframe:
      - Recomputes all indicators.
      - Builds a DataSnapshot and sends it to the Orchestrator.
      - Checks for anomalies and flags them on the snapshot (or sends a
        separate ANOMALY_DETECTED message if the anomaly occurs mid-candle).
    """

    def __init__(
        self,
        bus: dict[AgentName, asyncio.Queue[AgentMessage]],
        config: dict,
        exchange: BaseExchange,
    ) -> None:
        super().__init__(AgentName.MARKET_DATA, bus, config)
        self._exchange = exchange

        tcfg = config["trading"]
        self._symbol: str = tcfg["symbol"]
        self._primary_tf: str = _normalise_tf(tcfg["primary_timeframe"])
        self._confirm_tf: str = _normalise_tf(tcfg["confirmation_timeframe"])
        ind_cfg = config.get("indicators", {})
        self._buf_size: int = ind_cfg.get("candle_buffer_size", 500)
        self._anomaly_spike_pct: float = ind_cfg.get("anomaly_price_spike_pct", 5.0)
        self._anomaly_vol_mult: float = ind_cfg.get("anomaly_volume_multiplier", 10.0)
        self._ind_cfg: dict = ind_cfg

        # Circular buffers: tf → deque of OHLCV dicts (oldest first)
        self._buffers: dict[str, deque] = {
            tf: deque(maxlen=self._buf_size) for tf in _TIMEFRAMES
        }

        # Track last emitted candle timestamp per timeframe to detect new closes
        self._last_candle_ts: dict[str, int] = {tf: 0 for tf in _TIMEFRAMES}

        # Latest ticker values
        self._price: float = 0.0
        self._bid: float = 0.0
        self._ask: float = 0.0

        self._feed_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def _on_start(self) -> None:
        await self._exchange.connect()
        await self._bootstrap_historical()
        await self._exchange.subscribe_price_feed(self._symbol, self._on_price_tick)
        # Emit one snapshot immediately so the pipeline runs without waiting for a candle close
        await self._emit_snapshot()
        # Periodic OHLCV refresh (catches candle closes on every timeframe)
        self._poll_task = asyncio.create_task(self._candle_poll_loop())
        self.log.info(
            "MarketDataAgent started",
            symbol=self._symbol,
            primary_tf=self._primary_tf,
        )

    async def _on_stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await self._exchange.disconnect()

    # ------------------------------------------------------------------ #
    # Historical bootstrap                                                 #
    # ------------------------------------------------------------------ #

    async def _bootstrap_historical(self) -> None:
        self.log.info("Fetching historical OHLCV", symbol=self._symbol)
        for tf in _TIMEFRAMES:
            try:
                candles = await self._exchange.fetch_ohlcv(
                    self._symbol, tf, limit=self._buf_size
                )
                self._buffers[tf].extend(candles)
                if candles:
                    self._last_candle_ts[tf] = candles[-1]["timestamp"]
                self.log.info("Loaded candles", timeframe=tf, count=len(candles))
            except Exception as exc:
                self.log.error("Failed to fetch OHLCV", timeframe=tf, error=str(exc))

        # Initialise price from latest 1h close
        if self._buffers[self._primary_tf]:
            self._price = self._buffers[self._primary_tf][-1]["close"]

    # ------------------------------------------------------------------ #
    # Live price feed callback                                             #
    # ------------------------------------------------------------------ #

    async def _on_price_tick(self, symbol: str, price: float) -> None:
        self._price = price
        spread = price * 0.0001
        self._bid = price - spread / 2
        self._ask = price + spread / 2
        await self._check_1m_anomaly(price)

    async def _check_1m_anomaly(self, current_price: float) -> None:
        buf = self._buffers["1m"]
        if len(buf) < 2:
            return
        prev_close = buf[-2]["close"]
        if prev_close == 0:
            return
        pct_change = abs(current_price - prev_close) / prev_close * 100
        if pct_change > self._anomaly_spike_pct:
            reason = f"Price spike {pct_change:.2f}% on 1m (threshold {self._anomaly_spike_pct}%)"
            self.log.warning("Anomaly detected", reason=reason)
            await self._broadcast_anomaly(reason)

    # ------------------------------------------------------------------ #
    # Candle poll loop                                                     #
    # ------------------------------------------------------------------ #

    async def _candle_poll_loop(self) -> None:
        """
        Re-fetches the last few candles for every timeframe every 30 s.
        When a new closed candle appears on the primary timeframe, emit a
        DataSnapshot to the Orchestrator.
        """
        while True:
            try:
                await asyncio.sleep(30)
                for tf in _TIMEFRAMES:
                    await self._refresh_timeframe(tf)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Candle poll error", error=str(exc))

    async def _refresh_timeframe(self, tf: str) -> None:
        for attempt in range(3):
            try:
                candles = await self._exchange.fetch_ohlcv(self._symbol, tf, limit=3)
                break
            except Exception as exc:
                full_err = f"{type(exc).__name__}: {exc}"
                if attempt == 2:
                    self.log.error("fetch_ohlcv failed", tf=tf, error=full_err)
                    return
                await asyncio.sleep(2 ** attempt)
        else:
            return

        if not candles:
            return

        # The last candle from the exchange is the *current* (possibly open) candle.
        # The second-to-last is the most recently CLOSED candle.
        closed_candles = candles[:-1]
        for c in closed_candles:
            if c["timestamp"] > self._last_candle_ts[tf]:
                self._buffers[tf].append(c)
                self._last_candle_ts[tf] = c["timestamp"]
                self.log.debug("New closed candle", tf=tf, ts=c["timestamp"], close=c["close"])

                if tf == self._primary_tf:
                    await self._maybe_check_volume_anomaly()
                    await self._emit_snapshot()

    # ------------------------------------------------------------------ #
    # Anomaly detection                                                    #
    # ------------------------------------------------------------------ #

    async def _maybe_check_volume_anomaly(self) -> None:
        buf = self._buffers[self._primary_tf]
        if len(buf) < 21:
            return
        volumes = np.array([c["volume"] for c in buf])
        vol_sma = float(np.mean(volumes[-21:-1]))  # avg of previous 20
        latest_vol = volumes[-1]
        if vol_sma > 0 and latest_vol > vol_sma * self._anomaly_vol_mult:
            reason = (
                f"Volume spike {latest_vol:.0f} vs SMA {vol_sma:.0f} "
                f"({latest_vol / vol_sma:.1f}x) on {self._primary_tf}"
            )
            self.log.warning("Volume anomaly", reason=reason)
            await self._broadcast_anomaly(reason)

    async def _broadcast_anomaly(self, reason: str) -> None:
        msg = AgentMessage(
            sender=AgentName.MARKET_DATA,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.ANOMALY_DETECTED,
            payload={"reason": reason, "timestamp": int(time.time() * 1000)},
        )
        await self._send(msg)

    # ------------------------------------------------------------------ #
    # DataSnapshot emission                                                #
    # ------------------------------------------------------------------ #

    async def _emit_snapshot(self) -> None:
        snapshot = self._build_snapshot()
        msg = AgentMessage(
            sender=AgentName.MARKET_DATA,
            recipient=AgentName.ORCHESTRATOR,
            msg_type=MessageType.DATA_SNAPSHOT,
            payload=snapshot.model_dump(),
        )
        await self._send(msg)
        self.log.info(
            "DataSnapshot emitted",
            price=snapshot.price,
            spread_pct=round(snapshot.spread_pct, 4),
            anomaly=snapshot.anomaly_flag,
        )

    def _build_snapshot(self) -> DataSnapshot:
        buf_primary = self._buffers[self._primary_tf]
        buf_htf = self._buffers[self._confirm_tf]

        # Convert to numpy for indicator computation
        def to_arrays(buf):
            arr = list(buf)
            opens = np.array([c["open"] for c in arr], dtype=float)
            highs = np.array([c["high"] for c in arr], dtype=float)
            lows = np.array([c["low"] for c in arr], dtype=float)
            closes = np.array([c["close"] for c in arr], dtype=float)
            vols = np.array([c["volume"] for c in arr], dtype=float)
            return opens, highs, lows, closes, vols

        indicators: dict[str, float] = {}
        htf_indicators: dict[str, float] = {}

        if len(buf_primary) >= 2:
            o, h, l, c, v = to_arrays(buf_primary)
            indicators = compute_all(o, h, l, c, v, self._ind_cfg)

        if len(buf_htf) >= 2:
            o, h, l, c, v = to_arrays(buf_htf)
            htf_indicators = compute_all(o, h, l, c, v, self._ind_cfg)

        mid = (self._bid + self._ask) / 2 if self._bid and self._ask else self._price
        spread_pct = (
            (self._ask - self._bid) / mid * 100
            if mid > 0 else 0.0
        )

        # Serialise OHLCV buffers as list-of-dicts keyed by timeframe
        ohlcv_payload = {
            tf: list(self._buffers[tf])
            for tf in _TIMEFRAMES
            if self._buffers[tf]
        }

        return DataSnapshot(
            symbol=self._symbol,
            timestamp=int(time.time() * 1000),
            price=self._price,
            bid=self._bid or self._price,
            ask=self._ask or self._price,
            spread_pct=spread_pct,
            ohlcv=ohlcv_payload,
            indicators=indicators,
            htf_indicators=htf_indicators,
            anomaly_flag=False,
            anomaly_reason=None,
        )

    # ------------------------------------------------------------------ #
    # Inbound message handler                                              #
    # ------------------------------------------------------------------ #

    async def handle_message(self, msg: AgentMessage) -> None:
        # The Market Data Agent is mostly a producer.
        # The Orchestrator can request a snapshot on-demand.
        if msg.msg_type == MessageType.REQUEST_SIGNAL:
            await self._emit_snapshot()
