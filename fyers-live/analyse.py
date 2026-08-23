#!/usr/bin/env python3
"""
Compute technical indicators over stored price history.

Reads market_history.db (backfilled candles) and applies the indicator
library. Read-only.

Usage:
    py -3.12 analyse.py --symbol NSE:SBIN-EQ
    py -3.12 analyse.py --symbol NSE:SBIN-EQ --resolution 5 --bars 100
    py -3.12 analyse.py --scan
    py -3.12 analyse.py --scan --filter oversold
"""

import sys
import sqlite3
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HIST_DB = BASE_DIR / "market_history.db"

try:
    from indicators import (sma, ema, rsi, macd, atr, bollinger, vwap,
                            stochastic, adx, supertrend)
except ImportError:
    print("indicators.py not found - it must sit next to this script.")
    sys.exit(1)


def load(conn, symbol, resolution, limit):
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM history
           WHERE symbol=? AND resolution=? ORDER BY epoch DESC LIMIT ?""",
        (symbol, resolution, limit)).fetchall()
    rows.reverse()
    if not rows:
        return None
    return {
        "ts":     [r[0] for r in rows],
        "open":   [r[1] for r in rows],
        "high":   [r[2] for r in rows],
        "low":    [r[3] for r in rows],
        "close":  [r[4] for r in rows],
        "volume": [r[5] or 0 for r in rows],
    }


def session_vwap(dates, highs, lows, closes, volumes):
    """VWAP that resets at the start of each trading day.

    Plain cumulative VWAP across multi-day data produces a number no trader
    uses - real VWAP restarts every session at 9:15. This splits the series
    by date and runs VWAP within each day separately.
    """
    out = [None] * len(closes)
    cum_pv = cum_v = 0.0
    current_day = None
    for i in range(len(closes)):
        day = dates[i][:10]
        if day != current_day:
            cum_pv = cum_v = 0.0
            current_day = day
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        v = volumes[i] or 0
        cum_pv += typical * v
        cum_v += v
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def compute(d):
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    macd_line, macd_sig, macd_hist = macd(c)
    bb_u, bb_m, bb_l = bollinger(c, 20, 2.0)
    k, dd = stochastic(h, l, c, 14, 3)
    adx_v, plus_di, minus_di = adx(h, l, c, 14)
    st_line, st_dir = supertrend(h, l, c, 10, 3.0)
    return {
        "sma20": sma(c, 20), "sma50": sma(c, 50), "sma200": sma(c, 200),
        "ema9": ema(c, 9), "ema21": ema(c, 21),
        "rsi14": rsi(c, 14),
        "macd": macd_line, "macd_signal": macd_sig, "macd_hist": macd_hist,
        "atr14": atr(h, l, c, 14),
        "bb_upper": bb_u, "bb_mid": bb_m, "bb_lower": bb_l,
        "stoch_k": k, "stoch_d": dd,
        "adx": adx_v, "plus_di": plus_di, "minus_di": minus_di,
        "supertrend": st_line, "st_dir": st_dir,
        "vwap": session_vwap(d["ts"], h, l, c, v),
    }


def fmt(v, dp=2, suffix=""):
    return f"{v:,.{dp}f}{suffix}" if v is not None else "-"


def show(symbol, resolution, d, ind):
    i = -1
    c = d["close"][i]
    print("=" * 76)
    print(f"  {symbol}   ({resolution} bars, latest {d['ts'][i]})")
    print("=" * 76)
    print(f"\nClose {fmt(c)}   High {fmt(d['high'][i])}   "
          f"Low {fmt(d['low'][i])}   Volume {d['volume'][i]:,}")
    print(f"Bars loaded: {len(d['close'])}")

    print("\nTREND")
    trend_rows = [("SMA 20", "sma20"), ("SMA 50", "sma50"),
                  ("SMA 200", "sma200"), ("EMA 9", "ema9"),
                  ("EMA 21", "ema21")]
    # VWAP only means something intraday - on daily bars each "session" is a
    # single bar, so VWAP would just repeat the typical price.
    if resolution != "1D":
        trend_rows.append(("VWAP (today)", "vwap"))
    for name, key in trend_rows:
        val = ind[key][i]
        rel = ""
        if val is not None:
            rel = f"  price {'above' if c > val else 'below'} ({(c-val)/val*100:+.2f}%)"
        print(f"  {name:<12} {fmt(val):>12}{rel}")

    st, sd = ind["supertrend"][i], ind["st_dir"][i]
    if st is not None:
        print(f"  {'Supertrend':<12} {fmt(st):>12}  "
              f"{'UPTREND' if sd == 1 else 'DOWNTREND'}")

    print("\nMOMENTUM")
    r = ind["rsi14"][i]
    if r is not None:
        zone = ("overbought" if r >= 70 else "oversold" if r <= 30 else "neutral")
        print(f"  {'RSI 14':<12} {fmt(r):>12}  {zone}")
    ml, ms, mh = ind["macd"][i], ind["macd_signal"][i], ind["macd_hist"][i]
    if ml is not None and ms is not None:
        cross = "bullish" if ml > ms else "bearish"
        print(f"  {'MACD':<12} {fmt(ml, 3):>12}  signal {fmt(ms, 3)}, "
              f"hist {fmt(mh, 3)} ({cross})")
    kk, ddv = ind["stoch_k"][i], ind["stoch_d"][i]
    if kk is not None:
        print(f"  {'Stoch %K/%D':<12} {fmt(kk):>12}  /  {fmt(ddv)}")

    print("\nVOLATILITY & STRENGTH")
    a = ind["atr14"][i]
    if a is not None:
        print(f"  {'ATR 14':<12} {fmt(a):>12}  ({a/c*100:.2f}% of price)")
    bu, bm, bl = ind["bb_upper"][i], ind["bb_mid"][i], ind["bb_lower"][i]
    if bu is not None:
        pos = (c - bl) / (bu - bl) * 100 if bu > bl else 50
        print(f"  {'Bollinger':<12} {fmt(bl):>12} .. {fmt(bu)}   "
              f"price at {pos:.0f}% of band")
    ax, pdi, mdi = ind["adx"][i], ind["plus_di"][i], ind["minus_di"][i]
    if ax is not None:
        strength = ("strong trend" if ax >= 25 else
                    "weak/ranging" if ax < 20 else "developing")
        print(f"  {'ADX 14':<12} {fmt(ax):>12}  {strength}"
              f"   +DI {fmt(pdi)} / -DI {fmt(mdi)}")

    print("\nIndicators describe what price has already done. They are inputs")
    print("to a decision, not signals - and on their own they backtest poorly.\n")


def cmd_scan(conn, resolution, filt):
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM history WHERE resolution=? ORDER BY symbol",
        (resolution,))]
    if not symbols:
        print(f"No {resolution} data. Run backfill.py first.")
        return

    print("=" * 76)
    print(f"  INDICATOR SCAN  ({len(symbols)} symbols, {resolution} bars)")
    if filt:
        print(f"  filter: {filt}")
    print("=" * 76)
    print(f"\n{'Symbol':<24} {'Close':>10} {'RSI':>7} {'ADX':>7} "
          f"{'vs SMA50':>10} {'Trend':>10}")
    print("-" * 76)

    shown = 0
    for sym in symbols:
        d = load(conn, sym, resolution, 250)
        if not d or len(d["close"]) < 60:
            continue
        c = d["close"][-1]
        r = rsi(d["close"], 14)[-1]
        ax = adx(d["high"], d["low"], d["close"], 14)[0][-1]
        s50 = sma(d["close"], 50)[-1]
        _, sd = supertrend(d["high"], d["low"], d["close"], 10, 3.0)
        trend = "up" if sd[-1] == 1 else "down" if sd[-1] == -1 else "-"

        if filt == "oversold" and (r is None or r > 30):
            continue
        if filt == "overbought" and (r is None or r < 70):
            continue
        if filt == "trending" and (ax is None or ax < 25):
            continue
        if filt == "above-sma50" and (s50 is None or c <= s50):
            continue

        rel = f"{(c-s50)/s50*100:+.1f}%" if s50 else "-"
        print(f"{sym:<24} {c:>10,.2f} {fmt(r, 1):>7} {fmt(ax, 1):>7} "
              f"{rel:>10} {trend:>10}")
        shown += 1

    print("-" * 76)
    print(f"{shown} symbols shown.\n")


def main():
    ap = argparse.ArgumentParser(description="Technical analysis over stored history")
    ap.add_argument("--symbol")
    ap.add_argument("--resolution", default="1D")
    ap.add_argument("--bars", type=int, default=250)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--filter", choices=["oversold", "overbought",
                                         "trending", "above-sma50"])
    args = ap.parse_args()

    if not HIST_DB.exists():
        print("market_history.db not found. Run backfill.py first.")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)

    if args.scan:
        cmd_scan(conn, args.resolution, args.filter)
    elif args.symbol:
        d = load(conn, args.symbol, args.resolution, args.bars)
        if not d:
            print(f"No {args.resolution} data for {args.symbol}.")
            avail = [r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM history LIMIT 8")]
            print("Available: " + ", ".join(avail))
        else:
            show(args.symbol, args.resolution, d, compute(d))
    else:
        print("Pick --symbol TICKER or --scan.  Run with -h for options.")
    conn.close()


if __name__ == "__main__":
    main()
