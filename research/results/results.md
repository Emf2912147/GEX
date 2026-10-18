# Course pattern backtest

Index series: 4 · broad tickers: 75 · horizon 10 days · outcomes in ATR, drift-adjusted

| Test | Lesson | Grade | Index effect (t, n) | 1st half / 2nd half | Broad effect (t, n) |
|---|---|---|---|---|---|
| Zone touch count | 5 (Law 1) | **CREDIBLE** | +0.088 (t +2.8, n 1453) | +0.133 / +0.030 | +0.023 (t +3.2, n 25088) |
| Gap breakout vs grind | 5 (Law 2) | **NONE** | +0.162 (t +1.2, n 333) | +0.069 / +0.231 | +0.101 (t +2.6, n 5108) |
| Move character: orderly | 5 (Law 3) | **NONE** | -0.086 (t -0.1, n 151) | +0.269 / -0.388 | -0.068 (t -0.6, n 2327) |
| Move character: absorbed | 5 (Law 3) | **NONE** | +0.101 (t +0.2, n 13) | -0.199 / +0.344 | -0.010 (t -0.1, n 545) |
| Move character: violent | 5 (Law 3) | **NONE** | +5.040 (t +2.6, n 5) | – / – | +0.416 (t +2.6, n 278) |
| Close strength vs Fibonacci | 5 (Law 4) | **NONE** | +0.129 (t +0.9, n 573) | +0.032 / +0.312 | +0.088 (t +2.5, n 8408) |
| Breakout from contraction | 1 (Master Pattern) | **NONE** | -0.115 (t -0.9, n 541) | -0.049 / -0.188 | +0.012 (t +0.3, n 7766) |
| Late entry (extended) | 1-2 (entries) | **NONE** | -0.036 (t -0.3, n 1903) | -0.036 / -0.045 | -0.003 (t -0.1, n 31356) |
| Stop sweep and reclaim | 6 | **NONE** | -0.012 (t -0.2, n 1300) | +0.036 / -0.080 | -0.012 (t -0.8, n 22465) |
| Sweep vs clean breakdown | 6 | **NONE** | -0.001 (t -0.0, n 1300) | +0.114 / -0.155 | +0.038 (t +1.6, n 22465) |
| Pin window (monthly OPEX) | 8 | **SLIGHT** | +0.019 (t +2.0, n 10058) | +0.009 / +0.032 | +0.014 (t +5.3, n 167591) |
| Shakeout window (monthly OPEX) | 8 | **CONTRADICTED** | -0.050 (t -2.4, n 778) | -0.016 / -0.094 | -0.020 (t -3.5, n 10044) |
| Pin window (quad witching) | 8 | **NONE** | -0.009 (t -0.7, n 5352) | -0.020 / +0.004 | -0.011 (t -3.2, n 89243) |
| Shakeout window (quad witching) | 8 | **NONE** | +0.002 (t +0.0, n 235) | +0.099 / -0.088 | -0.021 (t -2.1, n 3053) |
| Open-interest wall shapes | 7 | **UNTESTED** | – (t –, n –) | – / – | – (t –, n –) |

Notes:
- **Zone touch count** — First touch of a support/resistance zone holds more often than the 3rd+. Metric: hold rate, 1st touch minus 3rd+ touch. → CREDIBLE: significant on the indices, stable across both halves, confirmed on stocks. Hold rates by touch (1/2/3+): {'INDEX': {'1': 0.44, '2': 0.392, '3': 0.352}, 'BROAD': {'1': 0.477, '2': 0.456, '3': 0.454}}
- **Gap breakout vs grind** — Breaking a 20-day level on a gap continues better than grinding through. Metric: 10-day continuation (ATR), gap minus grind. → NONE: no reliable effect on the indices.
- **Move character: orderly** — Steep, orderly moves continue. Metric: 10-day continuation (ATR) vs ordinary trends. → NONE: no reliable effect on the indices.
- **Move character: absorbed** — Steep-then-stalling moves reverse. Metric: 10-day continuation (ATR) vs ordinary trends (sign flipped: reversal). → NONE: no reliable effect on the indices.
- **Move character: violent** — Wide, violent moves have no trend: stand aside. Metric: mean continuation ~0 and wider dispersion than ordinary trends. → NONE: not supported. Dispersion ratio: –
- **Close strength vs Fibonacci** — Moves that hold above the 23.6% retracement continue better than those that lose 50%. Metric: 10-day continuation (ATR), strong minus weak. → NONE: no reliable effect on the indices.
- **Breakout from contraction** — Breakouts out of a volatility contraction continue better than other breakouts. Metric: 10-day continuation (ATR), squeeze minus other. → NONE: no reliable effect on the indices.
- **Late entry (extended)** — Entering > 2.5 ATR from the 20-day EMA does worse than entering 0.5-1.5 ATR away. Metric: 10-day continuation (ATR), normal minus extended. → NONE: no reliable effect on the indices.
- **Stop sweep and reclaim** — A sweep below a swing low that is reclaimed leads to a move the other way. Metric: 10-day move in the signal direction (ATR). → NONE: no reliable effect on the indices.
- **Sweep vs clean breakdown** — Reclaimed sweeps beat breakdowns that hold. Metric: 10-day move in the signal direction (ATR), sweep minus breakdown. → NONE: no reliable effect on the indices.
- **Pin window (monthly OPEX)** — Price compresses (pins) in the days before expiration. Metric: daily |move| / ATR, other days minus pin window. → SLIGHT: right direction on the indices but not strong or stable enough.
- **Shakeout window (monthly OPEX)** — Breakouts in the shakeout window fail more often. Metric: false-breakout rate, shakeout minus other days. → CONTRADICTED: index shows the opposite of the course rule.
- **Pin window (quad witching)** — Price compresses (pins) in the days before expiration. Metric: daily |move| / ATR, other days minus pin window. → NONE: no reliable effect on the indices.
- **Shakeout window (quad witching)** — Breakouts in the shakeout window fail more often. Metric: false-breakout rate, shakeout minus other days. → NONE: no reliable effect on the indices.
- **Open-interest wall shapes** — One-sided walls V-reverse; stacked walls chop; sloping walls give an ABC path. Metric: needs daily open-interest history. → UNTESTED: only 35 sessions of stored chains (2 monthly expirations): too few to test.
