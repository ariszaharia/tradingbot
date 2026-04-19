"""
Stress-test runner.

Runs each strategy under progressively worse cost assumptions to measure
how much execution cost degrades performance and whether any edge survives.

Cost scenarios:
  baseline   : fee=0.10%/side  slippage=0.05%
  moderate   : fee=0.15%/side  slippage=0.10%
  conservative: fee=0.20%/side slippage=0.15%  (worst realistic Binance taker)

Usage:
    python -m trading_bot.stress_runner
    python -m trading_bot.stress_runner --strategy trend_following
"""
from __future__ import annotations
import argparse

from trading_bot.backtest import BacktestEngine, FEE_RATE, SLIPPAGE, fetch_ohlcv

COST_SCENARIOS = [
    ("baseline    ", 0.001,  0.0005),
    ("moderate    ", 0.0015, 0.001),
    ("conservative", 0.002,  0.0015),
]

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


def _run(df_1h, df_4h, active, start, end, fee_rate, slippage):
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
        return 0.0, 0, 0.0
    ret = (engine.capital - 10_000.0) / 10_000.0 * 100
    wins = sum(1 for t in trades if t.pnl_net > 0)
    gp = sum(t.pnl_gross for t in trades if t.pnl_net > 0)
    gl = abs(sum(t.pnl_gross for t in trades if t.pnl_net <= 0)) or 1.0
    return ret, len(trades), gp / gl


def main() -> None:
    parser = argparse.ArgumentParser(description="Stress test execution costs")
    parser.add_argument("--strategy", type=str, default=None)
    args = parser.parse_args()

    strategies = STRATEGIES
    if args.strategy:
        strategies = [(args.strategy, [args.strategy])]

    print(f"\n{'='*80}")
    print("  STRESS TEST - execution cost sensitivity  BTC/USDT 1H")
    print(f"{'='*80}")
    print("  Loading 36-month dataset ...")

    df_1h = fetch_ohlcv("BTC/USDT", "1h", 36)
    df_4h = fetch_ohlcv("BTC/USDT", "4h", 36)

    for strat_name, active in strategies:
        print(f"\n  Strategy: {strat_name}")
        print(f"  {'Scenario':<16}  {'Window':<24}  {'ret':>7}  {'T':>4}  {'PF':>5}")
        print("  " + "-" * 64)
        for cost_name, fee, slip in COST_SCENARIOS:
            for win_name, start, end in WINDOWS:
                ret, t, pf = _run(df_1h, df_4h, active, start, end, fee, slip)
                print(f"  {cost_name}  {win_name}  {ret:+7.1f}%  {t:>4d}  {pf:5.2f}")
            print()

    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
