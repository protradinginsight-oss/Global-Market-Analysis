#!/usr/bin/env python3
"""
System health check - is everything actually running and collecting?

The dashboard shows how old each component's data is. This goes further:
it checks whether the processes are alive, whether rows are still arriving,
and whether tokens are valid - and in watch mode it shows data landing in
real time so you can see collection happening rather than infer it.

Run from the project root.

Usage:
    py -3.12 healthcheck.py            # one full check
    py -3.12 healthcheck.py --watch    # live, updates every 10s
    py -3.12 healthcheck.py --watch --interval 5
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import subprocess
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

DBS = {
    "live candles":   ROOT / "fyers-live" / "market_candles.db",
    "live ticks":     ROOT / "fyers-live" / "market_ticks.db",
    "option chains":  ROOT / "fyers-live" / "option_chains.db",
    "FII/DII":        ROOT / "fii-dii-tracker" / "fii_dii.db",
    "pre-market":     ROOT / "global-premarket" / "global_premarket.db",
    "india history":  ROOT / "fyers-live" / "market_history.db",
    "global history": ROOT / "global-history" / "global_history.db",
}

# Which table and timestamp column identifies "the newest row" per database.
ROW_SOURCES = {
    "live candles":  ("candles_1m", "minute_ts"),
    "live ticks":    ("ticks", "ts"),
    "option chains": ("chain_snapshot", "snapshot_ts"),
    "FII/DII":       ("fii_dii_flow", "fetched_at"),
    "pre-market":    ("global_snapshot", "captured_at"),
}

EXPECTED_PROCESSES = {
    "collector.py":    "live collector supervisor",
    "shard_worker.py": "per-account collector workers",
    "option_chain.py": "option chain poller",
    "watchdog.py":     "feed watchdog",
}


def ro(path):
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def running_processes():
    """Which of our scripts are currently running.

    Matches on the command line rather than the process name, since every
    one of these shows up as python.exe.

    Windows removed wmic in recent builds, so PowerShell's CIM query is
    tried first and wmic kept only as a fallback for older machines.
    """
    out = None

    # PowerShell - works on current Windows
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object {$_.Name -like '*python*'} | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout
    except Exception:
        pass

    # wmic - older Windows
    if out is None:
        try:
            r = subprocess.run(
                ["wmic", "process", "where", "name like '%python%'",
                 "get", "CommandLine"],
                capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                out = r.stdout
        except Exception:
            pass

    # ps - Linux/macOS
    if out is None:
        try:
            r = subprocess.run(["ps", "aux"], capture_output=True,
                               text=True, timeout=20)
            if r.returncode == 0:
                out = r.stdout
        except Exception:
            pass

    if out is None:
        return None
    return {script: out.count(script) for script in EXPECTED_PROCESSES}


def row_count(conn, table):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def newest(conn, table, col):
    try:
        r = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def age_seconds(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return (datetime.now(IST) - dt).total_seconds()
    except ValueError:
        return None


def fmt_age(secs):
    if secs is None:
        return "-"
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs/60:.0f}m"
    if secs < 172800:
        return f"{secs/3600:.0f}h"
    return f"{secs/86400:.0f}d"


def check_tokens():
    f = ROOT / "fyers-live" / "fyers_tokens.json"
    if not f.exists():
        return None, "no token file"
    try:
        toks = json.loads(f.read_text())
    except json.JSONDecodeError:
        return None, "token file unreadable"
    today = date.today().isoformat()
    fresh = [k for k, v in toks.items() if v.get("generated_on") == today]
    stale = [k for k in toks if k not in fresh]
    return fresh, stale


def market_state():
    sys.path.insert(0, str(ROOT / "fyers-live"))
    try:
        from market_hours import SEGMENTS, is_open
        return {k: is_open(k) for k in SEGMENTS}
    except ImportError:
        return None


def snapshot():
    """Everything we can measure right now."""
    out = {"time": datetime.now(IST), "dbs": {}, "procs": running_processes()}
    for name, path in DBS.items():
        entry = {"exists": path.exists()}
        if path.exists():
            entry["size_mb"] = path.stat().st_size / (1024 * 1024)
        conn = ro(path)
        if conn and name in ROW_SOURCES:
            table, col = ROW_SOURCES[name]
            entry["rows"] = row_count(conn, table)
            entry["newest"] = newest(conn, table, col)
            entry["age"] = age_seconds(entry["newest"])
        if conn:
            conn.close()
        out["dbs"][name] = entry
    out["tokens"] = check_tokens()
    out["market"] = market_state()
    return out


def render(snap, prev=None):
    lines = []
    now = snap["time"].strftime("%Y-%m-%d %H:%M:%S")
    lines.append("=" * 78)
    lines.append(f"  SYSTEM HEALTH   {now} IST")
    lines.append("=" * 78)

    # Market
    m = snap.get("market")
    if m:
        openseg = [k for k, (ok, _) in m.items() if ok]
        lines.append("")
        if openseg:
            lines.append(f"  Market: OPEN - {', '.join(openseg)}")
        else:
            reason = list(m.values())[0][1] if m else ""
            lines.append(f"  Market: closed ({reason})")

    # Tokens
    fresh, stale = snap["tokens"]
    lines.append("")
    if fresh is None:
        lines.append(f"  Tokens: {stale}")
    elif stale:
        lines.append(f"  Tokens: {len(fresh)} fresh, {len(stale)} STALE "
                     f"({', '.join(stale)})")
        lines.append("          Run: py -3.12 token_manager.py")
    else:
        lines.append(f"  Tokens: all {len(fresh)} fresh")

    # Processes
    procs = snap.get("procs")
    lines.append("")
    lines.append("  PROCESSES")
    lines.append("  " + "-" * 74)
    if procs is None:
        lines.append("    could not read the process list on this platform")
    else:
        for script, desc in EXPECTED_PROCESSES.items():
            n = procs.get(script, 0)
            state = f"{n} running" if n else "not running"
            mark = "  " if n else "  <-"
            lines.append(f"    {script:<20} {state:<16} {desc}{mark}")

    # Data
    lines.append("")
    lines.append("  DATA")
    lines.append("  " + "-" * 74)
    lines.append(f"    {'Source':<18} {'Rows':>12} {'New':>8} {'Age':>7} "
                 f"{'Size':>9}")
    lines.append("  " + "-" * 74)
    for name in DBS:
        d = snap["dbs"][name]
        if not d["exists"]:
            lines.append(f"    {name:<18} {'not created yet':>12}")
            continue
        rows = d.get("rows")
        delta = ""
        if prev and rows is not None:
            pr = prev["dbs"].get(name, {}).get("rows")
            if pr is not None:
                diff = rows - pr
                delta = f"+{diff}" if diff > 0 else ("0" if diff == 0 else str(diff))
        lines.append(
            f"    {name:<18} "
            f"{(f'{rows:,}' if rows is not None else '-'):>12} "
            f"{delta:>8} "
            f"{fmt_age(d.get('age')):>7} "
            f"{d.get('size_mb', 0):>8.1f}M")

    # The verdict that matters during market hours
    lines.append("")
    lines.append("  " + "-" * 74)
    if m and any(ok for ok, _ in m.values()):
        cand = snap["dbs"].get("live candles", {})
        age = cand.get("age")
        if age is None:
            lines.append("    Market is open but no candle data at all.")
            lines.append("    The collector is not running or not connected.")
        elif age > 300:
            lines.append(f"    Market is open but the newest candle is "
                         f"{fmt_age(age)} old.")
            lines.append("    A feed has stalled - check worker logs.")
        elif prev:
            pr = prev["dbs"].get("live candles", {}).get("rows")
            cr = cand.get("rows")
            if pr is not None and cr is not None and cr == pr:
                lines.append("    Candle count did not change since the last")
                lines.append("    check. Either very quiet, or collection has")
                lines.append("    stopped - watch for another cycle.")
            else:
                lines.append("    Collecting normally.")
        else:
            lines.append("    Data is current.")
    else:
        lines.append("    Market closed - collectors idle by design.")
        lines.append("    Row counts will not move until the next session.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="System health check")
    ap.add_argument("--watch", action="store_true",
                    help="keep refreshing so you can watch data arrive")
    ap.add_argument("--interval", type=int, default=10,
                    help="seconds between refreshes in watch mode")
    args = ap.parse_args()

    if not args.watch:
        print(render(snapshot()))
        return

    print("Watching. Ctrl+C to stop.\n")
    prev = None
    try:
        while True:
            snap = snapshot()
            os.system("cls" if os.name == "nt" else "clear")
            print(render(snap, prev))
            print(f"\n  refreshing every {args.interval}s - Ctrl+C to stop")
            prev = snap
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
