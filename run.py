#!/usr/bin/env python3
"""Entrypoint for the live Deriv accumulator bot (demo account)."""
import argparse
import asyncio
import json
import signal
import sys

from config import get_config
from src.bot import AccumulatorBot
from src.trade_logger import TradeLogger


def build_cfg(args):
    cfg = get_config()
    if args.symbol:
        cfg.strat.symbol = args.symbol
    if args.stake:
        cfg.strat.stake = args.stake
    if args.growth:
        cfg.strat.base_growth_rate = args.growth
    if args.max_trades is not None:
        cfg.strat.max_trades = args.max_trades
    if args.force_entry:
        cfg.strat.force_entry = True
    if args.dry_run:
        cfg.strat.dry_run = True
    if args.profile == "core":
        s = cfg.strat
        for f in ("use_ratio_cap", "use_body_floor", "use_avg_body", "use_consec_quiet",
                  "use_regime_filter", "use_adaptive_hold", "use_dynamic_growth",
                  "use_dynamic_anchor", "use_cross_instrument", "use_sema_filter"):
            setattr(s, f, False)
        s.ratio_cap = 1.0
    return cfg


async def main_async(args):
    cfg = build_cfg(args)
    if not cfg.conn.token or not cfg.conn.app_id:
        print("ERROR: DERIV_TOKEN / DERIV_APP_ID missing in .env"); sys.exit(1)
    banner = (
        "============================================================\n"
        " Deriv Accumulator bot — DEMO account\n"
        " Measured edge (see FINDINGS.md): per-tick survival is below\n"
        " break-even on every 1s instrument/growth tier; the fib filter\n"
        " has no predictive power. This is -EV; use --dry-run to collect\n"
        " data without spending balance.\n"
        "============================================================"
    )
    print(banner)
    print("CONFIG:", json.dumps(cfg.sanitized()["strat"], indent=2, default=str))
    logger = TradeLogger(cfg.log_dir)
    bot = AccumulatorBot(cfg, logger=logger)
    stop = asyncio.Event()

    def _sig(*_):
        print("\nshutdown requested"); stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _sig)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(bot.start())
    done, _ = await asyncio.wait({runner, asyncio.create_task(stop.wait())},
                                 return_when=asyncio.FIRST_COMPLETED)
    await bot.stop()
    logger.write_summary()
    print("SUMMARY:", json.dumps(logger.stats, indent=2))


def main():
    p = argparse.ArgumentParser(description="Deriv Accumulator bot (demo)")
    p.add_argument("--symbol", help="override instrument (default 1HZ50V)")
    p.add_argument("--stake", type=float, help="USD stake per contract")
    p.add_argument("--growth", type=float, help="base growth rate, e.g. 0.01")
    p.add_argument("--max-trades", type=int, dest="max_trades",
                   help="stop after N trades (0=unlimited)")
    p.add_argument("--profile", choices=["default", "core"], default="default",
                   help="'core' disables all improvements (pure Section 1.5)")
    p.add_argument("--force-entry", action="store_true", dest="force_entry",
                   help="testing aid: enter every measurable minute (demo lifecycle test)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="paper mode: log entry decisions but never place an order")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
