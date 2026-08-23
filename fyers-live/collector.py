#!/usr/bin/env python3
"""
Collector supervisor - launches and watches one worker process per account.

Each Fyers websocket must live in its own process: FyersDataSocket is a
singleton, so several sockets in one process silently collapse into one and
only the last-configured account's callbacks fire. This supervisor spawns a
separate shard_worker.py per account and restarts any that die.

Usage:
    py -3.12 collector.py                # run until Ctrl+C
    py -3.12 collector.py --dry-run      # verify setup, launch nothing
    py -3.12 collector.py --ignore-hours # run outside market hours
"""

import sys
import json
import time
import signal
import logging
import argparse
import subprocess
from datetime import datetime, timezone, timedelta, time as dtime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"
WORKER = BASE_DIR / "shard_worker.py"

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from market_hours import is_ticker_open, segment_for, summary as hours_summary
except ImportError:
    print("market_hours.py not found - it must sit next to this script.")
    sys.exit(1)

# If a worker dies repeatedly it's a real fault, not a blip - stop retrying
# so the log shows one clear problem rather than an endless restart loop.
MAX_RESTARTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [supervisor] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "collector.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("supervisor")

_stop = False


def market_is_open(all_symbols=None, now=None):
    """Open if ANY tracked symbol is in a live session.

    Once MCX is merged in, the supervisor cannot stop at 15:30 - commodities
    trade until 23:30. It shuts down only when every segment it covers has
    closed.
    """
    now = now or datetime.now(IST)
    if not all_symbols:
        # No universe loaded yet; fall back to NSE equity hours.
        from market_hours import is_open as seg_open
        return seg_open("NSE_CM", now)
    live = set()
    for sym in all_symbols:
        ok, _ = is_ticker_open(sym, now)
        if ok:
            live.add(segment_for(sym))
    if live:
        return True, "open: " + ", ".join(sorted(live))
    return False, "all segments closed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("  FYERS LIVE COLLECTOR (supervisor)")
    print("=" * 70)

    if not UNIVERSE_FILE.exists():
        print("universe.json not found. Run build_universe.py first.")
        sys.exit(1)
    if not TOKEN_FILE.exists():
        print("fyers_tokens.json not found. Run token_manager.py first.")
        sys.exit(1)
    if not WORKER.exists():
        print(f"{WORKER.name} not found - it must sit next to this script.")
        sys.exit(1)

    universe = json.loads(UNIVERSE_FILE.read_text())
    tokens = json.loads(TOKEN_FILE.read_text())
    shards = {k: v for k, v in universe["shards"].items() if v}

    total = sum(len(v) for v in shards.values())
    print(f"\nUniverse      : {total} symbols across {len(shards)} shards")
    print(f"Tick storage  : {len(universe.get('tick_symbols', []))} symbols")

    today = date.today().isoformat()
    problems = []
    for label, syms in shards.items():
        t = tokens.get(label)
        if not t:
            state = "NO TOKEN"
            problems.append(label)
        elif t.get("generated_on") != today:
            state = "STALE TOKEN"
            problems.append(label)
        else:
            state = "token OK"
        print(f"  {label:<26} {len(syms):>4} symbols   {state}")

    if problems:
        print(f"\nERROR: token problems for: {', '.join(problems)}")
        print("Run:  py -3.12 token_manager.py")
        sys.exit(1)
    print("\nAll tokens fresh for today.")

    all_symbols = [s for syms in shards.values() for s in syms]
    seg_counts = {}
    for sym in all_symbols:
        seg_counts[segment_for(sym)] = seg_counts.get(segment_for(sym), 0) + 1
    print(f"Segments      : " +
          ", ".join(f"{k} ({v})" for k, v in sorted(seg_counts.items())))

    is_open, reason = market_is_open(all_symbols)
    print(f"Market status : {reason}")
    if not is_open and not args.ignore_hours:
        print("\nMarket is closed - nothing to collect.")
        print("Use --ignore-hours to connect anyway (useful for testing).")
        return

    if args.dry_run:
        print("\nDry run - setup is fine, launching nothing. Exiting.")
        return

    def handle_stop(signum, frame):
        global _stop
        _stop = True
        print("\nStopping workers...")
    signal.signal(signal.SIGINT, handle_stop)

    procs = {}
    restarts = {label: 0 for label in shards}

    def launch(label):
        cmd = [sys.executable, str(WORKER), "--shard", label]
        if args.ignore_hours:
            cmd.append("--ignore-hours")
        # Workers log to their own files; keep the supervisor's console clean.
        p = subprocess.Popen(cmd, cwd=str(BASE_DIR),
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        log.info("launched worker for %s (pid %d)", label, p.pid)
        return p

    print(f"\nLaunching {len(shards)} worker processes...\n")
    for label in shards:
        procs[label] = launch(label)
        time.sleep(2)   # stagger connections

    print("Workers running. Per-shard detail is in worker_<label>.log")
    print("Press Ctrl+C to stop.\n")

    try:
        while not _stop:
            time.sleep(5)

            for label, p in list(procs.items()):
                if p.poll() is None:
                    continue
                if _stop:
                    # Shutting down anyway - don't spawn a worker just to
                    # kill it a moment later.
                    del procs[label]
                    continue
                code = p.returncode
                if restarts[label] >= MAX_RESTARTS:
                    log.error("%s died (exit %s) and hit the restart limit "
                              "of %d - not restarting. Check worker_%s.log",
                              label, code, MAX_RESTARTS, label)
                    del procs[label]
                    continue
                restarts[label] += 1
                log.warning("%s exited (code %s) - restarting (%d/%d)",
                            label, code, restarts[label], MAX_RESTARTS)
                time.sleep(3)
                procs[label] = launch(label)

            if not procs:
                log.error("All workers have stopped. Exiting.")
                break

            if not args.ignore_hours:
                ok, why = market_is_open(all_symbols)
                if not ok:
                    log.info("%s - shutting down workers", why)
                    break
    finally:
        for label, p in procs.items():
            if p.poll() is None:
                log.info("stopping %s", label)
                p.terminate()
        deadline = time.time() + 20
        for label, p in procs.items():
            remaining = max(0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning("%s didn't stop in time - killing", label)
                p.kill()
        print()
        print("=" * 70)
        print("All workers stopped.")
        print("Check worker_<label>.log files for per-shard totals.")
        print("=" * 70)


if __name__ == "__main__":
    main()
