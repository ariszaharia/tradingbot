"""
Backtest engine - 6 months of BTC/USDT 1H historical data.

Usage:
    python -m trading_bot.backtest [--months 6] [--capital 10000]

Data is fetched from Binance via CCXT and cached to disk so subsequent
runs are instant.  No real orders are placed.

Simulation rules:
  - Entry at close of signal candle (avoids look-ahead)
  - SL / TP checked against next candle high / low
  - Taker fee: 0.1% on entry and exit
  - Slippage: fixed 0.05% on entry (market order)
  - One position at a time (single-symbol)
  - All 7 Risk Agent rules enforced per candle
  - Cooldown of 2 candles after 2 consecutive losses
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
class Trade:
    signal_id: str
    strategy: str
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    pnl_gross: float
    pnl_net: float
    pnl_pct: float
    fees: float
    close_reason: str
    entry_idx: int
    exit_idx: int
    duration_candles: int


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
    max_pos_size_pct: float = 20.0
    min_confidence: float = 0.6
    cooldown_after_losses: int = 2

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

    def run(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> None:
        from trading_bot.strategies.trend_following import TrendFollowingStrategy
        from trading_bot.strategies.mean_reversion import MeanReversionStrategy
        from trading_bot.models.data_snapshot import DataSnapshot
        from trading_bot.models.trading_signal import Direction

        strategies = [TrendFollowingStrategy({}), MeanReversionStrategy({})]
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
        current_day = df_1h.index[warmup].date()
        self.daily_start_capital = self.capital

        print(f"\n  Running on {n - warmup} candles "
              f"({df_1h.index[warmup].date()} -> {df_1h.index[-1].date()}) ...")

        for i in range(warmup, n):
            ts = df_1h.index[i]

            if ts.date() != current_day:
                current_day = ts.date()
                self.daily_pnl = 0.0
                self.daily_start_capital = self.capital

            if self.cooldown_remaining > 0:
                self.cooldown_remaining -= 1

            window = df_1h.iloc[i - warmup: i + 1]
            opens  = window["open"].values.astype(float)
            highs  = window["high"].values.astype(float)
            lows   = window["low"].values.astype(float)
            closes = window["close"].values.astype(float)
            vols   = window["volume"].values.astype(float)

            indicators = compute_all(opens, highs, lows, closes, vols, ind_cfg)

            htf_mask = df_4h.index <= ts
            if htf_mask.sum() >= 50:
                htf_win = df_4h[htf_mask].iloc[-210:]
                htf_ind = compute_all(
                    htf_win["open"].values.astype(float),
                    htf_win["high"].values.astype(float),
                    htf_win["low"].values.astype(float),
                    htf_win["close"].values.astype(float),
                    htf_win["volume"].values.astype(float),
                    ind_cfg,
                )
            else:
                htf_ind = {}

            price = float(closes[-1])

            # Check open position SL / TP on this candle's high/low
            if self.open_trade is not None:
                exited = self._check_exit(
                    self.open_trade,
                    float(highs[-1]), float(lows[-1]),
                    price, i,
                )
                if exited:
                    self.equity_curve.append(self.capital)
                    continue

            snapshot = DataSnapshot(
                symbol="BTC/USDT",
                price=price,
                bid=price * 0.9999,
                ask=price * 1.0001,
                spread_pct=0.01,
                ohlcv={},
                indicators=indicators,
                htf_indicators=htf_ind,
                current_position_direction=(
                    self.open_trade.direction if self.open_trade else None
                ),
            )

            if self.open_trade is None:
                best = None
                for strat in strategies:
                    sig = strat.evaluate(snapshot)
                    if sig.direction in (Direction.FLAT, Direction.EXIT):
                        continue
                    if best is None or sig.confidence_score > best.confidence_score:
                        best = sig

                if best and self._risk_ok(best):
                    self._enter(best, i, price)

            self.equity_curve.append(self.capital)

        if self.open_trade is not None:
            self._force_close(self.open_trade, float(df_1h["close"].iloc[-1]), n - 1)

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

    def _enter(self, signal, idx: int, price: float) -> None:
        from trading_bot.utils.risk_calculator import calc_position_size

        entry = price * (1 + SLIPPAGE) if signal.direction.value == "LONG" \
                else price * (1 - SLIPPAGE)
        try:
            risk_units, _ = calc_position_size(
                self.capital, self.risk_pct, entry, signal.suggested_stop_loss
            )
        except ValueError:
            return
        # Cap to max_pos_size_pct so we never over-leverage
        max_units = (self.capital * self.max_pos_size_pct / 100) / entry
        units = min(risk_units, max_units)
        if units <= 0:
            return

        fee = entry * units * FEE_RATE
        self.capital -= fee
        self.daily_pnl -= fee
        self.trade_counter += 1

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
            fees=fee,
            close_reason="",
            entry_idx=idx,
            exit_idx=0,
            duration_candles=0,
        )

    # --- Exit ----------------------------------------------------------------

    def _check_exit(self, t: Trade, high: float, low: float, close: float, idx: int) -> bool:
        hit_sl = (low <= t.stop_loss)  if t.direction == "LONG" else (high >= t.stop_loss)
        hit_tp = (high >= t.take_profit) if t.direction == "LONG" else (low <= t.take_profit)

        if hit_tp and hit_sl:
            hit_sl = False  # assume TP hit first when both on same candle

        if hit_tp:
            self._close(t, t.take_profit, "TAKE_PROFIT", idx)
            return True
        if hit_sl:
            self._close(t, t.stop_loss, "STOP_LOSS", idx)
            return True
        return False

    def _close(self, t: Trade, exit_price: float, reason: str, idx: int) -> None:
        fee_exit = exit_price * t.quantity * FEE_RATE
        t.fees += fee_exit

        t.pnl_gross = (exit_price - t.entry_price) * t.quantity \
                      if t.direction == "LONG" \
                      else (t.entry_price - exit_price) * t.quantity
        t.pnl_net = t.pnl_gross - t.fees
        t.pnl_pct = t.pnl_gross / (t.entry_price * t.quantity) * 100
        t.exit_price = exit_price
        t.close_reason = reason
        t.exit_idx = idx
        t.duration_candles = idx - t.entry_idx

        self.capital  += t.pnl_net
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


def print_report(engine: BacktestEngine, months: int) -> None:
    trades = engine.trades
    equity = engine.equity_curve

    sep = "=" * 60
    line = "-" * 60

    if not trades:
        print("\n  No trades executed.")
        return

    wins   = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gp = sum(t.pnl_gross for t in wins)   if wins   else 0.0
    gl = abs(sum(t.pnl_gross for t in losses)) if losses else 1.0
    total_ret = (engine.capital - engine.initial_capital) / engine.initial_capital * 100
    rets_pct = [t.pnl_pct for t in trades]

    print(f"\n{sep}")
    print(f"  BACKTEST RESULTS  BTC/USDT 1H  ({months} months)")
    print(sep)
    print(f"  Period          : {engine.equity_curve and len(engine.equity_curve)} candles")
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
        sl = abs(sum(t.pnl_gross for t in st if t.pnl_net <= 0)) or 1
        pnl = sum(t.pnl_net for t in st)
        print(f"    {name:<24} trades={len(st):3d}  WR={len(sw)/len(st)*100:4.1f}%"
              f"  PF={sg/sl:.3f}  PnL=${pnl:+,.2f}")

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
    parser = argparse.ArgumentParser(description="BTC/USDT Backtest")
    parser.add_argument("--months",   type=int,   default=6)
    parser.add_argument("--capital",  type=float, default=10_000.0)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  BACKTEST  BTC/USDT  {args.months} months  capital=${args.capital:,.0f}")
    print(f"{'='*60}")
    print("  Fetching data ...")

    df_1h = fetch_ohlcv("BTC/USDT", "1h",  args.months)
    df_4h = fetch_ohlcv("BTC/USDT", "4h",  args.months)
    print(f"  1H candles: {len(df_1h)}  |  4H candles: {len(df_4h)}")

    engine = BacktestEngine(
        initial_capital=args.capital,
        risk_pct=args.risk_pct,
    )
    engine.run(df_1h, df_4h)
    print_report(engine, args.months)


if __name__ == "__main__":
    main()
