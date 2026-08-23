#!/usr/bin/env python3
"""
Option chain collector.

Polls Fyers' option chain endpoint for every F&O underlying and stores the
full chain - OI, volume, LTP, IV and Greeks - so the derivatives analytics
in section 3 of the checklist have something to work from.

Why REST and not websocket: roughly 8,000 option contracts exist against 800
available websocket slots, so streaming the chain is impossible. One REST
call returns a whole chain including Greeks, which makes polling the right
tool rather than a compromise.

Polling is tiered. Indices move constantly and are what you actually trade,
so they're polled often with deep strike coverage. Stocks are polled less
frequently with fewer strikes, because polling 208 stocks every 5 minutes
would generate roughly a million rows a day for data that mostly isn't used.

Usage:
    py -3.12 option_chain.py --once          # one full sweep, then stop
    py -3.12 option_chain.py                 # run continuously
    py -3.12 option_chain.py --indices-only  # just the 5 indices
    py -3.12 option_chain.py --status        # what's been collected
    py -3.12 option_chain.py --dry-run
"""

import sys
import json
import time
import signal
import sqlite3
import logging
import argparse
from datetime import datetime, timezone, timedelta, date, time as dtime
from pathlib import Path

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3. Install with:  py -3.12 -m pip install fyers-apiv3")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"
CHAIN_DB = BASE_DIR / "option_chains.db"
LOG_PATH = BASE_DIR / "option_chain.log"

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# Tiered polling. Indices get frequent, deep snapshots; stocks get less.
# Rough daily volume at these settings: indices ~5 x 41 x 75 = 15k rows,
# stocks ~208 x 21 x 13 = 57k rows. About 72k rows/day, which is manageable.
TIERS = {
    "index": {"interval_sec": 300,  "strikecount": 20},
    "stock": {"interval_sec": 1800, "strikecount": 10},
}

SECONDS_BETWEEN_CALLS = 0.4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("optionchain")

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


def init_db():
    conn = sqlite3.connect(CHAIN_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_snapshot (
            snapshot_ts   TEXT NOT NULL,   -- IST, when this sweep ran
            underlying    TEXT NOT NULL,
            spot          REAL,
            expiry_ts     INTEGER,
            strike        REAL NOT NULL,
            option_type   TEXT NOT NULL,   -- CE or PE
            symbol        TEXT,
            ltp           REAL,
            change_pct    REAL,
            volume        INTEGER,
            oi            INTEGER,
            oi_change     INTEGER,
            oi_change_pct REAL,
            prev_oi       INTEGER,
            iv            REAL,
            delta         REAL,
            gamma         REAL,
            theta         REAL,
            vega          REAL,
            bid           REAL,
            ask           REAL,
            PRIMARY KEY (snapshot_ts, underlying, strike, option_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_und ON chain_snapshot(underlying, snapshot_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_ts ON chain_snapshot(snapshot_ts)")
    # Records each sweep so gaps are visible rather than silent.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_log (
            snapshot_ts TEXT NOT NULL,
            underlying  TEXT NOT NULL,
            tier        TEXT,
            rows        INTEGER,
            status      TEXT,
            detail      TEXT,
            PRIMARY KEY (snapshot_ts, underlying)
        )
    """)
    conn.commit()
    return conn


def fetch_chain(client, symbol, strikecount):
    """One option chain call. Returns (rows, spot, note)."""
    resp = client.optionchain({
        "symbol": symbol,
        "strikecount": strikecount,
        "timestamp": "",       # empty = current expiry
        "greeks": 1,           # delta, gamma, theta, vega, iv
    })
    if not isinstance(resp, dict):
        raise ValueError(f"unexpected response type: {type(resp).__name__}")
    if resp.get("s") != "ok":
        raise ValueError(resp.get("message") or str(resp)[:200])

    data = resp.get("data") or {}
    options = data.get("optionsChain") or []
    if not options:
        return [], None, "empty chain"

    # The chain includes the underlying itself as one entry (strike_price -1),
    # which is where the spot price comes from.
    spot = None
    for o in options:
        if o.get("strike_price") in (-1, -1.0) or o.get("option_type") in ("", None):
            spot = o.get("ltp")
            break
    if spot is None:
        spot = data.get("indiavixData", {}).get("ltp")

    rows = []
    for o in options:
        strike = o.get("strike_price")
        otype = o.get("option_type")
        if otype not in ("CE", "PE") or strike in (None, -1, -1.0):
            continue
        # Verified against a live response: Fyers nests all of these under a
        # "greeks" key, and IV lives inside it rather than at the top level.
        gk = o.get("greeks") if isinstance(o.get("greeks"), dict) else {}
        rows.append({
            "strike": float(strike),
            "option_type": otype,
            "symbol": o.get("symbol"),
            "ltp": o.get("ltp"),
            "change_pct": o.get("ltpchp"),
            "volume": o.get("volume"),
            "oi": o.get("oi"),
            "oi_change": o.get("oich"),
            "oi_change_pct": o.get("oichp"),
            "prev_oi": o.get("prev_oi"),
            "iv": gk.get("iv"),
            "delta": gk.get("delta"),
            "gamma": gk.get("gamma"),
            "theta": gk.get("theta"),
            "vega": gk.get("vega"),
            "bid": o.get("bid"),
            "ask": o.get("ask"),
            "expiry_ts": o.get("fyToken") and data.get("expiryData", [{}])[0].get("expiry"),
        })
    return rows, spot, "ok"


def store_chain(conn, snapshot_ts, underlying, spot, rows):
    def num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def intg(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    payload = [
        (snapshot_ts, underlying, num(spot), intg(r.get("expiry_ts")),
         r["strike"], r["option_type"], r.get("symbol"),
         num(r.get("ltp")), num(r.get("change_pct")),
         intg(r.get("volume")), intg(r.get("oi")), intg(r.get("oi_change")),
         num(r.get("oi_change_pct")), intg(r.get("prev_oi")),
         num(r.get("iv")), num(r.get("delta")), num(r.get("gamma")),
         num(r.get("theta")), num(r.get("vega")),
         num(r.get("bid")), num(r.get("ask")))
        for r in rows
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO chain_snapshot
           (snapshot_ts, underlying, spot, expiry_ts, strike, option_type,
            symbol, ltp, change_pct, volume, oi, oi_change, oi_change_pct,
            prev_oi, iv, delta, gamma, theta, vega, bid, ask)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", payload)
    return len(payload)


def cmd_status(conn):
    print("=" * 74)
    print("  OPTION CHAIN DATA")
    print("=" * 74)
    n = conn.execute("SELECT COUNT(*) FROM chain_snapshot").fetchone()[0]
    if not n:
        print("\nNothing collected yet. Run:  py -3.12 option_chain.py --once")
        return
    size = CHAIN_DB.stat().st_size / (1024 * 1024)
    snaps = conn.execute("SELECT COUNT(DISTINCT snapshot_ts) FROM chain_snapshot").fetchone()[0]
    unds = conn.execute("SELECT COUNT(DISTINCT underlying) FROM chain_snapshot").fetchone()[0]
    first, last = conn.execute(
        "SELECT MIN(snapshot_ts), MAX(snapshot_ts) FROM chain_snapshot").fetchone()
    print(f"\nRows        : {n:,}   ({size:.1f} MB)")
    print(f"Snapshots   : {snaps:,}")
    print(f"Underlyings : {unds}")
    print(f"From        : {first}")
    print(f"To          : {last}")

    print(f"\n{'Underlying':<24} {'Snapshots':>10} {'Rows':>10} {'Latest':<22}")
    print("-" * 74)
    for u, s, r, l in conn.execute(
            "SELECT underlying, COUNT(DISTINCT snapshot_ts), COUNT(*), MAX(snapshot_ts) "
            "FROM chain_snapshot GROUP BY underlying ORDER BY COUNT(*) DESC LIMIT 15"):
        print(f"{u:<24} {s:>10,} {r:>10,} {(l or '')[:19]:<22}")

    fails = conn.execute(
        "SELECT COUNT(*) FROM sweep_log WHERE status!='ok'").fetchone()[0]
    if fails:
        print(f"\nFailed sweeps: {fails:,}")
        for u, d, c in conn.execute(
                "SELECT underlying, detail, COUNT(*) FROM sweep_log "
                "WHERE status!='ok' GROUP BY underlying, detail "
                "ORDER BY COUNT(*) DESC LIMIT 8"):
            print(f"  {u:<24} {c:>5}x  {(d or '')[:40]}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Collect option chains")
    ap.add_argument("--once", action="store_true", help="one sweep then stop")
    ap.add_argument("--indices-only", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true")
    args = ap.parse_args()

    conn = init_db()
    if args.status:
        cmd_status(conn)
        return

    if not UNIVERSE_FILE.exists():
        print("universe.json not found. Run build_universe.py first.")
        sys.exit(1)
    universe = json.loads(UNIVERSE_FILE.read_text())
    tokens = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}

    today = date.today().isoformat()
    usable = {l: t for l, t in tokens.items() if t.get("generated_on") == today}
    if not usable:
        print("No fresh tokens. Run:  py -3.12 token_manager.py")
        sys.exit(1)

    targets = []
    for item in universe["all"]:
        tier = "index" if item["kind"] == "index" else "stock"
        if args.indices_only and tier != "index":
            continue
        targets.append((item["ticker"], tier))

    idx_n = sum(1 for _, t in targets if t == "index")
    stk_n = len(targets) - idx_n

    print("=" * 74)
    print("  OPTION CHAIN COLLECTOR")
    print("=" * 74)
    print(f"\nIndices : {idx_n}  (every {TIERS['index']['interval_sec']//60} min, "
          f"{TIERS['index']['strikecount']} strikes each side)")
    print(f"Stocks  : {stk_n}  (every {TIERS['stock']['interval_sec']//60} min, "
          f"{TIERS['stock']['strikecount']} strikes each side)")
    print(f"Accounts: {len(usable)}")

    calls_per_day = (idx_n * (23400 // TIERS['index']['interval_sec']) +
                     stk_n * (23400 // TIERS['stock']['interval_sec']))
    print(f"\nEstimated calls/day: {calls_per_day:,}  "
          f"({calls_per_day//len(usable):,} per account, "
          f"{calls_per_day/len(usable)/100000*100:.1f}% of quota)")

    is_open, why = market_is_open()
    print(f"Market  : {why}")
    if not is_open and not args.ignore_hours and not args.once:
        print("\nMarket closed. Use --once for a test sweep, or --ignore-hours.")
        return

    if args.dry_run:
        print("\nDry run - fetching nothing.\n")
        return

    clients = [(l, fyersModel.FyersModel(client_id=t["client_id"],
                                         token=t["access_token"],
                                         log_path=str(BASE_DIR)))
               for l, t in usable.items()]

    def handle_stop(signum, frame):
        global _stop
        _stop = True
        print("\nStopping after current sweep...")
    signal.signal(signal.SIGINT, handle_stop)

    last_run = {}
    call_no = 0
    total_rows = 0
    sweeps = 0

    print()
    while not _stop:
        now = datetime.now(IST)
        snapshot_ts = now.replace(microsecond=0).isoformat()
        due = [(sym, tier) for sym, tier in targets
               if (sym not in last_run or
                   (now - last_run[sym]).total_seconds() >= TIERS[tier]["interval_sec"])]

        if due:
            ok = failed = rows_this = 0
            for sym, tier in due:
                if _stop:
                    break
                label, client = clients[call_no % len(clients)]
                call_no += 1
                try:
                    rows, spot, note = fetch_chain(
                        client, sym, TIERS[tier]["strikecount"])
                    if rows:
                        n = store_chain(conn, snapshot_ts, sym, spot, rows)
                        rows_this += n
                        ok += 1
                        status, detail = "ok", note
                    else:
                        failed += 1
                        status, detail = "empty", note
                except Exception as e:
                    failed += 1
                    status, detail = "error", f"{type(e).__name__}: {str(e)[:80]}"
                    log.warning("%s [%s]: %s", sym, label, detail)

                conn.execute(
                    "INSERT OR REPLACE INTO sweep_log "
                    "(snapshot_ts, underlying, tier, rows, status, detail) "
                    "VALUES (?,?,?,?,?,?)",
                    (snapshot_ts, sym, tier, rows_this, status, detail))
                last_run[sym] = now
                time.sleep(SECONDS_BETWEEN_CALLS)

            conn.commit()
            total_rows += rows_this
            sweeps += 1
            log.info("sweep: %d fetched, %d failed, %d rows (%d total)",
                     ok, failed, rows_this, total_rows)

        if args.once:
            break

        if not args.ignore_hours:
            still_open, why2 = market_is_open()
            if not still_open:
                log.info("market %s - stopping", why2)
                break

        time.sleep(10)

    conn.commit()
    print()
    print("=" * 74)
    print(f"Sweeps: {sweeps}   Rows stored: {total_rows:,}")
    print(f"Database: {CHAIN_DB.name}")
    print("Inspect with:  py -3.12 option_chain.py --status")
    print("=" * 74)
    conn.close()


if __name__ == "__main__":
    main()
