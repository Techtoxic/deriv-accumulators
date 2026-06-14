# Deriv Accumulators Bot

A Python implementation of the **Accumulator "low-volatility window" strategy**
on Deriv's synthetic volatility indices, built directly against Deriv's **new
API platform** (PAT app → OTP → authenticated WebSocket). MT5 is not used — the
core logic from the `XU_SEMA_QFib_TZ` indicator (the ±4 Fibonacci expansion as a
volatility proxy) is re-implemented in Python.

It implements the full strategy specification (core Steps 1–7) plus all 10
improvement vectors as configurable, unit-tested logic, with a backtester and an
edge analyzer so every parameter choice is driven by data.

> Account safety: the client hard-filters to the **demo** account
> (`account_type == "demo"`). It will refuse to resolve a real account.

---

## 1. The new Deriv API (this is not the legacy `app_id` flow)

Deriv replaced the classic numeric `app_id` + `wss://ws.binaryws.com` flow. This
bot uses the current platform (verified live):

```
REST  GET  https://api.derivws.com/trading/v1/options/accounts          -> list accounts (pick demo)
REST  POST https://api.derivws.com/trading/v1/options/accounts/{id}/otp  -> authenticated WS url
WS    wss://api.derivws.com/trading/v1/options/ws/demo?otp=...           -> trade
```

* REST auth headers: `Authorization: Bearer <PAT>` + `Deriv-App-ID: <APP_ID>`.
* The PAT token (`pat_...`) and the alphanumeric App ID are the new-style credentials.
* Over the WS the message protocol is the classic shape (`proposal`, `buy`,
  `sell`, `ticks`, `proposal_open_contract`) but the proposal uses
  `underlying_symbol` (not `symbol`).

Schemas used to build this came from the official
`deriv-com/deriv-api-schemas` release (`schemas.zip`).

### Accumulator specifics (corrected against the live API)
* The barrier offset `n` the strategy needs is
  `proposal.contract_details.barrier_spot_distance` (absolute price distance from
  spot to each barrier). The top-level `barrier_spot_distance` field is
  **Turbos-only** in this API — the spec's assumption there is wrong.
* Equivalent: `n = tick_size_barrier * spot`.
* Knockout mechanic (reverse-engineered from live `proposal_open_contract`): at
  tick *k* the band re-centres on the **previous** spot with half-width
  `tick_size_barrier * spot`, so the contract survives a tick **iff**
  `|spot[k] - spot[k-1]| <= tick_size_barrier`.

---

## 2. Setup

```bash
pip install -r requirements.txt
```

Create a gitignored `.env` (already present in this workspace, never committed):

```
DERIV_TOKEN=pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DERIV_APP_ID=xxxxxxxxxxxxxxxxxxxxx
DERIV_REST_BASE=https://api.derivws.com
```

---

## 3. Usage

```bash
# Live demo, default profile (improvements 2,3,4,5,6,10 on)
python run.py

# Pure Section 1.5 core strategy (all improvements off)
python run.py --profile core

# Paper mode: log every minute's decision, never place an order (safe 24/7 data collection)
python run.py --dry-run

# Deterministic lifecycle test: enter every measurable minute, stop after N trades
python run.py --force-entry --max-trades 3 --stake 2

# Offline backtest / profile comparison over real historical ticks
python backtest.py --symbol 1HZ50V --ticks 60000

# Per-tick edge analysis (survival vs break-even, ratio buckets, growth tiers)
python analyze.py
# Structural check across all 1s instruments + volatility clustering
python analyze2.py

# Unit tests
python -m pytest -q      # or: python tests/test_strategy.py
```

Key flags: `--symbol`, `--stake`, `--growth`, `--max-trades`, `--profile`,
`--force-entry`, `--dry-run`.

---

## 4. Strategy & code map

| Spec section | Where |
|---|---|
| Tick→minute phase model (tick_no = epoch%60 + 1) | `src/strategy.py::MinuteTracker` |
| Fib expansion volatility proxy (Step 3) | `src/strategy.py::fib_expansion` |
| Entry signal Steps 1–6 | `src/strategy.py::evaluate_entry` |
| Buy / monitor / exit Step 7 | `src/bot.py` |
| Barrier fetch (Step 4) | `src/deriv_client.py::proposal_accu` |
| Logging schema (Section 1.8) | `src/trade_logger.py` |

**Improvements (all toggleable in `config.py`):**
1. Dynamic anchor (argmin body over a scan window) — `use_dynamic_anchor`
2. Multi-tick average body — `use_avg_body` *(default on)*
3. Consecutive quiet-tick confirmation — `use_consec_quiet` *(default on)*
4. Ratio threshold cap `r` — `use_ratio_cap`, `ratio_cap=0.75` *(default on)*
5. 60-minute regime filter — `use_regime_filter` *(default on)*
6. Adaptive hold via quiet score — `use_adaptive_hold` *(default on)*
7. Cross-instrument correlation — `use_cross_instrument`
8. Dynamic growth-rate tier by ratio — `use_dynamic_growth`
9. SEMA / micro-trend direction alignment — `use_sema_filter`
10. Minimum body floor — `use_body_floor` *(default on)*

### Spec correction caught during build
Section 1.5 Step 3 states `fib_spread = 8 * body_size` ("always 8x"), and `8` is
load-bearing everywhere (the `n/8` quiet threshold in Improvements 2, 3, 10). But
the level formulae given there (`level_+4 = body_high + 3*body`,
`level_-4 = body_low - 3*body`) actually span `7*body`, not 8. We implement the
internally-consistent `fib_spread = 8*body` and place the diagnostic ±4 levels
symmetrically so `level_+4 - level_-4 == fib_spread`. See
`src/strategy.py::fib_expansion`.

---

## 5. Backtest & edge analysis (measured, not assumed)

The backtester replays **real historical ticks** through the exact same
`MinuteTracker` + `evaluate_entry` used live, then simulates payoff with the
verified knockout rule.

**Break-even:** because a knockout loses the whole stake, holding a tick is +EV
only if per-tick survival `q > 1/(1+growth_rate)` (e.g. `q > 99.01%` at 1%).

Measured over ~1,000 minutes of `1HZ50V` and confirmed across all five 1-second
volatility indices (`1HZ10/25/50/75/100V`) at all five growth tiers:

| symbol | best tier | observed per-tick q | break-even q | EV / tick |
|---|---|---|---|---|
| 1HZ50V | 1% | 0.9841 | 0.9901 | **−0.60%** |
| 1HZ75V | 3% | 0.9700 | 0.9709 | **−0.09%** (least bad) |
| all others | — | — | — | negative |

* **Every instrument/tier is −EV.** The best across the whole grid is `1HZ75V@3%`
  at −0.09%/tick — still negative.
* **The fib filter has no predictive power.** Bucketing entries by the fib ratio,
  per-tick survival is flat (~98.3%) regardless of how "quiet" the window is; the
  low-ratio ("quiet") buckets are **not** above break-even.
* **No volatility clustering to exploit.** Lag-1 lift
  `q(next small | prev small) − q(next small) ≈ ±0.0001` on every instrument. The
  core hypothesis — that quiet ticks predict quiet ticks — does not hold on these
  RNG instruments; the barriers are calibrated house-favorable.

**Conclusion:** the bot is correct and trades exactly as specified, but no
parameter configuration of this strategy has a positive expectancy on Deriv's
synthetic accumulators. Use `--dry-run` to keep collecting the Section 1.8 logs;
treat live demo runs as execution/behaviour validation, not as a profit engine.

Reproduce: `python backtest.py` then `python analyze.py` / `python analyze2.py`.

---

## 6. Logging (Section 1.8)

Each minute writes one complete row to `logs/minutes_<run>.csv`
(`timestamp, symbol, P15, P16, body_size, fib_spread, n, ratio, entry_taken,
reason, growth_rate, tick17_price, tick30_price, outcome, pnl, ticks_survived,
contract_id` + gate flags). A structured `logs/events_<run>.jsonl` and a rolling
`logs/summary_<run>.json` (win rate, avg pnl/entry, knockouts) are written too.

---

## 7. Project layout

```
config.py            strategy params + improvement toggles + Deriv calibration table
run.py               live entrypoint (CLI flags, demo-only)
backtest.py          historical replay + profile comparison
analyze.py           per-tick edge / conditional-survival analysis
analyze2.py          cross-instrument + volatility-clustering structural check
src/deriv_client.py  REST OTP + async WS (request/subscribe, reconnect)
src/strategy.py      MinuteTracker, fib expansion, evaluate_entry, QuietScorer
src/bot.py           orchestrator: tick loop, buy, monitor, adaptive/fixed exit
src/trade_logger.py  Section 1.8 logging
tests/test_strategy.py  unit tests for the strategy math + gates
```
