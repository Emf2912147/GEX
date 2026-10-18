# Options-era challenge (Oct 2020 - today)

Index: SPY, QQQ, IWM · broad: 75 tickers · put/call days: 1767 · 10-day outcomes in ATR, drift-adjusted, in the signal's direction

Average daily listed options volume (Cboe, all products): 2019: 4.3M, 2020: 5.3M, 2021: 6.0M, 2022: 6.8M, 2023: 8.1M, 2024: 8.7M, 2025: 10.8M, 2026: 13.6M

## 1. Patterns alone, options era only (placebo-adjusted, as v1)

| Test | Grade | Index excess (t, n) | halves | Broad excess (t, n) |
|---|---|---|---|---|
| Zone touch count | **NONE** | +0.010 (t +0.1, n 294) | +0.028 / -0.030 | -0.000 (t -0.0, n 7885) |
| Gap breakout vs grind | **NONE** | -0.244 (t -0.5, n 98) | -0.581 / +0.228 | -0.021 (t -0.2, n 1899) |
| Move character: orderly | **NONE** | – (t –, n 38) | – / – | -0.160 (t -0.6, n 888) |
| Move character: absorbed | **NONE** | – (t –, n 2) | – / – | +0.265 (t +1.0, n 216) |
| Move character: violent | **NONE** | – (t –, n 2) | – / – | -0.071 (t -0.2, n 216) |
| Close strength vs Fibonacci | **SLIGHT** | +0.234 (t +0.7, n 151) | +0.330 / +0.112 | +0.247 (t +3.0, n 2859) |
| Breakout from contraction | **NONE** | +0.295 (t +0.8, n 115) | +0.508 / +0.049 | -0.056 (t -0.5, n 2339) |
| Late entry (extended) | **NONE** | -0.082 (t -0.4, n 369) | -0.345 / +0.102 | -0.106 (t -1.4, n 9183) |
| Stop sweep and reclaim | **NONE** | -0.028 (t -0.1, n 267) | +0.042 / -0.101 | +0.033 (t +0.7, n 7237) |
| Sweep vs clean breakdown | **NONE** | -0.253 (t -0.9, n 267) | -0.121 / -0.389 | -0.048 (t -0.6, n 7237) |
| Pin window (monthly OPEX) | **SLIGHT** | +0.091 (t +2.0, n 2205) | +0.110 / +0.067 | +0.034 (t +6.0, n 55125) |
| Shakeout window (monthly OPEX) | **NONE** | -0.099 (t -1.0, n 168) | -0.174 / -0.037 | -0.008 (t -0.5, n 3215) |
| Pin window (quad witching) | **NONE** | +0.003 (t +0.1, n 1176) | -0.006 / +0.016 | -0.023 (t -2.1, n 29400) |
| Shakeout window (quad witching) | **NONE** | -0.041 (t -0.3, n 54) | -0.119 / +0.061 | -0.045 (t -1.7, n 1120) |
| Open-interest wall shapes | **UNTESTED** | – (t –, n –) | – / – | – (t –, n –) |

## 2. Indicators alone (signal direction, 10-day move in ATR)

| Indicator extreme | Index mean (t, n, win%) | Broad mean (t, n, win%) |
|---|---|---|
| RSI extreme | +0.22 (t +1.0, n 103, +50%) | +0.05 (t +1.1, n 2839, +52%) |
| Put/call extreme | -0.03 (t -0.2, n 246, +53%) | -0.04 (t -1.3, n 6150, +51%) |
| RSI divergence | -0.06 (t -0.3, n 142, +46%) | +0.08 (t +1.9, n 3225, +52%) |
| Any of the three | +0.07 (t +0.6, n 339, +51%) | +0.03 (t +1.3, n 9289, +52%) |

## 3. Pattern x extreme

A = pattern at an aligned extreme, B = pattern without it, C = the extreme alone.

| Pattern | Context | Grade | Index A / B / C | A-B (t) | A-C (t) | halves A-B | n A | Broad A-B (t) |
|---|---|---|---|---|---|---|---|---|
| First touch of a zone | RSI extreme | **NONE** | +2.44 / +0.03 / +0.22 | – (–) | – (–) | – / – | 4 | +0.06 (+0.3) |
| First touch of a zone | Put/call extreme | **NONE** | -0.03 / +0.08 / -0.03 | -0.11 (-0.4) | -0.00 (-0.0) | -0.80 / +0.69 | 45 | +0.03 (+0.4) |
| First touch of a zone | RSI divergence | **NONE** | +0.22 / +0.04 / -0.06 | +0.18 (+0.4) | +0.28 (+0.6) | +0.25 / +0.13 | 37 | -0.05 (-0.6) |
| First touch of a zone | Any of the three | **NONE** | +0.24 / -0.01 / +0.07 | +0.24 (+0.8) | +0.16 (+0.6) | -0.09 / +0.56 | 74 | +0.00 (+0.1) |
| 3rd+ touch of a zone | RSI extreme | **NONE** | – / -0.40 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| 3rd+ touch of a zone | Put/call extreme | **NONE** | -1.00 / -0.23 / -0.03 | -0.77 (-1.2) | -0.97 (-1.8) | – / -0.73 | 12 | +0.16 (+0.9) |
| 3rd+ touch of a zone | RSI divergence | **NONE** | -0.07 / -0.50 / -0.06 | +0.43 (+0.7) | -0.01 (-0.0) | – / -0.03 | 13 | +0.13 (+0.5) |
| 3rd+ touch of a zone | Any of the three | **NONE** | -0.52 / -0.31 / +0.07 | -0.21 (-0.4) | -0.59 (-1.4) | – / -0.55 | 22 | +0.11 (+0.8) |
| Gap breakout | RSI extreme | **NONE** | – / +0.24 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| Gap breakout | Put/call extreme | **NONE** | -1.17 / +0.26 / -0.03 | – (–) | – (–) | – / – | 1 | +0.01 (+0.0) |
| Gap breakout | RSI divergence | **NONE** | – / +0.24 / -0.06 | – (–) | – (–) | – / – | 0 | -0.79 (-1.9) |
| Gap breakout | Any of the three | **NONE** | -1.17 / +0.26 / +0.07 | – (–) | – (–) | – / – | 1 | -0.14 (-0.8) |
| Grind breakout | RSI extreme | **NONE** | – / -0.00 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| Grind breakout | Put/call extreme | **NONE** | -0.52 / +0.03 / -0.03 | -0.55 (-1.1) | -0.49 (-1.0) | – / +0.05 | 10 | +0.02 (+0.2) |
| Grind breakout | RSI divergence | **NONE** | +1.63 / -0.03 / -0.06 | – (–) | – (–) | – / – | 3 | -0.09 (-0.3) |
| Grind breakout | Any of the three | **NONE** | -0.03 / -0.00 / +0.07 | -0.03 (-0.1) | -0.10 (-0.2) | -0.38 / +0.35 | 13 | +0.01 (+0.1) |
| Fibonacci strong close | RSI extreme | **NONE** | – / -0.13 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| Fibonacci strong close | Put/call extreme | **NONE** | +1.61 / -0.17 / -0.03 | – (–) | – (–) | – / – | 3 | -0.00 (-0.0) |
| Fibonacci strong close | RSI divergence | **NONE** | – / -0.13 / -0.06 | – (–) | – (–) | – / – | 0 | – (–) |
| Fibonacci strong close | Any of the three | **NONE** | +1.61 / -0.17 / +0.07 | – (–) | – (–) | – / – | 3 | -0.00 (-0.0) |
| Orderly trend (continuation) | RSI extreme | **NONE** | – / -0.27 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| Orderly trend (continuation) | Put/call extreme | **NONE** | – / -0.27 / -0.03 | – (–) | – (–) | – / – | 0 | -0.11 (-0.4) |
| Orderly trend (continuation) | RSI divergence | **NONE** | – / -0.27 / -0.06 | – (–) | – (–) | – / – | 0 | – (–) |
| Orderly trend (continuation) | Any of the three | **NONE** | – / -0.27 / +0.07 | – (–) | – (–) | – / – | 0 | -0.11 (-0.4) |
| Absorbed move (reversal) | RSI extreme | **NONE** | -0.50 / +0.22 / +0.22 | -0.72 (-0.9) | -0.72 (-1.1) | – / -0.18 | 6 | +0.28 (+1.4) |
| Absorbed move (reversal) | Put/call extreme | **NONE** | -0.96 / +0.24 / -0.03 | – (–) | – (–) | – / – | 4 | -0.35 (-1.4) |
| Absorbed move (reversal) | RSI divergence | **NONE** | – / +0.00 / -0.06 | – (–) | – (–) | – / – | 0 | +0.55 (+1.3) |
| Absorbed move (reversal) | Any of the three | **NONE** | -0.42 / +0.35 / +0.07 | -0.77 (-1.0) | -0.50 (-0.9) | – / -1.08 | 9 | +0.14 (+0.7) |
| Breakout from contraction | RSI extreme | **NONE** | – / +0.05 / +0.22 | – (–) | – (–) | – / – | 0 | – (–) |
| Breakout from contraction | Put/call extreme | **NONE** | – / +0.05 / -0.03 | – (–) | – (–) | – / – | 0 | -0.15 (-0.8) |
| Breakout from contraction | RSI divergence | **NONE** | +1.63 / +0.01 / -0.06 | – (–) | – (–) | – / – | 3 | -0.12 (-0.5) |
| Breakout from contraction | Any of the three | **NONE** | +1.63 / +0.01 / +0.07 | – (–) | – (–) | – / – | 3 | -0.14 (-0.8) |
| Extended move (fade) | RSI extreme | **NONE** | +0.16 / -0.05 / +0.22 | +0.21 (+0.6) | -0.06 (-0.2) | +0.63 / -0.13 | 69 | +0.11 (+1.1) |
| Extended move (fade) | Put/call extreme | **NONE** | -0.09 / +0.16 / -0.03 | -0.25 (-0.7) | -0.06 (-0.2) | -0.27 / -0.18 | 42 | +0.28 (+2.5) |
| Extended move (fade) | RSI divergence | **NONE** | +0.53 / +0.03 / -0.06 | +0.50 (+0.6) | +0.59 (+0.7) | – / +0.22 | 10 | +0.24 (+1.2) |
| Extended move (fade) | Any of the three | **NONE** | -0.01 / +0.33 / +0.07 | -0.34 (-0.7) | -0.08 (-0.3) | +0.01 / -0.50 | 90 | +0.09 (+0.9) |
| Stop sweep and reclaim | RSI extreme | **NONE** | +1.38 / -0.16 / +0.22 | – (–) | – (–) | – / – | 4 | +0.01 (+0.1) |
| Stop sweep and reclaim | Put/call extreme | **NONE** | -0.12 / -0.14 / -0.03 | +0.02 (+0.1) | -0.09 (-0.2) | -0.53 / +0.79 | 46 | -0.11 (-1.4) |
| Stop sweep and reclaim | RSI divergence | **NONE** | +0.11 / -0.18 / -0.06 | +0.29 (+0.8) | +0.17 (+0.4) | +0.98 / -0.10 | 40 | +0.10 (+1.1) |
| Stop sweep and reclaim | Any of the three | **NONE** | +0.00 / -0.20 / +0.07 | +0.20 (+0.7) | -0.07 (-0.3) | -0.05 / +0.43 | 79 | -0.04 (-0.6) |
