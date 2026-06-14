#!/usr/bin/env python3
"""Structural edge check across 1-second instruments + volatility-clustering test.

For each symbol: fetch its REAL accumulator tick_size_barrier per growth tier
(live proposal) and a tick sample, then compute unconditional per-tick survival
q vs break-even 1/(1+gr). Also test lag-1 volatility clustering: does a small
previous tick predict a small next tick? If not, the fib premise cannot work.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_config
from src.deriv_client import DerivClient

SYMBOLS = ["1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"]
GROWTHS = [0.01, 0.02, 0.03, 0.04, 0.05]


async def fetch_ticks(cl, sym, pages):
    out = {}
    end = "latest"
    for _ in range(pages):
        h = await cl.ticks_history(sym, count=1000, end=end)
        t, p = h.get("times", []), h.get("prices", [])
        if not t:
            break
        for e, q in zip(t, p):
            out[int(e)] = float(q)
        end = int(t[0]) - 1
        await asyncio.sleep(0.12)
    return [out[k] for k in sorted(out)]


async def main():
    cl = DerivClient(get_config(), log=lambda *_: None)
    cl.resolve_account(); await cl.connect()
    print(f"{'symbol':>9}{'gr':>6}{'tsb':>11}{'be_q':>9}{'obs_q':>9}{'margin':>9}{'EV/tick':>9}")
    print("-" * 71)
    best = None
    for sym in SYMBOLS:
        # real tsb per tier
        tsb = {}
        for gr in GROWTHS:
            pr = await cl.proposal_accu(gr, symbol=sym)
            cd = pr.get("contract_details", {})
            tsb[gr] = float(cd.get("tick_size_barrier") or 0)
        prices = await fetch_ticks(cl, sym, 15)
        rets = [abs(prices[i] / prices[i-1] - 1.0) for i in range(1, len(prices))]
        for gr in GROWTHS:
            q = sum(1 for r in rets if r <= tsb[gr]) / len(rets)
            be = 1.0 / (1.0 + gr)
            ev = q * (1 + gr) - 1.0
            mark = "  <== +EV" if ev > 0 else ""
            print(f"{sym:>9}{gr:>6.2f}{tsb[gr]:>11.7f}{be:>9.5f}{q:>9.5f}"
                  f"{q-be:>+9.5f}{ev:>+9.5f}{mark}")
            if best is None or ev > best[1]:
                best = (f"{sym}@{gr}", ev)
        # lag-1 volatility clustering on this symbol (1% tsb)
        t1 = tsb[0.01]
        small = [r <= t1 for r in rets]
        q_un = sum(small) / len(small)
        pair = [(small[i-1], small[i]) for i in range(1, len(small))]
        cond_small = [b for a, b in pair if a]
        q_cond = sum(cond_small) / len(cond_small) if cond_small else 0
        print(f"{sym:>9}  clustering: q(next small)={q_un:.4f}  "
              f"q(next small | prev small)={q_cond:.4f}  lift={q_cond-q_un:+.4f}")
        print("-" * 71)
    print(f"\nBEST EV/tick across all symbols/tiers: {best[0]} = {best[1]:+.5f} "
          f"({'POSITIVE — exploitable' if best[1] > 0 else 'still negative — no edge'})")
    await cl.close()

asyncio.run(main())
