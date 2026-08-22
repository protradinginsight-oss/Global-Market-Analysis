#!/usr/bin/env python3
"""
Universe builder - STEP 2 of building the collector.

Reads the symbol files downloaded by explore_symbols.py and works out
exactly which tickers to subscribe to, then splits them across the 4
accounts.

Still collects no market data. It produces a plain-text list you can read
and sanity-check before anything subscribes to it - a wrong universe would
quietly collect the wrong data for weeks.

Usage:
    py -3.12 build_universe.py
"""

import sys
import csv
import json
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
SYMBOL_DIR = BASE_DIR / "symbol_data"
OUT_FILE = BASE_DIR / "universe.json"
REPORT_FILE = BASE_DIR / "universe_report.txt"

# Verified against the real files: column 9 is the tradeable ticker,
# column 13 is the underlying name, column 6 is the trading session.
COL_TICKER = 9
COL_UNDERLYING = 13
COL_SESSION = 6
COL_DESC = 1

# The F&O underlying name and the index's own ticker don't match - the
# derivatives file calls it BANKNIFTY while the cash file calls it
# NIFTYBANK. Matching by name alone silently drops every index, so these
# are mapped explicitly.
INDEX_MAP = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
}

# Raw ticks are bulky, so only a handful of very liquid names get them.
# Everything else is aggregated to 1-minute candles, which is what the
# strategies actually consume.
TICK_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "SBIN"]

# Fyers v3 data socket limit, per connection.
MAX_SYMBOLS_PER_CONNECTION = 200


def load_csv(name):
    path = SYMBOL_DIR / f"{name}.csv"
    if not path.exists():
        print(f"ERROR: {path} not found.")
        print("Run  py -3.12 explore_symbols.py  first to download it.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.reader(f) if len(r) > COL_UNDERLYING]


def get_fo_underlyings(fo_rows):
    """Every distinct underlying that has derivatives listed."""
    unders = defaultdict(int)
    for r in fo_rows:
        name = r[COL_UNDERLYING].strip()
        if name:
            unders[name] += 1
    return unders


def build_cash_lookup(cm_rows):
    """underlying name -> its cash ticker, for rows that are equities."""
    lookup = {}
    for r in cm_rows:
        ticker = r[COL_TICKER].strip()
        name = r[COL_UNDERLYING].strip()
        if ticker.endswith("-EQ") and name:
            lookup[name] = ticker
    return lookup


def find_index_tickers(cm_rows):
    """All index tickers present, so the explicit map can be verified."""
    return {r[COL_TICKER].strip() for r in cm_rows
            if r[COL_TICKER].strip().endswith("-INDEX")}


def get_session(rows, ticker):
    for r in rows:
        if r[COL_TICKER].strip() == ticker:
            return r[COL_SESSION].strip()
    return ""


def main():
    print("=" * 70)
    print("  BUILDING SUBSCRIPTION UNIVERSE")
    print("=" * 70)

    fo_rows = load_csv("NSE_FO")
    cm_rows = load_csv("NSE_CM")
    print(f"\nRead {len(fo_rows):,} F&O rows and {len(cm_rows):,} cash rows.")

    underlyings = get_fo_underlyings(fo_rows)
    print(f"Found {len(underlyings)} distinct F&O underlyings.")

    cash_lookup = build_cash_lookup(cm_rows)
    available_indices = find_index_tickers(cm_rows)

    resolved = []       # (underlying, ticker, kind)
    unresolved = []

    for name in sorted(underlyings):
        if name in INDEX_MAP:
            ticker = INDEX_MAP[name]
            if ticker in available_indices:
                resolved.append((name, ticker, "index"))
            else:
                unresolved.append((name, f"index ticker {ticker} not in cash file"))
        elif name in cash_lookup:
            resolved.append((name, cash_lookup[name], "stock"))
        else:
            unresolved.append((name, "no matching -EQ ticker in cash file"))

    print(f"Resolved {len(resolved)} to tradeable tickers.")
    if unresolved:
        print(f"{len(unresolved)} could not be resolved (listed in the report).")

    # Split across accounts. Shards are contiguous alphabetical blocks so the
    # same symbol lands on the same account run after run - makes debugging a
    # specific symbol's feed far easier than a hash-based split would.
    try:
        from config_local import ACCOUNTS
        account_labels = [a["label"] for a in ACCOUNTS]
    except ImportError:
        print("\nWARNING: config_local.py not found, using placeholder names.")
        account_labels = [f"acc{i}" for i in range(1, 5)]

    n_acc = len(account_labels)
    capacity = n_acc * MAX_SYMBOLS_PER_CONNECTION
    if len(resolved) > capacity:
        print(f"\nERROR: {len(resolved)} symbols exceeds capacity of {capacity}")
        print(f"({n_acc} accounts x {MAX_SYMBOLS_PER_CONNECTION} per connection)")
        sys.exit(1)

    per = -(-len(resolved) // n_acc)   # ceiling division
    shards = {}
    for i, label in enumerate(account_labels):
        chunk = resolved[i * per:(i + 1) * per]
        shards[label] = [t for _, t, _ in chunk]

    tick_tickers = [cash_lookup[s] for s in TICK_SYMBOLS if s in cash_lookup]
    missing_tick = [s for s in TICK_SYMBOLS if s not in cash_lookup]

    universe = {
        "built_from": "Fyers public symbol master",
        "total_symbols": len(resolved),
        "max_per_connection": MAX_SYMBOLS_PER_CONNECTION,
        "shards": shards,
        "tick_symbols": tick_tickers,
        "all": [{"underlying": u, "ticker": t, "kind": k} for u, t, k in resolved],
    }
    OUT_FILE.write_text(json.dumps(universe, indent=2))

    # A readable report, so the list can actually be eyeballed
    lines = []
    lines.append("SUBSCRIPTION UNIVERSE REPORT")
    lines.append("=" * 70)
    lines.append(f"Total symbols to subscribe: {len(resolved)}")
    lines.append(f"Capacity: {capacity} ({n_acc} accounts x {MAX_SYMBOLS_PER_CONNECTION})")
    lines.append(f"Headroom: {capacity - len(resolved)} spare slots")
    lines.append("")
    lines.append("INDICES")
    lines.append("-" * 70)
    for u, t, k in resolved:
        if k == "index":
            lines.append(f"  {u:<16} -> {t}")
    lines.append("")
    lines.append("SHARD ASSIGNMENT")
    lines.append("-" * 70)
    for label, syms in shards.items():
        lines.append(f"  {label:<26} {len(syms):>4} symbols   "
                     f"({syms[0] if syms else '-'} ... {syms[-1] if syms else '-'})")
    lines.append("")
    lines.append("RAW TICK STORAGE (everything else gets 1-min candles)")
    lines.append("-" * 70)
    for t in tick_tickers:
        lines.append(f"  {t}")
    if missing_tick:
        lines.append("  NOT FOUND: " + ", ".join(missing_tick))
    lines.append("")
    if unresolved:
        lines.append("UNRESOLVED (have derivatives but no cash ticker found)")
        lines.append("-" * 70)
        lines.append("These are usually delisted names, indices not in the map,")
        lines.append("or symbols that changed name. Worth a glance to confirm")
        lines.append("nothing important is being dropped.")
        for name, reason in unresolved:
            lines.append(f"  {name:<20} {reason}")
        lines.append("")
    lines.append("FULL LIST")
    lines.append("-" * 70)
    for u, t, k in resolved:
        lines.append(f"  {k:<6} {u:<20} {t}")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")

    print()
    print("=" * 70)
    print(f"Symbols to subscribe : {len(resolved)}")
    print(f"Capacity             : {capacity}  ({capacity - len(resolved)} spare)")
    print()
    for label, syms in shards.items():
        print(f"  {label:<26} {len(syms):>4} symbols")
    print()
    print(f"Indices found        : {sum(1 for _,_,k in resolved if k == 'index')}")
    print(f"Stocks found         : {sum(1 for _,_,k in resolved if k == 'stock')}")
    print(f"Tick-data symbols    : {len(tick_tickers)}")
    if unresolved:
        print(f"Unresolved           : {len(unresolved)}  (see report)")
    print()
    print(f"Written: universe.json  and  universe_report.txt")
    print("Open universe_report.txt and check the list looks sensible")
    print("before we subscribe to any of it.")


if __name__ == "__main__":
    main()
