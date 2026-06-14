"""
Accumulator strategy logic — core spec (Section 1.5) + the 10 improvement
vectors, written as pure / easily-testable units.

Tick numbering: instruments are UTC-minute aligned. For a 1s instrument the
n-th tick of a minute is  tick_no = (epoch % 60) + 1, so tick 1 = second 0,
tick 15 = second 14, ... tick 60 = second 59.  This matches how Deriv builds
1-minute candles and gives a stable mapping that tolerates the odd missing
tick.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque
from typing import Dict, List, Optional, Tuple

from config import StrategyConfig


# ---------------------------------------------------------------- knockout
def tick_knockout(prev_price: float, price: float, tsb: float) -> bool:
    """True if `price` breaches the accumulator band centred on `prev_price`.

    Verified live: band = prev_spot * (1 +/- tick_size_barrier), so the
    contract survives a tick iff |price/prev_price - 1| <= tsb.
    """
    if prev_price <= 0:
        return False
    return abs(price - prev_price) > tsb * prev_price


def classify_settled_outcome(status, profit, sell_price) -> str:
    """Outcome label for a contract that ended. Accumulators auto-terminate only
    on knockout (loss) or on hitting max payout/ticks (win); a negative profit
    therefore means a knockout even if `status` has not flipped to "lost" yet."""
    try:
        pv = float(profit)
    except (TypeError, ValueError):
        pv = 0.0
    if status == "lost" or pv < 0:
        return "KNOCKED_OUT"
    if sell_price:
        return "MANUAL_CLOSE"
    return "SURVIVED"


# --------------------------------------------------------------- fib expand
@dataclass
class FibExpansion:
    body_size: float
    direction: str            # "BULLISH" / "BEARISH"
    level_plus4: float
    level_minus4: float
    fib_spread: float         # == fib_multiple * body_size


def fib_expansion(p_open: float, p_close: float, fib_multiple: float = 8.0) -> FibExpansion:
    """±4 fib expansion from a single tick body (Section 1.3 / 1.5 Step 3).

    NOTE — spec correction: Section 1.5 STEP 3 states `fib_spread = 8 * body_size`
    ("always 8x ... regardless of direction"), and the value `8` is load-bearing
    throughout the spec (the n/8 quiet threshold in Improvements 2, 3 and 10).
    However the *level* formulae given there (level_+4 = body_high + 3*body,
    level_-4 = body_low - 3*body) actually span (P16-P15) + 6*body = 7*body, which
    contradicts the stated 8x. We implement the internally-consistent design:
    fib_spread = fib_multiple * body, with the diagnostic ±4 levels placed
    symmetrically about the body mid-point so that level_+4 - level_-4 == fib_spread.
    """
    body = abs(p_close - p_open)
    bull = p_close >= p_open
    spread = fib_multiple * body
    mid = (p_open + p_close) / 2.0
    plus4 = mid + spread / 2.0
    minus4 = mid - spread / 2.0
    return FibExpansion(body, "BULLISH" if bull else "BEARISH", plus4, minus4, spread)


# ----------------------------------------------------------- minute tracker
class MinuteTracker:
    """Maps incoming ticks into the current UTC minute candle and keeps a
    rolling body-size history for the 60-minute regime filter."""

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.current_minute: Optional[int] = None
        self.prices: Dict[int, float] = {}        # tick_no -> price (current minute)
        self.last_epoch: Optional[int] = None
        self.last_price: Optional[float] = None
        # rolling history of (epoch, body) over the regime window
        self._bodies = deque()
        self._baseline_sum = 0.0
        self._baseline_n = 0

    def add_tick(self, epoch: int, price: float) -> Tuple[int, bool]:
        """Returns (tick_no, minute_rolled)."""
        minute = epoch // 60
        tick_no = (epoch % 60) + 1
        rolled = False
        if self.current_minute is None:
            self.current_minute = minute
        elif minute != self.current_minute:
            self.current_minute = minute
            self.prices = {}
            rolled = True
        self.prices[tick_no] = price
        # rolling body history (consecutive ticks, any minute)
        if self.last_price is not None and self.last_epoch is not None and epoch > self.last_epoch:
            body = abs(price - self.last_price)
            self._bodies.append((epoch, body))
            self._baseline_sum += body
            self._baseline_n += 1
            horizon = self.cfg.regime_window_min * 60
            while self._bodies and self._bodies[0][0] < epoch - horizon:
                self._bodies.popleft()
        self.last_epoch = epoch
        self.last_price = price
        return tick_no, rolled

    def price(self, tick_no: int) -> Optional[float]:
        return self.prices.get(tick_no)

    def body(self, tick_no: int) -> Optional[float]:
        a, b = self.prices.get(tick_no - 1), self.prices.get(tick_no)
        if a is None or b is None:
            return None
        return abs(b - a)

    def recent_avg_body(self) -> Optional[float]:
        if not self._bodies:
            return None
        return sum(b for _, b in self._bodies) / len(self._bodies)

    def baseline_body(self) -> Optional[float]:
        if self._baseline_n == 0:
            return None
        return self._baseline_sum / self._baseline_n


# --------------------------------------------------------------- decision
@dataclass
class EntryDecision:
    enter: bool
    reason: str
    growth_rate: float
    n: float
    body_size: float
    fib_spread: float
    ratio: float
    direction: str
    anchor_tick: int
    # diagnostic gate flags
    floor_ok: bool = True
    ratio_ok: bool = True
    consec_ok: bool = True
    regime_ok: bool = True
    cross_ok: bool = True
    sema_ok: bool = True


def _select_anchor(tracker: MinuteTracker, cfg: StrategyConfig) -> Tuple[int, float, float]:
    """Return (anchor_tick, p_open, p_close) for the reference body."""
    if cfg.use_dynamic_anchor:
        best_tick, best_body = None, None
        for k in range(cfg.anchor_scan_lo + 1, cfg.anchor_scan_hi + 1):
            bd = tracker.body(k)
            if bd is None:
                continue
            if best_body is None or bd < best_body:
                best_body, best_tick = bd, k
        if best_tick is not None:
            return best_tick, tracker.price(best_tick - 1), tracker.price(best_tick)
    # fixed anchor (p15 -> p16)
    return cfg.p16_tick, tracker.price(cfg.p15_tick), tracker.price(cfg.p16_tick)


def _avg_body(tracker: MinuteTracker, cfg: StrategyConfig) -> Optional[float]:
    bodies = [tracker.body(k) for k in cfg.avg_body_window]
    bodies = [b for b in bodies if b is not None]
    if not bodies:
        return None
    return sum(bodies) / len(bodies)


def evaluate_entry(tracker: MinuteTracker, cfg: StrategyConfig, n: float, spot: float,
                   cross_pass: bool = True, sema_pass: bool = True) -> EntryDecision:
    """Core entry signal + improvements 1,2,3,4,5,7,8,9,10."""
    anchor_tick, p_open, p_close = _select_anchor(tracker, cfg)
    if p_open is None or p_close is None or n <= 0:
        return EntryDecision(False, "missing_data", cfg.base_growth_rate, n, 0, 0, 0, "NONE", anchor_tick)

    fib = fib_expansion(p_open, p_close, cfg.fib_multiple)
    body_size = fib.body_size

    # Improvement 2: replace single-tick body with windowed average
    if cfg.use_avg_body:
        ab = _avg_body(tracker, cfg)
        if ab is not None:
            body_size = ab
    fib_spread = cfg.fib_multiple * body_size
    ratio = fib_spread / n
    quiet_thresh = n / cfg.fib_multiple          # body must be < n/8 to be "quiet"

    # Improvement 10: minimum body floor (reject near-zero "price repeat")
    floor_ok = True
    if cfg.use_body_floor:
        floor = cfg.body_floor_frac * quiet_thresh
        floor_ok = body_size >= floor

    # Improvement 4: ratio cap (core spec is ratio < 1.0)
    cap = cfg.ratio_cap if cfg.use_ratio_cap else 1.0
    ratio_ok = ratio < cap

    # Improvement 3: consecutive quiet-tick confirmation
    consec_ok = True
    if cfg.use_consec_quiet:
        end = anchor_tick + 1   # last body considered is the entry tick body
        ticks = range(end - cfg.consec_quiet_count + 1, end + 1)
        bodies = [tracker.body(k) for k in ticks]
        if any(b is None for b in bodies):
            consec_ok = False
        else:
            consec_ok = all(b < quiet_thresh for b in bodies)

    # Improvement 5: 60-minute regime filter
    regime_ok = True
    if cfg.use_regime_filter:
        recent = tracker.recent_avg_body()
        baseline = cfg.regime_baseline_body or tracker.baseline_body()
        if recent is not None and baseline:
            regime_ok = recent <= cfg.regime_max_multiple * baseline

    # Improvement 8: dynamic growth-rate tier by ratio
    growth = cfg.base_growth_rate
    if cfg.use_dynamic_growth:
        for thr, gr in cfg.growth_tiers:
            if ratio < thr:
                growth = gr
                break

    enter = floor_ok and ratio_ok and consec_ok and regime_ok and cross_pass and sema_pass
    reason = "ENTER" if enter else "+".join(
        x for x, ok in [("floor", floor_ok), ("ratio", ratio_ok), ("consec", consec_ok),
                        ("regime", regime_ok), ("cross", cross_pass), ("sema", sema_pass)] if not ok
    )
    if cfg.force_entry:                 # testing aid: bypass gates, keep diagnostics
        enter, reason = True, "FORCED"
    return EntryDecision(enter, reason, growth, n, body_size, fib_spread, ratio,
                         fib.direction, anchor_tick, floor_ok, ratio_ok, consec_ok,
                         regime_ok, cross_pass, sema_pass)


# ---------------------------------------------- adaptive hold (Improvement 6)
class QuietScorer:
    """Running quiet score used to exit early or extend the hold."""

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.score = 0

    def update(self, prev_price: float, price: float, tsb: float) -> int:
        quiet_thresh = tsb * prev_price          # == n at this spot
        if abs(price - prev_price) < quiet_thresh:
            self.score += self.cfg.quiet_reward
        else:
            self.score -= self.cfg.noise_penalty
        return self.score

    def decide_exit(self, current_tick_no: int) -> Optional[str]:
        if self.score < self.cfg.quiet_score_exit:
            return "quiet_score_drop"
        if current_tick_no >= self.cfg.adaptive_max_tick:
            return "phase4_guard"
        return None
