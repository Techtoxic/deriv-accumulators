#!/usr/bin/env python3
"""Edge analysis: does low recent volatility actually raise the per-tick
accumulator survival probability above the break-even threshold?

Break-even (full stake lost on knockout): hold is +EV iff per-tick survival
q > 1/(1+growth_rate). We measure unconditional q per growth tier, then q
*conditioned* on the fib quiet-window filter, and the margin vs break-even.
"""
import json, os, sys, math
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import TICK_SIZE_BARRIER

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "ticks_cache.json")
ticks = [(int(e), float(p)) for e, p in json.load(open(CACHE))["ticks"]]
ticks.sort()
prices = [p for _, p in ticks]
N = len(prices)
rets = [abs(prices[i] / prices[i-1] - 1.0) for i in range(1, N)]
print(f"ticks={N}  span={ (ticks[-1][0]-ticks[0][0])/3600:.1f}h")

# unconditional per-tick survival q per growth tier
print("\n=== Unconditional per-tick survival vs break-even ===")
print(f"{'gr':>5}{'tsb':>12}{'breakeven_q':>13}{'observed_q':>12}{'margin':>10}{'EV/tick':>10}")
for gr, tsb in sorted(TICK_SIZE_BARRIER.items()):
    q = sum(1 for r in rets if r <= tsb) / len(rets)
    be = 1.0 / (1.0 + gr)
    evtick = q * (1 + gr) - 1.0
    print(f"{gr:>5.2f}{tsb:>12.7f}{be:>13.5f}{q:>12.5f}{q-be:>+10.5f}{evtick:>+10.5f}")

# build minute map: minute -> {tick_no: price}
minutes = defaultdict(dict)
for e, p in ticks:
    minutes[e // 60][(e % 60) + 1] = p

FIB = 8.0
def measure(minute):
    """fib ratio at fixed 15/16 anchor for a minute, using 1% barrier."""
    pm = minutes[minute]
    p15, p16 = pm.get(15), pm.get(16)
    if p15 is None or p16 is None:
        return None
    n = TICK_SIZE_BARRIER[0.01] * p16
    body = abs(p16 - p15)
    return (FIB * body) / n if n else None

# conditional survival: among entries (tick17) following a quiet window, what is
# the per-tick survival over the next 13 ticks, bucketed by ratio?
def post_entry_survival(minute, tsb, horizon=13):
    pm = minutes[minute]
    s_prev = pm.get(17)
    if s_prev is None:
        return None
    survived = 0
    for k in range(18, 18 + horizon):
        cur = pm.get(k)
        if cur is None:
            break
        if abs(cur - s_prev) > tsb * s_prev:
            return (survived, False)   # knockout
        survived += 1; s_prev = cur
    return (survived, True)

print("\n=== Survival to tick30 (13-tick hold @1%) bucketed by fib ratio ===")
buckets = defaultdict(lambda: [0, 0, 0])  # ratio_band -> [entries, survived_all, ticks_sum]
tsb1 = TICK_SIZE_BARRIER[0.01]
for m in minutes:
    r = measure(m)
    if r is None:
        continue
    res = post_entry_survival(m, tsb1, 13)
    if res is None:
        continue
    surv_ticks, full = res
    band = min(int(r / 0.25) * 0.25, 2.0)  # 0,0.25,0.5,...,2.0+
    buckets[band][0] += 1
    buckets[band][1] += 1 if full else 0
    buckets[band][2] += surv_ticks
print(f"{'ratio<':>8}{'entries':>9}{'surv_all%':>11}{'pertick_q':>11}{'be_q@1%':>9}{'EV/tick':>9}")
be1 = 1/1.01
for band in sorted(buckets):
    ent, surv, tks = buckets[band]
    if ent == 0:
        continue
    # per-tick q estimate from avg ticks survived before knockout/exit
    pertick = (tks / (tks + (ent - surv))) if (tks + (ent - surv)) else 0
    evt = pertick * 1.01 - 1
    print(f"{band+0.25:>8.2f}{ent:>9}{100*surv/ent:>10.1f}%{pertick:>11.5f}{be1:>9.5f}{evt:>+9.5f}")

# also: best growth tier conditional on strongest quiet (ratio<0.5)
print("\n=== Strong-quiet (ratio<0.5) entries: survival per growth tier ===")
print(f"{'gr':>5}{'entries':>9}{'pertick_q':>11}{'be_q':>9}{'EV/tick':>9}{'EV/13tk_hold':>14}")
for gr, tsb in sorted(TICK_SIZE_BARRIER.items()):
    ent = tks = ko = 0
    for m in minutes:
        r = measure(m)
        if r is None or r >= 0.5:
            continue
        res = post_entry_survival(m, tsb, 13)
        if res is None:
            continue
        surv_ticks, full = res
        ent += 1; tks += surv_ticks
        if not full:
            ko += 1
    if ent == 0:
        print(f"{gr:>5.2f}{ent:>9}  (none)"); continue
    pertick = tks / (tks + ko) if (tks + ko) else 0
    be = 1/(1+gr); evt = pertick*(1+gr)-1
    # EV of a 13-tick-target hold: survive13 -> (1+gr)^13-1 ; else -1
    p13 = pertick**13
    ev_hold = p13*((1+gr)**13 - 1) - (1-p13)*1
    print(f"{gr:>5.2f}{ent:>9}{pertick:>11.5f}{be:>9.5f}{evt:>+9.5f}{ev_hold:>+14.5f}")
