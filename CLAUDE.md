# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is a new, empty Tradingbot project. Update this file as the project takes shape.


You are the architect and implementer of an automated trading bot system based on 
multiple specialized agents that collaborate. Your system will trade on financial 
markets (crypto / forex / stocks — specified below) using a multi-agent architecture 
where each agent has a strictly defined role, a clearly defined input, a standardized 
output, and precise rules for communicating with the other agents.

═══════════════════════════════════════════════════════════
GENERAL SYSTEM CONTEXT
═══════════════════════════════════════════════════════════

Target market: [FILL IN: e.g. Crypto — BTC/USDT on Binance]
Primary timeframe: [FILL IN: e.g. 1H for signals, 5m for execution]
Initial simulation capital: [FILL IN: e.g. 10,000 USDT]
Initial run mode: PAPER TRADING (simulation with no real money)
Implementation language: Python 3.11+
Inter-agent communication framework: asyncio message queues (or LangGraph 
  if you prefer a more robust orchestrator)
Persistence: SQLite for journal + Redis for real-time state

═══════════════════════════════════════════════════════════
MULTI-AGENT ARCHITECTURE — 6 SPECIALIZED AGENTS
═══════════════════════════════════════════════════════════

──────────────────────────────────────────────────────────
AGENT 1 — ORCHESTRATOR AGENT
──────────────────────────────────────────────────────────
Role: The central brain. Receives data from all agents, 
  maintains the global system state, and decides the 
  execution order of each agent. Does NOT make trading 
  decisions directly — fully delegates.

Exact responsibilities:
  - Starts and stops the main trading loop
  - Maintains a global "system state": { mode, open_positions, 
    daily_pnl, risk_budget_remaining, last_signal, errors[] }
  - Routes messages between agents in the correct order:
      1. Market Data Agent → collect data
      2. Strategy Agent → generate signal
      3. Risk Agent → validate signal
      4. Execution Agent → place order (if Risk approves)
      5. Journal Agent → log everything
  - Implements a circuit breaker: if daily_drawdown exceeds 
    the configured threshold, puts the system in PAUSED mode 
    and sends an alert
  - Exposes a control interface: start / stop / status / 
    force_close_all

Input received: status messages and reports from all agents
Output sent: commands and parameters to each agent

Mandatory minimal code structure:
  class OrchestratorAgent:
      async def run_cycle(self) -> None
      async def handle_agent_message(self, msg: AgentMessage) -> None
      async def trigger_circuit_breaker(self, reason: str) -> None
      def get_system_state(self) -> SystemState

──────────────────────────────────────────────────────────
AGENT 2 — MARKET DATA AGENT
──────────────────────────────────────────────────────────
Role: The single source of truth for market data. 
  No other agent accesses the exchange directly for data.

Exact responsibilities:
  - WebSocket connection to exchange for real-time data 
    (price, volume, level 1 and 2 orderbook depth)
  - Periodic OHLCV fetch across all required timeframes: 
    1m, 5m, 15m, 1H, 4H, 1D
  - Data normalization and validation: checks for gaps, 
    incomplete candles, incorrect timestamps
  - Calculates and caches base technical indicators 
    used by the Strategy Agent:
      * EMA(9), EMA(21), EMA(50), EMA(200)
      * RSI(14), RSI(7)
      * ATR(14) — used by Risk Agent for sizing
      * MACD(12,26,9)
      * Bollinger Bands(20,2)
      * Volume SMA(20)
  - Detects data anomalies (price spikes > 5% in a single 
    1m candle, volume 10x above average) and signals 
    the Orchestrator
  - Maintains a circular buffer with the last 500 candles 
    per timeframe in memory (numpy array)

Input received: configuration (symbol, timeframes, exchange credentials)
Output emitted (DataSnapshot object):
  {
    symbol: str,
    timestamp: int,
    price: float,         # last trade price
    bid: float,
    ask: float,
    spread_pct: float,
    ohlcv: Dict[str, DataFrame],  # keyed by timeframe
    indicators: Dict[str, float], # all precalculated values
    anomaly_flag: bool,
    anomaly_reason: str | None
  }

Emit frequency: on every closed candle on the primary timeframe 
  + immediately on anomaly detection

──────────────────────────────────────────────────────────
AGENT 3 — STRATEGY AGENT
──────────────────────────────────────────────────────────
Role: Generates trading signals based on data received 
  from the Market Data Agent. Knows nothing about risk 
  or execution — produces only signals with clear reasoning.

Exact responsibilities:
  - Implements a MINIMUM of 2 distinct strategies that can 
    run simultaneously or alternately (configurable):

    STRATEGY A — Trend Following with EMA + RSI:
      * LONG condition: EMA9 > EMA21 > EMA50, RSI(14) between 
        45-65, price above EMA21, last candle volume 
        > Volume_SMA20 * 1.2
      * SHORT condition: EMA9 < EMA21 < EMA50, RSI(14) between 
        35-55, price below EMA21, volume above average
      * EXIT LONG condition: RSI(14) > 75 OR price below EMA50
      * EXIT SHORT condition: RSI(14) < 25 OR price above EMA50

    STRATEGY B — Mean Reversion with Bollinger Bands:
      * LONG condition: price touches lower Bollinger band, 
        RSI(7) < 25, current candle is bullish (close > open)
      * SHORT condition: price touches upper Bollinger band, 
        RSI(7) > 75, current candle is bearish
      * EXIT condition: price returns to Bollinger midline 
        OR RSI returns to 50

  - Calculates a CONVICTION SCORE (confidence_score) between 
    0.0 and 1.0 for each signal, based on how many conditions 
    are simultaneously met and multi-timeframe alignment
  - Checks alignment on higher timeframe: a LONG signal 
    on 1H must be confirmed by bullish trend on 4H 
    (EMA21 > EMA50 on 4H)
  - Does NOT emit a signal if spread > 0.1% or if 
    anomaly_flag is true

Output emitted (TradingSignal object):
  {
    direction: "LONG" | "SHORT" | "FLAT" | "EXIT",
    strategy_name: str,
    confidence_score: float,    # 0.0 - 1.0
    entry_price: float,
    suggested_stop_loss: float, # ATR-based: entry ± 1.5*ATR
    suggested_take_profit: float, # ATR-based: entry ± 3*ATR (R:R=2:1)
    timeframe: str,
    reasoning: List[str],  # list of conditions met
    timestamp: int
  }

  Emit FLAT if no condition is met — do not force signals 
  in sideways markets.

──────────────────────────────────────────────────────────
AGENT 4 — RISK AGENT
──────────────────────────────────────────────────────────
Role: The capital guardian. Validates every signal received 
  from the Strategy Agent and decides whether the order 
  can be placed, at what size, and with what protection 
  parameters. Has absolute VETO power.

Exact responsibilities:
  - Receives TradingSignal and SystemState, returns 
    a RiskDecision
  - Calculates position size using the Fixed Fractional method:
      risk_per_trade = capital * risk_pct  
        (default: risk_pct = 1% of total capital)
      distance_to_stop = abs(entry_price - stop_loss)
      position_size = risk_per_trade / distance_to_stop
      
  - Applies the following RISK RULES in order — 
    if any fails, returns REJECTED with reason:

    RULE 1 — Daily drawdown:
      If daily_pnl < -3% of capital → REJECT ALL, 
      send PAUSE to Orchestrator

    RULE 2 — Total drawdown:
      If total_pnl < -10% of initial capital → REJECT ALL, 
      send STOP to Orchestrator

    RULE 3 — Maximum exposure per trade:
      position_size * entry_price cannot exceed 20% 
      of total capital

    RULE 4 — Maximum simultaneous open positions:
      Maximum 3 open positions at the same time

    RULE 5 — Correlation check:
      Do not open positions on assets with correlation > 0.8 
      (check using last 30 days of data)

    RULE 6 — Confidence filter:
      If confidence_score < 0.6 → REJECT with reason 
      "low confidence"

    RULE 7 — Cooldown after loss:
      If the last 2 consecutive trades were losses → enforce 
      a cooldown of 2 candles before the next trade

  - Adjusts stop_loss and take_profit if those suggested 
    by the Strategy Agent do not meet the minimum R:R 
    ratio of 1.5:1
  - Logs ALL decisions (including rejected ones) 
    with complete reasoning

Output emitted (RiskDecision object):
  {
    approved: bool,
    rejection_reason: str | None,
    position_size: float,        # in asset units
    position_size_usd: float,    # USD value
    final_stop_loss: float,
    final_take_profit: float,
    risk_pct_of_capital: float,  # % of capital at risk
    reward_risk_ratio: float,
    timestamp: int
  }

──────────────────────────────────────────────────────────
AGENT 5 — EXECUTION AGENT
──────────────────────────────────────────────────────────
Role: The only agent that interacts with the exchange 
  for placing and managing orders. Receives instructions 
  approved by the Risk Agent and executes them efficiently, 
  handling network errors and slippage.

Exact responsibilities:
  - Receives approved RiskDecision + TradingSignal and 
    places orders in this sequence:
      1. Place the main order (MARKET or LIMIT, configurable)
      2. Immediately after confirmation, place STOP LOSS 
         as an OCO order (One-Cancels-Other) or stop-market
      3. Place TAKE PROFIT as a limit order
  - Manages slippage: if a MARKET order executes at > 0.15% 
    from the signal price, log as "high slippage" and 
    notify the Orchestrator
  - Implements retry logic for network errors:
      * Retry up to 3 times with exponential backoff 
        (1s, 2s, 4s)
      * If all 3 attempts fail → ABORT, log the error, 
        notify the Orchestrator
  - Monitors open positions: checks every 30 seconds 
    whether stop-loss or take-profit has been hit 
    (as a fallback to WebSocket events)
  - Implements optional trailing stop: if price moves 
    favorably by > 1*ATR, move stop-loss to breakeven
  - Handles partial fills: if an order is less than 90% 
    filled within 60 seconds, cancel the remainder 
    and proceed with the filled quantity

Output emitted (ExecutionReport object):
  {
    order_id: str,
    status: "FILLED" | "PARTIAL" | "REJECTED" | "ERROR",
    executed_price: float,
    executed_quantity: float,
    slippage_pct: float,
    fees_paid: float,
    stop_loss_order_id: str,
    take_profit_order_id: str,
    timestamp_open: int,
    error_message: str | None
  }

  On position close, additionally emits:
  {
    pnl_gross: float,
    pnl_net: float,       # after fees
    pnl_pct: float,
    duration_minutes: int,
    close_reason: "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "TRAILING_STOP"
  }

──────────────────────────────────────────────────────────
AGENT 6 — JOURNAL AGENT
──────────────────────────────────────────────────────────
Role: The system's permanent memory. Records everything 
  that happens, calculates performance statistics, and 
  generates reports. Makes no decisions — only observes 
  and reports.

Exact responsibilities:
  - Listens to all messages in the system (subscriber to 
    all message queues) and persists to SQLite:
      * All signals generated (including those rejected by Risk)
      * All Risk Agent decisions with complete reasoning
      * All orders placed and their status
      * All errors and anomalies
      * State snapshots every 1 hour

  - Calculates and updates in real time:
      Win rate: trades_won / total_trades_closed
      Average realized R:R
      Profit factor: gross_profit / gross_loss
      Sharpe ratio (if at least 30 trades available)
      Maximum drawdown (peak-to-trough on equity curve)
      Average trade duration
      Best and worst trade
      Performance per strategy (A vs B separately)
      Performance per hour of day (heat map)

  - Generates automatic daily report at 00:00 UTC:
      Text summary: trades, PnL, win rate, drawdown
      Saved as JSON + sent to Orchestrator for logging

  - Detects problematic patterns and alerts:
      * > 5 consecutive losing trades
      * Win rate below 40% over the last 20 trades
      * Fees exceed 10% of gross profit in the past week

  - Exposes internal API for queries:
      get_trade_history(start_date, end_date) -> List[Trade]
      get_performance_summary(period) -> PerformanceSummary
      get_strategy_comparison() -> Dict[str, Metrics]

═══════════════════════════════════════════════════════════
INTER-AGENT COMMUNICATION PROTOCOLS
═══════════════════════════════════════════════════════════

All messages between agents follow the AgentMessage structure:
  {
    msg_id: str (UUID),
    sender: AgentName,
    recipient: AgentName | "BROADCAST",
    msg_type: str,
    payload: Dict,
    timestamp: int,
    requires_ack: bool
  }

Main execution cycle (run loop):
  1. Market Data Agent emits DataSnapshot
  2. Orchestrator sends DataSnapshot to Strategy Agent
  3. Strategy Agent emits TradingSignal
  4. Orchestrator sends TradingSignal to Risk Agent
  5. Risk Agent emits RiskDecision
  6. If approved=True: Orchestrator sends to Execution Agent
  7. Execution Agent emits ExecutionReport
  8. Journal Agent receives ALL messages at every step
  9. Orchestrator updates SystemState
  10. Repeat from 1

═══════════════════════════════════════════════════════════
GLOBAL CONFIGURATION (config.yaml)
═══════════════════════════════════════════════════════════

The system must be fully configurable from config.yaml 
with no hardcoding in the source code:

trading:
  symbol: "BTC/USDT"
  exchange: "binance"
  mode: "paper"           # paper | live
  primary_timeframe: "1h"
  confirmation_timeframe: "4h"

capital:
  initial_capital: 10000  # USDT
  risk_per_trade_pct: 1.0
  max_drawdown_daily_pct: 3.0
  max_drawdown_total_pct: 10.0
  max_positions: 3
  max_position_size_pct: 20.0

strategy:
  active: ["trend_following", "mean_reversion"] # or just one
  min_confidence_score: 0.6
  cooldown_after_losses: 2   # candles to wait

execution:
  order_type: "market"        # market | limit
  max_slippage_pct: 0.15
  retry_attempts: 3
  trailing_stop_enabled: true
  trailing_stop_trigger_atr: 1.0

logging:
  level: "INFO"
  daily_report_time_utc: "00:00"
  alert_consecutive_losses: 5

═══════════════════════════════════════════════════════════
PROJECT STRUCTURE
═══════════════════════════════════════════════════════════

Implement the code in this directory structure:

trading_bot/
├── config.yaml
├── main.py                      # entry point
├── agents/
│   ├── __init__.py
│   ├── base_agent.py            # shared abstract class
│   ├── orchestrator_agent.py
│   ├── market_data_agent.py
│   ├── strategy_agent.py
│   ├── risk_agent.py
│   ├── execution_agent.py
│   └── journal_agent.py
├── models/
│   ├── agent_message.py
│   ├── data_snapshot.py
│   ├── trading_signal.py
│   ├── risk_decision.py
│   └── execution_report.py
├── strategies/
│   ├── base_strategy.py
│   ├── trend_following.py
│   └── mean_reversion.py
├── exchange/
│   ├── base_exchange.py
│   ├── binance_adapter.py
│   └── paper_trading_adapter.py  # faithful simulation for testing
├── storage/
│   ├── database.py              # SQLite ORM
│   └── cache.py                 # Redis wrapper
├── utils/
│   ├── indicators.py            # vectorized technical calculations (numpy)
│   ├── risk_calculator.py
│   └── logger.py
└── tests/
    ├── test_strategy_agent.py
    ├── test_risk_agent.py
    └── test_paper_trading.py

═══════════════════════════════════════════════════════════
MANDATORY IMPLEMENTATION REQUIREMENTS
═══════════════════════════════════════════════════════════

1. ASYNC FIRST: All agents run as asyncio coroutines. 
   No blocking calls in the event loop.

2. TYPE SAFETY: Use dataclasses or Pydantic for all 
   data models. No loose dicts passed between agents.

3. TESTABILITY: The paper trading adapter must faithfully 
   simulate: network latency (50-200ms random), slippage 
   (0-0.1% random), real fees (0.1% maker/taker Binance).

4. GRACEFUL SHUTDOWN: On SIGTERM/SIGINT, the Orchestrator 
   initiates an orderly shutdown: no new positions opened, 
   waits for all in-flight order confirmations, saves state, 
   stops agents in reverse order.

5. OBSERVABILITY: Each agent logs with structured format:
   timestamp | agent_name | level | message | context_dict

6. IDEMPOTENCY: Execution Agent checks whether an order 
   with the same signal_id already exists on the exchange 
   before sending a new one (prevents duplicates on reconnect).

═══════════════════════════════════════════════════════════
FIRST THING YOU MUST DO
═══════════════════════════════════════════════════════════

Implement in this order:
  1. Define all Pydantic models in models/
  2. Implement base_agent.py with the shared interface
  3. Implement paper_trading_adapter.py 
     (allows testing without a real exchange)
  4. Implement market_data_agent.py with historical data 
     (CCXT for offline OHLCV fetch)
  5. Implement strategy_agent.py + unit tests
  6. Implement risk_agent.py + unit tests
  7. Implement orchestrator_agent.py and connect the first 
     3 agents
  8. Implement execution_agent.py with the paper adapter
  9. Implement journal_agent.py
  10. Run a backtest on 6 months of BTC/USDT 1H historical 
      data and report the metrics

Ready? Start with step 1 — define all Pydantic models 
and explain each field with its correct type.

═══════════════════════════════════════════════════════════
SESSION NOTES — 2026-04-19
═══════════════════════════════════════════════════════════

## Changes made this session

### New files
- trading_bot/regime_runner.py   — runs 3 strategies x 3 windows, prints comparison matrix
- trading_bot/walkforward_runner.py — 9-fold OOS validation (IS=6m, OOS=3m)
- trading_bot/stress_runner.py   — cost sensitivity: baseline / moderate / conservative fees

### backtest.py
- BacktestEngine: added fee_rate, slippage_entry, use_strategy_exits fields
- use_strategy_exits defaults to False (SL/TP are sole exits — stall exits net-negative)
- Fixed cross-strategy EXIT bug: only the strategy that opened a position can close it
- Added --start-date, --end-date, --strategy, --fee-rate, --slippage, --use-strategy-exits CLI args
- Fixed end-of-run force-close to respect the end_idx window

### strategies/trend_following.py
- SL multiplier: 1.5 -> 2.0 ATR (wider room to breathe)
- TP multiplier: 4.0 -> 5.0 ATR (R:R = 2.5:1, breakeven WR = 28.6%)
- Added MODE 2: momentum continuation when ADX > 35 (no strict EMA21 touch required)
- Pullback zone widened: 0.1% -> 0.3% of EMA21
- Pullback close condition tightened: close > EMA21 -> close > EMA9 (strong bounce only)
- Pullback now requires MACD hist > 0 as a HARD gate (eliminates fading momentum setups)
- Added DI+ > DI- directional gate for LONG entries
- Time exit extended: 12 -> 20 candles (but use_strategy_exits=False makes this moot)

### strategies/mean_reversion.py
- RSI7 thresholds relaxed: < 20 -> < 25  and  > 80 -> > 75
- Time exit extended: 6 -> 8 candles

## Verified results (end-of-session state)

Trend-following only, exits disabled, across all three 1-year windows:

  Bull  Apr23-Apr24:  ret=+1.8%  WR=37%  PF=1.36  T=51  MDD=3.9%
  Mid   Apr24-Apr25:  ret=+1.5%  WR=39%  PF=1.28  T=49  MDD=2.4%
  Recent Apr25-Apr26: ret=+3.3%  WR=41%  PF=1.47  T=58  MDD=3.3%

ALL THREE WINDOWS POSITIVE — first time achieving this milestone.

6-month backtest: +0.66%, PF=1.23, MDD=3.64% (vs original -10.12%)
12-month backtest: +0.88%, PF=1.36, MDD=2.73%

Walk-forward (IS=6m, OOS=3m, 9 folds) — trend_only, run with exits=False but BEFORE MACD gate:
  avg ret=-0.0%  avg PF=1.20  positive folds=4/9  [FAIL]
  Problem fold: Nov 2023-Feb 2024 (ret=-3.3%, PF=0.48)

Stress test results are from OLD code (pre-improvements). Need rerun.

═══════════════════════════════════════════════════════════
SESSION NOTES — 2026-04-19 (session 4)
═══════════════════════════════════════════════════════════

## Strategy redesign implemented (STRATEGY_REDESIGN.md by Opus)

Old strategies (trend_following, mean_reversion) replaced by:
  - breakout_strategy.py       — regime-gated volatility compression breakout
  - cascade_reversal.py        — liquidation cascade reversal (LONG only)
  - weekly_momentum.py         — weekly-trend-gated 4H EMA21 pullback

New infrastructure:
  - models/market_regime.py    — MarketRegime Pydantic model
  - strategies/regime_detector.py — detects CONSOLIDATION / TRENDING / VOLATILE from daily data
    * Uses 4-day LAG: evaluates conditions on data[:-4] so breakout candles don't
      mask their own consolidation (critical design detail — do not remove the lag)
  - utils/levels.py            — Fibonacci retracement, support levels
  - models/trading_signal.py   — added ExitLevel, exit_levels (scaled partial exits)
  - backtest.py                — full rewrite: daily + weekly data, look-ahead-free masks
    * 4H mask: df_4h.index <= ts - Timedelta(hours=4)
    * Daily mask: df_1d.index <= ts - Timedelta(hours=24)
    * Weekly mask: df_1w.index <= ts - Timedelta(days=7)
    * Partial exit engine: TP1 → breakeven → TP2 → trailing
  - Daily data cache: BTC_USDT_1d_36m.json (1080 candles, fetched this session)
  - Weekly derived from daily resample (avoids CCXT weekly alignment issues)

## End-of-session test results

weekly_momentum (36m, 15 trades): WR=26.7%, PF=0.158, ret=-1.60%
  73% strategy exits (EMA21/EMA50 invalidation) — caused by Bug 1 below

breakout: 0 trades in 3 years — root cause not fully diagnosed (see bugs below)

cascade_reversal: 0 trades — RSI<22 + volume>3x + 5% drop all simultaneously is rare;
  may need to loosen one condition or accept very low trade frequency

## Bugs fixed this session (not yet re-tested)

Bug 1 — weekly_momentum immediate invalidation exits:
  Entry was missing 4H EMA21 > EMA50 gate. Trades entered when EMA21 < EMA50,
  then invalidation exit fired on the next bar. Fix: hard gate added before entry checks.

Bug 2 — weekly_momentum TP1 too close to entry:
  prev_swing_high was 20 4H bars = 3.3 days. Too shallow — winner was $6 gross.
  Fix: changed to 60 4H bars ≈ 10 days (renamed key: prev_swing_high_60).

## What to run next session

Run these IN ORDER. Each one short (seconds with cached data). Stop at first failure.

1. weekly_momentum fixed (should see fewer strategy exits, larger winners):
      py -m trading_bot.backtest --strategy weekly_momentum --months 36

2. breakout diagnostic (check if any consolidation + breakout setups exist in data):
      py -c "
import json, numpy as np, pandas as pd
from trading_bot.utils.indicators import compute_all
from trading_bot.strategies.regime_detector import detect_regime
data_1d = json.loads(open('trading_bot/.cache/BTC_USDT_1d_36m.json').read())
data_4h = json.loads(open('trading_bot/.cache/BTC_USDT_4h_36m.json').read())
def to_df(d):
    df = pd.DataFrame(d, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    return df.set_index('timestamp')
df_1d = to_df(data_1d); df_4h = to_df(data_4h)
cfg = {'ema_periods':[9,21,50,200],'rsi_periods':[7,14],'atr_period':14,'macd_fast':12,'macd_slow':26,'macd_signal':9,'bb_period':20,'bb_std':2.0,'volume_sma_period':20}
cons=bo=vol_ok=rsi_ok=body_ok=all_ok=0
for i in range(0, len(df_4h)-1):
    ts = df_4h.index[i]
    d_mask = df_1d.index <= ts - pd.Timedelta(hours=24)
    if d_mask.sum() < 165: continue
    d_win = df_1d[d_mask].iloc[-250:]
    r = detect_regime(d_win['high'].values.astype(float), d_win['low'].values.astype(float), d_win['close'].values.astype(float), 0)
    if r is None or r.regime != 'CONSOLIDATION': continue
    cons += 1
    win = df_4h.iloc[max(0,i-210):i+1]
    ind = compute_all(win['open'].values.astype(float), win['high'].values.astype(float), win['low'].values.astype(float), win['close'].values.astype(float), win['volume'].values.astype(float), cfg)
    c = ind.get('close',0); h = ind.get('high',0); l = ind.get('low',0); o = ind.get('open',0)
    v = ind.get('volume',0); vs = ind.get('volume_sma_20',1); rsi = ind.get('rsi_14',0)
    rh = r.consolidation_range_high
    if c > rh: bo += 1
    else: continue
    bp = abs(c-o)/(h-l) if h>l else 0
    sl_pct = (c - r.consolidation_range_low)/c*100
    if v > vs*2: vol_ok += 1
    if 55<=rsi<=70: rsi_ok += 1
    if bp > 0.6: body_ok += 1
    if v>vs*2 and 55<=rsi<=70 and bp>0.6 and sl_pct<=3: all_ok+=1; print(f'{ts.date()} rsi={rsi:.0f} vol={v/vs:.1f}x body={bp:.0%} sl={sl_pct:.1f}%')
print(f'Consolidation 4H bars: {cons}')
print(f'4H close > range_high: {bo}')
print(f'vol>2x:{vol_ok}  rsi_ok:{rsi_ok}  body_ok:{body_ok}  ALL:{all_ok}')
"

3. cascade_reversal (just count how many setups exist regardless of all conditions):
      py -m trading_bot.backtest --strategy cascade_reversal --months 36

## Key invariants (NEW)

- Do NOT remove the 4-day lag from regime_detector — it is structural, not cosmetic.
- Do NOT re-enable trailing_stop globally — managed per-strategy via exit_levels.
- Do NOT run combined mode until each strategy independently shows PF > 1.0.
- Acceptance gate: positive return AND PF > 1.0 in all tested windows before valid.
- All backtest runs use --months 36 (12m too short for regime detector warmup).
- use_strategy_exits=True by default for new strategies (opposite of old system).
