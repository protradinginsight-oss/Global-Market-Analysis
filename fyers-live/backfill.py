#!/usr/bin/env python3
"""
Historical backfill - fetch past candles for the whole universe.

Works any time, including weekends and outside market hours, because it uses
Fyers' REST history endpoint rather than the live websocket. This is how the
database gets populated with enough history to actually build and test
analysis against, instead of waiting weeks for live data to accumulate.

Work is spread across all 4 accounts so each one's daily REST quota carries a
quarter of the load, and it's resumable: already-fetched symbol/day ranges
are skipped, so an interrupted run can simply be restarted.

Usage:
    py -3.12 backfill.py --days 365 --resolution 1D
    py -3.12 backfill.py --days 30 --resolution 5
    py -3.12 backfill.py --days 30 --resolution 5 --symbols NSE:SBIN-EQ,NSE:INFY-EQ
    py -3.12 backfill.py --days 365 --resolution 1D --dry-run
"""

import sys
import json
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3. Install with:  py -3.12 -m pip install fyers-apiv3")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"
HIST_DB = BASE_DIR / "market_history.db"
LOG_PATH = BASE_DIR / "backfill.log"

IST = timezone(timedelta(hours=5, minutes=30))

# Fyers caps how much can be pulled in one call. Daily candles allow a long
# span; intraday resolutions are limited to roughly 100 days per request, so
# longer ranges get split into chunks.
MAX_DAYS_PER_CALL = {"1D": 365, "D": 365, "Day": 365}
DEFAULT_CHUNK_DAYS = 90

# Fyers rate-limits per second and per minute. This is deliberately
# conservative - a backfill that takes longer but finishes beats one that
# gets throttled halfway through.
SECONDS_BETWEEN_CALLS = 0.6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backfill")


def init_db():
    conn = sqlite3.connect(HIST_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            symbol     TEXT NOT NULL,
            resolution TEXT NOT NULL,
            ts         TEXT NOT NULL,     -- IST timestamp of candle start
            epoch      INTEGER NOT NULL,
            open  REAL, high REAL, low REAL, close REAL,
            volume INTEGER,
            PRIMARY KEY (symbol, resolution, epoch)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_sym ON history(symbol, resolution)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_ts ON history(ts)")
    # Tracks what's already been fetched so a rerun doesn't repeat work.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            symbol     TEXT NOT NULL,
            resolution TEXT NOT NULL,
            range_from TEXT NOT NULL,
            range_to   TEXT NOT NULL,
            rows       INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (symbol, resolution, range_from, range_to)
        )
    """)
    conn.commit()
    return conn


def already_fetched(conn, symbol, resolution, rfrom, rto):
    cur = conn.execute(
        "SELECT rows FROM fetch_log WHERE symbol=? AND resolution=? "
        "AND range_from=? AND range_to=?",
        (symbol, resolution, rfrom, rto))
    row = cur.fetchone()
    return row is not None


def date_chunks(start, end, chunk_days):
    """Split a date range into request-sized pieces."""
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_chunk(client, symbol, resolution, rfrom, rto):
    """One history call. Returns (candles, note).

    'no_data' is a successful response meaning there genuinely is nothing in
    that range - a weekend, a holiday, or a period before the stock listed.
    It is not an error, and treating it as one would mean retrying the same
    empty range on every rerun forever.
    """
    resp = client.history({
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": rfrom,
        "range_to": rto,
        "cont_flag": "1",
    })
    if not isinstance(resp, dict):
        raise ValueError(f"unexpected response type: {type(resp).__name__}")

    status = resp.get("s")
    if status == "no_data":
        return [], "no_data"
    if status != "ok":
        raise ValueError(resp.get("message") or str(resp)[:200])
    return resp.get("candles", []) or [], "ok"


def store(conn, symbol, resolution, candles):
    """Fyers returns [epoch, open, high, low, close, volume] per candle."""
    rows = []
    for c in candles:
        if len(c) < 6:
            continue
        epoch = int(c[0])
        ts = datetime.fromtimestamp(epoch, IST).isoformat()
        rows.append((symbol, resolution, ts, epoch,
                     float(c[1]), float(c[2]), float(c[3]), float(c[4]), int(c[5])))
    if rows:
        conn.executemany(
            """INSERT OR REPLACE INTO history
               (symbol, resolution, ts, epoch, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Backfill historical candles")
    ap.add_argument("--days", type=int, default=365,
                    help="how many days back to fetch (default 365)")
    ap.add_argument("--resolution", default="1D",
                    help="1D for daily, or minutes: 1, 5, 15, 30, 60 (default 1D)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated list; default is the whole universe")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without fetching")
    args = ap.parse_args()

    if not UNIVERSE_FILE.exists():
        print("universe.json not found. Run build_universe.py first.")
        sys.exit(1)
    if not TOKEN_FILE.exists():
        print("fyers_tokens.json not found. Run token_manager.py first.")
        sys.exit(1)

    universe = json.loads(UNIVERSE_FILE.read_text())
    tokens = json.loads(TOKEN_FILE.read_text())

    today = date.today().isoformat()
    usable = {label: t for label, t in tokens.items()
              if t.get("generated_on") == today}
    if not usable:
        print("No fresh tokens. Run:  py -3.12 token_manager.py")
        sys.exit(1)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [item["ticker"] for item in universe["all"]]

    end = date.today()
    start = end - timedelta(days=args.days)
    chunk_days = MAX_DAYS_PER_CALL.get(args.resolution, DEFAULT_CHUNK_DAYS)
    chunks = list(date_chunks(start, end, chunk_days))

    total_calls = len(symbols) * len(chunks)
    est_seconds = total_calls * SECONDS_BETWEEN_CALLS
    per_account = total_calls / len(usable)

    print("=" * 70)
    print("  HISTORICAL BACKFILL")
    print("=" * 70)
    print(f"\nSymbols       : {len(symbols)}")
    print(f"Resolution    : {args.resolution}")
    print(f"Date range    : {start} to {end}  ({args.days} days)")
    print(f"Chunks/symbol : {len(chunks)}  ({chunk_days} days each)")
    print(f"Total calls   : {total_calls:,}")
    print(f"Accounts      : {len(usable)}  ({per_account:,.0f} calls each)")
    print(f"Est. time     : {est_seconds/60:.0f} minutes")
    print()
    print("Fyers allows 100,000 REST calls per account per day, so this is")
    print(f"about {per_account/100000*100:.1f}% of one account's daily quota.")

    if args.dry_run:
        print("\nDry run - fetching nothing. Exiting.")
        return

    conn = init_db()

    # One client per account, used round-robin so the load spreads evenly.
    clients = []
    for label, t in usable.items():
        clients.append((label, fyersModel.FyersModel(
            client_id=t["client_id"], token=t["access_token"],
            log_path=str(BASE_DIR))))
    print(f"\nUsing accounts: {', '.join(l for l, _ in clients)}\n")

    stored = skipped = failed = empty = 0
    failures = []
    start_time = time.time()
    call_no = 0

    for i, symbol in enumerate(symbols):
        for rfrom, rto in chunks:
            rfrom_s, rto_s = rfrom.isoformat(), rto.isoformat()

            if already_fetched(conn, symbol, args.resolution, rfrom_s, rto_s):
                skipped += 1
                continue

            label, client = clients[call_no % len(clients)]
            call_no += 1

            try:
                candles, note = fetch_chunk(client, symbol, args.resolution,
                                            rfrom_s, rto_s)
                n = store(conn, symbol, args.resolution, candles)
                conn.execute(
                    """INSERT OR REPLACE INTO fetch_log
                       (symbol, resolution, range_from, range_to, rows, fetched_at)
                       VALUES (?,?,?,?,?,?)""",
                    (symbol, args.resolution, rfrom_s, rto_s, n,
                     datetime.now(IST).isoformat()))
                conn.commit()
                stored += n
                if note == "no_data":
                    empty += 1
            except Exception as e:
                failed += 1
                failures.append((symbol, str(e)[:120]))
                log.warning("%s [%s] %s to %s: %s",
                            symbol, label, rfrom_s, rto_s, str(e)[:150])

            time.sleep(SECONDS_BETWEEN_CALLS)

        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed else 0
            remaining = (len(symbols) - i - 1) / rate if rate else 0
            print(f"  {i+1:>4}/{len(symbols)} symbols | {stored:>8,} candles "
                  f"| {failed} failed | ~{remaining/60:.0f} min left")

    conn.close()

    print()
    print("=" * 70)
    print(f"Candles stored : {stored:,}")
    print(f"Chunks skipped : {skipped:,}  (already fetched)")
    print(f"Empty ranges   : {empty:,}  (no trading data - weekend/holiday/pre-listing)")
    print(f"Failed calls   : {failed}")
    if failures:
        print("\nFirst few failures:")
        for sym, err in failures[:10]:
            print(f"  {sym:<24} {err}")
        print("\nRerun the same command to retry only what's missing -")
        print("successful chunks are recorded and will be skipped.")
    print(f"\nDatabase: {HIST_DB.name}")
    print("=" * 70)


if __name__ == "__main__":
    main()
