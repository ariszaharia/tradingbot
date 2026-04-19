"""
Walk-forward validation.

Slides a fixed window of training + test data across the full history.
For each fold:
  - IS  (in-sample):  the first IS_MONTHS months — used only for context
  - OOS (out-of-sample): the next OOS_MONTHS months — measured performance

Results across OOS folds are the only valid performance estimate.
Never select parameters based on a single IS window.

Usage:
    python -m trading_bot.walkforward_runner
    python -m trading_bot.walkforward_runner --is-months 6 --oos-months 3
    python -m trading_bot.walkforward_runner --strategy trend_following
"""
from __future__ import annotations
import argparse
from datetime import date, timedelta

import pandas as pd

from trading_bot.backtest import (
    BacktestEngine,
    FEE_RATE,
    SLIPPAGE,
    fetch_ohlcv,
)

IS_MONTHS  = 6   # in-sample window (context only)
OOS_MONTHS = 3   # out-of-sample window (what we measure)
STEP_MONTHS = OOS_MONTHS  # walk forward by one OOS window at a time


def _months_offset(start: date, months: int) -> date:
    m = start.month - 1 + months
    year = start.year + m // 12
    month = m % 12 + 1
    return date(year, month, start.day)


def _run_window(df_1h, df_4h, active, start_str, end_str, fee_rate, slippage):
    engine = BacktestEngine(
        initial_capital=10_000.0,
        risk_pct=1.0,
        min_confidence=0.65,
        cooldown_after_losses=3,
        trailing_stop_enabled=False,
        fee_rate=fee_rate,
        slippage_entry=slippage,
    )
    engine.run(df_1h, df_4h, active_strategies=active,
               start_date=start_str, end_date=end_str)

    trades = engine.trades
    if not trades:
        return {"trades": 0, "ret": 0.0, "wr": 0.0, "pf": 0.0, "mdd": 0.0}

    wins = [t for t in trades if t.pnl_net > 0]
    gp = sum(t.pnl_gross for t in wins) if wins else 0.0
    gl = abs(sum(t.pnl_gross for t in trades if t.pnl_net <= 0)) or 1.0
    ret = (engine.capital - 10_000.0) / 10_000.0 * 100

    equity = engine.equity_curve
    peak, mdd = (equity[0] if equity else 0), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)

    return {
        "trades": len(trades),
        "ret": ret,
        "wr": len(wins) / len(trades) * 100,
        "pf": gp / gl,
        "mdd": mdd,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward validation")
    parser.add_argument("--is-months",  type=int, default=IS_MONTHS,
                        help="In-sample window length in months")
    parser.add_argument("--oos-months", type=int, default=OOS_MONTHS,
                        help="Out-of-sample window length in months")
    parser.add_argument("--strategy",   type=str, default=None,
                        help="Limit to one strategy: trend_following | mean_reversion")
    parser.add_argument("--fee-rate",   type=float, default=FEE_RATE)
    parser.add_argument("--slippage",   type=float, default=SLIPPAGE)
    args = parser.parse_args()

    strategies: list[tuple[str, list[str]]] = [
        ("trend_only",     ["trend_following"]),
        ("reversion_only", ["mean_reversion"]),
        ("combined",       ["trend_following", "mean_reversion"]),
    ]
    if args.strategy:
        strategies = [(args.strategy, [args.strategy])]

    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD  BTC/USDT 1H  "
          f"IS={args.is_months}m  OOS={args.oos_months}m")
    print(f"{'='*70}")
    print("  Loading 36-month dataset ...")

    df_1h = fetch_ohlcv("BTC/USDT", "1h", 36)
    df_4h = fetch_ohlcv("BTC/USDT", "4h", 36)

    # Full date range available in dataset
    first_date = df_1h.index[0].date()
    last_date  = df_1h.index[-1].date()

    # Build OOS windows — warmup needs IS months before OOS start
    oos_windows: list[tuple[date, date]] = []
    oos_start = _months_offset(first_date, args.is_months)
    while True:
        oos_end = _months_offset(oos_start, args.oos_months)
        if oos_end > last_date:
            break
        oos_windows.append((oos_start, oos_end))
        oos_start = _months_offset(oos_start, args.oos_months)

    if not oos_windows:
        print("  Not enough data for walk-forward. Need IS + OOS months of history.")
        return

    print(f"  {len(oos_windows)} OOS folds  "
          f"({oos_windows[0][0]} -> {oos_windows[-1][1]})\n")

    for strat_name, active in strategies:
        print(f"  Strategy: {strat_name}")
        print(f"  {'OOS window':<28}  {'ret':>7}  {'WR':>6}  {'PF':>5}  "
              f"{'trades':>6}  {'MDD':>6}")
        print("  " + "-" * 65)

        fold_results = []
        for oos_s, oos_e in oos_windows:
            r = _run_window(
                df_1h, df_4h, active,
                str(oos_s), str(oos_e),
                args.fee_rate, args.slippage,
            )
            fold_results.append(r)
            ret_str = f"{r['ret']:+.1f}%"
            print(f"  {str(oos_s)+'->'+str(oos_e):<28}  "
                  f"{ret_str:>7}  {r['wr']:5.1f}%  {r['pf']:5.2f}  "
                  f"{r['trades']:>6}  {r['mdd']:5.1f}%")

        # Summary across folds
        pos_rets = sum(1 for r in fold_results if r["ret"] > 0)
        avg_ret  = sum(r["ret"] for r in fold_results) / len(fold_results)
        avg_pf   = sum(r["pf"]  for r in fold_results) / len(fold_results)
        avg_wr   = sum(r["wr"]  for r in fold_results) / len(fold_results)
        pass_all = (
            all(r["ret"] > 0  for r in fold_results)
            and all(r["pf"] > 1.0 for r in fold_results)
        )
        verdict = "PASS [OK]" if pass_all else "FAIL [X]"
        print(f"\n  Summary: avg ret={avg_ret:+.1f}%  avg WR={avg_wr:.1f}%  "
              f"avg PF={avg_pf:.2f}  "
              f"positive folds={pos_rets}/{len(fold_results)}  [{verdict}]")
        print()

    print(f"{'='*70}\n")
    print("  Acceptance criteria:")
    print("    • Positive return in ALL folds")
    print("    • Profit factor > 1.0 in ALL folds")
    print("    • Stable across bull/sideways/bear windows")


if __name__ == "__main__":
    main()
