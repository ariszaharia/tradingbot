# Trading Bot Strategy Redesign — Opus Analysis

## Diagnosis: Why the Current Approach Fails

### The Numbers Don't Lie

Your best result across three 1-year windows:

| Window | Return | WR | PF | Trades | MDD |
|--------|--------|----|----|--------|-----|
| Bull (Apr23-24) | +1.8% | 37% | 1.36 | 51 | 3.9% |
| Mid (Apr24-25) | +1.5% | 39% | 1.28 | 49 | 2.4% |
| Recent (Apr25-26) | +3.3% | 41% | 1.47 | 58 | 3.3% |

Walk-forward: **4/9 folds positive** — this is a failing grade.

Let me be direct: **+1.5% annual return with 3.9% max drawdown is not a tradable edge.** BTC's risk-free staking/lending yield exceeds this. The strategy is consuming your time and compute for returns you could beat by doing nothing.

### Root Causes (Not Symptoms)

**1. The indicators are commoditized.** EMA crossovers, RSI, Bollinger Bands, MACD — these are the first things every retail algo trader implements. The "edge" from these indicators was arbitraged away years ago in crypto. When thousands of bots use identical signals, the signals stop working because everyone is entering and exiting at the same points.

**2. You're trend-following on 1H in a market that trends on Daily.** BTC doesn't trend meaningfully on 1-hour bars. It has long consolidation phases broken by explosive moves that happen in minutes. Your 1H trend-following strategy is designed for a market microstructure that doesn't exist in BTC.

**3. Mean reversion on Bollinger Bands is a volatility-selling strategy in disguise.** It works great 90% of the time and then gives back all profits in a single event. Your session notes already show this — it's net negative when combined with trend-following.

**4. The parameter space is being overfit.** Moving SL from 1.5→2.0 ATR, TP from 4.0→5.0 ATR, adding MACD gates, tightening pullback conditions — each tweak improves in-sample but the walk-forward keeps failing. This is the classic curve-fitting spiral.

**5. Strategy exits were disabled because they were net-negative.** This is a red flag, not a solution. It means your entry logic has no genuine ability to read when a trade should close. You're relying entirely on fixed SL/TP, which means you're really running a coin flip with a 2.5:1 R:R that needs >28.6% hit rate to break even. You're barely clearing that bar.

---

## Strategic Framework: What Actually Works in Crypto

Before I propose specific strategies, here are the principles that should guide everything:

### Principle 1: Trade the Regime, Not the Indicator

BTC alternates between three regimes:
- **Trending** (~30% of the time): Strong directional moves, often triggered by macro events
- **Range-bound** (~50% of the time): Chop between support and resistance
- **Volatile/Event-driven** (~20% of the time): Liquidation cascades, exchange events, news

Your current system applies the same strategy regardless of regime. This is why walk-forward fails — different folds capture different regimes and the strategy only works in one of them.

### Principle 2: Fewer, Higher-Conviction Trades

You're taking ~50 trades per year with 37-41% win rate. The math is brutal:
- 50 trades × 0.1% fees × 2 (entry + exit) = 10% of capital eaten by fees annually
- You need to generate >10% gross just to break even after friction

**The fix: take 15-25 trades per year with 50%+ win rate.** Wider filters, stricter entry conditions, let most setups pass.

### Principle 3: Asymmetric Payoff Over Win Rate

Don't try to be right often. Try to be right big. The best crypto strategies lose 55-60% of the time but make 4-5x on winners what they lose on losers.

---

## Proposed Strategy Architecture

### STRATEGY 1: Regime-Gated Breakout (Primary)

**Concept:** Only trade when BTC is transitioning from low-volatility consolidation into a trending phase. This is the one repeatable pattern in BTC that hasn't been fully arbitraged — the "volatility compression before expansion" setup.

#### Regime Detection (Daily Timeframe)
```
CONSOLIDATION DETECTED when ALL of:
  - ATR(14) on Daily < ATR(50) on Daily  (volatility contracting)
  - Bollinger Band width(20,2) in bottom 20th percentile of last 100 days
  - ADX(14) on Daily < 20  (no trend)
  - Price contained within a range where (high - low) / low < 8% over last 14 days
```

#### Entry Trigger (4H Timeframe)
```
LONG BREAKOUT when consolidation detected AND:
  - 4H candle closes above the 14-day high (breakout)
  - Volume on breakout candle > 2.0 × Volume_SMA(20) on 4H
  - RSI(14) on 4H is between 55-70 (momentum, not overbought)
  - The breakout candle body is > 60% of total range (strong close, not a wick)
  
SHORT BREAKOUT when consolidation detected AND:
  - 4H candle closes below the 14-day low
  - Same volume, RSI (30-45), and body conditions as above (mirrored)
```

#### Why This Works
- **Few signals:** Consolidation-to-breakout happens 3-6 times per year in BTC. You'll take 10-20 trades annually.
- **Not crowded:** Most retail bots are chasing EMA crosses on 1H. This trades on Daily regime + 4H trigger — different timescale, different competition.
- **Genuine edge:** Volatility compression before expansion is a well-documented phenomenon in all markets. It's not an indicator pattern — it's market microstructure.
- **Favorable R:R:** Breakouts from tight ranges tend to move 2-5x the range width. A 4% consolidation range often yields a 10-15% move.

#### Exit Rules
```
STOP LOSS:
  - Place at the opposite end of the consolidation range
  - Never more than 3% from entry (hard cap)
  - If the consolidation range is > 3%, SKIP THE TRADE (R:R won't work)

TAKE PROFIT (scaled):
  - TP1: 1.5 × range width → close 40% of position
  - TP2: 3.0 × range width → close 40% of position
  - TP3: trailing stop at 1.5 × ATR(14) on Daily → remaining 20%

INVALIDATION EXIT:
  - If price re-enters the consolidation range within 8 hours of breakout → EXIT ALL
  - This is the "failed breakout" filter — the most important exit rule
```

#### Conviction Scoring
```
Base score: 0.5 (meets minimum conditions)
+0.1 if Weekly EMA(21) > EMA(50) (aligned with higher timeframe)
+0.1 if consolidation lasted > 21 days (tighter spring = bigger move)
+0.1 if volume on breakout is > 3x average (strong participation)
+0.1 if no recent failed breakout in last 14 days
+0.1 if BTC dominance trending in direction of trade

Minimum to trade: 0.7
```

---

### STRATEGY 2: Liquidation Cascade Reversal (Secondary)

**Concept:** When BTC drops sharply (>5% in <4 hours), it often triggers forced liquidations that push price below fair value. Fade the cascade after momentum exhausts.

#### Why This Works
BTC's leveraged derivatives market creates a unique dynamic: sharp moves trigger stop-losses and liquidations, which trigger more stops and liquidations. The cascade overshoots and snaps back. This is not classic "mean reversion" — it's a specific market microstructure phenomenon tied to derivative leverage.

#### Entry Conditions (1H Timeframe)
```
LONG REVERSAL when ALL:
  - Price has dropped > 5% in the last 4 hours
  - Current 1H RSI(14) < 22 (extreme oversold — tighter than standard)
  - Volume spike: current 1H volume > 3x the 20-period SMA
  - At least 2 of the last 4 1H candles have lower wicks > 50% of body
    (buyers stepping in, rejecting lower prices)
  - Price is within 2% of a significant support level:
    - Daily EMA(200), OR
    - Previous weekly low, OR
    - Psychological round number (e.g., 90000, 85000, 80000)
  - Spread < 0.2% (market not completely broken)
```

#### Exit Rules
```
STOP LOSS: 
  - 1.5% below the lowest wick in the cascade
  - Hard cap: never more than 2.5% from entry

TAKE PROFIT:
  - TP1: 50% Fibonacci retracement of the cascade move → close 50%
  - TP2: 78.6% Fibonacci retracement → close 30%
  - Trailing: 1.0 × ATR(14) on 1H for remaining 20%

TIME EXIT:
  - If < 50% of target reached in 12 hours → EXIT ALL
  - Reversals either work quickly or they don't
```

#### Risk Rules Specific to This Strategy
```
- Maximum 1 reversal trade per 7-day period (these setups cluster; only take the first)
- Position size capped at 0.5% risk (half normal) — these are inherently riskier
- Do NOT trade if the drop is news-driven (exchange hack, regulation): 
  check if anomaly_flag is set with reason containing keywords
- Do NOT trade if this is the third >5% drop in 30 days (trend is down, not a cascade)
```

---

### STRATEGY 3: Weekly Momentum Continuation (Tertiary)

**Concept:** When BTC establishes a clear weekly trend, enter on pullbacks to the 4H EMA(21) with the trend. This is the closest to your current trend-following but on a much higher timeframe with stricter filters.

#### Regime Gate (Weekly Timeframe)
```
WEEKLY UPTREND when ALL:
  - Weekly EMA(9) > EMA(21) > EMA(50)
  - Weekly close above EMA(9) for at least 2 consecutive weeks
  - Weekly ADX(14) > 25
  
WEEKLY DOWNTREND: mirror conditions
```

#### Entry Trigger (4H Timeframe)
```
LONG PULLBACK when weekly uptrend AND:
  - Price has pulled back to within 0.5% of 4H EMA(21)
  - 4H RSI(14) between 40-55 (pulled back but not collapsed)
  - The pullback candle that touches EMA(21) is followed by a bullish candle
    whose close is above the pullback candle's high (confirmation)
  - MACD histogram on 4H has turned positive (momentum resuming)
  - Volume on confirmation candle > Volume_SMA(20) × 1.3
```

#### Exit Rules
```
STOP LOSS:
  - Below 4H EMA(50) or 2.0 × ATR(14) on 4H, whichever is tighter
  - Hard cap: 2.5%

TAKE PROFIT:
  - TP1: Previous swing high → close 50%
  - Trailing: 1.5 × ATR(14) on 4H for remaining 50%

INVALIDATION:
  - If 4H EMA(21) crosses below EMA(50) → EXIT ALL regardless of P&L
  - If Weekly close below EMA(21) → EXIT ALL
```

---

## Implementation Changes to the Codebase

### 1. Add Regime Detector Module

Create `trading_bot/strategies/regime_detector.py`:

```python
@dataclass
class MarketRegime:
    regime: Literal["CONSOLIDATION", "TRENDING_UP", "TRENDING_DOWN", "VOLATILE"]
    confidence: float  # 0.0 - 1.0
    consolidation_range_high: float | None
    consolidation_range_low: float | None
    range_duration_days: int
    bb_width_percentile: float
    adx_value: float
    timestamp: int
```

This module runs on the DAILY timeframe only and feeds into Strategy 1.

### 2. Add Fibonacci / Support Level Calculator

Create `trading_bot/utils/levels.py`:

```python
def calculate_cascade_levels(
    cascade_high: float,
    cascade_low: float
) -> dict:
    """Returns fib retracement levels for a liquidation cascade."""
    
def find_support_levels(
    daily_data: DataFrame,
    ema_200: float
) -> list[float]:
    """Returns significant support levels near current price."""
```

### 3. Restructure Timeframes

Current: Primary 1H, confirmation 4H
Proposed:
- **Regime detection:** Daily + Weekly
- **Entry trigger:** 4H (Strategies 1 and 3)
- **Entry trigger:** 1H (Strategy 2 only)
- **Execution:** 5m (for precise entry timing)

Update `config.yaml`:
```yaml
trading:
  symbol: "BTC/USDT"
  exchange: "binance"
  mode: "paper"
  timeframes:
    regime: ["1d", "1w"]
    signal: ["4h"]
    cascade: ["1h"]
    execution: ["5m"]
```

### 4. Scaled Exits (Critical Change)

Your current system has a single SL and single TP per trade. This is leaving money on the table on winners and taking full losses on losers.

Implement `PartialExitManager` in the Execution Agent:

```python
@dataclass
class ExitPlan:
    levels: list[ExitLevel]  # ordered by target price
    
@dataclass  
class ExitLevel:
    price: float
    pct_of_position: float  # 0.0 - 1.0
    exit_type: Literal["FIXED", "TRAILING"]
    trailing_distance: float | None  # ATR multiple for trailing
```

When TP1 hits, close 40-50% and move SL to breakeven on the rest. This single change will dramatically improve your profit factor because:
- Winners that reverse after TP1 still bank profit instead of becoming breakeven/losses
- Winners that keep going have room to run with the trailing portion
- Your effective win rate goes up because partial-profit trades count as wins

### 5. Update Risk Agent

```yaml
capital:
  risk_per_trade_pct: 1.0      # keep for breakout/momentum
  risk_per_cascade_pct: 0.5    # halved for reversal trades
  max_drawdown_daily_pct: 3.0  # keep
  max_drawdown_total_pct: 10.0 # keep
  max_positions: 2             # reduced from 3 — fewer, higher quality
  max_position_size_pct: 15.0  # reduced from 20%
```

---

## Backtesting Plan

### Phase 1: Regime Detector Validation
Before backtesting any strategy, validate the regime detector in isolation:
- Run it across 3 years of BTC daily data
- Manually verify: does it correctly identify the 6-8 major consolidation-to-breakout events?
- Check false positive rate: how often does it flag "consolidation" that isn't followed by a breakout?
- Target: >60% of flagged consolidation events should produce a >5% move within 14 days

### Phase 2: Strategy 1 (Breakout) Backtest
- Run across the 3 existing windows
- Expected: 10-20 trades per year, 45-55% win rate, average winner 3-5x average loser
- Key metric: does the failed-breakout exit filter prevent the worst losses?
- Gate: PF > 1.5 in at least 2/3 windows before proceeding

### Phase 3: Strategy 2 (Cascade Reversal) Backtest
- Need to identify all >5% drops in 4H in the data first
- Expected: 5-10 setups per year, 55-65% win rate (reversals are higher probability)
- Key metric: average time in trade (should be < 24 hours)
- Gate: PF > 1.3 in at least 2/3 windows

### Phase 4: Strategy 3 (Weekly Momentum) Backtest
- Expected: 15-25 trades per year, 40-50% win rate
- This is closest to existing trend-following but with weekly regime gate
- Gate: PF > 1.2 in at least 2/3 windows

### Phase 5: Combined Walk-Forward
- Only combine strategies that independently passed their gates
- Run 9-fold walk-forward with IS=6m, OOS=3m
- Gate: >6/9 folds positive, average PF > 1.15

---

## What NOT to Do

1. **Do not add more indicators.** The problem isn't insufficient information — it's insufficient signal quality. More indicators = more degrees of freedom = more overfitting.

2. **Do not optimize parameters on historical data.** The parameters above are round numbers chosen for structural reasons, not optimization. 14-day range? Because that's roughly how long BTC consolidations last before a move. 5% drop threshold? Because that's where liquidations cascade. These aren't curve-fit.

3. **Do not run combined strategies until each is independently validated.** The cross-strategy EXIT bug you fixed was just one symptom. Running multiple strategies simultaneously creates correlation between trades that isn't captured by individual backtests.

4. **Do not use strategy exits (time-based or indicator-based) without proving they add value.** Your session notes already showed these are net-negative. Fixed SL/TP with partial exits is cleaner.

5. **Do not add a trailing stop that tightens to less than 1.5 × ATR.** Your session notes show the trailing stop was cutting winners. A trailing stop that's too tight turns a trend-following strategy into a mean-reversion strategy (taking small profits repeatedly).

---

## Priority Implementation Order

1. **Regime detector** — this is the foundation everything else builds on
2. **Partial exit manager** — single biggest improvement to the execution engine
3. **Strategy 1 (Breakout)** — highest expected edge, lowest trade frequency
4. **Backtest Strategy 1 in isolation across all 3 windows**
5. **Strategy 3 (Weekly Momentum)** — refinement of existing trend-following
6. **Strategy 2 (Cascade Reversal)** — add only if the first two aren't sufficient
7. **Walk-forward validation of combined system**

---

## The Honest Assessment

A well-implemented version of Strategy 1 alone should produce +8-15% annual returns on BTC with <5% max drawdown, at ~15 trades per year. That's a realistic target. If backtesting shows less than +5% annual, the strategy needs more work or BTC may not be the right instrument for this approach.

The current +1.5-3.3% returns are not worth the complexity. Either this redesign produces meaningfully better results, or the honest answer is that systematic BTC trading on hourly timeframes with standard indicators isn't viable — and the capital should go into a longer-term trend-following approach on weekly bars or a completely different asset class.
