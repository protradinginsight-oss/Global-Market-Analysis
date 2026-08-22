#!/usr/bin/env python3
"""
Query the global history database.

Turns the collected rows into things you can actually read: correlations
between markets, how instruments behaved around major events, and what
happened over any given window.

Read-only.

Usage:
    py -3.12 query_global.py --correlations
    py -3.12 query_global.py --correlations --years 3
    py -3.12 query_global.py --event 2020-03-12
    py -3.12 query_global.py --events
    py -3.12 query_global.py --series ^NSEI --from 2020-01-01
    py -3.12 query_global.py --macro DEXINUS
"""

import sys
import sqlite3
import argparse
import statistics
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "global_history.db"

# The pairs worth watching for an Indian trader, rather than every
# combination - a 42x42 matrix is unreadable and mostly noise.
KEY_PAIRS = [
    ("^NSEI", "^GSPC",    "Nifty vs S&P 500"),
    ("^NSEI", "^IXIC",    "Nifty vs Nasdaq"),
    ("^NSEI", "EEM",      "Nifty vs EM equities"),
    ("^NSEI", "INDA",     "Nifty vs India ETF (foreign positioning)"),
    ("^NSEI", "USDINR=X", "Nifty vs rupee"),
    ("^NSEI", "BZ=F",     "Nifty vs Brent crude"),
    ("^NSEI", "^VIX",     "Nifty vs global fear gauge"),
    ("^NSEI", "DX-Y.NYB", "Nifty vs dollar index"),
    ("^NSEI", "^TNX",     "Nifty vs US 10Y yield"),
    ("^NSEI", "BTC-USD",  "Nifty vs Bitcoin"),
    ("USDINR=X", "DX-Y.NYB", "Rupee vs dollar index"),
    ("USDINR=X", "BZ=F",  "Rupee vs crude"),
    ("GC=F", "DX-Y.NYB",  "Gold vs dollar"),
    ("GC=F", "^TNX",      "Gold vs real rates proxy"),
    ("^NSEBANK", "^NSEI", "Bank Nifty vs Nifty"),
]


def daily_returns(conn, ticker, start, end):
    rows = conn.execute(
        "SELECT trade_date, close FROM prices WHERE ticker=? "
        "AND trade_date BETWEEN ? AND ? AND close IS NOT NULL "
        "ORDER BY trade_date", (ticker, start, end)).fetchall()
    out = {}
    for i in range(1, len(rows)):
        prev, curr = rows[i - 1][1], rows[i][1]
        if prev and prev > 0:
            out[rows[i][0]] = (curr - prev) / prev
    return out


def correlation(a, b):
    """Pearson correlation over dates present in both series."""
    shared = sorted(set(a) & set(b))
    if len(shared) < 30:
        return None, len(shared)
    xs = [a[d] for d in shared]
    ys = [b[d] for d in shared]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None, len(shared)
    return num / (dx * dy), len(shared)


def lagged_returns(conn, ticker, start, end, shift_days=1):
    """Returns keyed by the NEXT trading date, for lead/lag testing.

    A global market's close is only a usable cue for India if it precedes
    the Indian session. Comparing same-day closes gets this backwards for
    US markets, which close after India has already finished trading.
    """
    rows = conn.execute(
        "SELECT trade_date, close FROM prices WHERE ticker=? "
        "AND trade_date BETWEEN ? AND ? AND close IS NOT NULL "
        "ORDER BY trade_date", (ticker, start, end)).fetchall()
    out = {}
    for i in range(1, len(rows)):
        prev, curr = rows[i - 1][1], rows[i][1]
        if prev and prev > 0:
            # Attribute this return to the following calendar day, so it
            # lines up with the Indian session that reacts to it.
            d = date.fromisoformat(rows[i][0]) + timedelta(days=shift_days)
            out[d.isoformat()] = (curr - prev) / prev
    return out


def cmd_lagged(conn, years):
    """Does last night's global move predict today's Indian move?

    This is the question the pre-market panel implicitly assumes 'yes' to.
    """
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=365 * years)).isoformat()

    print("=" * 78)
    print(f"  OVERNIGHT LEAD TEST  (last {years} years)")
    print("  Does yesterday's global close predict today's Indian move?")
    print("=" * 78)

    nifty = daily_returns(conn, "^NSEI", start, end)
    banknifty = daily_returns(conn, "^NSEBANK", start, end)

    leads = [
        ("^GSPC",    "S&P 500"),
        ("^IXIC",    "Nasdaq"),
        ("^VIX",     "CBOE VIX"),
        ("EEM",      "EM equities"),
        ("INDA",     "India ETF (US-listed)"),
        ("BZ=F",     "Brent crude"),
        ("GC=F",     "Gold"),
        ("DX-Y.NYB", "Dollar index"),
        ("^TNX",     "US 10Y yield"),
        ("BTC-USD",  "Bitcoin"),
        ("^N225",    "Nikkei"),
        ("^HSI",     "Hang Seng"),
    ]

    print(f"\n{'Global market (lagged 1 day)':<32} {'vs Nifty':>10} "
          f"{'vs BankNifty':>14} {'Days':>8}")
    print("-" * 78)

    results = []
    for ticker, label in leads:
        lag = lagged_returns(conn, ticker, start, end)
        c1, n1 = correlation(lag, nifty)
        c2, _ = correlation(lag, banknifty)
        results.append((label, c1, n1))
        s1 = f"{c1:+.3f}" if c1 is not None else "-"
        s2 = f"{c2:+.3f}" if c2 is not None else "-"
        print(f"{label:<32} {s1:>10} {s2:>14} {n1:>8,}")

    print("-" * 78)

    strong = [(l, c) for l, c, n in results
              if c is not None and abs(c) >= 0.15]
    strong.sort(key=lambda x: -abs(x[1]))

    print("\nWhat this means for the pre-market panel:")
    if strong:
        print("\n  Cues with a real (if modest) overnight lead:")
        for label, c in strong:
            direction = "same direction" if c > 0 else "inverse"
            print(f"    {label:<28} {c:+.3f}  ({direction})")
    else:
        print("\n  None of these show a meaningful overnight lead on Nifty.")

    weak = [(l, c) for l, c, n in results
            if c is not None and abs(c) < 0.15]
    if weak:
        print("\n  Cues with little or no measurable lead:")
        for label, c in weak:
            print(f"    {label:<28} {c:+.3f}")
        print("\n  Weighting these in a pre-market read gives them influence")
        print("  the historical record does not support.")

    print("\nCaveat: correlation of daily returns measures direction and")
    print("co-movement, not magnitude, and averages across every regime in")
    print("the window. A cue that matters enormously during a crisis and not")
    print("at all otherwise will look weak here.\n")


def cmd_correlations(conn, years):
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=365 * years)).isoformat()

    print("=" * 76)
    print(f"  CROSS-MARKET CORRELATIONS  (daily returns, last {years} years)")
    print("=" * 76)
    print(f"\n{'Pair':<42} {'Corr':>8} {'Days':>8}  Strength")
    print("-" * 76)

    cache = {}
    for t1, t2, label in KEY_PAIRS:
        for t in (t1, t2):
            if t not in cache:
                cache[t] = daily_returns(conn, t, start, end)
        c, n = correlation(cache[t1], cache[t2])
        if c is None:
            print(f"{label:<42} {'-':>8} {n:>8}  insufficient overlap")
            continue
        a = abs(c)
        strength = ("strong" if a >= 0.6 else "moderate" if a >= 0.3
                    else "weak" if a >= 0.15 else "negligible")
        print(f"{label:<42} {c:>+8.3f} {n:>8,}  {strength}")

    print("-" * 76)
    print("\nCorrelation of daily returns, not levels. Two markets can trend")
    print("together for years while their day-to-day moves are uncorrelated,")
    print("and it's the daily relationship that matters for an overnight read.")
    print("\nNote that non-overlapping trading hours mechanically weaken some")
    print("of these: the S&P closes after Indian markets, so same-day moves")
    print("are only partly comparable.\n")


def cmd_events(conn):
    print("=" * 76)
    print("  CURATED MAJOR EVENTS")
    print("=" * 76)
    print(f"\n{'Date':<14} {'Category':<18} Description")
    print("-" * 76)
    for d, desc, cat in conn.execute(
            "SELECT event_date, description, category FROM events "
            "ORDER BY event_date"):
        print(f"{d:<14} {cat or '':<18} {desc}")
    print("-" * 76)
    print("\nInspect any of these with:  --event YYYY-MM-DD\n")


def cmd_event(conn, event_date, window=10):
    row = conn.execute(
        "SELECT description, category FROM events WHERE event_date=?",
        (event_date,)).fetchone()
    desc = row[0] if row else "(not in the curated events table)"

    d = date.fromisoformat(event_date)
    before = (d - timedelta(days=window * 2)).isoformat()
    after = (d + timedelta(days=window * 2)).isoformat()

    print("=" * 76)
    print(f"  MARKET REACTION: {event_date}")
    print(f"  {desc}")
    print("=" * 76)
    print(f"\n{'Instrument':<24} {'Before':>11} {'After':>11} {'Change':>10}")
    print("-" * 76)

    watch = ["^NSEI", "^GSPC", "^VIX", "BZ=F", "GC=F", "USDINR=X",
             "DX-Y.NYB", "^TNX", "BTC-USD", "EEM"]

    for ticker in watch:
        pre = conn.execute(
            "SELECT close FROM prices WHERE ticker=? AND trade_date<=? "
            "AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
            (ticker, event_date)).fetchone()
        post = conn.execute(
            "SELECT close FROM prices WHERE ticker=? AND trade_date>=? "
            "AND trade_date<=? AND close IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 1",
            (ticker, event_date, after)).fetchone()
        if not pre or not post or not pre[0]:
            print(f"{ticker:<24} {'-':>11} {'-':>11} {'no data':>10}")
            continue
        chg = (post[0] - pre[0]) / pre[0] * 100
        label = next((l for t, l, _, _ in
                      __import__("universe").GLOBAL_UNIVERSE if t == ticker), ticker)
        print(f"{label[:23]:<24} {pre[0]:>11,.2f} {post[0]:>11,.2f} {chg:>+9.2f}%")

    print("-" * 76)
    print(f"\nChange measured from the last close on or before {event_date}")
    print(f"to the last close within {window * 2} days after.\n")


def cmd_series(conn, ticker, start):
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close FROM prices "
        "WHERE ticker=? AND trade_date>=? ORDER BY trade_date DESC LIMIT 20",
        (ticker, start)).fetchall()
    if not rows:
        print(f"No data for {ticker} since {start}.")
        avail = conn.execute(
            "SELECT ticker FROM instruments ORDER BY ticker LIMIT 12").fetchall()
        print("Available tickers include: " + ", ".join(a[0] for a in avail))
        return
    meta = conn.execute(
        "SELECT label, first_date, last_date, row_count FROM instruments "
        "WHERE ticker=?", (ticker,)).fetchone()
    print("=" * 76)
    print(f"  {ticker}  {meta[0] if meta else ''}")
    print("=" * 76)
    if meta:
        print(f"\nHistory: {meta[1]} to {meta[2]}  ({meta[3]:,} rows)")
    print(f"\n{'Date':<14} {'Open':>11} {'High':>11} {'Low':>11} {'Close':>11}")
    print("-" * 76)
    for d, o, h, l, c in rows:
        print(f"{d:<14} {o or 0:>11,.2f} {h or 0:>11,.2f} "
              f"{l or 0:>11,.2f} {c or 0:>11,.2f}")
    print()


def cmd_macro(conn, series_id):
    meta = conn.execute(
        "SELECT label, note, first_date, last_date, row_count FROM macro_series "
        "WHERE series_id=?", (series_id,)).fetchone()
    if not meta:
        print(f"No macro series '{series_id}'.")
        avail = conn.execute("SELECT series_id FROM macro_series ORDER BY 1").fetchall()
        print("Available: " + ", ".join(a[0] for a in avail))
        return
    print("=" * 76)
    print(f"  {series_id}  {meta[0]}")
    print("=" * 76)
    print(f"\n{meta[1]}")
    print(f"History: {meta[2]} to {meta[3]}  ({meta[4]:,} observations)\n")
    print(f"{'Date':<14} {'Value':>14}")
    print("-" * 34)
    for d, v in conn.execute(
            "SELECT obs_date, value FROM macro WHERE series_id=? "
            "ORDER BY obs_date DESC LIMIT 15", (series_id,)):
        print(f"{d:<14} {v:>14,.4f}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Query global history")
    ap.add_argument("--correlations", action="store_true")
    ap.add_argument("--lagged", action="store_true",
                    help="test whether last night's global close leads today's Nifty")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--event", help="inspect one event date (YYYY-MM-DD)")
    ap.add_argument("--series", help="show recent rows for a ticker")
    ap.add_argument("--macro", help="show a FRED series")
    ap.add_argument("--from", dest="start", default="2000-01-01")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print("global_history.db not found. Run collect_global.py first.")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    if args.lagged:
        cmd_lagged(conn, args.years)
    elif args.correlations:
        cmd_correlations(conn, args.years)
    elif args.events:
        cmd_events(conn)
    elif args.event:
        cmd_event(conn, args.event)
    elif args.series:
        cmd_series(conn, args.series, args.start)
    elif args.macro:
        cmd_macro(conn, args.macro)
    else:
        print("Pick one of: --correlations, --lagged, --events, --event DATE, "
              "--series TICKER, --macro SERIES")
        print("Run with -h for details.")

    conn.close()


if __name__ == "__main__":
    main()
