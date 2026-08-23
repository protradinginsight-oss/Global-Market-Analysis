#!/usr/bin/env python3
"""
Open interest buildup classification.

Classifies each option leg by the combination of price change and OI change,
then aggregates it into a read on where positioning is building.

The four classic categories come from futures analysis:

    price up   + OI up    = Long Buildup      (new longs entering)
    price down + OI up    = Short Buildup     (new shorts entering)
    price up   + OI down  = Short Covering    (shorts closing)
    price down + OI down  = Long Unwinding    (longs closing)

Applying these to options directly is a common mistake, because the
directional meaning flips between calls and puts. Long buildup on a CALL is
bullish for the underlying; long buildup on a PUT is bearish. This module
tracks both the mechanical category and what it implies for the underlying,
and reports them separately.

A second caveat worth keeping in view: OI counts contracts, not intent. A
rise in call OI could be speculative buying or covered-call writing against
stock, and the chain cannot distinguish them. Treat this as evidence about
where activity is concentrated, not a reading of anyone's mind.

Usage:
    py -3.12 buildup.py --symbol NSE:NIFTY50-INDEX
    py -3.12 buildup.py --symbol NSE:NIFTY50-INDEX --detail
    py -3.12 buildup.py --all
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
CHAIN_DB = BASE_DIR / "option_chains.db"

# Moves smaller than this are treated as flat. Without a deadband, a 0.01%
# price tick and a handful of contracts get classified as a "buildup", which
# fills the output with noise.
PRICE_FLOOR_PCT = 0.5
OI_FLOOR_PCT = 1.0

# A tilt computed from a few thousand contracts is arithmetic, not evidence.
# NIFTYNXT50 produced a confident-looking "bull 90%" from 4,250 contracts
# against 225 - true as a ratio, meaningless as a signal. These floors
# suppress a directional call when the activity behind it is too thin.
MIN_OI_FOR_TILT = 500_000        # total OI change across the chain
MIN_LEGS_FOR_TILT = 6            # legs contributing to the classification
MIN_VOLUME_FOR_TILT = 50_000     # total traded volume across the chain

# A leg with OI but no trades today is stale quoting rather than activity.
MIN_LEG_VOLUME = 100


def assess_quality(legs):
    """Is there enough activity here for a directional read to mean anything?

    Returns (usable, reasons) - reasons lists what fell short.
    """
    classified = [l for l in legs
                  if l.get("category") not in (None, "Flat",
                                               "Price move, OI flat",
                                               "OI change, price flat")]
    total_oi_chg = sum(abs(l.get("oi_chg") or 0) for l in classified)
    total_volume = sum(l.get("volume") or 0 for l in legs)
    n_legs = len(classified)

    reasons = []
    if total_oi_chg < MIN_OI_FOR_TILT:
        reasons.append(f"OI change {total_oi_chg:,} below {MIN_OI_FOR_TILT:,}")
    if n_legs < MIN_LEGS_FOR_TILT:
        reasons.append(f"only {n_legs} legs classified, need {MIN_LEGS_FOR_TILT}")
    if total_volume < MIN_VOLUME_FOR_TILT:
        reasons.append(f"volume {total_volume:,} below {MIN_VOLUME_FOR_TILT:,}")

    return (not reasons), reasons, {
        "oi_change": total_oi_chg, "volume": total_volume, "legs": n_legs,
    }


def classify(price_chg_pct, oi_chg_pct):
    """The mechanical four-way classification, plus a flat case."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    price_flat = abs(price_chg_pct) < PRICE_FLOOR_PCT
    oi_flat = abs(oi_chg_pct) < OI_FLOOR_PCT
    if price_flat and oi_flat:
        return "Flat"
    if oi_flat:
        return "Price move, OI flat"
    if price_flat:
        return "OI change, price flat"
    if price_chg_pct > 0 and oi_chg_pct > 0:
        return "Long Buildup"
    if price_chg_pct < 0 and oi_chg_pct > 0:
        return "Short Buildup"
    if price_chg_pct > 0 and oi_chg_pct < 0:
        return "Short Covering"
    return "Long Unwinding"


def implication(category, option_type):
    """What a category means for the UNDERLYING, which depends on the leg.

    Long buildup in calls is bullish; the identical pattern in puts is
    bearish. Reporting the raw category alone would invite exactly that
    confusion.
    """
    if category in (None, "Flat", "Price move, OI flat", "OI change, price flat"):
        return "neutral"
    bullish_on_call = {"Long Buildup": "bullish", "Short Covering": "bullish",
                       "Short Buildup": "bearish", "Long Unwinding": "bearish"}
    lean = bullish_on_call.get(category, "neutral")
    if option_type == "PE":
        # Same activity on the put side implies the opposite for spot.
        lean = {"bullish": "bearish", "bearish": "bullish"}.get(lean, lean)
    return lean


def latest_snapshot(conn, underlying):
    row = conn.execute(
        "SELECT MAX(snapshot_ts) FROM chain_snapshot WHERE underlying=?",
        (underlying,)).fetchone()
    return row[0] if row and row[0] else None


def load(conn, underlying, ts):
    rows = conn.execute(
        """SELECT strike, option_type, ltp, change_pct, oi, oi_change,
                  oi_change_pct, prev_oi, spot, volume
           FROM chain_snapshot WHERE underlying=? AND snapshot_ts=?
           ORDER BY strike""", (underlying, ts)).fetchall()
    out = []
    spot = None
    for st, ot, ltp, chg, oi, oich, oichp, poi, sp, vol in rows:
        if sp:
            spot = sp
        # Fall back to computing the percentage if the feed omitted it.
        if oichp is None and poi:
            oichp = (oich / poi * 100) if poi else None
        out.append({"strike": st, "type": ot, "ltp": ltp, "price_chg": chg,
                    "oi": oi, "oi_chg": oich, "oi_chg_pct": oichp,
                    "volume": vol})
    return out, spot


def analyse(conn, underlying, detail=False):
    ts = latest_snapshot(conn, underlying)
    if not ts:
        print(f"No chain data for {underlying}.")
        return None
    legs, spot = load(conn, underlying, ts)
    if not legs:
        print(f"Empty chain for {underlying}.")
        return None

    for leg in legs:
        # A leg with no trades today has a stale quote, not real activity.
        # Classifying it manufactures a signal out of an untraded price.
        if (leg.get("volume") or 0) < MIN_LEG_VOLUME:
            leg["category"] = "Untraded"
            leg["lean"] = "neutral"
            continue
        leg["category"] = classify(leg["price_chg"], leg["oi_chg_pct"])
        leg["lean"] = implication(leg["category"], leg["type"])

    print("=" * 82)
    print(f"  OI BUILDUP - {underlying}")
    print(f"  snapshot {ts}" + (f"   spot {spot:,.2f}" if spot else ""))
    print("=" * 82)

    # Weight by OI change: a category showing up on a strike with 5 million
    # contracts added matters more than the same category on one with 500.
    by_cat = defaultdict(lambda: {"count": 0, "oi_added": 0})
    for leg in legs:
        c = leg["category"]
        if c in (None, "Flat", "Untraded"):
            continue
        by_cat[c]["count"] += 1
        by_cat[c]["oi_added"] += abs(leg["oi_chg"] or 0)

    if not by_cat:
        print("\nNo meaningful buildup - everything within the noise floor.")
        print(f"(price move under {PRICE_FLOOR_PCT}% or OI change under "
              f"{OI_FLOOR_PCT}% counts as flat)\n")
        return None

    print(f"\n{'Category':<26} {'Legs':>6} {'OI changed':>14}")
    print("-" * 82)
    for c, d in sorted(by_cat.items(), key=lambda x: -x[1]["oi_added"]):
        print(f"{c:<26} {d['count']:>6} {d['oi_added']:>14,}")

    bull = sum(abs(l["oi_chg"] or 0) for l in legs if l["lean"] == "bullish")
    bear = sum(abs(l["oi_chg"] or 0) for l in legs if l["lean"] == "bearish")
    total = bull + bear

    usable, reasons, q = assess_quality(legs)
    untraded = sum(1 for l in legs if l.get("category") == "Untraded")

    print(f"\nActivity behind this read:")
    print(f"  legs classified  : {q['legs']}"
          + (f"   ({untraded} skipped as untraded)" if untraded else ""))
    print(f"  total OI change  : {q['oi_change']:,}")
    print(f"  total volume     : {q['volume']:,}")

    print("\nWhat this implies for the underlying, weighted by OI change:")
    if total == 0:
        print("  No directional implication.")
    elif not usable:
        bp, sp_ = bull / total * 100, bear / total * 100
        print(f"  bullish activity : {bull:>14,}  ({bp:.0f}%)")
        print(f"  bearish activity : {bear:>14,}  ({sp_:.0f}%)")
        print("\n  TOO THIN TO CALL - not reporting a tilt:")
        for r in reasons:
            print(f"    {r}")
        print("  The percentages above are arithmetic on a small base. A")
        print("  '90% bullish' built on four thousand contracts is not a")
        print("  signal, and presenting it as one would be misleading.")
    else:
        bp, sp_ = bull / total * 100, bear / total * 100
        print(f"  bullish activity : {bull:>14,}  ({bp:.0f}%)")
        print(f"  bearish activity : {bear:>14,}  ({sp_:.0f}%)")
        gap = abs(bp - sp_)
        if gap < 10:
            print("\n  Close to balanced - no clear positioning bias.")
        else:
            side = "bullish" if bp > sp_ else "bearish"
            strength = "modest" if gap < 30 else "pronounced"
            print(f"\n  {strength.capitalize()} {side} tilt.")

    # Where the activity concentrated - often more informative than the mix
    movers = sorted([l for l in legs if l["oi_chg"]],
                    key=lambda l: -abs(l["oi_chg"]))[:8]
    print(f"\nLargest OI changes:")
    print(f"  {'Strike':>9} {'Leg':<4} {'LTP':>9} {'Price%':>8} "
          f"{'OI chg':>12} {'OI%':>8}  {'Category':<20} Implies")
    print("-" * 82)
    for l in movers:
        print(f"  {l['strike']:>9,.0f} {l['type']:<4} {l['ltp'] or 0:>9,.2f} "
              f"{l['price_chg'] or 0:>+7.1f}% {l['oi_chg'] or 0:>12,} "
              f"{l['oi_chg_pct'] or 0:>+7.1f}%  "
              f"{(l['category'] or '-'):<20} {l['lean']}")

    if detail:
        print(f"\nFull chain:")
        print(f"  {'Strike':>9} {'Leg':<4} {'Price%':>8} {'OI%':>8}  "
              f"{'Category':<22} Implies")
        print("-" * 82)
        for l in legs:
            if l["category"] in (None, "Flat"):
                continue
            print(f"  {l['strike']:>9,.0f} {l['type']:<4} "
                  f"{l['price_chg'] or 0:>+7.1f}% {l['oi_chg_pct'] or 0:>+7.1f}%  "
                  f"{l['category']:<22} {l['lean']}")

    print("\nOI counts contracts, not intent. Rising call OI could be")
    print("speculative buying or covered-call writing against stock - the")
    print("chain cannot tell them apart. This shows where activity is")
    print("concentrated, which is useful, but it is not a view of anyone's")
    print("actual position.\n")

    return {"bullish": bull, "bearish": bear}


def cmd_all(conn):
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot ORDER BY underlying")]
    if not unds:
        print("No chain data. Run:  py -3.12 option_chain.py --once")
        return
    print("=" * 78)
    print("  OI BUILDUP SUMMARY")
    print("=" * 78)
    print(f"\n{'Underlying':<24} {'Bullish OI':>13} {'Bearish OI':>13} "
          f"{'Volume':>12} {'Tilt':>12}")
    print("-" * 78)
    thin = 0
    for u in unds:
        ts = latest_snapshot(conn, u)
        legs, spot = load(conn, u, ts)
        bull = bear = 0
        for leg in legs:
            if (leg.get("volume") or 0) < MIN_LEG_VOLUME:
                leg["category"] = "Untraded"
                continue
            cat = classify(leg["price_chg"], leg["oi_chg_pct"])
            leg["category"] = cat
            lean = implication(cat, leg["type"])
            if lean == "bullish":
                bull += abs(leg["oi_chg"] or 0)
            elif lean == "bearish":
                bear += abs(leg["oi_chg"] or 0)
        total = bull + bear
        usable, reasons, q = assess_quality(legs)
        if total == 0:
            tilt = "-"
        elif not usable:
            tilt = "too thin"
            thin += 1
        else:
            gap = (bull - bear) / total * 100
            tilt = ("balanced" if abs(gap) < 10 else
                    f"{'bull' if gap > 0 else 'bear'} {abs(gap):.0f}%")
        print(f"{u:<24} {bull:>13,} {bear:>13,} {q['volume']:>12,} {tilt:>12}")
    print("-" * 78)
    if thin:
        print(f"\n{thin} underlying(s) marked 'too thin': there is not enough")
        print("traded volume or OI movement behind them for a directional")
        print("read to mean anything, however lopsided the raw ratio looks.")
    print()


def main():
    ap = argparse.ArgumentParser(description="OI buildup classification")
    ap.add_argument("--symbol")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    if not CHAIN_DB.exists():
        print("option_chains.db not found. Run:  py -3.12 option_chain.py --once")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{CHAIN_DB}?mode=ro", uri=True)

    if args.all:
        cmd_all(conn)
    elif args.symbol:
        analyse(conn, args.symbol, args.detail)
    else:
        print("Pick --symbol TICKER or --all")
        unds = [r[0] for r in conn.execute(
            "SELECT DISTINCT underlying FROM chain_snapshot LIMIT 8")]
        if unds:
            print("\nAvailable: " + ", ".join(unds))
    conn.close()


if __name__ == "__main__":
    main()
