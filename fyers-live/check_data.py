#!/usr/bin/env python3
"""
Quick look at what the collector has stored.

Read-only - safe to run while the collector is going, since the databases
use WAL mode.

Usage:
    py -3.12 check_data.py
"""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CANDLE_DB = BASE_DIR / "market_candles.db"
TICK_DB = BASE_DIR / "market_ticks.db"


def size_mb(p):
    return p.stat().st_size / (1024 * 1024) if p.exists() else 0


def main():
    print("=" * 68)
    print("  COLLECTED DATA SUMMARY")
    print("=" * 68)

    if not CANDLE_DB.exists():
        print("\nNo candle database yet - run the collector first.")
        return

    c = sqlite3.connect(f"file:{CANDLE_DB}?mode=ro", uri=True)
    total, symbols = c.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM candles_1m").fetchone()
    print(f"\n1-MINUTE CANDLES  ({size_mb(CANDLE_DB):.1f} MB)")
    print(f"  Rows            : {total:,}")
    print(f"  Distinct symbols: {symbols}")

    if total:
        first, last = c.execute(
            "SELECT MIN(minute_ts), MAX(minute_ts) FROM candles_1m").fetchone()
        print(f"  Earliest        : {first}")
        print(f"  Latest          : {last}")

        print("\n  Sample rows:")
        for row in c.execute(
                "SELECT symbol, minute_ts, open, high, low, close, volume, tick_count "
                "FROM candles_1m ORDER BY minute_ts DESC LIMIT 5"):
            sym, ts, o, h, l, cl, v, n = row
            print(f"    {sym:<22} {ts[11:16]}  O={o:<9.2f} H={h:<9.2f} "
                  f"L={l:<9.2f} C={cl:<9.2f} vol={v:<10} ticks={n}")

        print("\n  Coverage by day:")
        for day, cnt, syms in c.execute(
                "SELECT substr(minute_ts,1,10), COUNT(*), COUNT(DISTINCT symbol) "
                "FROM candles_1m GROUP BY 1 ORDER BY 1 DESC LIMIT 7"):
            print(f"    {day}   {cnt:>8,} candles across {syms:>4} symbols")
    c.close()

    if TICK_DB.exists():
        t = sqlite3.connect(f"file:{TICK_DB}?mode=ro", uri=True)
        n, syms = t.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM ticks").fetchone()
        print(f"\nRAW TICKS  ({size_mb(TICK_DB):.1f} MB)")
        print(f"  Rows            : {n:,}")
        print(f"  Distinct symbols: {syms}")
        if n:
            print("\n  Per symbol:")
            for sym, cnt in t.execute(
                    "SELECT symbol, COUNT(*) FROM ticks GROUP BY symbol ORDER BY 2 DESC"):
                print(f"    {sym:<22} {cnt:>8,}")
        t.close()

    print("\n" + "=" * 68)


if __name__ == "__main__":
    main()
