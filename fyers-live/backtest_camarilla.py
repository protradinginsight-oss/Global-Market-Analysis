#!/usr/bin/env python3
"""
Camarilla R3/S3 breakout backtest.

Answers the question the screener can't: across real history, how often did
an open near R3 actually reach R4 before hitting its stop - and at what
proximity threshold is that hit rate best?

Uses 5-minute candles rather than daily, deliberately. With daily OHLC you
cannot tell whether the high or the low came first, so a day that touched
both the target and the stop is indistinguishable from a clean win. Walking
5-minute bars in order resolves the sequence properly. The cost is sample
size: 5-minute history covers ~60 days, not a year.

Usage:
    py -3.12 backtest_camarilla.py
    py -3.12 backtest_camarilla.py --thresholds 5,10,15,20,25,50
    py -3.12 backtest_camarilla.py --stop pp --detail
    py -3.12 backtest_camarilla.py --symbol NSE:SBIN-EQ --detail
"""

import sys
import sqlite3
import argparse
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HIST_DB = BASE_DIR / "market_history.db"

MULTIPLIERS = {4: 1.1 / 2, 3: 1.1 / 4, 2: 1.1 / 6, 1: 1.1 / 12}


def camarilla_levels(high, low, close):
    rng = high - low
    lv = {"pp": (high + low + close) / 3, "range": rng}
    for n, mult in MULTIPLIERS.items():
        lv[f"r{n}"] = close + rng * mult
        lv[f"s{n}"] = close - rng * mult
    return lv


def load_daily(conn):
    """symbol -> ordered list of (date, o, h, l, c)."""
    out = defaultdict(list)
    for sym, ts, o, h, l, c in conn.execute(
            "SELECT symbol, ts, open, high, low, close FROM history "
            "WHERE resolution='1D' ORDER BY symbol, epoch"):
        out[sym].append((ts[:10], o, h, l, c))
    return out


def load_intraday(conn, resolution="5"):
    """symbol -> date -> ordered list of (epoch, o, h, l, c)."""
    out = defaultdict(lambda: defaultdict(list))
    for sym, ts, ep, o, h, l, c in conn.execute(
            "SELECT symbol, ts, epoch, open, high, low, close FROM history "
            "WHERE resolution=? ORDER BY symbol, epoch", (resolution,)):
        out[sym][ts[:10]].append((ep, o, h, l, c))
    return out


def simulate(bars, entry, target, stop, side):
    """Walk bars in order and see which level is touched first.

    Returns 'target', 'stop', 'neither', or 'no_entry'.

    Entry only counts once price actually trades through the entry level -
    being near it at the open is a setup, not a fill. A bar that spans both
    target and stop is counted as a stop, because we cannot see the order
    within a single bar and assuming the good outcome would flatter the
    results.
    """
    entered = False
    for _, o, h, l, c in bars:
        if not entered:
            if side == "LONG" and h >= entry:
                entered = True
            elif side == "SHORT" and l <= entry:
                entered = True
            else:
                continue
            # Check the entry bar itself for target/stop too
        if side == "LONG":
            hit_stop = l <= stop
            hit_target = h >= target
        else:
            hit_stop = h >= stop
            hit_target = l <= target
        if hit_stop:
            return "stop"        # pessimistic when both in one bar
        if hit_target:
            return "target"
    return "neither" if entered else "no_entry"


def main():
    ap = argparse.ArgumentParser(description="Backtest Camarilla R3/S3 breakouts")
    ap.add_argument("--thresholds", default="5,10,15,20,25,35,50",
                    help="proximity thresholds to test, as %% of prior range")
    ap.add_argument("--stop", default="pp", choices=["pp", "r2s2", "entry"],
                    help="stop level: pp (pivot), r2s2 (R2/S2), entry (breakeven)")
    ap.add_argument("--symbol", help="restrict to one symbol")
    ap.add_argument("--detail", action="store_true", help="list individual trades")
    ap.add_argument("--resolution", default="5", help="intraday resolution to use")
    args = ap.parse_args()

    if not HIST_DB.exists():
        print("market_history.db not found. Run backfill.py first.")
        sys.exit(1)

    thresholds = sorted(float(t) for t in args.thresholds.split(","))

    conn = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)
    print("Loading history...")
    daily = load_daily(conn)
    intraday = load_intraday(conn, args.resolution)
    conn.close()

    if not intraday:
        print(f"No {args.resolution}-minute data found. Run:")
        print(f"  py -3.12 backfill.py --days 60 --resolution {args.resolution}")
        sys.exit(1)

    symbols = [args.symbol] if args.symbol else sorted(intraday.keys())
    if args.symbol and args.symbol not in intraday:
        print(f"{args.symbol} has no {args.resolution}-minute data.")
        sys.exit(1)

    # Build every candidate setup once, tagged with its proximity, then
    # evaluate each threshold as a filter over the same set.
    setups = []
    skipped_no_prev = 0
    rejected_bad_stop = 0
    rejected_tiny_risk = 0

    for sym in symbols:
        days = daily.get(sym, [])
        by_date = {d[0]: d for d in days}
        dates = sorted(by_date)
        intra = intraday.get(sym, {})

        for i in range(1, len(dates)):
            today, prev = dates[i], dates[i - 1]
            if today not in intra:
                continue
            _, po, ph, pl, pc = by_date[prev]
            if ph is None or pl is None or ph <= pl:
                skipped_no_prev += 1
                continue

            lv = camarilla_levels(ph, pl, pc)
            bars = intra[today]
            if not bars:
                continue
            day_open = bars[0][1]
            if not day_open or lv["range"] <= 0:
                continue

            for side, entry_key, target_key in (
                    ("LONG", "r3", "r4"), ("SHORT", "s3", "s4")):
                entry = lv[entry_key]
                dist = abs(day_open - entry) / lv["range"] * 100

                if args.stop == "pp":
                    stop = lv["pp"]
                elif args.stop == "r2s2":
                    stop = lv["r2"] if side == "SHORT" else lv["s2"]
                else:
                    stop = entry

                # A stop must sit on the losing side of the entry. The pivot
                # point in particular can land ABOVE R3 when the previous
                # close is near the low, which would make a long's "stop"
                # sit above its entry - not a stop at all. Skip those rather
                # than let them generate meaningless trades.
                if args.stop != "entry":
                    if side == "LONG" and stop >= entry:
                        rejected_bad_stop += 1
                        continue
                    if side == "SHORT" and stop <= entry:
                        rejected_bad_stop += 1
                        continue
                    # Near-zero risk produces absurd reward:risk ratios that
                    # dominate any average. Require the stop to be a
                    # meaningful distance away.
                    if abs(entry - stop) < lv["range"] * 0.02:
                        rejected_tiny_risk += 1
                        continue

                setups.append({
                    "symbol": sym, "date": today, "side": side,
                    "dist": dist, "entry": entry,
                    "target": lv[target_key], "stop": stop, "bars": bars,
                })

    print(f"Symbols: {len(symbols)} | candidate setups: {len(setups):,}")
    if rejected_bad_stop or rejected_tiny_risk:
        print(f"Rejected: {rejected_bad_stop:,} with the stop on the wrong side "
              f"of entry, {rejected_tiny_risk:,} with near-zero risk")
    print()
    if not setups:
        print("No setups found.")
        return

    print("=" * 78)
    print(f"  CAMARILLA R3/S3 BACKTEST  ({args.resolution}-min bars, stop = {args.stop})")
    print("=" * 78)
    print(f"\n{'Thresh':>7} {'Setups':>8} {'Entered':>8} {'Target':>8} {'Stop':>8} "
          f"{'Hit rate':>9} {'R:R':>7} {'Expect.':>10}")
    print("-" * 78)

    results_by_threshold = {}
    for th in thresholds:
        outcomes = defaultdict(int)
        trades = []
        per_trade_r = []      # realised R multiple for each decided trade
        rr_list = []
        for s in setups:
            if s["dist"] > th:
                continue
            r = simulate(s["bars"], s["entry"], s["target"], s["stop"], s["side"])
            outcomes[r] += 1

            reward = abs(s["target"] - s["entry"])
            risk = abs(s["entry"] - s["stop"])
            if risk > 0:
                rr = reward / risk
                rr_list.append(rr)
                # Expectancy must be built from each trade's own R multiple.
                # Averaging R:R first and multiplying by the win rate lets a
                # few near-zero-risk setups distort the whole result.
                if r == "target":
                    per_trade_r.append(rr)
                elif r == "stop":
                    per_trade_r.append(-1.0)

            if args.detail and r != "no_entry":
                trades.append((s, r))

        n = sum(outcomes.values())
        entered = outcomes["target"] + outcomes["stop"] + outcomes["neither"]
        decided = outcomes["target"] + outcomes["stop"]
        rate = outcomes["target"] / decided * 100 if decided else 0

        # Median R:R is reported rather than mean: the mean is easily
        # dragged upward by a few setups where the stop sits very close to
        # the entry.
        avg_rr = (sorted(rr_list)[len(rr_list) // 2]) if rr_list else 0
        expectancy = sum(per_trade_r) / len(per_trade_r) if per_trade_r else 0

        results_by_threshold[th] = (n, entered, outcomes, rate, trades,
                                    avg_rr, expectancy)

        print(f"{th:>6.0f}% {n:>8,} {entered:>8,} {outcomes['target']:>8,} "
              f"{outcomes['stop']:>8,} {rate:>8.1f}% {avg_rr:>7.2f} "
              f"{expectancy:>+9.3f}")

    print("-" * 78)
    print("\nHit rate  = target reached / (target + stop).")
    print("R:R       = MEDIAN reward:risk across setups (median, not mean,")
    print("            because a few near-zero-risk setups distort the mean).")
    print("Expect.   = mean realised R per decided trade: each win counts its")
    print("            own reward:risk, each loss counts -1.")
    print("\nExpectancy is the number that matters. A high hit rate bought by")
    print("widening the stop is not an edge - it just trades many small wins")
    print("for fewer, larger losses. Positive expectancy is the bar.")

    # Rank by expectancy, not hit rate, and require a usable sample.
    best = max(results_by_threshold.items(),
               key=lambda kv: kv[1][6] if kv[1][2]["target"] + kv[1][2]["stop"] >= 30 else -999)
    th, (n, entered, outcomes, rate, trades, avg_rr, expectancy) = best
    decided = outcomes["target"] + outcomes["stop"]

    print(f"\nBest expectancy: {expectancy:+.3f}R at {th:.0f}% threshold "
          f"({decided:,} decided trades, hit rate {rate:.1f}%, R:R {avg_rr:.2f})")

    if expectancy <= 0:
        print("\nThis configuration LOSES money before costs are even counted.")
        print("Not a bug - an honest result. The entry filter alone has no edge.")
    else:
        print(f"\nPositive before costs, but only just. Indian F&O costs - STT,")
        print("brokerage, GST, stamp duty, and slippage - typically run a few")
        print("tenths of a percent per round trip. Against an average range of")
        print("roughly 1-2%, that can consume an edge of this size entirely.")
        print("Treat this as 'not yet disproven', not as 'profitable'.")

    if decided < 100:
        print("\nCAUTION: fewer than 100 decided trades - too small to draw")
        print("conclusions from differences between thresholds.")

    if args.detail and trades:
        print(f"\n{'=' * 78}")
        print(f"  TRADES AT {th:.0f}% THRESHOLD (first 40)")
        print(f"{'=' * 78}")
        print(f"  {'Date':<12} {'Symbol':<22} {'Side':<6} {'Entry':>10} "
              f"{'Target':>10} {'Result':>9}")
        print("  " + "-" * 74)
        for s, r in trades[:40]:
            print(f"  {s['date']:<12} {s['symbol']:<22} {s['side']:<6} "
                  f"{s['entry']:>10.2f} {s['target']:>10.2f} {r:>9}")
        if len(trades) > 40:
            print(f"  ... and {len(trades) - 40} more")

    print()


if __name__ == "__main__":
    main()
