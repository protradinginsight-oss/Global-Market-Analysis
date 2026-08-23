#!/usr/bin/env python3
"""
F&O universe snapshots - a record of which stocks were F&O-eligible, when.

The problem this solves is easy to miss and impossible to fix later.

Your current universe is 213 symbols: the stocks that are in F&O *today*.
Backtesting or training a model over several years using that list quietly
assumes you would have been trading exactly the names that survived and
still qualify. You wouldn't have been. Stocks enter and leave F&O eligibility
regularly, and the ones that left are usually the ones that did badly.

A model trained on survivors learns patterns that were only visible with
hindsight, and it will look far better in backtest than it can ever be live.

Fyers publishes the current symbol master but no history of it, so the only
way to have this record is to start keeping it. Run it monthly; it takes
seconds and the file is tiny.

Usage:
    py -3.12 universe_snapshot.py              # take today's snapshot
    py -3.12 universe_snapshot.py --list       # what's been recorded
    py -3.12 universe_snapshot.py --changes    # what entered/left between snapshots
    py -3.12 universe_snapshot.py --as-of 2026-08-23   # the list on a given date
"""

import sys
import json
import sqlite3
import argparse
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
MCX_FILE = BASE_DIR / "mcx_universe.json"
SNAP_DB = BASE_DIR / "universe_snapshots.db"


def init_db():
    conn = sqlite3.connect(SNAP_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_date TEXT NOT NULL,
            taken_at      TEXT NOT NULL,
            segment       TEXT NOT NULL,   -- NSE_FO, MCX
            symbol_count  INTEGER,
            PRIMARY KEY (snapshot_date, segment)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            snapshot_date TEXT NOT NULL,
            segment       TEXT NOT NULL,
            underlying    TEXT NOT NULL,
            ticker        TEXT,
            kind          TEXT,
            PRIMARY KEY (snapshot_date, segment, underlying)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_members_und "
                 "ON members(underlying, snapshot_date)")
    conn.commit()
    return conn


def take_snapshot(conn, snapshot_date=None):
    snapshot_date = snapshot_date or date.today().isoformat()
    taken_at = datetime.now().isoformat()
    total = 0

    if UNIVERSE_FILE.exists():
        uni = json.loads(UNIVERSE_FILE.read_text())
        rows = [(snapshot_date, "NSE_FO", a["underlying"], a["ticker"], a["kind"])
                for a in uni.get("all", [])
                if not a["ticker"].startswith("MCX:")]
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO members "
                "(snapshot_date, segment, underlying, ticker, kind) "
                "VALUES (?,?,?,?,?)", rows)
            conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(snapshot_date, taken_at, segment, symbol_count) VALUES (?,?,?,?)",
                (snapshot_date, taken_at, "NSE_FO", len(rows)))
            total += len(rows)
            print(f"  NSE_FO  {len(rows)} underlyings")

    if MCX_FILE.exists():
        mcx = json.loads(MCX_FILE.read_text())
        rows = [(snapshot_date, "MCX", c["underlying"], c["ticker"], "commodity")
                for c in mcx.get("contracts", [])]
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO members "
                "(snapshot_date, segment, underlying, ticker, kind) "
                "VALUES (?,?,?,?,?)", rows)
            conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(snapshot_date, taken_at, segment, symbol_count) VALUES (?,?,?,?)",
                (snapshot_date, taken_at, "MCX", len(rows)))
            total += len(rows)
            print(f"  MCX     {len(rows)} contracts")

    conn.commit()
    return total


def cmd_list(conn):
    rows = conn.execute(
        "SELECT snapshot_date, segment, symbol_count, taken_at FROM snapshots "
        "ORDER BY snapshot_date DESC, segment").fetchall()
    print("=" * 70)
    print("  UNIVERSE SNAPSHOTS")
    print("=" * 70)
    if not rows:
        print("\nNone recorded yet.\n")
        return
    print(f"\n{'Date':<14} {'Segment':<10} {'Count':>7}  Taken")
    print("-" * 70)
    for d, seg, n, t in rows:
        print(f"{d:<14} {seg:<10} {n:>7}  {t[:19]}")
    print("-" * 70)
    dates = sorted({r[0] for r in rows})
    print(f"\n{len(dates)} distinct snapshot date(s): {dates[0]} to {dates[-1]}")
    if len(dates) < 2:
        print("\nOnly one snapshot so far. Changes become visible once there")
        print("are at least two - run this monthly.")
    print()


def cmd_changes(conn, segment="NSE_FO"):
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM members WHERE segment=? "
        "ORDER BY snapshot_date", (segment,))]
    print("=" * 70)
    print(f"  MEMBERSHIP CHANGES - {segment}")
    print("=" * 70)
    if len(dates) < 2:
        print(f"\nNeed at least 2 snapshots to show changes ({len(dates)} so far).")
        print("Run this monthly and the history builds itself.\n")
        return

    for i in range(1, len(dates)):
        prev_d, curr_d = dates[i - 1], dates[i]
        prev = {r[0] for r in conn.execute(
            "SELECT underlying FROM members WHERE segment=? AND snapshot_date=?",
            (segment, prev_d))}
        curr = {r[0] for r in conn.execute(
            "SELECT underlying FROM members WHERE segment=? AND snapshot_date=?",
            (segment, curr_d))}
        added, removed = sorted(curr - prev), sorted(prev - curr)
        if not added and not removed:
            print(f"\n{prev_d} -> {curr_d}: no change ({len(curr)} members)")
            continue
        print(f"\n{prev_d} -> {curr_d}   ({len(prev)} -> {len(curr)})")
        if added:
            print(f"  ADDED ({len(added)}): {', '.join(added)}")
        if removed:
            print(f"  REMOVED ({len(removed)}): {', '.join(removed)}")
            print("  ^ these are the names a survivor-only backtest would")
            print("    silently exclude, and they are usually the weak ones.")
    print()


def cmd_as_of(conn, as_of, segment="NSE_FO"):
    """The universe as it stood on or before a date - what a point-in-time
    backtest should actually use."""
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM members WHERE segment=? "
        "AND snapshot_date<=?", (segment, as_of)).fetchone()
    if not row or not row[0]:
        print(f"No snapshot on or before {as_of}.")
        earliest = conn.execute(
            "SELECT MIN(snapshot_date) FROM members WHERE segment=?",
            (segment,)).fetchone()
        if earliest and earliest[0]:
            print(f"Earliest recorded snapshot is {earliest[0]}.")
        return
    snap = row[0]
    members = [r[0] for r in conn.execute(
        "SELECT underlying FROM members WHERE segment=? AND snapshot_date=? "
        "ORDER BY underlying", (segment, snap))]
    print("=" * 70)
    print(f"  {segment} UNIVERSE AS OF {as_of}")
    print(f"  (from the snapshot taken {snap})")
    print("=" * 70)
    print(f"\n{len(members)} members\n")
    for i in range(0, len(members), 4):
        print("  " + "".join(f"{m:<20}" for m in members[i:i + 4]))
    print()


def main():
    ap = argparse.ArgumentParser(description="Record F&O universe membership")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--changes", action="store_true")
    ap.add_argument("--as-of", metavar="DATE")
    ap.add_argument("--segment", default="NSE_FO", choices=["NSE_FO", "MCX"])
    ap.add_argument("--date", help="record under this date instead of today")
    args = ap.parse_args()

    conn = init_db()

    if args.list:
        cmd_list(conn)
    elif args.changes:
        cmd_changes(conn, args.segment)
    elif args.as_of:
        cmd_as_of(conn, args.as_of, args.segment)
    else:
        if not UNIVERSE_FILE.exists() and not MCX_FILE.exists():
            print("No universe files found. Run build_universe.py "
                  "and mcx_setup.py --build first.")
            sys.exit(1)
        print("=" * 70)
        print("  TAKING UNIVERSE SNAPSHOT")
        print("=" * 70)
        print()
        n = take_snapshot(conn, args.date)
        print(f"\nRecorded {n} entries for "
              f"{args.date or date.today().isoformat()}.")
        print(f"Database: {SNAP_DB.name}")
        print("\nRun this monthly. It takes seconds, the file stays tiny, and")
        print("it is the only way to know later which stocks were actually")
        print("tradeable at a given time - Fyers publishes today's list but")
        print("no history of it.\n")

    conn.close()


if __name__ == "__main__":
    main()
