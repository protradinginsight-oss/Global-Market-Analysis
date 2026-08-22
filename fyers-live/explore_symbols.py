#!/usr/bin/env python3
"""
Symbol universe explorer - STEP 1 of building the collector.

This does not collect any market data. Its only job is to download Fyers'
public symbol list and show what's actually in it, so the real collector can
be built against the verified format rather than a guess.

Run it, then send the output back.

Usage:
    py -3.12 explore_symbols.py
"""

import sys
import csv
import io
from pathlib import Path
from collections import Counter

try:
    import requests
except ImportError:
    print("Missing requests. Install with:  py -3.12 -m pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "symbol_data"
OUT_DIR.mkdir(exist_ok=True)

# Fyers publishes symbol master files publicly - no auth needed.
SOURCES = {
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "NSE_CM": "https://public.fyers.in/sym_details/NSE_CM.csv",
}


def download(name, url):
    print(f"\nDownloading {name} ...")
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return None

    raw = resp.text
    path = OUT_DIR / f"{name}.csv"
    path.write_text(raw, encoding="utf-8")
    size_mb = len(resp.content) / (1024 * 1024)
    print(f"  OK - {size_mb:.1f} MB saved to symbol_data\\{name}.csv")
    return raw


def inspect(name, raw):
    """Report the file's shape without assuming any particular layout."""
    print(f"\n{'=' * 70}")
    print(f"  {name}")
    print(f"{'=' * 70}")

    lines = raw.splitlines()
    print(f"Total rows: {len(lines):,}")

    if not lines:
        print("  (empty file)")
        return

    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        print("  (no parseable CSV rows)")
        return

    widths = Counter(len(r) for r in rows[:500])
    print(f"Column counts seen in first 500 rows: {dict(widths)}")

    print("\nFirst 3 rows, one column per line:")
    for i, row in enumerate(rows[:3]):
        print(f"\n  --- row {i} ---")
        for j, cell in enumerate(row):
            shown = cell if len(cell) <= 60 else cell[:57] + "..."
            print(f"    [{j:>2}] {shown}")

    # Find which column holds the tradeable ticker, e.g. "NSE:SBIN-EQ".
    ticker_col = None
    for j in range(len(rows[0])):
        sample = [r[j] for r in rows[:200] if len(r) > j]
        hits = sum(1 for s in sample if s.startswith("NSE:"))
        if hits > len(sample) * 0.8:
            ticker_col = j
            break

    if ticker_col is None:
        print("\nCould not identify the ticker column automatically.")
        return

    print(f"\nTicker column looks like column [{ticker_col}].")
    tickers = [r[ticker_col] for r in rows if len(r) > ticker_col]

    suffixes = Counter()
    for t in tickers:
        if t.endswith("-EQ"):
            suffixes["-EQ (cash equity)"] += 1
        elif t.endswith("-INDEX"):
            suffixes["-INDEX"] += 1
        elif "FUT" in t:
            suffixes["FUT (futures)"] += 1
        elif t.endswith("CE"):
            suffixes["CE (call options)"] += 1
        elif t.endswith("PE"):
            suffixes["PE (put options)"] += 1
        else:
            suffixes["other"] += 1

    print("\nWhat kinds of instruments are in here:")
    for kind, count in suffixes.most_common():
        print(f"  {kind:<24} {count:>8,}")

    print("\nSample tickers of each kind:")
    for pattern, label in [("-EQ", "cash equity"), ("-INDEX", "index"),
                           ("FUT", "futures"), ("CE", "call option")]:
        matches = [t for t in tickers if
                   (t.endswith(pattern) if pattern != "FUT" else "FUT" in t)][:3]
        if matches:
            print(f"  {label:<14} {matches}")


def main():
    print("=" * 70)
    print("  FYERS SYMBOL UNIVERSE EXPLORER")
    print("  (downloads the public symbol list, collects no market data)")
    print("=" * 70)

    any_ok = False
    for name, url in SOURCES.items():
        raw = download(name, url)
        if raw:
            any_ok = True
            inspect(name, raw)

    print(f"\n{'=' * 70}")
    if any_ok:
        print("Done. Files saved in the symbol_data folder.")
        print("Send this output back so the collector can be built to match.")
    else:
        print("Nothing downloaded. Check your internet connection and retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
