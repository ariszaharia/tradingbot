# Profitability Notes

Current backtest result is negative, so the code is not profitable as-is.

## What I would change next

- Increase selectivity: require stronger trend confirmation and add a volatility or regime filter so the bot skips choppy periods.
- Tighten mean-reversion usage: it is contributing the weakest performance, so either disable it by default or require stronger oversold/overbought conditions.
- Raise the confidence floor: signals below a higher threshold should not trade, especially after fees and slippage.
- Add a time-based exit: close trades that do not move in the expected direction within a fixed number of candles.
- Reduce overtrading after losses: extend cooldowns after losing streaks and consider a daily trade cap.
- Tune ATR multiples and risk sizing on out-of-sample data only, not the same window used to evaluate performance.
- Keep the execution model conservative: assume worse fills, higher fees, and wider spread when validating any improvement.

## What changed in the repo

- Added this `AGENTS.md` file with the current profitability diagnosis and the highest-impact changes to try next.

## Handoff for Claude

The system lost money over the last 6 months on BTC/USDT 1H. The next step should be to run controlled strategy experiments, starting with disabling mean reversion, increasing confirmation requirements, and testing regime filters under the same fee/slippage assumptions.