#!/usr/bin/env python3
"""
MCX universe setup.

Downloads Fyers' MCX symbol master, reports what's in it, and builds a
subscription list of the front-month contracts worth tracking.

MCX differs from NSE in ways that matter:
  - Everything is a futures contract; there is no cash/spot ticker to
    subscribe to, so the universe is contracts rather than underlyings.
  - Multiple expiries trade at once, and liquidity concentrates in the
    front month. Subscribing to every expiry wastes slots on contracts
    that barely trade.
  - Trading runs to nearly midnight, which the NSE-hours collector would
    have cut off at 3:30 PM.

Usage:
    py -3.12 mcx_setup.py --explore     # download and describe the file
    py -3.12 mcx_setup.py --build       # build the universe
"""

import sys
import csv
import io
import json
import argparse
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict, Counter

try:
    import requests
except ImportError:
    print("Missing requests. Install with:  py -3.12 -m pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
SYMBOL_DIR = BASE_DIR / "symbol_data"
OUT_FILE = BASE_DIR / "mcx_universe.json"
REPORT_FILE = BASE_DIR / "mcx_universe_report.txt"

MCX_URL = "https://public.fyers.in/sym_details/MCX_COM.csv"

# Verified column positions from the NSE files; MCX uses the same layout.
COL_TICKER = 9
COL_UNDERLYING = 13
COL_SESSION = 6
COL_DESC = 1
COL_EXPIRY_EPOCH = 8

# What's actually worth tracking. MCX lists a long tail of contracts that
# barely trade; these are the ones with real liquidity and real relevance.
# Verified against the real MCX symbol master. Contract counts in the file
# are a good liquidity proxy: GOLD has 4,014 contracts listed, GOLDGUINEA
# has 6.
WANTED = [
    # Energy
    "CRUDEOIL", "CRUDEOILM",
    "NATURALGAS", "NATGASMINI",
    # Bullion
    "GOLD", "GOLDM", "SILVER", "SILVERM",
    # Base metals
    "COPPER", "ZINC", "ALUMINIUM", "LEAD",
    "ZINCMINI", "ALUMINI", "LEADMINI",
    # MCX's own sector indices - useful as a commodity-complex read in the
    # same way Nifty is for equities.
    "MCXBULLDEX", "MCXMETLDEX",
    # Deliberately excluded, all with only ~5-6 contracts listed and
    # correspondingly thin trade: GOLDGUINEA, GOLDPETAL, GOLDTEN,
    # SILVER100, STEELREBAR, MENTHAOIL, CARDAMOM, COTTON.
    # NICKEL is absent from the symbol master entirely - it appears to have
    # been delisted rather than merely being illiquid.
]


def download():
    SYMBOL_DIR.mkdir(exist_ok=True)
    print(f"Downloading MCX symbol master...")
    resp = requests.get(MCX_URL, timeout=60)
    resp.raise_for_status()
    path = SYMBOL_DIR / "MCX_COM.csv"
    path.write_text(resp.text, encoding="utf-8")
    print(f"  {len(resp.content)/1024:.0f} KB saved to symbol_data\\MCX_COM.csv")
    return resp.text


def load_rows(raw=None):
    path = SYMBOL_DIR / "MCX_COM.csv"
    if raw is None:
        if not path.exists():
            print("MCX_COM.csv not found. Run with --explore first.")
            sys.exit(1)
        raw = path.read_text(encoding="utf-8")
    return [r for r in csv.reader(io.StringIO(raw)) if len(r) > COL_UNDERLYING]


def cmd_explore():
    raw = download()
    rows = load_rows(raw)

    print(f"\n{'=' * 74}")
    print("  MCX SYMBOL MASTER")
    print(f"{'=' * 74}")
    print(f"\nTotal rows: {len(rows):,}")

    print("\nFirst row, one column per line:")
    for j, cell in enumerate(rows[0]):
        shown = cell if len(cell) <= 55 else cell[:52] + "..."
        print(f"  [{j:>2}] {shown}")

    kinds = Counter()
    for r in rows:
        t = r[COL_TICKER]
        if "FUT" in t:
            kinds["futures"] += 1
        elif t.endswith("CE"):
            kinds["call options"] += 1
        elif t.endswith("PE"):
            kinds["put options"] += 1
        else:
            kinds["other"] += 1
    print("\nInstrument types:")
    for k, v in kinds.most_common():
        print(f"  {k:<18} {v:>7,}")

    unders = Counter(r[COL_UNDERLYING].strip() for r in rows
                     if r[COL_UNDERLYING].strip())
    print(f"\nDistinct underlyings: {len(unders)}")
    print("\nMost contracts, by underlying:")
    for u, c in unders.most_common(25):
        want = "  <- tracking" if u in WANTED else ""
        print(f"  {u:<18} {c:>6,}{want}")

    sessions = Counter(r[COL_SESSION].strip() for r in rows)
    print("\nTrading sessions present in the file:")
    for s, c in sessions.most_common(8):
        print(f"  {s:<32} {c:>7,}")

    print("\nSample futures tickers:")
    futs = [r[COL_TICKER] for r in rows if "FUT" in r[COL_TICKER]][:8]
    for f in futs:
        print(f"  {f}")

    missing = [w for w in WANTED if w not in unders]
    if missing:
        print(f"\nWanted but not present: {', '.join(missing)}")
        print("(names may differ on MCX - check the underlying list above)")

    print("\nRun with --build to create the subscription universe.\n")


def cmd_build():
    rows = load_rows()

    # Group futures by underlying, keeping expiry so the front month can be
    # picked. Options are excluded: MCX option chains are better pulled via
    # the REST optionchain endpoint, same as NSE.
    by_underlying = defaultdict(list)
    for r in rows:
        ticker = r[COL_TICKER].strip()
        und = r[COL_UNDERLYING].strip()
        if "FUT" not in ticker or und not in WANTED:
            continue
        try:
            expiry = int(r[COL_EXPIRY_EPOCH]) if r[COL_EXPIRY_EPOCH].strip() else 0
        except ValueError:
            expiry = 0
        by_underlying[und].append({
            "ticker": ticker,
            "expiry_epoch": expiry,
            "expiry": (datetime.fromtimestamp(expiry).date().isoformat()
                       if expiry else None),
            "description": r[COL_DESC].strip(),
            "session": r[COL_SESSION].strip(),
        })

    if not by_underlying:
        print("No matching MCX futures found. Run --explore and check")
        print("whether the underlying names in WANTED match the file.")
        sys.exit(1)

    now_epoch = int(datetime.now().timestamp())
    universe = []
    for und, contracts in sorted(by_underlying.items()):
        # Front month = nearest expiry that hasn't passed. Liquidity sits
        # there; far months on MCX are often barely traded.
        future = sorted([c for c in contracts if c["expiry_epoch"] > now_epoch],
                        key=lambda c: c["expiry_epoch"])
        if not future:
            continue
        front = future[0]
        entry = dict(front)
        entry["underlying"] = und
        entry["all_expiries"] = len(contracts)
        universe.append(entry)

    from market_hours import segment_for
    payload = {
        "built_on": date.today().isoformat(),
        "source": "Fyers MCX_COM symbol master",
        "count": len(universe),
        "contracts": [
            {"ticker": u["ticker"], "underlying": u["underlying"],
             "expiry": u["expiry"], "segment": segment_for(u["ticker"])}
            for u in universe
        ],
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2))

    lines = ["MCX SUBSCRIPTION UNIVERSE", "=" * 74,
             f"Built: {date.today().isoformat()}",
             f"Contracts: {len(universe)} (front month only)", ""]
    lines.append(f"{'Underlying':<16} {'Ticker':<28} {'Expiry':<12} "
                 f"{'Expiries':>9} Segment")
    lines.append("-" * 74)
    for u in universe:
        lines.append(f"{u['underlying']:<16} {u['ticker']:<28} "
                     f"{u['expiry'] or '-':<12} {u['all_expiries']:>9} "
                     f"{segment_for(u['ticker'])}")
    lines.append("")
    lines.append("Only front-month contracts are included. MCX lists several")
    lines.append("expiries per commodity but liquidity concentrates in the")
    lines.append("nearest one, so tracking the rest would spend websocket")
    lines.append("slots on contracts that barely trade.")
    lines.append("")
    lines.append("Options are excluded - MCX option chains come from the REST")
    lines.append("optionchain endpoint, the same as NSE.")
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")

    print("=" * 74)
    print("  MCX UNIVERSE BUILT")
    print("=" * 74)
    print(f"\n{'Underlying':<16} {'Ticker':<28} {'Expiry':<12} Segment")
    print("-" * 74)
    for u in universe:
        print(f"{u['underlying']:<16} {u['ticker']:<28} "
              f"{u['expiry'] or '-':<12} {segment_for(u['ticker'])}")
    print("-" * 74)

    # Front-month contracts expire constantly on MCX - several within days.
    # A collector subscribed to an expired contract receives nothing and
    # reports no error, so this file has to be rebuilt regularly.
    today = date.today()
    soon = []
    for u in universe:
        if not u["expiry"]:
            continue
        days = (date.fromisoformat(u["expiry"]) - today).days
        if days <= 7:
            soon.append((u["underlying"], u["expiry"], days))
    if soon:
        soon.sort(key=lambda x: x[2])
        print(f"\n  EXPIRING SOON - rebuild after these roll over:")
        for und, exp, days in soon:
            when = "TODAY" if days == 0 else ("tomorrow" if days == 1
                                              else f"in {days} days")
            print(f"    {und:<16} {exp}   {when}")
        print("\n  Re-run:  py -3.12 mcx_setup.py --explore --build")
        print("  Weekly is usually enough; more often around monthly expiry.")

    print(f"\n{len(universe)} front-month contracts -> {OUT_FILE.name}")
    print(f"Readable report -> {REPORT_FILE.name}")
    print("\nNote MCX trades until nearly midnight. The market_hours module")
    print("handles this; a collector using NSE hours would stop at 3:30 PM")
    print("and lose the entire evening session.\n")


def main():
    ap = argparse.ArgumentParser(description="MCX universe setup")
    ap.add_argument("--explore", action="store_true",
                    help="download and describe the symbol master")
    ap.add_argument("--build", action="store_true",
                    help="build the subscription universe")
    args = ap.parse_args()

    if args.explore:
        cmd_explore()
    elif args.build:
        cmd_build()
    else:
        print("Run with --explore first, then --build.")


if __name__ == "__main__":
    main()
