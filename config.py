"""
Central configuration for the Deriv Accumulator bot.

Secrets (token / app id) are loaded from a gitignored .env file and never
hard-coded here. Everything else is a tunable strategy parameter so the
core spec and all 10 improvement vectors can be toggled / optimised.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List


# --------------------------------------------------------------------------
# .env loader (no external dependency)
# --------------------------------------------------------------------------
def load_env(path: str = None) -> Dict[str, str]:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    env: Dict[str, str] = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    # allow real environment to override file
    for k in ("DERIV_TOKEN", "DERIV_APP_ID", "DERIV_REST_BASE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()


# --------------------------------------------------------------------------
# Deriv calibration constants (measured live from ACCU proposals on 1HZ50V).
# tick_size_barrier is the fraction of spot used for the knockout band.
# These are Deriv platform calibrations, refreshed live at runtime but kept
# here as a backtest default / sanity baseline.
# --------------------------------------------------------------------------
TICK_SIZE_BARRIER: Dict[float, float] = {
    0.01: 0.000216569832,
    0.02: 0.000202418022,
    0.03: 0.000189832631,
    0.04: 0.000180618039,
    0.05: 0.000171916735,
}
MAX_TICKS: Dict[float, int] = {0.01: 250, 0.02: 125, 0.03: 85, 0.04: 65, 0.05: 50}


@dataclass
class ConnectionConfig:
    token: str = ENV.get("DERIV_TOKEN", "")
    app_id: str = ENV.get("DERIV_APP_ID", "")
    rest_base: str = ENV.get("DERIV_REST_BASE", "https://api.derivws.com")
    account_type: str = "demo"          # HARD SAFETY: only ever trade demo
    ws_max_size: int = 8 * 1024 * 1024
    request_timeout: float = 20.0
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0


@dataclass
class StrategyConfig:
    # ---- instrument / contract ----
    symbol: str = "1HZ50V"              # Volatility 50 (1s) — 60 ticks/minute
    currency: str = "USD"
    stake: float = 1.0                  # USD per contract (demo; resettable)
    base_growth_rate: float = 0.01      # reference tier for the entry gate

    # ---- candle / tick phase model (Section 1.4) ----
    ticks_per_minute: int = 60
    p15_tick: int = 15                  # last tick of Phase 1
    p16_tick: int = 16                  # first tick of Phase 2
    entry_tick: int = 17                # buy one tick after measurement
    hold_to_tick: int = 30              # boundary before Phase-3 regime risk

    # ---- core fib expansion (Section 1.5) ----
    fib_multiple: float = 8.0           # fib_spread = 8 * body_size

    # ---- Improvement 4: ratio threshold cap ----
    use_ratio_cap: bool = True
    ratio_cap: float = 0.75             # enter only if fib_spread / n < r

    # ---- Improvement 10: minimum body floor ----
    use_body_floor: bool = True
    body_floor_frac: float = 0.10       # reject if body < 0.10 * (n / fib_multiple)

    # ---- Improvement 2: multi-tick average body ----
    use_avg_body: bool = True
    avg_body_window: List[int] = field(default_factory=lambda: [13, 14, 15, 16, 17])

    # ---- Improvement 3: consecutive quiet-tick confirmation ----
    use_consec_quiet: bool = True
    consec_quiet_count: int = 3         # last N tick bodies must each be < n/8

    # ---- Improvement 1: dynamic anchor (argmin body over a scan window) ----
    use_dynamic_anchor: bool = False
    anchor_scan_lo: int = 5
    anchor_scan_hi: int = 25

    # ---- Improvement 5: 60-minute regime filter ----
    use_regime_filter: bool = True
    regime_window_min: int = 60
    regime_max_multiple: float = 1.5    # suppress if 60m avg body > 1.5x baseline
    regime_baseline_body: float = 0.0   # 0 => learn baseline from rolling history

    # ---- Improvement 6: adaptive hold via quiet score ----
    use_adaptive_hold: bool = True
    quiet_reward: int = 1
    noise_penalty: int = 2
    quiet_score_exit: int = 0           # exit if score drops below this
    quiet_score_extend: int = 10        # extend hold if score >= this
    adaptive_max_tick: int = 45         # never hold past Phase-4 burst

    # ---- Improvement 8: dynamic growth-rate tier by ratio ----
    use_dynamic_growth: bool = False    # default off until >=200 entries logged
    growth_tiers: List[tuple] = field(default_factory=lambda: [
        (0.30, 0.03),   # ratio < 0.30 -> 3%
        (0.50, 0.02),   # ratio < 0.50 -> 2%
        (0.75, 0.01),   # ratio < 0.75 -> 1%
    ])

    # ---- Improvement 7: cross-instrument correlation filter ----
    use_cross_instrument: bool = False
    cross_symbols: List[str] = field(default_factory=lambda: ["1HZ25V"])
    cross_ratio_cap: float = 0.75

    # ---- Improvement 9: SEMA / micro-trend direction alignment ----
    use_sema_filter: bool = False
    sema_period: int = 12               # micro-trend lookback (proxy for SEMA1)

    # ---- take-profit safety (optional accumulator limit order) ----
    use_take_profit: bool = False
    take_profit: float = 0.0

    # ---- runtime guards ----
    max_trades: int = 0                 # 0 = unlimited (until stopped)
    one_contract_at_a_time: bool = True
    force_entry: bool = False           # testing aid: enter every measurable minute
    dry_run: bool = False               # paper mode: log decisions, never buy

    def tsb(self, growth_rate: float) -> float:
        return TICK_SIZE_BARRIER.get(round(growth_rate, 2), TICK_SIZE_BARRIER[0.01])


@dataclass
class Config:
    conn: ConnectionConfig = field(default_factory=ConnectionConfig)
    strat: StrategyConfig = field(default_factory=StrategyConfig)
    log_dir: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def sanitized(self) -> dict:
        d = {"conn": asdict(self.conn), "strat": asdict(self.strat), "log_dir": self.log_dir}
        d["conn"]["token"] = "***REDACTED***" if d["conn"]["token"] else ""
        return d


def get_config() -> Config:
    return Config()
