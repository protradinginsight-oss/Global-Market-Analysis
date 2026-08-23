#!/usr/bin/env python3
"""
Market breadth - how many stocks are participating, not just where the index is.

An index can rise while most of its constituents fall, if a few heavyweights
carry it. Breadth measures the difference. Narrow rallies - index up, breadth
weak - have historically been less durable than broad ones, and the divergence
is often visible before it shows up in price.

Computed from the 213-symbol history already stored, so no new data
collection is needed.

Measures included:
  - Advance/decline ratio and the cumulative A/D line
  - Percentage of stocks above their 50-day and 200-day moving averages
  - New 52-week highs versus new lows
  - Up-volume versus down-volume
  - McClellan Oscillator (smoothed A/D, catches divergence earlier)

Usage:
    py -3.12 breadth.py                    # latest reading
    py -3.12 breadth.py --history 20       # last 20 sessions
    py -3.12 breadth.py --divergence       # where index and breadth disagree
"""

import sys
import sqlite3
import argparse
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
HIST_DB = BASE_DIR / "market_history.db"

try:
    from indicators import sma, ema
except ImportError:
    print("indicators.py not found - it must sit next to this script.")
    sys.exit(1)

INDEX_TICKERS = ("NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX",
                 "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX",
                 "NSE:NIFTYNXT50-INDEX")

# A stock needs this much history before "above its 200-day average" means
# anything. Recently listed names would otherwise be counted as missing
# rather than simply not yet measurable.
MIN_BARS_200 = 200
MIN_BARS_50 = 50


def load_all(conn):
    """symbol -> ordered list of (date, close, volume), stocks only."""
    rows = conn.execute(
        "SELECT symbol, ts, close, volume FROM history "
        "WHERE resolution='1D' AND close IS NOT NULL ORDER BY symbol, epoch"
    ).fetchall()
    data = defaultdict(list)
    for sym, ts, close, vol in rows:
        if sym in INDEX_TICKERS or sym.startswith("MCX:") or sym.startswith("BSE:"):
            continue
        data[sym].append((ts[:10], close, vol or 0))
    return data


def load_index(conn, ticker="NSE:NIFTY50-INDEX"):
    rows = conn.execute(
        "SELECT ts, close FROM history WHERE symbol=? AND resolution='1D' "
        "AND close IS NOT NULL ORDER BY epoch", (ticker,)).fetchall()
    return {r[0][:10]: r[1] for r in rows}


def compute_breadth(data, lookback=60):
    """Breadth series for the most recent `lookback` sessions."""
    all_dates = sorted({d for series in data.values() for d, _, _ in series})
    if len(all_dates) < 2:
        return []
    dates = all_dates[-lookback:] if lookback else all_dates

    # Index each symbol by date once, rather than scanning repeatedly.
    by_sym = {}
    for sym, series in data.items():
        by_sym[sym] = {d: (c, v) for d, c, v in series}

    # Precompute moving averages per symbol, aligned to its own dates.
    ma_state = {}
    for sym, series in data.items():
        closes = [c for _, c, _ in series]
        d_list = [d for d, _, _ in series]
        s50 = sma(closes, 50)
        s200 = sma(closes, 200)
        ma_state[sym] = {
            d: (closes[i], s50[i], s200[i],
                max(closes[max(0, i - 251):i + 1]),
                min(closes[max(0, i - 251):i + 1]),
                len(closes[:i + 1]))
            for i, d in enumerate(d_list)
        }

    out = []
    for i, d in enumerate(dates):
        prev_d = all_dates[all_dates.index(d) - 1] if all_dates.index(d) > 0 else None
        adv = dec = unch = 0
        up_vol = down_vol = 0
        above50 = below50 = 0
        above200 = below200 = 0
        new_high = new_low = 0

        for sym, dmap in by_sym.items():
            if d not in dmap:
                continue
            close, vol = dmap[d]
            if prev_d and prev_d in dmap:
                prev_close = dmap[prev_d][0]
                if close > prev_close:
                    adv += 1
                    up_vol += vol
                elif close < prev_close:
                    dec += 1
                    down_vol += vol
                else:
                    unch += 1

            st = ma_state[sym].get(d)
            if st:
                c, s50v, s200v, hi52, lo52, nbars = st
                if s50v is not None and nbars >= MIN_BARS_50:
                    above50 += 1 if c > s50v else 0
                    below50 += 1 if c <= s50v else 0
                if s200v is not None and nbars >= MIN_BARS_200:
                    above200 += 1 if c > s200v else 0
                    below200 += 1 if c <= s200v else 0
                # A new 52-week extreme means today IS the extreme.
                if nbars >= 200:
                    if c >= hi52:
                        new_high += 1
                    elif c <= lo52:
                        new_low += 1

        total_ma50 = above50 + below50
        total_ma200 = above200 + below200
        out.append({
            "date": d,
            "advances": adv, "declines": dec, "unchanged": unch,
            "ad_ratio": (adv / dec) if dec else None,
            "ad_net": adv - dec,
            "up_volume": up_vol, "down_volume": down_vol,
            "vol_ratio": (up_vol / down_vol) if down_vol else None,
            "pct_above_50": (above50 / total_ma50 * 100) if total_ma50 else None,
            "pct_above_200": (above200 / total_ma200 * 100) if total_ma200 else None,
            "new_highs": new_high, "new_lows": new_low,
            "participating": adv + dec,
        })

    # McClellan Oscillator: 19-day EMA minus 39-day EMA of net advances.
    # Smoother than the raw ratio, so divergences show up earlier.
    nets = [r["ad_net"] for r in out]
    e19, e39 = ema(nets, 19), ema(nets, 39)
    for i, r in enumerate(out):
        r["mcclellan"] = (e19[i] - e39[i]) if (e19[i] is not None
                                               and e39[i] is not None) else None
    return out


def describe(r, index_close=None, index_prev=None):
    print("=" * 76)
    print(f"  MARKET BREADTH - {r['date']}")
    print("=" * 76)

    if index_close and index_prev:
        chg = (index_close - index_prev) / index_prev * 100
        print(f"\nNifty 50      : {index_close:,.2f}  ({chg:+.2f}%)")

    print(f"\nADVANCE / DECLINE")
    print(f"  Advancing        {r['advances']:>6}")
    print(f"  Declining        {r['declines']:>6}")
    print(f"  Unchanged        {r['unchanged']:>6}")
    if r["ad_ratio"]:
        tone = ("strongly positive" if r["ad_ratio"] >= 2 else
                "positive" if r["ad_ratio"] > 1.2 else
                "strongly negative" if r["ad_ratio"] <= 0.5 else
                "negative" if r["ad_ratio"] < 0.8 else "mixed")
        print(f"  A/D ratio        {r['ad_ratio']:>6.2f}   {tone}")
    print(f"  Net advances     {r['ad_net']:>+6}")

    print(f"\nPARTICIPATION")
    if r["pct_above_50"] is not None:
        print(f"  Above 50-DMA     {r['pct_above_50']:>5.1f}%")
    if r["pct_above_200"] is not None:
        print(f"  Above 200-DMA    {r['pct_above_200']:>5.1f}%")
    print(f"  New 52w highs    {r['new_highs']:>6}")
    print(f"  New 52w lows     {r['new_lows']:>6}")

    print(f"\nVOLUME")
    print(f"  Up volume        {r['up_volume']:>16,}")
    print(f"  Down volume      {r['down_volume']:>16,}")
    if r["vol_ratio"]:
        print(f"  Up/down ratio    {r['vol_ratio']:>16.2f}")

    if r.get("mcclellan") is not None:
        m = r["mcclellan"]
        zone = ("overbought" if m > 70 else "oversold" if m < -70 else "neutral")
        print(f"\nMcClellan Osc    {m:>+16.1f}   {zone}")

    print(f"\nBased on {r['participating']} stocks that traded.")


def cmd_latest(conn):
    data = load_all(conn)
    if not data:
        print("No daily stock data. Run:  py -3.12 backfill.py --days 365 "
              "--resolution 1D")
        return
    series = compute_breadth(data, lookback=60)
    if not series:
        print("Not enough history to compute breadth.")
        return
    idx = load_index(conn)
    r = series[-1]
    prev_date = series[-2]["date"] if len(series) > 1 else None
    describe(r, idx.get(r["date"]), idx.get(prev_date) if prev_date else None)

    print("\nBreadth measures participation. An index can rise on a handful of")
    print("heavyweights while most stocks fall - that shows up here and not")
    print("in the index level. It is context for a directional view, not a")
    print("signal on its own.\n")


def cmd_history(conn, n):
    data = load_all(conn)
    series = compute_breadth(data, lookback=max(n + 40, 60))
    if not series:
        print("Not enough history.")
        return
    idx = load_index(conn)
    rows = series[-n:]
    print("=" * 84)
    print(f"  BREADTH HISTORY - last {len(rows)} sessions")
    print("=" * 84)
    print(f"\n{'Date':<12} {'Nifty':>10} {'Adv':>5} {'Dec':>5} {'A/D':>6} "
          f"{'>50DMA':>8} {'>200DMA':>9} {'H/L':>9} {'McOsc':>8}")
    print("-" * 84)
    for r in rows:
        nifty = idx.get(r["date"])
        print(f"{r['date']:<12} "
              f"{(f'{nifty:,.0f}' if nifty else '-'):>10} "
              f"{r['advances']:>5} {r['declines']:>5} "
              f"{(f'{r['ad_ratio']:.2f}' if r['ad_ratio'] else '-'):>6} "
              f"{(f'{r['pct_above_50']:.0f}%' if r['pct_above_50'] is not None else '-'):>8} "
              f"{(f'{r['pct_above_200']:.0f}%' if r['pct_above_200'] is not None else '-'):>9} "
              f"{f'{r['new_highs']}/{r['new_lows']}':>9} "
              f"{(f'{r['mcclellan']:+.0f}' if r.get('mcclellan') is not None else '-'):>8}")
    print("-" * 84)
    print()


def cmd_divergence(conn, n=30):
    """Sessions where the index and breadth disagreed.

    A rising index on negative breadth means the move is being carried by a
    few names. That is the pattern worth flagging - not because it predicts
    a reversal reliably, but because it changes what the index level means.
    """
    data = load_all(conn)
    series = compute_breadth(data, lookback=max(n + 40, 60))
    idx = load_index(conn)
    if not series:
        print("Not enough history.")
        return

    rows = series[-n:]
    found = []
    for i in range(1, len(rows)):
        d, pd_ = rows[i]["date"], rows[i - 1]["date"]
        c, pc = idx.get(d), idx.get(pd_)
        if not c or not pc:
            continue
        idx_up = c > pc
        breadth_up = rows[i]["ad_net"] > 0
        if idx_up != breadth_up:
            found.append((d, (c - pc) / pc * 100, rows[i]["advances"],
                          rows[i]["declines"], idx_up))

    print("=" * 76)
    print(f"  INDEX / BREADTH DIVERGENCE - last {len(rows)} sessions")
    print("=" * 76)
    if not found:
        print("\nNone - index direction and breadth agreed on every session.\n")
        return
    print(f"\n{len(found)} session(s) where they disagreed:\n")
    print(f"{'Date':<12} {'Nifty':>9} {'Adv':>5} {'Dec':>5}  Reading")
    print("-" * 76)
    for d, chg, adv, dec, idx_up in found:
        reading = ("index up, breadth negative - narrow advance"
                   if idx_up else
                   "index down, breadth positive - selling concentrated")
        print(f"{d:<12} {chg:>+8.2f}% {adv:>5} {dec:>5}  {reading}")
    print("-" * 76)
    print("\nDivergence changes what the index level means; it does not")
    print("reliably predict a reversal. Plenty of narrow rallies continue.\n")


def main():
    ap = argparse.ArgumentParser(description="Market breadth")
    ap.add_argument("--history", type=int, metavar="N",
                    help="show the last N sessions")
    ap.add_argument("--divergence", action="store_true")
    ap.add_argument("--sessions", type=int, default=30,
                    help="window for divergence scan (default 30)")
    args = ap.parse_args()

    if not HIST_DB.exists():
        print("market_history.db not found. Run backfill.py first.")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)

    if args.history:
        cmd_history(conn, args.history)
    elif args.divergence:
        cmd_divergence(conn, args.sessions)
    else:
        cmd_latest(conn)
    conn.close()


if __name__ == "__main__":
    main()
