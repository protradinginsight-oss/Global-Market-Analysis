#!/usr/bin/env python3
"""
Feed watchdog.

The supervisor in collector.py restarts workers that die. This catches the
failure mode it can't see: a worker that is alive, connected, and receiving
nothing. That happens when a websocket goes half-open, when a subscription
silently lapses, or when the network drops in a way TCP doesn't notice - and
it produces a clean-looking log with a hole in the data.

Checks are made against the database rather than by asking the workers,
because a worker reporting on its own health is exactly what can't be trusted
here.

Run this alongside the collector in a second window.

Usage:
    py -3.12 watchdog.py                  # monitor during market hours
    py -3.12 watchdog.py --check          # one check, print, exit
    py -3.12 watchdog.py --ignore-hours
"""

import sys
import json
import time
import signal
import sqlite3
import logging
import argparse
import subprocess
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CANDLE_DB = BASE_DIR / "market_candles.db"
UNIVERSE_FILE = BASE_DIR / "universe.json"
LOG_PATH = BASE_DIR / "watchdog.log"

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# A shard is considered stale if nothing has been written for this long.
# Generous enough to survive the collector's 30-second flush cycle plus a
# quiet minute, tight enough to catch a real outage quickly.
STALE_SECONDS = 180

# How often to check.
CHECK_INTERVAL = 60

# Alert only once per shard per outage, rather than every check.
RENOTIFY_SECONDS = 900

# Minimum fraction of a shard's symbols that must have written recently.
# Not 100%: illiquid F&O names genuinely go minutes without a trade, so
# demanding full coverage would alert constantly. 60% is loose enough to
# tolerate quiet stocks and tight enough to catch a subscription that
# mostly lapsed.
MIN_COVERAGE = 0.6

try:
    from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = TELEGRAM_CHAT_ID = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("watchdog")

_stop = False


def market_is_open(now=None):
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False, "weekend"
    if now.time() < MARKET_OPEN:
        return False, "pre-open"
    if now.time() > MARKET_CLOSE:
        return False, "closed"
    return True, "open"


def notify(message):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("your-"):
        log.info("(telegram not configured) %s", message.replace("\n", " | "))
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        log.error("telegram send failed: %s", e)


def shard_map():
    """symbol -> shard label, so staleness can be attributed to a worker."""
    if not UNIVERSE_FILE.exists():
        return {}, {}
    u = json.loads(UNIVERSE_FILE.read_text())
    sym_to_shard = {}
    for label, syms in u["shards"].items():
        for s in syms:
            sym_to_shard[s] = label
    return sym_to_shard, u["shards"]


def check_freshness(conn, sym_to_shard, shards):
    """Per-shard: how recently did any of its symbols get a candle written?"""
    now = datetime.now(IST)
    rows = conn.execute(
        "SELECT symbol, MAX(minute_ts) FROM candles_1m "
        "WHERE minute_ts >= ? GROUP BY symbol",
        ((now - timedelta(hours=2)).isoformat(),)).fetchall()

    latest_by_shard = {label: None for label in shards}
    symbols_seen = {label: 0 for label in shards}

    for sym, ts in rows:
        label = sym_to_shard.get(sym)
        if not label:
            continue
        symbols_seen[label] += 1
        if ts and (latest_by_shard[label] is None or ts > latest_by_shard[label]):
            latest_by_shard[label] = ts

    report = {}
    for label, expected in shards.items():
        latest = latest_by_shard[label]
        if latest is None:
            report[label] = {
                "status": "NO DATA", "age_sec": None,
                "symbols": 0, "expected": len(expected),
            }
            continue
        try:
            age = (now - datetime.fromisoformat(latest)).total_seconds()
        except ValueError:
            age = None
        status = "OK" if (age is not None and age <= STALE_SECONDS) else "STALE"

        # Recency alone isn't enough. A shard where 3 of 50 symbols are
        # still ticking looks perfectly healthy on a "did anything arrive?"
        # check, but 47 symbols are silently missing. That partial failure
        # is easier to miss than a total outage and just as damaging.
        coverage = symbols_seen[label] / len(expected) if expected else 0
        if status == "OK" and coverage < MIN_COVERAGE:
            status = "PARTIAL"

        report[label] = {
            "status": status, "age_sec": age, "latest": latest,
            "symbols": symbols_seen[label], "expected": len(expected),
            "coverage": coverage,
        }
    return report


def restart_shard(label, ignore_hours):
    """Kill any worker for this shard, then start a fresh one.

    Uses taskkill with a command-line filter because the worker is a plain
    python process - matching on the shard argument avoids killing unrelated
    Python processes.
    """
    try:
        subprocess.run(
            ["wmic", "process", "where",
             f"CommandLine like '%shard_worker.py%{label}%'", "delete"],
            capture_output=True, timeout=20)
    except Exception as e:
        log.warning("could not kill existing worker for %s: %s", label, e)

    time.sleep(2)
    cmd = [sys.executable, str(BASE_DIR / "shard_worker.py"), "--shard", label]
    if ignore_hours:
        cmd.append("--ignore-hours")
    try:
        p = subprocess.Popen(cmd, cwd=str(BASE_DIR),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("restarted worker for %s (pid %d)", label, p.pid)
        return True
    except Exception as e:
        log.error("failed to restart %s: %s", label, e)
        return False


def print_report(report):
    print(f"\n{'Shard':<28} {'Status':<10} {'Age':>8} {'Symbols':>16} {'Latest':<22}")
    print("-" * 88)
    for label, r in report.items():
        age = f"{r['age_sec']:.0f}s" if r["age_sec"] is not None else "-"
        syms = f"{r['symbols']}/{r['expected']} ({r.get('coverage',0)*100:.0f}%)"
        latest = (r.get("latest") or "-")[:19]
        print(f"{label:<28} {r['status']:<10} {age:>8} {syms:>16} {latest:<22}")
    print("-" * 88)


def main():
    ap = argparse.ArgumentParser(description="Watch collector feed freshness")
    ap.add_argument("--check", action="store_true", help="one check then exit")
    ap.add_argument("--ignore-hours", action="store_true")
    ap.add_argument("--no-restart", action="store_true",
                    help="alert only, never restart a worker")
    args = ap.parse_args()

    if not CANDLE_DB.exists():
        print("market_candles.db not found - the collector hasn't run yet.")
        sys.exit(1)

    sym_to_shard, shards = shard_map()
    if not shards:
        print("universe.json not found. Run build_universe.py first.")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{CANDLE_DB}?mode=ro", uri=True)

    if args.check:
        report = check_freshness(conn, sym_to_shard, shards)
        print("=" * 88)
        print(f"  FEED FRESHNESS  ({datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 88)
        print_report(report)
        bad = [l for l, r in report.items() if r["status"] != "OK"]
        if bad:
            print(f"\n{len(bad)} shard(s) need attention: {', '.join(bad)}")
        else:
            print("\nAll shards fresh.")
        print()
        conn.close()
        return

    def handle_stop(signum, frame):
        global _stop
        _stop = True
        print("\nStopping watchdog...")
    signal.signal(signal.SIGINT, handle_stop)

    print("=" * 88)
    print("  FEED WATCHDOG")
    print("=" * 88)
    print(f"\nShards      : {len(shards)}")
    print(f"Stale after : {STALE_SECONDS}s with no new candles")
    print(f"Partial if  : under {MIN_COVERAGE*100:.0f}% of a shard's symbols reporting")
    print(f"Check every : {CHECK_INTERVAL}s")
    print(f"Restart     : {'disabled (alert only)' if args.no_restart else 'enabled'}")
    print(f"Telegram    : {'configured' if TELEGRAM_BOT_TOKEN and not TELEGRAM_BOT_TOKEN.startswith('your-') else 'not configured - logging only'}")
    print("\nPress Ctrl+C to stop.\n")

    last_alert = {}
    restarts = {}

    while not _stop:
        open_now, why = market_is_open()
        if not open_now and not args.ignore_hours:
            log.info("market %s - watchdog idle", why)
            break

        report = check_freshness(conn, sym_to_shard, shards)
        now = time.time()
        problems = []

        for label, r in report.items():
            if r["status"] == "OK":
                if label in last_alert:
                    log.info("%s recovered (age %.0fs)", label, r["age_sec"])
                    notify(f"Feed recovered: {label}")
                    last_alert.pop(label, None)
                continue

            problems.append(label)
            age = f"{r['age_sec']:.0f}s" if r["age_sec"] is not None else "no data at all"
            log.warning("%s is %s (last write %s, %d/%d symbols = %.0f%% coverage)",
                        label, r["status"], age, r["symbols"], r["expected"],
                        r.get("coverage", 0) * 100)

            if now - last_alert.get(label, 0) > RENOTIFY_SECONDS:
                last_alert[label] = now
                notify(f"Feed {r['status']}: {label}\nlast write {age}\n"
                       f"{r['symbols']}/{r['expected']} symbols "
                       f"({r.get('coverage', 0)*100:.0f}% coverage)")

                if not args.no_restart:
                    n = restarts.get(label, 0)
                    if n >= 3:
                        log.error("%s has been restarted %d times - not "
                                  "restarting again. Investigate manually.",
                                  label, n)
                        notify(f"{label}: restart limit reached, needs "
                               "manual attention")
                    else:
                        restarts[label] = n + 1
                        if restart_shard(label, args.ignore_hours):
                            notify(f"Restarted worker: {label} "
                                   f"(attempt {n+1}/3)")

        if not problems:
            log.info("all %d shards fresh", len(shards))

        for _ in range(CHECK_INTERVAL):
            if _stop:
                break
            time.sleep(1)

    conn.close()
    print("\nWatchdog stopped.")


if __name__ == "__main__":
    main()
