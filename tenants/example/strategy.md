# Example strategy — produced by the SETUP interview, in the user's own words + rules

## Watchlist
SPY, QQQ, AAPL

## Buy rules (user's words, then the checkable rule)
- "I want to buy the dip when the market drops a few percent but isn't broken."
  - {side: buy, metric: pct_from_5d_high, op: ">=", value: 3.0,
     plain_english: "price is 3% or more below its 5-day high"}
  - {side: buy, metric: price_vs_sma50_pct, op: ">=", value: -2.0,
     plain_english: "price is no more than 2% below its 50-day average"}

## Sell rules
- "Take profit when it pops back."
  - {side: sell, metric: pct_from_5d_high, op: "<=", value: 0.5,
     plain_english: "price is back within 0.5% of its 5-day high"}

## Position size
Max 5% of account per position.

## Check-in cadence (informational)
User plans to /scan most weekday mornings. No background scanning exists.
