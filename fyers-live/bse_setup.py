#!/usr/bin/env python3
"""
BSE universe setup - Sensex, Bankex, and BSE-listed F&O names.

Follows the same shape as mcx_setup.py: download the exchange's symbol
master, describe it, then build a subscription list.

Why bother when NSE is already covered: Sensex is the index most Indian
retail flow and media reference, its option chain is separately traded from
Nifty's, and BSE has been taking meaningful derivatives market share. Sensex
and Bankex expiries also fall on different days from Nifty's, which matters
for anyone selling premium across both.

Usage:
    py -3.12 bse_setup.py --explore
    py -3.12 bse_setup.py --build
"""

import sys
import csv
import io
import json
import argparse
from datetime import datetime, date
from pathlib import Path
from collections import Counter

try:
    import requests
except ImportError:
    print("Missing requests. Install with:  py -3.12 -m pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
SYMBOL_DIR = BASE_DIR / "symbol_data"
OUT_FILE = BASE_DIR / "bse_universe.json"
REPORT_FILE = BASE_DIR / "bse_universe_report.txt"

# BSE publishes two files, mirroring NSE's cash/derivatives split.
SOURCES = {
    "BSE_CM": "https://public.fyers.in/sym_details/BSE_CM.csv",
    "BSE_FO": "https://public.fyers.in/sym_details/BSE_FO.csv",
}

COL_TICKER = 9
COL_UNDERLYING = 13
COL_SESSION = 6
COL_DESC = 1
COL_EXPIRY_EPOCH = 8

# The BSE indices worth tracking. Their derivatives are actively traded and
# their expiry calendars differ from NSE's.
WANTED_INDICES = ["SENSEX", "BANKEX", "SENSEX50"]


def download(name, url):
    SYMBOL_DIR.mkdir(exist_ok=True)
    print(f"Downloading {name} ...", end=" ", flush=True)
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"FAILED: {type(e).__name__}")
        return None
    (SYMBOL_DIR / f"{name}.csv").write_text(resp.text, encoding="utf-8")
    print(f"{len(resp.content)/1024:.0f} KB")
    return resp.text


def load_rows(name):
    path = SYMBOL_DIR / f"{name}.csv"
    if not path.exists():
        return []
    return [r for r in csv.reader(io.StringIO(path.read_text(encoding="utf-8")))
            if len(r) > COL_UNDERLYING]


def describe(name, rows):
    print(f"\n{'=' * 74}")
    print(f"  {name}")
    print(f"{'=' * 74}")
    print(f"\nRows: {len(rows):,}")
    if not rows:
        print("  (nothing downloaded)")
        return

    print("\nFirst row, one column per line:")
    for j, cell in enumerate(rows[0]):
        shown = cell if len(cell) <= 55 else cell[:52] + "..."
        print(f"  [{j:>2}] {shown}")

    kinds = Counter()
    for r in rows:
        t = r[COL_TICKER]
        if t.endswith("-INDEX"):
            kinds["index"] += 1
        elif t.endswith("-EQ") or t.endswith("-A") or t.endswith("-B"):
            kinds["cash equity"] += 1
        elif "FUT" in t:
            kinds["futures"] += 1
        elif t.endswith("CE"):
            kinds["call options"] += 1
        elif t.endswith("PE"):
            kinds["put options"] += 1
        else:
            kinds["other"] += 1
    print("\nInstrument types:")
    for k, v in kinds.most_common():
        print(f"  {k:<16} {v:>8,}")

    unders = Counter(r[COL_UNDERLYING].strip() for r in rows
                     if r[COL_UNDERLYING].strip())
    print(f"\nDistinct underlyings: {len(unders)}")
    print("\nTop 20 by contract count:")
    for u, c in unders.most_common(20):
        want = "  <- tracking" if u in WANTED_INDICES else ""
        print(f"  {u:<20} {c:>7,}{want}")

    idx = sorted({r[COL_TICKER] for r in rows
                  if r[COL_TICKER].endswith("-INDEX")})
    if idx:
        print(f"\nIndex tickers ({len(idx)}), first 15:")
        for t in idx[:15]:
            print(f"  {t}")

    sessions = Counter(r[COL_SESSION].strip() for r in rows)
    print("\nTrading sessions:")
    for s, c in sessions.most_common(5):
        print(f"  {s:<32} {c:>8,}")

    missing = [w for w in WANTED_INDICES if w not in unders]
    if missing:
        print(f"\nWanted but absent: {', '.join(missing)}")
        print("(check the underlying list above - BSE naming may differ)")


def cmd_explore():
    print("=" * 74)
    print("  BSE SYMBOL MASTERS")
    print("=" * 74)
    print()
    for name, url in SOURCES.items():
        download(name, url)
    for name in SOURCES:
        describe(name, load_rows(name))
    print("\nRun with --build once the names above look right.\n")


def cmd_build():
    cm_rows = load_rows("BSE_CM")
    fo_rows = load_rows("BSE_FO")
    if not cm_rows and not fo_rows:
        print("No BSE files found. Run --explore first.")
        sys.exit(1)

    from market_hours import segment_for

    universe = []

    # Index spot tickers - the levels themselves, for charting and analysis.
    index_tickers = {}
    for r in cm_rows:
        t = r[COL_TICKER].strip()
        u = r[COL_UNDERLYING].strip()
        if t.endswith("-INDEX") and u in WANTED_INDICES:
            index_tickers[u] = t
    for u, t in sorted(index_tickers.items()):
        universe.append({"underlying": u, "ticker": t, "kind": "index"})

    # Front-month index futures, same reasoning as MCX: liquidity sits in
    # the nearest expiry.
    now_epoch = int(datetime.now().timestamp())
    by_und = {}
    for r in fo_rows:
        t = r[COL_TICKER].strip()
        u = r[COL_UNDERLYING].strip()
        if "FUT" not in t or u not in WANTED_INDICES:
            continue
        try:
            exp = int(r[COL_EXPIRY_EPOCH]) if r[COL_EXPIRY_EPOCH].strip() else 0
        except ValueError:
            exp = 0
        if exp <= now_epoch:
            continue
        if u not in by_und or exp < by_und[u]["expiry_epoch"]:
            by_und[u] = {"ticker": t, "expiry_epoch": exp,
                         "expiry": datetime.fromtimestamp(exp).date().isoformat()}
    for u, d in sorted(by_und.items()):
        universe.append({"underlying": u, "ticker": d["ticker"],
                         "kind": "future", "expiry": d["expiry"]})

    if not universe:
        print("Nothing matched. Run --explore and check whether the index")
        print("names in WANTED_INDICES match what's actually in the files.")
        sys.exit(1)

    payload = {
        "built_on": date.today().isoformat(),
        "source": "Fyers BSE_CM and BSE_FO symbol masters",
        "count": len(universe),
        "contracts": [
            {"ticker": u["ticker"], "underlying": u["underlying"],
             "kind": u["kind"], "expiry": u.get("expiry"),
             "segment": segment_for(u["ticker"])}
            for u in universe
        ],
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2))

    lines = ["BSE SUBSCRIPTION UNIVERSE", "=" * 74,
             f"Built: {date.today().isoformat()}",
             f"Entries: {len(universe)}", "",
             f"{'Underlying':<14} {'Ticker':<30} {'Kind':<10} Expiry",
             "-" * 74]
    for u in universe:
        lines.append(f"{u['underlying']:<14} {u['ticker']:<30} "
                     f"{u['kind']:<10} {u.get('expiry') or '-'}")
    lines.append("")
    lines.append("Sensex and Bankex expiries fall on different days from")
    lines.append("Nifty's, which matters when selling premium across both.")
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")

    print("=" * 74)
    print("  BSE UNIVERSE BUILT")
    print("=" * 74)
    print(f"\n{'Underlying':<14} {'Ticker':<30} {'Kind':<10} Expiry")
    print("-" * 74)
    for u in universe:
        print(f"{u['underlying']:<14} {u['ticker']:<30} "
              f"{u['kind']:<10} {u.get('expiry') or '-'}")
    print("-" * 74)

    today = date.today()
    soon = [(u["underlying"], u["expiry"],
             (date.fromisoformat(u["expiry"]) - today).days)
            for u in universe if u.get("expiry")]
    soon = [s for s in soon if s[2] <= 7]
    if soon:
        print("\n  EXPIRING SOON - rebuild after these roll:")
        for und, exp, d in sorted(soon, key=lambda x: x[2]):
            when = "TODAY" if d == 0 else ("tomorrow" if d == 1 else f"in {d} days")
            print(f"    {und:<14} {exp}   {when}")

    print(f"\n{len(universe)} entries -> {OUT_FILE.name}")
    print("\nBSE keeps the same hours as NSE (09:15-15:30), so no new market")
    print("hours handling is needed - unlike MCX.\n")


def main():
    ap = argparse.ArgumentParser(description="BSE universe setup")
    ap.add_argument("--explore", action="store_true")
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()
    if args.explore:
        cmd_explore()
    elif args.build:
        cmd_build()
    else:
        print("Run with --explore first, then --build.")


if __name__ == "__main__":
    main()
