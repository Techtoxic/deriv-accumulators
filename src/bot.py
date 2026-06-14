"""
Bot orchestrator: drives the per-minute accumulator strategy on a live 1s
tick stream. Holds at most one contract at a time.

State machine per UTC minute:
  ticks 1..16  -> record prices (P15, P16, window bodies)
  tick  17     -> fetch live ACCU proposal (n), evaluate entry, buy if confirmed
  ticks 18..   -> monitor open contract; exit at tick 30 (fixed) or via the
                  adaptive quiet-score logic (Improvement 6); knockout ends it.
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional

from config import Config
from src.deriv_client import DerivClient, DerivError
from src.strategy import (MinuteTracker, evaluate_entry, QuietScorer,
                          fib_expansion, tick_knockout)
from src.trade_logger import TradeLogger, utcnow


class AccumulatorBot:
    def __init__(self, cfg: Config, logger: TradeLogger = None, log=print):
        self.cfg = cfg
        self.s = cfg.strat
        self.log = log
        self.logger = logger or TradeLogger(cfg.log_dir)
        self.client = DerivClient(cfg, log=log)
        self.tracker = MinuteTracker(self.s)
        self.cross_trackers = {sym: MinuteTracker(self.s) for sym in self.s.cross_symbols}
        self.attempted_minute: Optional[int] = None
        self.holding = False
        self.trade_count = 0
        self._tick_q: Optional[asyncio.Queue] = None
        self._cross_q = {}

    async def start(self):
        demo = self.client.resolve_account()
        self.log(f"Account {demo['account_id']} balance={demo['balance']} {demo['currency']}")
        await self.client.connect()
        self._tick_q = await self.client.subscribe_ticks(self.s.symbol)
        if self.s.use_cross_instrument:
            for sym in self.s.cross_symbols:
                self._cross_q[sym] = await self.client.subscribe_ticks(sym)
        self.log(f"Subscribed ticks: {self.s.symbol}"
                 + (f" + cross {self.s.cross_symbols}" if self.s.use_cross_instrument else ""))
        await self._run()

    async def _drain_cross(self):
        for sym, q in self._cross_q.items():
            while not q.empty():
                t = q.get_nowait()
                self.cross_trackers[sym].add_tick(int(t["epoch"]), float(t["quote"]))

    async def _run(self):
        while True:
            tick = await self._tick_q.get()
            await self._drain_cross()
            epoch = int(tick["epoch"]); price = float(tick["quote"])
            tick_no, rolled = self.tracker.add_tick(epoch, price)
            if rolled:
                self.attempted_minute = None
            if self.holding:
                continue  # safety; holding handled inline in _enter_and_hold
            if (self.attempted_minute != self.tracker.current_minute
                    and tick_no >= self.s.entry_tick):
                self.attempted_minute = self.tracker.current_minute
                await self._attempt_entry(tick_no, epoch, price)
            if self.s.max_trades and self.trade_count >= self.s.max_trades:
                self.log(f"max_trades={self.s.max_trades} reached; stopping")
                return

    # ----------------------------------------------------------- entry
    def _cross_pass(self) -> bool:
        if not self.s.use_cross_instrument:
            return True
        for sym, tr in self.cross_trackers.items():
            p15, p16 = tr.price(self.s.p15_tick), tr.price(self.s.p16_tick)
            if p15 is None or p16 is None:
                return False
            # use the SAME tsb fraction as a proxy n for the cross symbol
            n = self.s.tsb(self.s.base_growth_rate) * (p16 or 1)
            body = abs(p16 - p15)
            if n <= 0 or (self.s.fib_multiple * body) / n >= self.s.cross_ratio_cap:
                return False
        return True

    def _sema_pass(self, direction: str) -> bool:
        if not self.s.use_sema_filter:
            return True
        anchor = self.s.p16_tick
        past = self.tracker.price(anchor - self.s.sema_period)
        now = self.tracker.price(anchor)
        if past is None or now is None:
            return True  # not enough data -> don't block
        trend = "BULLISH" if now >= past else "BEARISH"
        return trend == direction

    async def _attempt_entry(self, tick_no: int, epoch: int, price: float):
        p15 = self.tracker.price(self.s.p15_tick)
        p16 = self.tracker.price(self.s.p16_tick)
        rec = {"timestamp": utcnow(), "symbol": self.s.symbol,
               "P15": p15, "P16": p16, "entry_taken": False}
        if p15 is None or p16 is None:
            rec["reason"] = "missing_P15_P16"
            self.logger.log_minute(rec)
            self.log(self.logger.console_line(rec))
            return
        # fetch live barrier offset n at the reference growth rate
        try:
            prop = await self.client.proposal_accu(self.s.base_growth_rate)
            cd = prop.get("contract_details", {})
            n = float(cd.get("barrier_spot_distance") or 0)
            spot = float(prop.get("spot") or price)
            if n <= 0:  # fallback to tick_size_barrier * spot
                n = float(cd.get("tick_size_barrier") or self.s.tsb(self.s.base_growth_rate)) * spot
        except DerivError as e:
            rec["reason"] = f"proposal_err:{e.code}"
            self.logger.log_minute(rec)
            self.log(self.logger.console_line(rec))
            return

        direction = "BULLISH" if p16 >= p15 else "BEARISH"
        decision = evaluate_entry(self.tracker, self.s, n, spot,
                                  cross_pass=self._cross_pass(),
                                  sema_pass=self._sema_pass(direction))
        rec.update({
            "body_size": round(decision.body_size, 6),
            "fib_spread": round(decision.fib_spread, 6),
            "n": round(decision.n, 6),
            "ratio": round(decision.ratio, 6),
            "reason": decision.reason,
            "growth_rate": decision.growth_rate,
            "floor_ok": decision.floor_ok, "ratio_ok": decision.ratio_ok,
            "consec_ok": decision.consec_ok, "regime_ok": decision.regime_ok,
        })
        if not decision.enter:
            self.logger.log_minute(rec)
            self.log(self.logger.console_line(rec))
            return
        if self.s.dry_run:              # paper mode: record the would-be entry, never buy
            rec["entry_taken"] = True
            rec["reason"] = "DRY_RUN:" + decision.reason
            self.logger.log_minute(rec)
            self.log(f"[DRY] {self.logger.console_line(rec)} (no order placed)")
            return
        # ------- BUY -------
        try:
            tp = self.s.take_profit if self.s.use_take_profit else None
            buy = await self.client.buy_accu(decision.growth_rate, take_profit=tp)
        except DerivError as e:
            rec["reason"] = f"buy_err:{e.code}"
            self.logger.log_minute(rec)
            self.log(f"BUY FAILED {e}")
            return
        cid = buy.get("contract_id")
        rec["entry_taken"] = True
        rec["contract_id"] = cid
        rec["tick17_price"] = price
        self.trade_count += 1
        # NB: the per-minute CSV row is written once the trade closes so it is a
        # complete Section 1.8 record (outcome/pnl/ticks_survived filled in).
        self.log(f"{self.logger.console_line(rec)} -> BUY contract_id={cid} buy_price={buy.get('buy_price')}")
        self.holding = True
        try:
            await self._hold_and_exit(cid, decision, rec, price)
        finally:
            self.holding = False
            await self.client.forget_contract(cid)

    # ----------------------------------------------------------- hold/exit
    async def _hold_and_exit(self, cid: int, decision, entry_rec: dict, entry_price: float):
        s = self.s
        base_hold = s.hold_to_tick - s.entry_tick          # ticks after entry (=13)
        adaptive_cap = s.adaptive_max_tick - s.entry_tick  # =28
        gr = decision.growth_rate
        tsb = s.tsb(gr)
        scorer = QuietScorer(s)
        poc_q = await self.client.subscribe_contract(cid)
        last_spot = entry_price
        last_tp = -1
        exit_reason = None
        final = {}
        deadline = time.time() + max(adaptive_cap, base_hold) * 3 + 30
        while time.time() < deadline:
            try:
                poc = await asyncio.wait_for(poc_q.get(), timeout=15)
            except asyncio.TimeoutError:
                poc = await self.client.contract_state(cid)
            # keep main tracker fresh for regime continuity / next minute
            while self._tick_q and not self._tick_q.empty():
                t = self._tick_q.get_nowait()
                self.tracker.add_tick(int(t["epoch"]), float(t["quote"]))
            await self._drain_cross()
            status = poc.get("status")
            tp = poc.get("tick_passed")
            spot = poc.get("current_spot")
            spot = float(spot) if spot not in (None, "") else last_spot
            # knockout / settled by platform
            if poc.get("is_sold") or poc.get("is_expired") or status in ("won", "lost"):
                final = poc
                if status == "lost":
                    exit_reason = "KNOCKED_OUT"
                else:
                    exit_reason = "SURVIVED" if not poc.get("sell_price") else "MANUAL_CLOSE"
                break
            # update quiet score once per new tick
            if tp is not None and tp != last_tp:
                if last_tp >= 0:
                    scorer.update(last_spot, spot, tsb)
                last_tp = tp
                last_spot = spot
            # exit decisioning
            cur_tick_no = (int(poc.get("current_spot_time") or 0) % 60) + 1
            if s.use_adaptive_hold and tp is not None:
                if scorer.score < s.quiet_score_exit and tp >= 1:
                    exit_reason = "MANUAL_CLOSE"; break
                if tp >= adaptive_cap:
                    exit_reason = "MANUAL_CLOSE"; break
                if tp >= base_hold and scorer.score < s.quiet_score_extend:
                    exit_reason = "MANUAL_CLOSE"; break
            elif tp is not None and tp >= base_hold:
                exit_reason = "MANUAL_CLOSE"; break

        # perform sell if still open
        if exit_reason == "MANUAL_CLOSE":
            try:
                sell = await self.client.sell(cid, price=0)
                final = await self.client.contract_state(cid)
            except DerivError as e:
                self.log(f"sell error {e}; refetching state")
                final = await self.client.contract_state(cid)
        # finalize record
        profit = final.get("profit")
        try:
            pnl = float(profit)
        except (TypeError, ValueError):
            pnl = 0.0
        ticks_survived = final.get("tick_passed", last_tp if last_tp >= 0 else 0)
        exit_spot = final.get("current_spot") or final.get("exit_spot") or last_spot
        # merge outcome into the entry record -> one complete per-minute CSV row
        entry_rec.update({
            "tick30_price": exit_spot,
            "outcome": exit_reason or "UNKNOWN",
            "pnl": round(pnl, 4),
            "ticks_survived": ticks_survived,
        })
        self.logger.log_minute(entry_rec)
        self.logger.log_trade_result(entry_rec)
        self.log(f"  EXIT {exit_reason} cid={cid} pnl={pnl:+.4f} ticks={ticks_survived} "
                 f"bal_pnl_total={self.logger.stats['pnl']:+.4f}")

    async def stop(self):
        await self.client.close()
