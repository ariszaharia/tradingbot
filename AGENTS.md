# Trading Bot Handoff Notes

## Current status (2026-04-19 end of session)

ALL THREE REGIME WINDOWS ARE NOW POSITIVE for the first time.
The strategy is not yet walk-forward validated (4/9 OOS folds positive),
but the per-window metrics have crossed the PF > 1.0 threshold in all regimes.

## Verified results — current code

### Per-regime (trend_following only, exits disabled):

| Window            | Return | WR   | PF   | Trades | MDD   |
|-------------------|--------|------|------|--------|-------|
| Bull  Apr23-Apr24 | +1.8%  | 37%  | 1.36 | 51     | 3.9%  |
| Mid   Apr24-Apr25 | +1.5%  | 39%  | 1.28 | 49     | 2.4%  |
| Recent Apr25-Apr26| +3.3%  | 41%  | 1.47 | 58     | 3.3%  |

### Standard backtests:
- 6-month: +0.66%, WR=36.8%, PF=1.23, MDD=3.64%
- 12-month: +0.88%, WR=43.0%, PF=1.36, MDD=2.73%

### Walk-forward (IS=6m, OOS=3m, 9 folds) — run with exits=False but BEFORE MACD gate:
- trend_only: avg ret=-0.0%, avg PF=1.20, positive folds=4/9
- Problem fold: Nov 2023-Feb 2024 (ret=-3.3%, PF=0.48)
- Walk-forward has NOT been re-run with the final MACD gate improvement yet.

### Stress test — run on OLD code (pre-improvements, exits still enabled):
- Results are stale and do not reflect current strategy. Must rerun.

## What changed this session

### Bug fixes
- Cross-strategy EXIT bug: combined mode was using MeanReversion exits to close
  TrendFollowing positions and vice versa, inflating trade count and collapsing WR.
  Fixed: only the strategy that opened a position can emit strategy exits.
- Strategy exits (stall/structural) disabled by default: confirmed net-negative across
  all windows. SL/TP are now the sole exit mechanism.

### Strategy improvements (trend_following.py)
- SL: 1.5 ATR -> 2.0 ATR (wider room to breathe on pullbacks)
- TP: 4.0 ATR -> 5.0 ATR (R:R = 2.5:1, breakeven WR = 28.6%)
- Added MODE 2 — momentum continuation (ADX > 35):
  Enters when EMA9>EMA21>EMA50>EMA200, price between EMA21 and EMA9, bullish candle,
  RSI14 in [45,72]. Captures sustained trends that don't pull back to EMA21.
- Pullback mode tightened:
  - Close condition: close > EMA21 -> close > EMA9 (strong bounce confirmation)
  - Added MACD hist > 0 as hard gate (eliminates fading-momentum pullback entries)
  - Pullback zone kept at 0.3% of EMA21
  - Added DI+ > DI- directional gate

### Strategy improvements (mean_reversion.py)
- RSI7 thresholds relaxed: <20 -> <25 and >80 -> >75

### Infrastructure added
- backtest.py: --start-date, --end-date, --strategy, --fee-rate, --slippage,
  --use-strategy-exits CLI args; use_strategy_exits flag in BacktestEngine
- regime_runner.py: automated 3-strategy x 3-window matrix
- walkforward_runner.py: 9-fold OOS validation (IS=6m, OOS=3m)
- stress_runner.py: cost sensitivity under baseline / moderate / conservative fees

## Key learnings this session

- Combined mode is worse than trend-only in most windows (cross-strategy exit bug was
  a major reason; retest after fix is pending).
- Strategy stall/structural exits are net-negative: they cut temporary dips before
  recovery, creating more losing trades than they prevent. SL/TP handles it better.
- Momentum mode (ADX > 35) is consistently the strongest mode across all windows.
- Pullback mode improved dramatically once close > EMA9 + MACD > 0 were required.
- Trailing stop (with 5 ATR TP) is counterproductive: exits at 1 ATR before full TP.

## Likely remaining root causes

- The Nov 2023 - Feb 2024 fold is deeply negative (PF=0.48) in walk-forward.
  This period likely had choppy, trendless BTC behavior. A stronger regime gate
  might skip this period entirely.
- PF > 1.0 in all 3 windows but returns are small (+1-3% per year).
  Fees consume most of the gross edge. Need higher PF or fewer trades with larger moves.

## Priority next experiments

1. Re-run stress test with current code — verify edge survives moderate costs.
2. Re-run walk-forward with current code — check if MACD gate fix improves fold count.
3. Investigate bad fold (Nov 2023-Feb 2024): what market conditions caused PF=0.48?
4. Test combined mode with cross-strategy exit fix in place.
5. Consider volume filter for momentum mode (vol > vol_sma * 1.1 as hard gate).

## Acceptance criteria (unchanged)

Only consider a setup viable if it passes ALL of these:
- Positive out-of-sample return in multiple windows.
- Profit factor consistently above 1.0.
- Controlled drawdown relative to baseline.
- Stability across bull/sideways/bear windows.
- Performance remains acceptable under conservative cost assumptions.

## Short handoff for Claude

The bot now shows positive returns (+1.8%, +1.5%, +3.3%) across all three regime windows
for the first time, using trend_following with momentum mode (ADX>35) as the main driver
and a tightened pullback mode (close > EMA9 + MACD > 0). Strategy exits are disabled.
Walk-forward is still failing (4/9 folds positive) — the latest MACD gate improvement
has NOT yet been tested in walk-forward. Run the stress test and walk-forward first,
then investigate the Nov 2023-Feb 2024 bad fold.
