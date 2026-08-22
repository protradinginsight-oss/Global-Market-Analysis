#!/usr/bin/env python3
"""
Camarilla pivot calculator and screener.

Computes Camarilla levels for every symbol from the previous trading day's
high, low and close, then screens for setups where price is sitting near R3
or S3 - the entry zone for the breakout strategy.

Reads the daily candles already backfilled into market_history.db, so it
works any time, including weekends.

Usage:
    py -3.12 camarilla.py                    # levels + screen for latest day
    py -3.12 camarilla.py --date 2026-08-21  # a specific trading day
    py -3.12 camarilla.py --symbol NSE:SBIN-EQ   # detail for one symbol
    py -3.12 camarilla.py --threshold 1.0    # tighter screen (default 2%)
    py -3.12 camarilla.py --export           # write results to CSV
"""

import sys
import csv
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HIST_DB = BASE_DIR / "market_history.db"
PIVOT_DB = BASE_DIR / "camarilla_pivots.db"
EXPORT_CSV = BASE_DIR / "camarilla_screen.csv"

# Camarilla multipliers. The 1.1/n series is the standard formulation.
MULTIPLIERS = {
    4: 1.1 / 2,
    3: 1.1 / 4,
    2: 1.1 / 6,
    1: 1.1 / 12,
}


def camarilla_levels(high, low, close):
    """Compute Camarilla pivots from one day's H/L/C.

    Returns a dict with pivot point, R1-R4 and S1-S4. R4/S4 are the breakout
    targets; R3/S3 are the entry levels the strategy watches.
    """
    rng = high - low
    levels = {"pp": (high + low + close) / 3, "range": rng}
    for n, mult in MULTIPLIERS.items():
        levels[f"r{n}"] = close + rng * mult
        levels[f"s{n}"] = close - rng * mult
    # R5/S5 are a common extension used as stretch targets beyond R4/S4.
    levels["r5"] = levels["r4"] + 1.168 * (levels["r4"] - levels["r3"])
    levels["s5"] = levels["s4"] - 1.168 * (levels["s3"] - levels["s4"])
    return levels


def init_pivot_db():
    conn = sqlite3.connect(PIVOT_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pivots (
            symbol     TEXT NOT NULL,
            for_date   TEXT NOT NULL,   -- the day these levels apply TO
            based_on   TEXT NOT NULL,   -- the day whose H/L/C produced them
            prev_high  REAL, prev_low REAL, prev_close REAL, day_range REAL,
            pp REAL,
            r1 REAL, r2 REAL, r3 REAL, r4 REAL, r5 REAL,
            s1 REAL, s2 REAL, s3 REAL, s4 REAL, s5 REAL,
            PRIMARY KEY (symbol, for_date)
        )
    """)
    conn.commit()
    return conn


def get_trading_days(hist, limit=10):
    """Most recent daily-candle dates present in the database."""
    rows = hist.execute(
        "SELECT DISTINCT substr(ts,1,10) AS d FROM history "
        "WHERE resolution='1D' ORDER BY d DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def load_day(hist, day):
    """All symbols' OHLC for one day."""
    rows = hist.execute(
        "SELECT symbol, open, high, low, close, volume FROM history "
        "WHERE resolution='1D' AND substr(ts,1,10)=?", (day,)).fetchall()
    return {r[0]: {"open": r[1], "high": r[2], "low": r[3],
                   "close": r[4], "volume": r[5]} for r in rows}


def main():
    ap = argparse.ArgumentParser(description="Camarilla pivots and screener")
    ap.add_argument("--date", help="trading day to compute levels FOR (yyyy-mm-dd)")
    ap.add_argument("--symbol", help="show full detail for one symbol")
    ap.add_argument("--threshold", type=float, default=25.0,
                    help="screen: distance to R3/S3 as %% of prior day's RANGE "
                         "(default 25). See --absolute for the old behaviour.")
    ap.add_argument("--absolute", action="store_true",
                    help="measure distance as %% of price instead of %% of range")
    ap.add_argument("--export", action="store_true", help="write screen to CSV")
    args = ap.parse_args()

    if not HIST_DB.exists():
        print("market_history.db not found. Run backfill.py first.")
        sys.exit(1)

    hist = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)

    days = get_trading_days(hist)
    if len(days) < 2:
        print("Need at least 2 days of daily candles. Run:")
        print("  py -3.12 backfill.py --days 365 --resolution 1D")
        sys.exit(1)

    # Levels for a given day come from the PREVIOUS day's H/L/C.
    if args.date:
        if args.date not in days:
            print(f"No daily data for {args.date}.")
            print(f"Available recent days: {', '.join(days[:5])}")
            sys.exit(1)
        idx = days.index(args.date)
        if idx + 1 >= len(days):
            print(f"No prior trading day before {args.date} in the database.")
            sys.exit(1)
        for_date, based_on = days[idx], days[idx + 1]
    else:
        # Default: levels for the most recent day, from the one before it
        for_date, based_on = days[0], days[1]

    prev = load_day(hist, based_on)
    curr = load_day(hist, for_date)

    print("=" * 78)
    print("  CAMARILLA PIVOTS")
    print("=" * 78)
    print(f"\nLevels for  : {for_date}")
    print(f"Based on    : {based_on}  (previous day's high/low/close)")
    print(f"Symbols     : {len(prev)}")

    pconn = init_pivot_db()
    computed = []

    for symbol, bar in prev.items():
        if bar["high"] is None or bar["low"] is None or bar["close"] is None:
            continue
        if bar["high"] <= bar["low"]:
            continue          # bad or non-trading bar
        lv = camarilla_levels(bar["high"], bar["low"], bar["close"])
        computed.append((symbol, bar, lv))
        pconn.execute(
            """INSERT OR REPLACE INTO pivots
               (symbol, for_date, based_on, prev_high, prev_low, prev_close,
                day_range, pp, r1, r2, r3, r4, r5, s1, s2, s3, s4, s5)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, for_date, based_on, bar["high"], bar["low"], bar["close"],
             lv["range"], lv["pp"], lv["r1"], lv["r2"], lv["r3"], lv["r4"],
             lv["r5"], lv["s1"], lv["s2"], lv["s3"], lv["s4"], lv["s5"]))
    pconn.commit()
    print(f"Computed    : {len(computed)} sets of levels -> {PIVOT_DB.name}")

    # ---- single symbol detail ----
    if args.symbol:
        match = [c for c in computed if c[0] == args.symbol]
        if not match:
            print(f"\n{args.symbol} not found for {based_on}.")
            sample = [c[0] for c in computed[:5]]
            print(f"Example symbols: {', '.join(sample)}")
            sys.exit(1)
        symbol, bar, lv = match[0]
        actual = curr.get(symbol)

        print(f"\n{'=' * 78}")
        print(f"  {symbol}")
        print(f"{'=' * 78}")
        print(f"\nPrevious day ({based_on}):")
        print(f"  High {bar['high']:.2f}   Low {bar['low']:.2f}   "
              f"Close {bar['close']:.2f}   Range {lv['range']:.2f}")
        print(f"\nLevels for {for_date}:")
        for key in ["r5", "r4", "r3", "r2", "r1"]:
            tag = "  <- breakout target" if key == "r4" else (
                  "  <- long entry" if key == "r3" else "")
            print(f"  {key.upper():<4} {lv[key]:>12.2f}{tag}")
        print(f"  {'PP':<4} {lv['pp']:>12.2f}")
        for key in ["s1", "s2", "s3", "s4", "s5"]:
            tag = "  <- breakdown target" if key == "s4" else (
                  "  <- short entry" if key == "s3" else "")
            print(f"  {key.upper():<4} {lv[key]:>12.2f}{tag}")

        if actual:
            print(f"\nWhat actually happened on {for_date}:")
            print(f"  Open {actual['open']:.2f}   High {actual['high']:.2f}   "
                  f"Low {actual['low']:.2f}   Close {actual['close']:.2f}")
            hit_r3 = actual["high"] >= lv["r3"]
            hit_r4 = actual["high"] >= lv["r4"]
            hit_s3 = actual["low"] <= lv["s3"]
            hit_s4 = actual["low"] <= lv["s4"]
            print(f"  Reached R3: {hit_r3}    Reached R4: {hit_r4}")
            print(f"  Reached S3: {hit_s3}    Reached S4: {hit_s4}")
        pconn.close()
        hist.close()
        return

    # ---- pre-market screen ----
    # The strategy starts from stocks opening near R3 or S3, so this finds
    # which ones are in that zone. Uses the open of `for_date` where
    # available, otherwise the prior close as a stand-in.
    print(f"\n{'=' * 78}")
    if args.absolute:
        print(f"  SCREEN: within {args.threshold}% of price, from R3 or S3")
    else:
        print(f"  SCREEN: within {args.threshold}% of prior day's RANGE, from R3 or S3")
    print(f"{'=' * 78}")

    hits = []
    for symbol, bar, lv in computed:
        ref_bar = curr.get(symbol)
        if ref_bar and ref_bar.get("open"):
            ref_price, ref_kind = ref_bar["open"], "open"
        else:
            ref_price, ref_kind = bar["close"], "prev close"
        if not ref_price:
            continue

        for level_name in ("r3", "s3"):
            level = lv[level_name]
            if level <= 0:
                continue
            gap = abs(ref_price - level)

            if args.absolute:
                # Old behaviour: distance as a percentage of price. Note this
                # scales with price, not with the level spacing, so on a
                # narrow-range day it flags almost everything.
                dist = gap / level * 100
            else:
                # Distance as a percentage of the previous day's range, which
                # is what the levels themselves are derived from. R3 sits at
                # 27.5% of range above close, so a value under ~25 means the
                # open is genuinely sitting in the entry zone rather than
                # merely being a small number of rupees away.
                if lv["range"] <= 0:
                    continue
                dist = gap / lv["range"] * 100

            if dist <= args.threshold:
                hits.append({
                    "symbol": symbol,
                    "side": "LONG" if level_name == "r3" else "SHORT",
                    "level_name": level_name.upper(),
                    "level": level,
                    "price": ref_price,
                    "price_kind": ref_kind,
                    "dist": dist,
                    "dist_pct_price": gap / level * 100,
                    "target": lv["r4"] if level_name == "r3" else lv["s4"],
                    "range": lv["range"],
                })

    hits.sort(key=lambda h: h["dist"])

    if not hits:
        print(f"\nNo symbols within {args.threshold}% on {for_date}.")
    else:
        unit = "of price" if args.absolute else "of range"
        print(f"\n{len(hits)} candidate(s) out of {len(computed)} symbols, closest first:\n")
        print(f"  {'Symbol':<24} {'Side':<6} {'Lvl':<4} {'Level':>10} "
              f"{'Price':>10} {'Dist':>7} {'Target':>10}")
        print("  " + "-" * 74)
        for h in hits[:40]:
            print(f"  {h['symbol']:<24} {h['side']:<6} {h['level_name']:<4} "
                  f"{h['level']:>10.2f} {h['price']:>10.2f} "
                  f"{h['dist']:>6.1f}% {h['target']:>10.2f}")
        if len(hits) > 40:
            print(f"  ... and {len(hits) - 40} more (use --export for the full list)")

        print(f"\n  Distance measured as % {unit}. Price source: {hits[0]['price_kind']}")

        both = {}
        for h in hits:
            both.setdefault(h["symbol"], []).append(h["side"])
        dual = [s for s, sides in both.items() if len(sides) > 1]
        if dual:
            print(f"\n  NOTE: {len(dual)} symbol(s) qualify as BOTH long and short.")
            print("  That means the open sits between two tight levels - the setup")
            print("  is ambiguous, not doubly good. Consider a lower threshold.")

    if args.export and hits:
        with open(EXPORT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(hits[0].keys()))
            w.writeheader()
            w.writerows(hits)
        print(f"\nExported {len(hits)} rows to {EXPORT_CSV.name}")

    print()
    print("These are levels and proximity only - not signals. The strategy")
    print("also wants volume, VWAP and direction confirmation before entry,")
    print("and none of that is checked here.")
    print()

    pconn.close()
    hist.close()


if __name__ == "__main__":
    main()
