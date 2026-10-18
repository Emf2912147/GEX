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

## Options-era challenge (v2, Oct 2020 - Oct 2026)

`research/pattern_backtest_v2.py`, results in `research/results/results_v2.md`.
Hypothesis: options volume has grown (Cboe exchanges: 5.3M contracts/day in
2020 -> 13.6M in 2026), so the patterns are options-driven, should show up in
the recent era, and be more reliable at put/call, RSI or divergence extremes.

| Finding | Evidence |
|---|---|
| OPEX pin window got STRONGER | daily moves 1-4 days before monthly OPEX ~9% of an ATR smaller on the indices (t 2.0, both halves), stocks t 6.0; ~5x the full-history effect. Still SLIGHT on the index bar. |
| Zone touch count faded | index excess +0.01 (t 0.1) since 2020 (was +0.07 over 1994-2026) |
| Gap breakout faded | index -0.24 (t -0.5) since 2020 |
| Fibonacci close strength | stocks t 3.0, indices t 0.7: SLIGHT, stocks only |
| Indicators alone | RSI extreme +0.22 ATR (t 1.0), put/call extreme -0.03, divergence -0.06 on the indices: no edge |
| Pattern x extreme (40 cells) | none SLIGHT or better; only 10 cells had 15+ index signals; extremes did not make any pattern more reliable |

Conclusion: the options-mechanics effect (expiration pinning) did grow with
options volume; the chart patterns did not, and coincident extremes did not
rescue them. Statistical power at extremes on 3 indices x 6 years is low.
