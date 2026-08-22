#!/usr/bin/env python3
"""
Global historical data collector.

Pulls long-horizon daily history for global indices, commodities, currencies,
rates and crypto from Yahoo Finance, plus macro series from FRED, and stores
everything locally alongside a curated table of major market events.

Yahoo is used rather than Twelve Data because the free tier there is built
for recent quotes, not decades of history. Yahoo needs no API key and goes
back to the 1990s for most indices. It's an unofficial interface, so it can
break without notice - which is exactly why this stores data locally rather
than fetching it live each time.

Usage:
    py -3.12 collect_global.py --years 20
    py -3.12 collect_global.py --years 20 --skip-fred
    py -3.12 collect_global.py --status
    py -3.12 collect_global.py --dry-run
"""

import sys
import time
import sqlite3
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "global_history.db"

try:
    from universe import GLOBAL_UNIVERSE, FRED_SERIES, MAJOR_EVENTS
except ImportError:
    print("universe.py not found - it must sit next to this script.")
    sys.exit(1)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
SECONDS_BETWEEN_CALLS = 1.0


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            adj_close REAL, volume INTEGER,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(trade_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            ticker TEXT PRIMARY KEY,
            label TEXT, category TEXT, note TEXT,
            first_date TEXT, last_date TEXT, row_count INTEGER,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro (
            series_id TEXT NOT NULL,
            obs_date TEXT NOT NULL,
            value REAL,
            PRIMARY KEY (series_id, obs_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_series (
            series_id TEXT PRIMARY KEY,
            label TEXT, note TEXT,
            first_date TEXT, last_date TEXT, row_count INTEGER,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_date TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT,
            PRIMARY KEY (event_date, description)
        )
    """)
    conn.commit()
    return conn


def store_events(conn):
    conn.executemany(
        "INSERT OR REPLACE INTO events (event_date, description, category) "
        "VALUES (?,?,?)", MAJOR_EVENTS)
    conn.commit()
    return len(MAJOR_EVENTS)


def fetch_prices(ticker, start, end):
    """Download daily history for one ticker. Returns list of row tuples."""
    import yfinance as yf
    df = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                     progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        return []

    # yfinance returns a MultiIndex when given a list; flatten defensively
    # since a single-ticker call has returned both shapes across versions.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    rows = []
    for idx, r in df.iterrows():
        d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]

        def val(name):
            try:
                v = r[name]
                return None if v != v else float(v)   # NaN check
            except (KeyError, TypeError, ValueError):
                return None

        vol = val("Volume")
        rows.append((ticker, d, val("Open"), val("High"), val("Low"),
                     val("Close"), val("Adj Close"),
                     int(vol) if vol is not None else None))
    return rows


def fetch_fred(series_id):
    """Download one FRED series as CSV. Returns list of (date, value)."""
    import requests
    import csv
    import io
    resp = requests.get(FRED_CSV.format(series=series_id), timeout=60)
    resp.raise_for_status()
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader, None)
    if not header:
        return []
    rows = []
    for row in reader:
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        # FRED uses '.' for missing observations
        if v in (".", "", "NA"):
            continue
        try:
            rows.append((series_id, d, float(v)))
        except ValueError:
            continue
    return rows


def update_instrument_meta(conn, ticker, label, category, note):
    row = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM prices "
        "WHERE ticker=?", (ticker,)).fetchone()
    conn.execute(
        """INSERT OR REPLACE INTO instruments
           (ticker,label,category,note,first_date,last_date,row_count,updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ticker, label, category, note, row[0], row[1], row[2],
         datetime.now().isoformat()))


def cmd_status(conn):
    print("=" * 76)
    print("  GLOBAL HISTORY DATABASE")
    print("=" * 76)

    n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    if not n:
        print("\nNo price data yet. Run:  py -3.12 collect_global.py --years 20")
        return

    size = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"\nPrice rows: {n:,}   ({size:.1f} MB on disk)")
    print(f"\n{'Ticker':<14} {'Label':<22} {'From':<12} {'To':<12} {'Rows':>8}")
    print("-" * 76)
    for t, l, f, la, c in conn.execute(
            "SELECT ticker,label,first_date,last_date,row_count FROM instruments "
            "ORDER BY category, ticker"):
        print(f"{t:<14} {(l or '')[:21]:<22} {(f or '-'):<12} "
              f"{(la or '-'):<12} {c or 0:>8,}")

    m = conn.execute("SELECT COUNT(*) FROM macro").fetchone()[0]
    if m:
        print(f"\nMacro observations: {m:,}")
        print(f"\n{'Series':<22} {'Label':<26} {'From':<12} {'Rows':>8}")
        print("-" * 76)
        for s, l, f, c in conn.execute(
                "SELECT series_id,label,first_date,row_count FROM macro_series "
                "ORDER BY series_id"):
            print(f"{s:<22} {(l or '')[:25]:<26} {(f or '-'):<12} {c or 0:>8,}")

    e = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"\nCurated events: {e}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Collect global historical data")
    ap.add_argument("--years", type=int, default=20,
                    help="how many years back to fetch (default 20)")
    ap.add_argument("--skip-fred", action="store_true",
                    help="skip FRED macro series")
    ap.add_argument("--skip-prices", action="store_true",
                    help="skip Yahoo price data")
    ap.add_argument("--status", action="store_true",
                    help="show what's already stored and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without fetching")
    args = ap.parse_args()

    conn = init_db()

    if args.status:
        cmd_status(conn)
        return

    end = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=365 * args.years)

    print("=" * 76)
    print("  GLOBAL HISTORICAL DATA COLLECTION")
    print("=" * 76)
    print(f"\nInstruments  : {len(GLOBAL_UNIVERSE)} (Yahoo Finance)")
    print(f"Macro series : {len(FRED_SERIES)} (FRED)")
    print(f"Events       : {len(MAJOR_EVENTS)} (curated)")
    print(f"Date range   : {start} to {date.today()}  ({args.years} years)")
    print(f"Est. time    : {(len(GLOBAL_UNIVERSE)+len(FRED_SERIES)) * SECONDS_BETWEEN_CALLS / 60:.0f}-"
          f"{(len(GLOBAL_UNIVERSE)+len(FRED_SERIES)) * 3 / 60:.0f} minutes")

    if args.dry_run:
        print("\nDry run - fetching nothing. Exiting.")
        return

    n_events = store_events(conn)
    print(f"\nStored {n_events} curated events.")

    total_rows = 0
    failed = []

    if not args.skip_prices:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            print("\nMissing yfinance. Install with:")
            print("  py -3.12 -m pip install yfinance")
            sys.exit(1)

        print(f"\nFetching {len(GLOBAL_UNIVERSE)} instruments from Yahoo...\n")
        for i, (ticker, label, category, note) in enumerate(GLOBAL_UNIVERSE, 1):
            try:
                rows = fetch_prices(ticker, start, end)
                if rows:
                    conn.executemany(
                        """INSERT OR REPLACE INTO prices
                           (ticker,trade_date,open,high,low,close,adj_close,volume)
                           VALUES (?,?,?,?,?,?,?,?)""", rows)
                    conn.commit()
                    update_instrument_meta(conn, ticker, label, category, note)
                    conn.commit()
                    total_rows += len(rows)
                    span = f"{rows[0][1]} to {rows[-1][1]}"
                    print(f"  [{i:>2}/{len(GLOBAL_UNIVERSE)}] {ticker:<12} "
                          f"{len(rows):>6,} rows  {span}")
                else:
                    failed.append((ticker, "no data returned"))
                    print(f"  [{i:>2}/{len(GLOBAL_UNIVERSE)}] {ticker:<12} "
                          f"     -  no data")
            except Exception as e:
                failed.append((ticker, f"{type(e).__name__}: {str(e)[:80]}"))
                print(f"  [{i:>2}/{len(GLOBAL_UNIVERSE)}] {ticker:<12} "
                      f"     -  FAILED: {type(e).__name__}")
            time.sleep(SECONDS_BETWEEN_CALLS)

    macro_rows = 0
    if not args.skip_fred:
        print(f"\nFetching {len(FRED_SERIES)} macro series from FRED...\n")
        for i, (series_id, label, note) in enumerate(FRED_SERIES, 1):
            try:
                rows = fetch_fred(series_id)
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO macro (series_id,obs_date,value) "
                        "VALUES (?,?,?)", rows)
                    meta = conn.execute(
                        "SELECT MIN(obs_date),MAX(obs_date),COUNT(*) FROM macro "
                        "WHERE series_id=?", (series_id,)).fetchone()
                    conn.execute(
                        """INSERT OR REPLACE INTO macro_series
                           (series_id,label,note,first_date,last_date,row_count,updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (series_id, label, note, meta[0], meta[1], meta[2],
                         datetime.now().isoformat()))
                    conn.commit()
                    macro_rows += len(rows)
                    print(f"  [{i:>2}/{len(FRED_SERIES)}] {series_id:<22} "
                          f"{len(rows):>7,} obs  from {rows[0][1]}")
                else:
                    failed.append((series_id, "no data"))
                    print(f"  [{i:>2}/{len(FRED_SERIES)}] {series_id:<22}       -  no data")
            except Exception as e:
                failed.append((series_id, f"{type(e).__name__}: {str(e)[:80]}"))
                print(f"  [{i:>2}/{len(FRED_SERIES)}] {series_id:<22}       -  FAILED")
            time.sleep(SECONDS_BETWEEN_CALLS)

    print()
    print("=" * 76)
    print(f"Price rows stored : {total_rows:,}")
    print(f"Macro rows stored : {macro_rows:,}")
    print(f"Failures          : {len(failed)}")
    if failed:
        print("\nFailed items:")
        for name, err in failed[:15]:
            print(f"  {name:<22} {err}")
        print("\nRerun to retry - existing data is preserved and updated in place.")
    print(f"\nDatabase: {DB_PATH.name}")
    print("Run  py -3.12 collect_global.py --status  to inspect.")
    print("=" * 76)


if __name__ == "__main__":
    main()
