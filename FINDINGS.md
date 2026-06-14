# Findings — Accumulator strategy edge analysis

Reproduce with `python backtest.py`, `python analyze.py`, `python analyze2.py`.
All numbers below are measured from live Deriv data (demo == real RNG for synthetics).

## TL;DR
The bot implements the spec faithfully and trades correctly on the demo account,
but **the strategy has no positive expectancy on Deriv's synthetic accumulators.**
Every instrument/growth-tier combination is −EV, the fib "quiet window" filter has
no predictive power, and there is no short-term volatility clustering to exploit.

## Why break-even is so high
A knockout loses the entire stake; a surviving tick multiplies value by `(1+g)`.
So holding one more tick is +EV only if per-tick survival
`q > 1/(1+g)`:

| growth | break-even per-tick survival |
|---|---|
| 1% | 99.01% |
| 2% | 98.04% |
| 3% | 97.09% |
| 4% | 96.15% |
| 5% | 95.24% |

## 1. Backtest over ~1,000 minutes of 1HZ50V (replay of the live decision code)
```
profile                entries  entry%   surv%    win%  avgTk   pnl/ent   pnl/min    totPnl
core                       231   23.1%   79.7%   79.7%   11.7  -0.09347 -0.021591   -21.591
core_ratio75               183   18.3%   78.7%   78.7%   11.7  -0.10445 -0.019115   -19.115
core+4+10                  168   16.8%   79.2%   79.2%   11.8  -0.09901 -0.016634   -16.634
default                      2    0.2%   50.0%   ...    27.5  -0.33935 -0.000679    -0.679
```
The full core strategy loses ~$21.6 per 1,000 minutes at $1 stake. Stacking the
improvement filters cuts entry count (eventually to near-zero, where results are
just noise) but never produces a real positive edge.

## 2. Per-tick survival vs break-even (1HZ50V, all tiers)
```
   gr         tsb  breakeven_q  observed_q    margin   EV/tick
 0.01   0.0002166      0.99010     0.98413  -0.00597  -0.00603
 0.02   0.0002024      0.98039     0.97647  -0.00393  -0.00400
 0.03   0.0001898      0.97087     0.96633  -0.00454  -0.00468
 0.04   0.0001806      0.96154     0.95650  -0.00504  -0.00524
 0.05   0.0001719      0.95238     0.94535  -0.00703  -0.00738
```
Observed survival is **below** break-even at every tier → negative EV per tick.

## 3. Does the fib filter help? (survival bucketed by fib ratio)
```
  ratio<  entries  surv_all%  pertick_q  be_q@1%  EV/tick
    0.25       52      80.8%    0.98353  0.99010 -0.00664
    0.50       60      80.0%    0.98340  0.99010 -0.00676
    0.75       71      76.1%    0.97998  0.99010 -0.01022
    1.00       48      83.3%    0.98604  0.99010 -0.00410
    1.25       68      86.8%    0.98930  0.99010 -0.00081
    ...
```
Per-tick survival is **flat (~98.3%)** regardless of the ratio. The "quiet"
(low-ratio) buckets are not safer than loud ones, and none clears break-even.
The core hypothesis (quiet windows survive better) is not supported by data.

## 4. Structural check — all 1-second instruments + volatility clustering
```
   symbol    gr        tsb     be_q    obs_q   margin  EV/tick
   1HZ10V  0.03  0.0000380  0.97087  0.96813 -0.00274 -0.00282
   1HZ25V  0.02  0.0001012  0.98039  0.97867 -0.00173 -0.00176
   1HZ50V  0.02  0.0002024  0.98039  0.97747 -0.00293 -0.00299
   1HZ75V  0.03  0.0002847  0.97087  0.97000 -0.00088 -0.00090   <- least bad anywhere
  1HZ100V  0.03  0.0003797  0.97087  0.96446 -0.00641 -0.00660

  clustering lift  q(next small | prev small) - q(next small):
   1HZ10V +0.0000   1HZ25V +0.0001   1HZ50V +0.0002   1HZ75V -0.0001   1HZ100V +0.0001

BEST EV/tick across ALL symbols/tiers = 1HZ75V@3% = -0.00090  (still negative)
```
* No instrument/tier is +EV. Best across the entire grid is −0.09%/tick.
* Lag-1 volatility-clustering lift ≈ 0 everywhere → a quiet tick does **not**
  predict a quiet next tick. With no persistence, a volatility-window filter
  cannot work; Deriv's barriers are calibrated house-favorable by design.

## Practical takeaways
* The execution stack (new-API auth, OTP, WS, proposal, buy, monitor, adaptive
  exit, logging) is correct and verified live — see the demo trades in the run logs.
* For data collection without spending balance, run `python run.py --dry-run`.
* No staking/exit/tier tweak changes the sign of EV; the only +EV action is not
  trading these contracts.
