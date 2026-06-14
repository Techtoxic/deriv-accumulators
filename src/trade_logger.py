"""
Logging per Section 1.8 — one row every minute whether or not an entry is
taken, plus a structured JSONL stream for richer optimisation records.
"""
from __future__ import annotations
import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

FIELDS = [
    "timestamp", "symbol", "P15", "P16", "body_size", "fib_spread", "n", "ratio",
    "entry_taken", "reason", "growth_rate", "tick17_price", "tick30_price",
    "outcome", "pnl", "ticks_survived", "contract_id",
    # diagnostic gate flags
    "floor_ok", "ratio_ok", "consec_ok", "regime_ok",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TradeLogger:
    def __init__(self, log_dir: str, run_tag: str = None):
        os.makedirs(log_dir, exist_ok=True)
        tag = run_tag or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.csv_path = os.path.join(log_dir, f"minutes_{tag}.csv")
        self.jsonl_path = os.path.join(log_dir, f"events_{tag}.jsonl")
        self.summary_path = os.path.join(log_dir, f"summary_{tag}.json")
        self._csv_init = False
        self.stats = {
            "minutes": 0, "entries": 0, "wins": 0, "losses": 0,
            "knockouts": 0, "manual_close": 0, "pnl": 0.0,
        }

    def _row(self, rec: dict) -> dict:
        return {k: rec.get(k, "") for k in FIELDS}

    def log_minute(self, rec: dict):
        self.stats["minutes"] += 1
        if rec.get("entry_taken"):
            self.stats["entries"] += 1
        with open(self.csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if not self._csv_init and os.stat(self.csv_path).st_size == 0:
                w.writeheader()
            self._csv_init = True
            w.writerow(self._row(rec))
        self.event("minute", rec)

    def log_trade_result(self, rec: dict):
        outcome = rec.get("outcome")
        pnl = float(rec.get("pnl") or 0)
        self.stats["pnl"] += pnl
        if outcome == "KNOCKED_OUT":
            self.stats["knockouts"] += 1
            self.stats["losses"] += 1
        elif outcome in ("SURVIVED", "MANUAL_CLOSE", "SOLD"):
            if pnl >= 0:
                self.stats["wins"] += 1
            else:
                self.stats["losses"] += 1
            if outcome != "SURVIVED":
                self.stats["manual_close"] += 1
        self.event("trade_result", rec)
        self.write_summary()

    def event(self, kind: str, data: dict):
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps({"ts": utcnow(), "kind": kind, **data}, default=str) + "\n")

    def write_summary(self):
        s = dict(self.stats)
        n = max(s["entries"], 1)
        s["win_rate"] = round(s["wins"] / n, 4)
        s["avg_pnl_per_entry"] = round(s["pnl"] / n, 4)
        s["updated"] = utcnow()
        with open(self.summary_path, "w") as f:
            json.dump(s, f, indent=2)

    def console_line(self, rec: dict) -> str:
        if rec.get("entry_taken"):
            return (f"{rec['timestamp']} ENTER {rec['symbol']} gr={rec['growth_rate']} "
                    f"ratio={rec['ratio']:.3f} n={rec['n']:.4f} body={rec['body_size']:.4f}")
        return (f"{rec['timestamp']} skip  {rec['symbol']} "
                f"ratio={rec.get('ratio', 0):.3f} n={rec.get('n', 0):.4f} "
                f"reason={rec.get('reason', '')}")
