"""
Backtest engine — BTC/USDT multi-timeframe.

Usage:
    python -m trading_bot.backtest [--months 6] [--capital 10000]
    python -m trading_bot.backtest --strategy breakout
    python -m trading_bot.backtest --strategy cascade_reversal
    python -m trading_bot.backtest --strategy weekly_momentum
    python -m trading_bot.backtest --start-date 2023-04-18 --end-date 2024-04-18
    python -m trading_bot.backtest --fee-rate 0.002 --slippage 0.0015

Simulation rules:
  - Entry at close of signal candle (no look-ahead)
  - SL / TP checked against next candle high / low
  - Partial exits: each level checked independently; SL moves to breakeven after TP1
  - Taker fee: 0.1% per side (configurable)
  - Slippage: 0.05% on entry (configurable)
  - One position at a time
  - Strategy exits enabled (use_strategy_exits=True by default for new strategies)
"""
from __future__ import annotations
import argparse
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from trading_bot.utils.indicators import compute_all


# ---------------------------------------------------------------------------
# Data fetching / caching
# ---------------------------------------------------------------------------

_CACHE_DIR = Path("trading_bot/.cache")
_CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(symbol: str, tf: str, months: int) -> Path:
    key = symbol.replace("/", "_")
    return _CACHE_DIR / f"{key}_{tf}_{months}m.json"


def fetch_ohlcv(symbol: str, timeframe: str, months: int) -> pd.DataFrame:
    cache = _cache_path(symbol, timeframe, months)
    if cache.exists():
        print(f"  [cache] {timeframe} loaded from {cache.name}")
        data = json.loads(cache.read_text())
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    print(f"  [fetch] {timeframe} downloading from Binance ...")
    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True})

    since_ms = int((time.time() - months * 30 * 86_400) * 1000)
    all_candles: list = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not batch:
            break
        all_candles.extend(batch)
        since_ms = batch[-1][0] + 1
        if len(batch) < 1000:
            break

    cache.write_text(json.dumps(all_candles))
    print(f"  [fetch] {timeframe}: {len(all_candles)} candles saved")

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class PartialExit:
    price: float
    fraction: float
    trailing: bool = False
    trailing_atr_mult: float = 0.0
    hit: bool = False


@dataclass
class Trade:
    signal_id: str
    strategy: str
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float        # TP1 reference (for reporting)
    quantity: float           # original full quantity
    pnl_gross: float
    pnl_net: float
    pnl_pct: float
    fees: float
    close_reason: str
    entry_idx: int
    exit_idx: int
    duration_candles: int
    atr_entry: float = 0.0
    peak_favorable: float = 0.0
    # Partial exit support
    partial_exits: list = field(default_factory=list)  # list[PartialExit]
    quantity_remaining: float = 0.0   # decreases as partials hit
    realized_gross: float = 0.0       # accumulated gross P&L from partials
    breakeven_set: bool = False


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

FEE_RATE = 0.001    # 0.1% per side
SLIPPAGE  = 0.0005  # 0.05% entry slippage


@dataclass
class BacktestEngine:
    initial_capital: float = 10_000.0
    risk_pct: float = 1.0
    max_dd_daily_pct: float = 3.0
    max_dd_total_pct: float = 10.0
    max_pos_size_pct: float = 15.0
    min_confidence: float = 0.65
    cooldown_after_losses: int = 2
    trailing_stop_enabled: bool = False
    trailing_stop_trigger_atr: float = 1.5
    fee_rate: float = FEE_RATE
    slippage_entry: float = SLIPPAGE
    use_strategy_exits: bool = True   # enabled for new strategies

    capital: float = field(init=False)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    open_trade: Trade | None = None
    consecutive_losses: int = 0
    cooldown_remaining: int = 0
    daily_start_capital: float = field(init=False)
    daily_pnl: float = 0.0
    trade_counter: int = 0

    def __post_init__(self):
        self.capital = self.initial_capital
        self.daily_start_capital = self.initial_capital

    def run(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        df_1w: pd.DataFrame,
        active_strategies: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        from trading_bot.strategies.trend_following import TrendFollowingStrategy
        from trading_bot.strategies.mean_reversion import MeanReversionStrategy
        from trading_bot.strategies.breakout_strategy import BreakoutStrategy
        from trading_bot.strategies.cascade_reversal import CascadeReversalStrategy
        from trading_bot.strategies.weekly_momentum import WeeklyMomentumStrategy
        from trading_bot.models.data_snapshot import DataSnapshot
        from trading_bot.models.trading_signal import Direction
        from trading_bot.strategies.regime_detector import detect_regime

        _registry = {
            "trend_following":  TrendFollowingStrategy({}),
            "mean_reversion":   MeanReversionStrategy({}),
            "breakout":         BreakoutStrategy({}),
            "cascade_reversal": CascadeReversalStrategy({}),
            "weekly_momentum":  WeeklyMomentumStrategy({}),
        }
        enabled = active_strategies or list(_registry.keys())
        strategies = [_registry[k] for k in enabled if k in _registry]

        ind_cfg = {
            "ema_periods": [9, 21, 50, 200],
            "rsi_periods": [7, 14],
            "atr_period": 14,
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
            "bb_period": 20, "bb_std": 2.0,
            "volume_sma_period": 20,
        }

        warmup = 210
        n = len(df_1h)

        if start_date:
            start_ts  = pd.Timestamp(start_date, tz="UTC")
            start_idx = max(warmup, int(df_1h.index.searchsorted(start_ts)))
        else:
            start_idx = warmup
        if end_date:
            end_ts  = pd.Timestamp(end_date, tz="UTC")
            end_idx = int(df_1h.index.searchsorted(end_ts))
        else:
            end_idx = n

        if start_idx >= end_idx:
            print("  No candles in specified date range.")
            return

        current_day = df_1h.index[start_idx].date()
        self.daily_start_capital = self.capital

        print(f"\n  Running on {end_idx - start_idx} candles "
              f"({df_1h.index[start_idx].date()} -> {df_1h.index[end_idx - 1].date()}) ...")

        for i in range(start_idx, end_idx):
            ts = df_1h.index[i]

            # Daily PnL reset
            if ts.date() != current_day:
                current_day = ts.date()
                self.daily_pnl = 0.0
                self.daily_start_capital = self.capital

            if self.cooldown_remaining > 0:
                self.cooldown_remaining -= 1

            # --- 1H indicators -----------------------------------------------
            window = df_1h.iloc[i - warmup: i + 1]
            ind_1h = compute_all(
                window["open"].values.astype(float),
                window["high"].values.astype(float),
                window["low"].values.astype(float),
                window["close"].values.astype(float),
                window["volume"].values.astype(float),
                ind_cfg,
            )

            # --- 4H indicators (use only CLOSED 4H bars — shift by 4H) ------
            # A 4H bar with open T closes at T+4H. Only include if T+4H <= ts,
            # i.e. T <= ts - 4H. This prevents look-ahead into the current bar.
            htf_mask = df_4h.index <= ts - pd.Timedelta(hours=4)
            if htf_mask.sum() >= 50:
                htf_win = df_4h[htf_mask].iloc[-210:]
                ind_4h = compute_all(
                    htf_win["open"].values.astype(float),
                    htf_win["high"].values.astype(float),
                    htf_win["low"].values.astype(float),
                    htf_win["close"].values.astype(float),
                    htf_win["volume"].values.astype(float),
                    ind_cfg,
                )
            else:
                ind_4h = {}

            # --- Daily indicators + regime (closed daily bars only) ----------
            # Daily bar with open T closes at T+24H. Use T <= ts - 24H.
            d_mask = df_1d.index <= ts - pd.Timedelta(hours=24)
            ind_1d: dict = {}
            regime = None
            if d_mask.sum() >= 165:
                d_win = df_1d[d_mask].iloc[-250:]
                ind_1d = compute_all(
                    d_win["open"].values.astype(float),
                    d_win["high"].values.astype(float),
                    d_win["low"].values.astype(float),
                    d_win["close"].values.astype(float),
                    d_win["volume"].values.astype(float),
                    ind_cfg,
                )
                regime = detect_regime(
                    d_win["high"].values.astype(float),
                    d_win["low"].values.astype(float),
                    d_win["close"].values.astype(float),
                    timestamp=int(ts.timestamp() * 1000),
                )

            # --- Weekly indicators (closed weekly bars only) -----------------
            # Weekly bar (Mon open) closes the following Monday. Use T <= ts - 7D.
            w_mask = df_1w.index <= ts - pd.Timedelta(days=7)
            ind_1w: dict = {}
            if w_mask.sum() >= 55:
                w_win = df_1w[w_mask].iloc[-200:]
                ind_1w = compute_all(
                    w_win["open"].values.astype(float),
                    w_win["high"].values.astype(float),
                    w_win["low"].values.astype(float),
                    w_win["close"].values.astype(float),
                    w_win["volume"].values.astype(float),
                    ind_cfg,
                )

            price       = float(window["close"].iloc[-1])
            candle_high = float(window["high"].iloc[-1])
            candle_low  = float(window["low"].iloc[-1])

            # --- SL / TP exit check -----------------------------------------
            if self.open_trade is not None:
                exited = self._check_exit(self.open_trade, candle_high, candle_low, price, i)
                if exited:
                    self.equity_curve.append(self.capital)
                    continue

            # --- Strategy EXIT signals ---------------------------------------
            if self.open_trade is not None and self.use_strategy_exits:
                candles_open = i - self.open_trade.entry_idx
                exit_snap = DataSnapshot(
                    symbol="BTC/USDT", price=price,
                    bid=price * 0.9999, ask=price * 1.0001, spread_pct=0.01,
                    ohlcv={},
                    indicators=ind_1h, htf_indicators=ind_4h,
                    daily_indicators=ind_1d, weekly_indicators=ind_1w,
                    regime=regime,
                    current_position_direction=self.open_trade.direction,
                    candles_in_position=candles_open,
                )
                open_strat_name = self.open_trade.strategy
                for strat in strategies:
                    if strat.name != open_strat_name:
                        continue
                    sig = strat.evaluate(exit_snap)
                    if sig.direction == Direction.EXIT:
                        self._close(self.open_trade, price, "STRATEGY_EXIT", i)
                        break
                if self.open_trade is None:
                    self.equity_curve.append(self.capital)
                    continue

            # --- Trailing stop update (end of candle) -----------------------
            if self.open_trade is not None and self.trailing_stop_enabled:
                self._update_trailing_stop(self.open_trade, price, candle_high, candle_low)

            # Also update trailing stop for partial-exit trailing legs
            if self.open_trade is not None:
                self._update_partial_trailing(self.open_trade, price, candle_high, candle_low)

            # --- New entry ---------------------------------------------------
            if self.open_trade is None:
                entry_snap = DataSnapshot(
                    symbol="BTC/USDT", price=price,
                    bid=price * 0.9999, ask=price * 1.0001, spread_pct=0.01,
                    ohlcv={},
                    indicators=ind_1h, htf_indicators=ind_4h,
                    daily_indicators=ind_1d, weekly_indicators=ind_1w,
                    regime=regime,
                    current_position_direction=None,
                    candles_in_position=0,
                )
                best = None
                for strat in strategies:
                    sig = strat.evaluate(entry_snap)
                    if sig.direction in (Direction.FLAT, Direction.EXIT):
                        continue
                    if best is None or sig.confidence_score > best.confidence_score:
                        best = sig

                if best and self._risk_ok(best):
                    self._enter(best, i, price, ind_1h)

            self.equity_curve.append(self.capital)

        if self.open_trade is not None:
            last_close = float(df_1h["close"].iloc[end_idx - 1])
            self._force_close(self.open_trade, last_close, end_idx - 1)

    # --- Trailing stop helpers -----------------------------------------------

    def _update_trailing_stop(self, t: Trade, close: float, high: float, low: float) -> None:
        if t.atr_entry <= 0:
            return
        trigger = self.trailing_stop_trigger_atr * t.atr_entry
        if t.direction == "LONG":
            t.peak_favorable = max(t.peak_favorable, high)
            if t.peak_favorable - t.entry_price >= trigger:
                trail_sl = t.peak_favorable - t.atr_entry
                new_sl = max(trail_sl, t.entry_price)
                t.stop_loss = max(t.stop_loss, new_sl)
        else:
            t.peak_favorable = min(t.peak_favorable, low)
            if t.entry_price - t.peak_favorable >= trigger:
                trail_sl = t.peak_favorable + t.atr_entry
                new_sl = min(trail_sl, t.entry_price)
                t.stop_loss = min(t.stop_loss, new_sl)

    def _update_partial_trailing(self, t: Trade, close: float, high: float, low: float) -> None:
        """Activate trailing stop on the trailing leg once all fixed TPs are hit."""
        if not t.partial_exits or t.atr_entry <= 0:
            return
        fixed = [l for l in t.partial_exits if not l.trailing]
        trailing_level = next((l for l in t.partial_exits if l.trailing), None)
        if trailing_level is None or trailing_level.hit:
            return
        if not all(l.hit for l in fixed):
            return  # trailing leg not yet active

        # Once all fixed TPs hit, trail the remaining position
        atr = t.atr_entry * trailing_level.trailing_atr_mult
        if t.direction == "LONG":
            t.peak_favorable = max(t.peak_favorable, high)
            trail_sl = t.peak_favorable - atr
            # Only move SL up (ratchet)
            if trail_sl > t.stop_loss:
                t.stop_loss = trail_sl
        else:
            t.peak_favorable = min(t.peak_favorable, low)
            trail_sl = t.peak_favorable + atr
            if trail_sl < t.stop_loss:
                t.stop_loss = trail_sl

    # --- Risk gate -----------------------------------------------------------

    def _risk_ok(self, signal) -> bool:
        if signal.confidence_score < self.min_confidence:
            return False
        if self.cooldown_remaining > 0:
            return False
        if self.daily_pnl < 0:
            dd = -self.daily_pnl / self.daily_start_capital * 100
            if dd >= self.max_dd_daily_pct:
                return False
        total_dd = (self.initial_capital - self.capital) / self.initial_capital * 100
        if total_dd >= self.max_dd_total_pct:
            return False
        if signal.entry_price <= 0 or signal.suggested_stop_loss <= 0:
            return False
        if abs(signal.entry_price - signal.suggested_stop_loss) < 1e-8:
            return False
        return True

    # --- Entry ---------------------------------------------------------------

    def _enter(self, signal, idx: int, price: float, indicators: dict) -> None:
        from trading_bot.utils.risk_calculator import calc_position_size

        slippage_factor = (1 + self.slippage_entry) if signal.direction.value == "LONG" \
                          else (1 - self.slippage_entry)
        entry = price * slippage_factor

        try:
            risk_units, _ = calc_position_size(
                self.capital, self.risk_pct, entry, signal.suggested_stop_loss
            )
        except ValueError:
            return

        max_units = (self.capital * self.max_pos_size_pct / 100) / entry
        units = min(risk_units, max_units)
        if units <= 0:
            return

        fee_entry = entry * units * self.fee_rate
        self.trade_counter += 1
        atr = indicators.get("atr_14", 0.0)

        partial_exits = [
            PartialExit(
                price=el.price,
                fraction=el.fraction,
                trailing=el.trailing,
                trailing_atr_mult=el.trailing_atr_mult,
            )
            for el in signal.exit_levels
        ]

        self.open_trade = Trade(
            signal_id=f"bt-{self.trade_counter:05d}",
            strategy=signal.strategy_name,
            direction=signal.direction.value,
            entry_price=entry,
            exit_price=0.0,
            stop_loss=signal.suggested_stop_loss,
            take_profit=signal.suggested_take_profit,
            quantity=units,
            pnl_gross=0.0, pnl_net=0.0, pnl_pct=0.0,
            fees=fee_entry,
            close_reason="",
            entry_idx=idx,
            exit_idx=0,
            duration_candles=0,
            atr_entry=atr,
            peak_favorable=entry,
            partial_exits=partial_exits,
            quantity_remaining=units,
        )

    # --- Exit ----------------------------------------------------------------

    def _check_exit(self, t: Trade, high: float, low: float, close: float, idx: int) -> bool:
        if not t.partial_exits:
            # Single-TP mode (old strategies / simple exits)
            hit_sl = (low <= t.stop_loss)   if t.direction == "LONG" else (high >= t.stop_loss)
            hit_tp = (high >= t.take_profit) if t.direction == "LONG" else (low <= t.take_profit)
            if hit_tp and hit_sl:
                hit_sl = False
            if hit_tp:
                self._close(t, t.take_profit, "TAKE_PROFIT", idx)
                return True
            if hit_sl:
                self._close(t, t.stop_loss, "STOP_LOSS", idx)
                return True
            return False

        # Partial-exit mode
        # Check fixed (non-trailing) TPs
        for level in t.partial_exits:
            if level.hit or level.trailing:
                continue
            price_hit = (high >= level.price) if t.direction == "LONG" else (low <= level.price)
            if price_hit:
                self._record_partial(t, level, level.price)

        # If all non-trailing levels hit and nothing remains → finalise
        fixed_levels = [l for l in t.partial_exits if not l.trailing]
        if fixed_levels and all(l.hit for l in fixed_levels) and t.quantity_remaining <= 1e-8:
            self._finalize_trade(t, close, "ALL_TARGETS", idx)
            return True

        # Check SL (applies to whatever quantity remains)
        hit_sl = (low <= t.stop_loss) if t.direction == "LONG" else (high >= t.stop_loss)
        if hit_sl:
            self._close(t, t.stop_loss, "STOP_LOSS", idx)
            return True

        return False

    def _record_partial(self, t: Trade, level: PartialExit, exit_price: float) -> None:
        qty = t.quantity * level.fraction
        t.quantity_remaining = max(t.quantity_remaining - qty, 0.0)

        fee = exit_price * qty * self.fee_rate
        t.fees += fee

        pnl = (exit_price - t.entry_price) * qty if t.direction == "LONG" \
              else (t.entry_price - exit_price) * qty
        t.realized_gross += pnl
        level.hit = True

        # Move SL to breakeven after first TP
        if not t.breakeven_set:
            if t.direction == "LONG":
                t.stop_loss = max(t.stop_loss, t.entry_price)
            else:
                t.stop_loss = min(t.stop_loss, t.entry_price)
            t.breakeven_set = True

    def _finalize_trade(self, t: Trade, close_price: float, reason: str, idx: int) -> None:
        """All partial levels exhausted — nothing left to close."""
        t.pnl_gross = t.realized_gross
        t.pnl_net   = t.pnl_gross - t.fees
        t.pnl_pct   = t.pnl_gross / (t.entry_price * t.quantity) * 100
        t.exit_price = close_price
        t.close_reason = reason
        t.exit_idx = idx
        t.duration_candles = idx - t.entry_idx

        self.capital   += t.pnl_net
        self.daily_pnl += t.pnl_net
        self.trades.append(t)
        self.open_trade = None

        is_win = t.pnl_net > 0
        self.consecutive_losses = 0 if is_win else self.consecutive_losses + 1
        if self.consecutive_losses >= 2:
            self.cooldown_remaining = self.cooldown_after_losses

    def _close(self, t: Trade, exit_price: float, reason: str, idx: int) -> None:
        """Close remaining open quantity (full position or partial remainder)."""
        qty = t.quantity_remaining if t.partial_exits else t.quantity

        fee_exit = exit_price * qty * self.fee_rate
        t.fees += fee_exit

        pnl_this = (exit_price - t.entry_price) * qty if t.direction == "LONG" \
                   else (t.entry_price - exit_price) * qty

        t.pnl_gross = t.realized_gross + pnl_this
        t.pnl_net   = t.pnl_gross - t.fees
        t.pnl_pct   = t.pnl_gross / (t.entry_price * t.quantity) * 100
        t.exit_price = exit_price
        t.close_reason = reason
        t.exit_idx = idx
        t.duration_candles = idx - t.entry_idx

        self.capital   += t.pnl_net
        self.daily_pnl += t.pnl_net
        self.trades.append(t)
        self.open_trade = None

        is_win = t.pnl_net > 0
        self.consecutive_losses = 0 if is_win else self.consecutive_losses + 1
        if self.consecutive_losses >= 2:
            self.cooldown_remaining = self.cooldown_after_losses

    def _force_close(self, t: Trade, price: float, idx: int) -> None:
        self._close(t, price, "END_OF_DATA", idx)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    std = float(arr.std())
    return float(arr.mean() / std * math.sqrt(252 * 24)) if std > 0 else 0.0


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0.0
        mdd = max(mdd, dd)
    return mdd


def print_report(engine: BacktestEngine, months: int = 0) -> None:
    trades = engine.trades
    equity = engine.equity_curve

    sep  = "=" * 60
    line = "-" * 60

    if not trades:
        print("\n  No trades executed.")
        return

    wins   = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gp = sum(t.pnl_gross for t in wins)   if wins   else 0.0
    gl = abs(sum(t.pnl_gross for t in losses)) if losses else 1.0
    total_ret = (engine.capital - engine.initial_capital) / engine.initial_capital * 100
    rets_pct  = [t.pnl_pct for t in trades]

    print(f"\n{sep}")
    print(f"  BACKTEST RESULTS  BTC/USDT")
    print(sep)
    print(f"  Period          : {len(equity)} candles")
    print(f"  Initial capital : ${engine.initial_capital:>10,.2f}")
    print(f"  Final capital   : ${engine.capital:>10,.2f}  ({total_ret:+.2f}%)")
    print(line)
    print(f"  Total trades    : {len(trades)}")
    print(f"  Wins / Losses   : {len(wins)} / {len(losses)}")
    print(f"  Win rate        : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Profit factor   : {gp/gl:.3f}")
    print(f"  Sharpe (ann.)   : {_sharpe(rets_pct):.3f}")
    print(f"  Max drawdown    : {_max_drawdown(equity):.2f}%")
    print(f"  Avg duration(h) : {sum(t.duration_candles for t in trades)/len(trades):.1f}")
    print(f"  Total fees      : ${sum(t.fees for t in trades):,.2f}")
    print(line)
    best  = max(trades, key=lambda t: t.pnl_net)
    worst = min(trades, key=lambda t: t.pnl_net)
    print(f"  Best trade      : +${best.pnl_net:,.2f}  ({best.strategy}, {best.close_reason})")
    print(f"  Worst trade     :  -${abs(worst.pnl_net):,.2f}  ({worst.strategy}, {worst.close_reason})")
    print(line)

    print("\n  Per-strategy breakdown:")
    by_strat: dict[str, list[Trade]] = {}
    for t in trades:
        by_strat.setdefault(t.strategy, []).append(t)
    for name, st in sorted(by_strat.items()):
        sw = [t for t in st if t.pnl_net > 0]
        sg = sum(t.pnl_gross for t in sw) if sw else 0
        sl_g = abs(sum(t.pnl_gross for t in st if t.pnl_net <= 0)) or 1
        pnl  = sum(t.pnl_net for t in st)
        print(f"    {name:<24} trades={len(st):3d}  WR={len(sw)/len(st)*100:4.1f}%"
              f"  PF={sg/sl_g:.3f}  PnL=${pnl:+,.2f}")

    print("\n  Close reason breakdown:")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.close_reason] = reasons.get(t.close_reason, 0) + 1
    for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:<22} {cnt:3d}  ({cnt/len(trades)*100:.1f}%)")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="BTC/USDT Multi-Timeframe Backtest")
    parser.add_argument("--months",     type=int,   default=12,
                        help="Months of data to fetch (default 12; 36 for date-windowed)")
    parser.add_argument("--capital",    type=float, default=10_000.0)
    parser.add_argument("--risk-pct",   type=float, default=1.0)
    parser.add_argument("--strategy",   type=str,   default=None,
                        help="Run single strategy: breakout | cascade_reversal | weekly_momentum "
                             "| trend_following | mean_reversion")
    parser.add_argument("--start-date", type=str,   default=None)
    parser.add_argument("--end-date",   type=str,   default=None)
    parser.add_argument("--fee-rate",   type=float, default=None)
    parser.add_argument("--slippage",   type=float, default=None)
    parser.add_argument("--no-strategy-exits", action="store_true", default=False,
                        help="Disable strategy exit signals (SL/TP only)")
    args = parser.parse_args()

    months = args.months
    if args.start_date:
        months = max(months, 36)

    print(f"\n{'='*60}")
    print(f"  BACKTEST  BTC/USDT  {months} months  capital=${args.capital:,.0f}")
    if args.start_date or args.end_date:
        print(f"  Window: {args.start_date or 'start'} -> {args.end_date or 'end'}")
    if args.strategy:
        print(f"  Strategy: {args.strategy}")
    if args.fee_rate or args.slippage:
        print(f"  Costs: fee={args.fee_rate or FEE_RATE:.4f}  slippage={args.slippage or SLIPPAGE:.4f}")
    print(f"{'='*60}")
    print("  Fetching data ...")

    df_1h = fetch_ohlcv("BTC/USDT", "1h", months)
    df_4h = fetch_ohlcv("BTC/USDT", "4h", months)
    df_1d = fetch_ohlcv("BTC/USDT", "1d", months)

    # Derive weekly from daily resample (avoids CCXT weekly alignment issues)
    df_1w = df_1d.resample("W").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()

    print(f"  1H: {len(df_1h)}  4H: {len(df_4h)}  1D: {len(df_1d)}  1W: {len(df_1w)}")

    import yaml
    try:
        cfg_path = Path("trading_bot/config.yaml")
        cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        cfg = {}

    active   = cfg.get("strategy", {}).get("active", ["breakout", "cascade_reversal", "weekly_momentum"])
    if args.strategy:
        active = [args.strategy]
    min_conf = cfg.get("strategy", {}).get("min_confidence_score", 0.65)
    cooldown = cfg.get("strategy", {}).get("cooldown_after_losses", 2)
    trailing_enabled = cfg.get("execution", {}).get("trailing_stop_enabled", False)
    trailing_trigger = cfg.get("execution", {}).get("trailing_stop_trigger_atr", 1.5)
    max_pos_pct = cfg.get("capital", {}).get("max_position_size_pct", 15.0)

    engine = BacktestEngine(
        initial_capital=args.capital,
        risk_pct=args.risk_pct,
        min_confidence=min_conf,
        cooldown_after_losses=cooldown,
        trailing_stop_enabled=trailing_enabled,
        trailing_stop_trigger_atr=trailing_trigger,
        max_pos_size_pct=max_pos_pct,
        fee_rate=args.fee_rate if args.fee_rate is not None else FEE_RATE,
        slippage_entry=args.slippage if args.slippage is not None else SLIPPAGE,
        use_strategy_exits=not args.no_strategy_exits,
    )
    engine.run(df_1h, df_4h, df_1d, df_1w,
               active_strategies=active,
               start_date=args.start_date, end_date=args.end_date)
    print_report(engine, months)


if __name__ == "__main__":
    main()
