# Course pattern grades (2026-10-18)

Backtest: `research/pattern_backtest.py`, results in `research/results/`.
Graded on SPY (1994+), QQQ (1999+), IWM (2000+) and SPX 1985-93; 75 liquid
stocks/ETFs (2005+) as a cross-check. 10-day outcomes in ATR, drift-adjusted,
measured as the EXCESS over a random-walk placebo (shuffled bars with the
same volatility), because several rules produce an "edge" on random prices.

| Pattern (lesson) | Grade | Evidence (indices) | App |
|---|---|---|---|
| Zone touch count (5, Law 1) | SLIGHT | 1st touch held 44% vs 35% at 3rd+; ~7 pts beyond placebo; positive in all 6 settings (t 1.4 to 2.8), both halves; small on stocks | Info: touch count on each zone |
| Gap breakout (5, Law 2) | SLIGHT | gap breakouts +0.31 ATR better over 10 days (t 2.2), both halves; weak on stocks (t 0.8) | Info: flag a recent gap breakout |
| OPEX pin window (8) | SLIGHT | daily moves ~2% smaller 1-4 days before monthly OPEX; clear on stocks (t 3.5), weak on indices (t 1.1); too small to trade | Info: OPEX phase |
| OPEX shakeout window (8) | NOT SUPPORTED (opposite) | breakouts ~2 weeks before OPEX failed LESS often (indices t -2.0, stocks t -2.6) | Info: "do not treat as suspect" |
| Close strength vs Fibonacci (5, Law 4) | NONE | indices t 0.8; stocks t 2.0 only | - |
| Steep orderly / absorbed moves (5, Law 3) | NONE | no effect; violent moves too rare on indices (13) to judge | - |
| Breakout from contraction (1) | NONE | indices t -1.0 | - |
| Late (extended) entries (1-2) | NONE | indices t -0.6; on stocks extended entries did slightly BETTER (momentum) | - |
| Stop sweep and reclaim (6) | NONE | indices t 0.3 | - |
| Quad-witching pin / shakeout (8) | NONE | indices t -0.4 / -0.4 | - |
| Open-interest wall shapes (7) | UNTESTED | 35 stored chain sessions (2 expirations) | - |

No pattern reached CREDIBLE (index t >= 2.5, stable in both halves, confirmed
on stocks, beyond placebo), so none changes the app's trade scoring.
