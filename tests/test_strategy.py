"""Unit tests for the accumulator strategy math + gates. Run with:
    python -m pytest -q     (or)    python tests/test_strategy.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import StrategyConfig
from src.strategy import (fib_expansion, tick_knockout, MinuteTracker,
                          evaluate_entry, QuietScorer, classify_settled_outcome)


def approx(a, b, eps=1e-9):
    return abs(a - b) <= eps


# ---------------------------------------------------------------- fib math
def test_fib_spread_is_8x_body_both_directions():
    up = fib_expansion(100.0, 101.0, 8.0)
    dn = fib_expansion(101.0, 100.0, 8.0)
    assert approx(up.body_size, 1.0) and approx(dn.body_size, 1.0)
    assert up.direction == "BULLISH" and dn.direction == "BEARISH"
    assert approx(up.fib_spread, 8.0) and approx(dn.fib_spread, 8.0)
    # diagnostic levels must be self-consistent with the spread
    assert approx(up.level_plus4 - up.level_minus4, up.fib_spread)
    assert approx(dn.level_plus4 - dn.level_minus4, dn.fib_spread)


def test_fib_zero_body():
    f = fib_expansion(100.0, 100.0, 8.0)
    assert approx(f.body_size, 0.0) and approx(f.fib_spread, 0.0)


# ---------------------------------------------------------------- knockout
def test_tick_knockout_boundary():
    tsb = 0.001  # 0.1%
    assert tick_knockout(100.0, 100.2, tsb) is True    # 0.2% move -> breach
    assert tick_knockout(100.0, 100.05, tsb) is False  # 0.05% move -> survive
    # exactly at the boundary survives (strict >)
    assert tick_knockout(100.0, 100.1, tsb) is False


# --------------------------------------------------------------- tracker
def test_minute_tracker_tick_numbering_and_rollover():
    cfg = StrategyConfig()
    tr = MinuteTracker(cfg)
    base = 1_000_000 * 60  # epoch aligned to a minute start
    # feed ticks for seconds 0..16 of one minute
    for sec in range(0, 17):
        tno, rolled = tr.add_tick(base + sec, 100.0 + sec)
        assert tno == sec + 1
        assert rolled is False
    assert tr.price(15) == 100.0 + 14   # tick 15 == second 14
    assert tr.price(16) == 100.0 + 15
    assert approx(tr.body(16), 1.0)
    # next minute rolls over
    tno, rolled = tr.add_tick(base + 60, 200.0)
    assert tno == 1 and rolled is True
    assert tr.price(15) is None


def _load_minute(tr, prices_by_tick, minute_index=2_000_000):
    """Populate a tracker's current minute deterministically."""
    base = minute_index * 60
    for tno in sorted(prices_by_tick):
        tr.add_tick(base + (tno - 1), prices_by_tick[tno])
    return tr


# --------------------------------------------------------------- entry gate
def _flat_minute(step):
    # build a minute where every consecutive body == `step`
    return {t: 100000.0 + step * t for t in range(1, 18)}


def test_core_entry_quiet_passes_and_loud_fails():
    cfg = StrategyConfig()
    # disable extra gates to test the core ratio rule in isolation
    cfg.use_avg_body = False; cfg.use_consec_quiet = False
    cfg.use_regime_filter = False; cfg.use_body_floor = False
    cfg.use_ratio_cap = False  # core spec ratio < 1.0
    n = 80.0  # so quiet_thresh = n/8 = 10
    tr = _load_minute(MinuteTracker(cfg), _flat_minute(step=1.0))   # body=1 -> fib=8 < 80
    d = evaluate_entry(tr, cfg, n, spot=100000.0)
    assert d.enter and approx(d.fib_spread, 8.0) and d.ratio < 1.0

    tr2 = _load_minute(MinuteTracker(cfg), _flat_minute(step=20.0)) # body=20 -> fib=160 > 80
    d2 = evaluate_entry(tr2, cfg, n, spot=100000.0)
    assert not d2.enter and d2.ratio > 1.0


def test_ratio_cap_improvement4():
    cfg = StrategyConfig()
    cfg.use_avg_body = False; cfg.use_consec_quiet = False
    cfg.use_regime_filter = False; cfg.use_body_floor = False
    cfg.use_ratio_cap = True; cfg.ratio_cap = 0.75
    n = 80.0
    # body=8 -> fib=64 -> ratio=0.8 > 0.75 -> rejected by cap (but < 1.0)
    tr = _load_minute(MinuteTracker(cfg), _flat_minute(step=8.0))
    d = evaluate_entry(tr, cfg, n, spot=100000.0)
    assert not d.enter and not d.ratio_ok
    # body=5 -> fib=40 -> ratio=0.5 < 0.75 -> ok
    tr2 = _load_minute(MinuteTracker(cfg), _flat_minute(step=5.0))
    d2 = evaluate_entry(tr2, cfg, n, spot=100000.0)
    assert d2.enter and d2.ratio_ok


def test_body_floor_improvement10():
    cfg = StrategyConfig()
    cfg.use_avg_body = False; cfg.use_consec_quiet = False
    cfg.use_regime_filter = False; cfg.use_ratio_cap = False
    cfg.use_body_floor = True; cfg.body_floor_frac = 0.10
    n = 80.0  # quiet_thresh=10, floor = 0.1*10 = 1.0
    tr = _load_minute(MinuteTracker(cfg), _flat_minute(step=0.5))   # body 0.5 < floor 1.0
    d = evaluate_entry(tr, cfg, n, spot=100000.0)
    assert not d.enter and not d.floor_ok


def test_consec_quiet_improvement3():
    cfg = StrategyConfig()
    cfg.use_avg_body = False; cfg.use_regime_filter = False
    cfg.use_ratio_cap = False; cfg.use_body_floor = False
    cfg.use_consec_quiet = True; cfg.consec_quiet_count = 3
    n = 80.0  # quiet_thresh = 10
    prices = _flat_minute(step=1.0)
    # inject one loud body at tick 16 (body 16->15 = 50 > 10) within the last-3 window
    prices[16] = prices[15] + 50.0
    tr = _load_minute(MinuteTracker(cfg), prices)
    d = evaluate_entry(tr, cfg, n, spot=100000.0)
    assert not d.consec_ok and not d.enter


def test_dynamic_growth_improvement8():
    cfg = StrategyConfig()
    cfg.use_avg_body = False; cfg.use_consec_quiet = False
    cfg.use_regime_filter = False; cfg.use_body_floor = False
    cfg.use_ratio_cap = True; cfg.ratio_cap = 0.75
    cfg.use_dynamic_growth = True
    n = 800.0  # quiet_thresh=100
    # body=10 -> fib=80 -> ratio=0.1 < 0.30 -> growth 0.03
    tr = _load_minute(MinuteTracker(cfg), _flat_minute(step=10.0))
    d = evaluate_entry(tr, cfg, n, spot=100000.0)
    assert d.enter and approx(d.growth_rate, 0.03)


# --------------------------------------------------------------- quiet score
def test_quiet_scorer():
    cfg = StrategyConfig()
    cfg.quiet_reward = 1; cfg.noise_penalty = 2
    qs = QuietScorer(cfg)
    tsb = 0.001
    qs.update(100.0, 100.02, tsb)   # quiet (+1)
    qs.update(100.0, 100.02, tsb)   # quiet (+1) -> 2
    assert qs.score == 2
    qs.update(100.0, 100.5, tsb)    # loud (-2) -> 0
    assert qs.score == 0


def test_classify_settled_outcome():
    # negative profit is a knockout even if status hasn't flipped to "lost" yet
    assert classify_settled_outcome("open", "-1.00", None) == "KNOCKED_OUT"
    assert classify_settled_outcome("lost", "-1.00", None) == "KNOCKED_OUT"
    # we sold it back -> manual close
    assert classify_settled_outcome("sold", "0.32", "1.32") == "MANUAL_CLOSE"
    # auto-terminated in profit (hit max payout/ticks), not sold by us
    assert classify_settled_outcome("won", "5.00", None) == "SURVIVED"
    # robust to missing profit
    assert classify_settled_outcome(None, None, None) == "SURVIVED"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn(); passed += 1; print(f"PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")
