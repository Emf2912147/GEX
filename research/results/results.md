# Course pattern backtest

Index series: 4 (SPY, QQQ, IWM, SPX 1985-93) · broad: 75 tickers · horizon 10 days · ATR units, drift-adjusted · effects are EXCESS over a random-walk placebo

| Test | Lesson | Grade | Index excess (t, n) [raw / placebo] | halves | Broad excess (t, n) |
|---|---|---|---|---|---|
| Zone touch count | 5 (Law 1) | **NONE** | +0.072 (t +1.4, n 1451) [+0.090 / +0.018] | +0.089 / +0.040 | +0.016 (t +1.4, n 25092) |
| Gap breakout vs grind | 5 (Law 2) | **SLIGHT** | +0.308 (t +2.2, n 334) [+0.206 / -0.102] | +0.222 / +0.373 | +0.055 (t +0.8, n 5122) |
| Move character: orderly | 5 (Law 3) | **NONE** | -0.277 (t -0.3, n 151) [-0.086 / +0.191] | +0.386 / -0.975 | -0.202 (t -1.4, n 2327) |
| Move character: absorbed | 5 (Law 3) | **NONE** | +0.170 (t +0.2, n 13) [+0.101 / -0.069] | -0.546 / +0.995 | +0.216 (t +1.3, n 545) |
| Move character: violent | 5 (Law 3) | **CONTRADICTED** | -5.346 (t -2.6, n 13) [-5.055 / +0.291] | – / – | -0.184 (t -0.8, n 545) |
| Close strength vs Fibonacci | 5 (Law 4) | **NONE** | +0.160 (t +0.8, n 573) [+0.138 / -0.022] | +0.077 / +0.328 | +0.101 (t +2.0, n 8407) |
| Breakout from contraction | 1 (Master Pattern) | **NONE** | -0.171 (t -1.0, n 542) [-0.139 / +0.032] | -0.110 / -0.240 | -0.013 (t -0.2, n 7757) |
| Late entry (extended) | 1-2 (entries) | **NONE** | -0.115 (t -0.6, n 1903) [-0.036 / +0.079] | -0.070 / -0.190 | -0.074 (t -2.3, n 31357) |
| Stop sweep and reclaim | 6 | **NONE** | +0.045 (t +0.3, n 1301) [-0.016 / -0.061] | +0.076 / +0.003 | +0.014 (t +0.6, n 22488) |
| Sweep vs clean breakdown | 6 | **NONE** | +0.056 (t +0.3, n 1301) [-0.006 / -0.062] | +0.148 / -0.063 | +0.029 (t +0.7, n 22488) |
| Pin window (monthly OPEX) | 8 | **SLIGHT** | +0.020 (t +1.1, n 10058) [+0.019 / -0.001] | +0.005 / +0.040 | +0.014 (t +3.5, n 167591) |
| Shakeout window (monthly OPEX) | 8 | **NONE** | -0.065 (t -2.0, n 778) [-0.051 / +0.014] | -0.023 / -0.118 | -0.024 (t -2.6, n 10051) |
| Pin window (quad witching) | 8 | **NONE** | -0.011 (t -0.4, n 5352) [-0.009 / +0.002] | -0.015 / -0.005 | -0.008 (t -1.8, n 89243) |
| Shakeout window (quad witching) | 8 | **NONE** | -0.019 (t -0.4, n 236) [+0.008 / +0.027] | +0.094 / -0.130 | -0.016 (t -1.1, n 3057) |
| Open-interest wall shapes | 7 | **UNTESTED** | – (t –, n –) [– / –] | – / – | – (t –, n –) |

## Zone-touch robustness (placebo-adjusted)

| Variant | Index excess (t, n) | halves | Broad excess (t, n) |
|---|---|---|---|
| zone touch: pivot 3, hold 1.5 ATR, break 0.5 ATR | +0.069 (t +1.9, n 2637) | +0.111 / +0.019 | +0.018 (t +2.1, n 46306) |
| zone touch: pivot 8, hold 1.5 ATR, break 0.5 ATR | +0.171 (t +2.8, n 713) | +0.189 / +0.150 | -0.009 (t -0.6, n 12995) |
| zone touch: pivot 5, hold 1.0 ATR, break 0.5 ATR | +0.072 (t +1.9, n 1451) | +0.134 / -0.006 | +0.017 (t +1.6, n 25092) |
| zone touch: pivot 5, hold 2.0 ATR, break 0.75 ATR | +0.080 (t +2.0, n 1451) | +0.087 / +0.072 | +0.018 (t +1.9, n 25092) |
| zone touch: pivot 5, hold 1.5 ATR, break 0.25 ATR | +0.090 (t +2.7, n 1451) | +0.111 / +0.064 | +0.012 (t +1.0, n 25092) |

## Notes
- **Zone touch count**: First touch of a support/resistance zone holds more often than the 3rd+. hold rate, 1st touch minus 3rd+. → NONE: no reliable effect on the indices.
- **Gap breakout vs grind**: Breaking a 20-day level on a gap continues better than grinding through. 10-day continuation (ATR), gap minus grind. → SLIGHT: right direction on the indices but not strong or stable enough.
- **Move character: orderly**: Steep, orderly moves continue. 10-day continuation (ATR), orderly minus ordinary trends. → NONE: no reliable effect on the indices.
- **Move character: absorbed**: Steep-then-stalling moves reverse. 10-day continuation (ATR), ordinary minus absorbed. → NONE: no reliable effect on the indices.
- **Move character: violent**: Wide, violent moves have no trend (stand aside). 10-day continuation (ATR), ordinary minus violent. → CONTRADICTED: index shows the opposite of the course rule.
- **Close strength vs Fibonacci**: Moves that hold above the 23.6% retracement continue better than those that lose 50%. 10-day continuation (ATR), strong minus weak. → NONE: no reliable effect on the indices.
- **Breakout from contraction**: Breakouts out of a volatility contraction continue better than other breakouts. 10-day continuation (ATR), squeeze minus other. → NONE: no reliable effect on the indices.
- **Late entry (extended)**: Entering > 2.5 ATR from the 20-day EMA does worse than entering 0.5-1.5 ATR away. 10-day continuation (ATR), normal minus extended. → NONE: no reliable effect on the indices.
- **Stop sweep and reclaim**: A reclaimed sweep of a swing low/high reverses. 10-day move in the signal direction (ATR). → NONE: no reliable effect on the indices.
- **Sweep vs clean breakdown**: Reclaimed sweeps beat breakdowns that hold. signal-direction move (ATR), sweep minus breakdown. → NONE: no reliable effect on the indices.
- **Pin window (monthly OPEX)**: Price compresses in the 1-4 days before monthly expiration. daily |move| / ATR, other days minus pin window. → SLIGHT: clear on stocks, weak on the indices.
- **Shakeout window (monthly OPEX)**: Breakouts ~2 weeks before monthly expiration fail more often. false-breakout rate, shakeout minus other days. → NONE: no reliable effect on the indices.
- **Pin window (quad witching)**: Price compresses ~2 weeks before quad witching. daily |move| / ATR, other days minus pin window. → NONE: no reliable effect on the indices.
- **Shakeout window (quad witching)**: Breakouts ~1 month before quad witching fail more often. false-breakout rate, shakeout minus other days. → NONE: no reliable effect on the indices.
- **Open-interest wall shapes**: One-sided walls V-reverse; stacked walls chop; sloping walls give an ABC path. needs daily open-interest history. → UNTESTED: only 35 sessions of stored chains (2 monthly expirations): too few to test.
