"""
Regime isolation test.

Loads 36 months of BTC/USDT 1H data and runs each strategy in isolation
across three 1-year windows to identify which strategy has edge in which regime.

Windows (approximate):
  Bull  : 2023-04-18 → 2024-04-18  (market +159%)
  Mid   : 2024-04-18 → 2025-04-18
  Recent: 2025-04-18 → 2026-04-18

Strategies tested:
  trend_only     — TrendFollowingStrategy alone
  reversion_only — MeanReversionStrategy alone
  combined       — both (current config)

Usage:
    python -m trading_bot.regime_runner
    python -m trading_bot.regime_runner --fee-rate 0.002 --slippage 0.0015  # stress
"""
from __future__ import annotations
import argparse

from trading_bot.backtest import (
    BacktestEngine,
    FEE_RATE,
    SLIPPAGE,
    fetch_ohlcv,
    print_report,
)

WINDOWS = [
    ("Bull  (Apr23-Apr24)", "2023-04-18", "2024-04-18"),
    ("Mid   (Apr24-Apr25)", "2024-04-18", "2025-04-18"),
    ("Recent(Apr25-Apr26)", "2025-04-18", "2026-04-18"),
]

STRATEGIES: list[tuple[str, list[str]]] = [
    ("trend_only",     ["trend_following"]),
    ("reversion_only", ["mean_reversion"]),
    ("combined",       ["trend_following", "mean_reversion"]),
]


def _run_one(
    df_1h,
    df_4h,
    active: list[str],
    start: str,
    end: str,
    fee_rate: float,
    slippage: float,
) -> dict:
    engine = BacktestEngine(
        initial_capital=10_000.0,
        risk_pct=1.0,
        min_confidence=0.65,
        cooldown_after_losses=3,
        trailing_stop_enabled=False,
        fee_rate=fee_rate,
        slippage_entry=slippage,
    )
    engine.run(df_1h, df_4h, active_strategies=active, start_date=start, end_date=end)

    trades = engine.trades
    if not trades:
        return {
            "trades": 0, "wr": 0.0, "pf": 0.0,
            "ret": (engine.capital - engine.initial_capital) / engine.initial_capital * 100,
            "mdd": 0.0,
        }

    wins  = [t for t in trades if t.pnl_net > 0]
    gp = sum(t.pnl_gross for t in wins) if wins else 0.0
    gl = abs(sum(t.pnl_gross for t in trades if t.pnl_net <= 0)) or 1.0
    ret = (engine.capital - engine.initial_capital) / engine.initial_capital * 100

    equity = engine.equity_curve
    peak, mdd = (equity[0] if equity else 0), 0.0
    for v in equity:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0.0
        mdd = max(mdd, dd)

    return {
        "trades": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pf": gp / gl,
        "ret": ret,
        "mdd": mdd,
    }


def _fmt(r: dict) -> str:
    return (f"ret={r['ret']:+6.1f}%  WR={r['wr']:4.1f}%  "
            f"PF={r['pf']:.2f}  T={r['trades']:3d}  MDD={r['mdd']:.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Regime isolation test")
    parser.add_argument("--fee-rate",  type=float, default=FEE_RATE)
    parser.add_argument("--slippage",  type=float, default=SLIPPAGE)
    parser.add_argument("--verbose",   action="store_true",
                        help="Print full per-run report after the summary table")
    args = parser.parse_args()

    stress = args.fee_rate != FEE_RATE or args.slippage != SLIPPAGE
    tag = f"STRESS fee={args.fee_rate:.4f} slip={args.slippage:.4f}" if stress else "baseline"

    print(f"\n{'='*78}")
    print(f"  REGIME ISOLATION TEST  BTC/USDT 1H  [{tag}]")
    print(f"{'='*78}")
    print("  Loading 36-month dataset ...")

    df_1h = fetch_ohlcv("BTC/USDT", "1h", 36)
    df_4h = fetch_ohlcv("BTC/USDT", "4h", 36)
    print(f"  1H: {len(df_1h)} candles | 4H: {len(df_4h)} candles\n")

    col_w = 62
    hdr = f"  {'Strategy':<18}" + "".join(f"  {w[0]:<{col_w}}" for w in WINDOWS)
    print(hdr)
    print("  " + "-" * (18 + (col_w + 2) * len(WINDOWS)))

    all_results: dict[str, dict[str, dict]] = {}
    for strat_name, active in STRATEGIES:
        row = f"  {strat_name:<18}"
        all_results[strat_name] = {}
        for win_name, start, end in WINDOWS:
            r = _run_one(df_1h, df_4h, active, start, end, args.fee_rate, args.slippage)
            all_results[strat_name][win_name] = r
            row += f"  {_fmt(r):<{col_w}}"
        print(row)

    print(f"\n{'='*78}")

    # Highlight best performer per window
    print("\n  Best per window (by return):")
    for win_name, _, _ in WINDOWS:
        best_strat = max(STRATEGIES, key=lambda s: all_results[s[0]][win_name]["ret"])
        r = all_results[best_strat[0]][win_name]
        print(f"    {win_name}: {best_strat[0]:<18}  ret={r['ret']:+.1f}%  "
              f"WR={r['wr']:.1f}%  PF={r['pf']:.2f}")

    # Highlight any positive PF across all windows
    print("\n  Strategies with PF > 1.0 in ALL windows:")
    found = False
    for strat_name, _ in STRATEGIES:
        if all(all_results[strat_name][w[0]]["pf"] > 1.0 for w in WINDOWS):
            print(f"    [OK] {strat_name}")
            found = True
    if not found:
        print("    (none)")

    print(f"\n{'='*78}\n")

    if args.verbose:
        print("\n  === FULL PER-RUN REPORTS ===")
        for strat_name, active in STRATEGIES:
            for win_name, start, end in WINDOWS:
                print(f"\n  --- {strat_name} | {win_name} ---")
                engine = BacktestEngine(
                    initial_capital=10_000.0,
                    risk_pct=1.0,
                    min_confidence=0.65,
                    cooldown_after_losses=3,
                    trailing_stop_enabled=False,
                    fee_rate=args.fee_rate,
                    slippage_entry=args.slippage,
                )
                engine.run(df_1h, df_4h, active_strategies=active,
                           start_date=start, end_date=end)
                print_report(engine)


if __name__ == "__main__":
    main()
