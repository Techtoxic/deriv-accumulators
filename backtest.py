#!/usr/bin/env python3
"""
Offline backtester / profile optimiser.

Replays real historical ticks through the SAME MinuteTracker + evaluate_entry
used by the live bot, then simulates the accumulator payoff with the
empirically-verified knockout rule:  knockout at tick k  iff
|spot[k] - spot[k-1]| > tick_size_barrier * spot[k-1];  otherwise the contract
value compounds by (1 + growth_rate) each surviving tick.

Lets us evaluate hundreds of minutes in seconds and compare strategy profiles
without risking (resettable) demo balance or waiting in real time.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from dataclasses import replace
from typing import Dict, List, Tuple

from config import Config, get_config, StrategyConfig
from src.deriv_client import DerivClient
from src.strategy import MinuteTracker, evaluate_entry, QuietScorer, tick_knockout

TICKS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "ticks_cache.json")


# ----------------------------------------------------------------- fetch
async def fetch_ticks(symbol: str, total: int) -> List[Tuple[int, float]]:
    cfg = get_config()
    client = DerivClient(cfg, log=lambda *_: None)
    client.resolve_account()
    await client.connect()
    out: Dict[int, float] = {}
    end = "latest"
    try:
        calls = 0
        while len(out) < total and calls < total // 800 + 5:
            hist = await client.ticks_history(symbol, count=1000, end=end)
            times = hist.get("times", []); prices = hist.get("prices", [])
            calls += 1
            if not times:
                break
            before = len(out)
            for t, p in zip(times, prices):
                out[int(t)] = float(p)
            if len(out) == before:      # no new ticks -> reached the start of history
                break
            end = int(times[0]) - 1     # page backwards (contiguous, verified)
            await asyncio.sleep(0.15)   # gentle on rate limits
    finally:
        await client.close()
    return sorted(out.items())


def load_or_fetch(symbol: str, total: int, refresh: bool) -> List[Tuple[int, float]]:
    if not refresh and os.path.exists(TICKS_CACHE):
        data = json.load(open(TICKS_CACHE))
        if data.get("symbol") == symbol and len(data.get("ticks", [])) >= total * 0.8:
            return [(int(e), float(p)) for e, p in data["ticks"]]
    ticks = asyncio.run(fetch_ticks(symbol, total))
    os.makedirs(os.path.dirname(TICKS_CACHE), exist_ok=True)
    json.dump({"symbol": symbol, "ticks": ticks}, open(TICKS_CACHE, "w"))
    return ticks


# ----------------------------------------------------------------- simulate
def simulate_hold(prices: Dict[int, float], entry_tick: int, tsb: float,
                  gr: float, stake: float, cfg: StrategyConfig):
    base_hold = cfg.hold_to_tick - entry_tick
    adaptive_cap = cfg.adaptive_max_tick - entry_tick
    s_prev = prices.get(entry_tick)
    if s_prev is None:
        return None
    scorer = QuietScorer(cfg)
    tick_passed = 0
    last_k = max(prices)
    for k in range(entry_tick + 1, min(60, entry_tick + adaptive_cap) + 1):
        cur = prices.get(k)
        if cur is None:
            break  # ran out of ticks this minute -> close at current value
        if tick_knockout(s_prev, cur, tsb):
            return {"outcome": "KNOCKED_OUT", "pnl": -stake, "ticks_survived": tick_passed}
        tick_passed += 1
        scorer.update(s_prev, cur, tsb)
        s_prev = cur
        if cfg.use_adaptive_hold:
            if scorer.score < cfg.quiet_score_exit and tick_passed >= 1:
                break
            if tick_passed >= adaptive_cap:
                break
            if tick_passed >= base_hold and scorer.score < cfg.quiet_score_extend:
                break
        elif tick_passed >= base_hold:
            break
    value = stake * ((1.0 + gr) ** tick_passed)
    return {"outcome": "MANUAL_CLOSE", "pnl": value - stake, "ticks_survived": tick_passed}


# ----------------------------------------------------------------- replay
def replay(cfg: Config, ticks: List[Tuple[int, float]]) -> dict:
    s = cfg.strat
    tracker = MinuteTracker(s)
    minutes: Dict[int, Dict[int, float]] = {}
    for epoch, price in ticks:
        minutes.setdefault(epoch // 60, {})[(epoch % 60) + 1] = price

    stats = {"minutes": 0, "evaluated": 0, "entries": 0, "knockouts": 0,
             "wins": 0, "losses": 0, "pnl": 0.0, "ticks_survived_sum": 0,
             "ratios": []}
    decided_minute = None
    for epoch, price in ticks:
        tick_no, rolled = tracker.add_tick(epoch, price)
        minute = tracker.current_minute
        if tick_no < s.entry_tick or decided_minute == minute:
            continue
        decided_minute = minute
        stats["minutes"] += 1
        p15, p16 = tracker.price(s.p15_tick), tracker.price(s.p16_tick)
        if p15 is None or p16 is None:
            continue
        spot = p16
        n = s.tsb(s.base_growth_rate) * spot
        d = evaluate_entry(tracker, s, n, spot)
        stats["evaluated"] += 1
        stats["ratios"].append(d.ratio)
        if not d.enter:
            continue
        sim = simulate_hold(minutes[minute], s.entry_tick, s.tsb(d.growth_rate),
                            d.growth_rate, s.stake, s)
        if sim is None:
            continue
        stats["entries"] += 1
        stats["pnl"] += sim["pnl"]
        stats["ticks_survived_sum"] += sim["ticks_survived"]
        if sim["outcome"] == "KNOCKED_OUT":
            stats["knockouts"] += 1; stats["losses"] += 1
        else:
            stats["wins" if sim["pnl"] >= 0 else "losses"] += 1

    e = max(stats["entries"], 1)
    ev = max(stats["evaluated"], 1)
    return {
        "minutes_scanned": stats["evaluated"],
        "entries": stats["entries"],
        "entry_rate": round(stats["entries"] / ev, 4),
        "knockouts": stats["knockouts"],
        "survival_rate": round(1 - stats["knockouts"] / e, 4),
        "win_rate": round(stats["wins"] / e, 4),
        "avg_ticks_survived": round(stats["ticks_survived_sum"] / e, 2),
        "total_pnl": round(stats["pnl"], 4),
        "avg_pnl_per_entry": round(stats["pnl"] / e, 5),
        "avg_pnl_per_minute": round(stats["pnl"] / ev, 6),
    }


# ----------------------------------------------------------------- profiles
def profile(name: str) -> Config:
    cfg = get_config()
    s = cfg.strat
    toggles = ["use_ratio_cap", "use_body_floor", "use_avg_body", "use_consec_quiet",
               "use_regime_filter", "use_adaptive_hold", "use_dynamic_growth",
               "use_dynamic_anchor", "use_sema_filter", "use_cross_instrument"]
    if name == "core":
        for t in toggles:
            setattr(s, t, False)
        s.ratio_cap = 1.0
    elif name == "core_ratio75":   # core + Impr4 only
        for t in toggles:
            setattr(s, t, False)
        s.use_ratio_cap = True; s.ratio_cap = 0.75
    elif name == "core+4+10":
        for t in toggles:
            setattr(s, t, False)
        s.use_ratio_cap = True; s.use_body_floor = True
    elif name == "core+4+10+3":
        for t in toggles:
            setattr(s, t, False)
        s.use_ratio_cap = s.use_body_floor = s.use_consec_quiet = True
    elif name == "core+4+10+3+2":
        for t in toggles:
            setattr(s, t, False)
        s.use_ratio_cap = s.use_body_floor = s.use_consec_quiet = s.use_avg_body = True
    elif name == "default":
        pass  # config defaults (4,10,3,2,5,6 on)
    elif name == "default+adaptive_off":
        s.use_adaptive_hold = False
    elif name == "default+dyn_growth":
        s.use_dynamic_growth = True
    elif name == "aggressive_ratio50":
        s.ratio_cap = 0.50
    return cfg


PROFILES = ["core", "core_ratio75", "core+4+10", "core+4+10+3", "core+4+10+3+2",
            "default", "default+adaptive_off", "default+dyn_growth", "aggressive_ratio50"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="1HZ50V")
    ap.add_argument("--ticks", type=int, default=60000, help="approx ticks to use (~min*60)")
    ap.add_argument("--refresh", action="store_true", help="force refetch tick history")
    ap.add_argument("--profiles", nargs="*", default=PROFILES)
    args = ap.parse_args()

    print(f"Loading ~{args.ticks} ticks for {args.symbol} ...")
    ticks = load_or_fetch(args.symbol, args.ticks, args.refresh)
    span_min = (ticks[-1][0] - ticks[0][0]) / 60 if len(ticks) > 1 else 0
    print(f"Loaded {len(ticks)} ticks spanning ~{span_min:.0f} minutes "
          f"({span_min/60:.1f}h)\n")

    rows = []
    for name in args.profiles:
        cfg = profile(name)
        cfg.strat.symbol = args.symbol
        r = replay(cfg, ticks)
        r["profile"] = name
        rows.append(r)

    cols = ["profile", "entries", "entry_rate", "survival_rate", "win_rate",
            "avg_ticks_survived", "avg_pnl_per_entry", "avg_pnl_per_minute", "total_pnl"]
    print(f"{'profile':<22}{'entries':>8}{'entry%':>8}{'surv%':>8}{'win%':>8}"
          f"{'avgTk':>7}{'pnl/ent':>10}{'pnl/min':>10}{'totPnl':>10}")
    print("-" * 101)
    for r in sorted(rows, key=lambda x: x["avg_pnl_per_minute"], reverse=True):
        print(f"{r['profile']:<22}{r['entries']:>8}{r['entry_rate']*100:>7.1f}%"
              f"{r['survival_rate']*100:>7.1f}%{r['win_rate']*100:>7.1f}%"
              f"{r['avg_ticks_survived']:>7.1f}{r['avg_pnl_per_entry']:>10.5f}"
              f"{r['avg_pnl_per_minute']:>10.6f}{r['total_pnl']:>10.3f}")
    out = os.path.join(os.path.dirname(TICKS_CACHE), "backtest_results.json")
    json.dump(rows, open(out, "w"), indent=2)
    print(f"\nSaved -> {out}")


if __name__ == "__main__":
    main()
