#!/usr/bin/env python3
"""
Volatility term structure - is the front week richer than the monthly?

For a premium seller this is the concrete weekly question: sell the nearest
expiry and collect faster theta, or go further out for more absolute premium?
The term structure answers it with the market's own pricing rather than habit.

Two shapes, and what they usually mean:

  Contango (upward sloping, front IV below back IV) is the normal state.
  Longer expiries carry more uncertainty so they price higher. Selling the
  front week collects less premium but decays faster.

  Backwardation (downward sloping, front IV above back IV) means the market
  is pricing near-term risk - an event, a result, a shock. Front-week premium
  looks generous precisely because something is expected to happen. That is
  the shape where selling the front week has historically hurt most.

Requires the collector running with multiple expiries. Read-only.

Usage:
    py -3.12 term_structure.py
    py -3.12 term_structure.py --symbol NSE:NIFTYBANK-INDEX
    py -3.12 term_structure.py --all
"""

import sys
import sqlite3
import argparse
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHAIN_DB = BASE_DIR / "option_chains.db"


# If the nearest available strike is further than this from spot, its IV is
# a smile reading rather than an ATM one.
MAX_ATM_DISTANCE_PCT = 1.0

# The larger problem in practice is illiquidity, not distance. A far-dated
# index option can sit exactly at the money and still carry a nonsense IV,
# because with almost no trades the last price is stale and the spread is
# wide - and Fyers derives IV from that price. BANKNIFTY's October contract
# showed 27.45% IV against 10.07% for the front week from an ATM strike
# 0.07% from spot; the strike was fine, the liquidity was not.
MIN_ATM_VOLUME = 500       # contracts traded today at the ATM strike
MIN_ATM_OI = 10_000        # open interest at the ATM strike


def latest_snapshot(conn, underlying):
    r = conn.execute("SELECT MAX(snapshot_ts) FROM chain_snapshot "
                     "WHERE underlying=?", (underlying,)).fetchone()
    return r[0] if r and r[0] else None


def atm_iv_by_expiry(conn, underlying, ts):
    """ATM implied vol for each expiry present in this snapshot."""
    rows = conn.execute(
        """SELECT expiry_ts, expiry_date, strike, option_type, iv, spot,
                  volume, oi
           FROM chain_snapshot
           WHERE underlying=? AND snapshot_ts=? AND iv IS NOT NULL AND iv>0
           ORDER BY expiry_ts, strike""", (underlying, ts)).fetchall()
    if not rows:
        return []

    by_exp = {}
    spot = None
    for exp_ts, exp_date, strike, otype, iv, sp, vol, oi in rows:
        if sp:
            spot = sp
        by_exp.setdefault((exp_ts, exp_date), []).append(
            (strike, otype, iv, vol or 0, oi or 0))

    out = []
    for (exp_ts, exp_date), legs in sorted(
            by_exp.items(), key=lambda x: (x[0][0] or 0)):
        if spot is None:
            continue
        strikes = sorted({s for s, _, _, _, _ in legs})
        if not strikes:
            continue
        atm = min(strikes, key=lambda k: abs(k - spot))
        atm_legs = [l for l in legs if l[0] == atm]
        ivs = [l[2] for l in atm_legs]
        if not ivs:
            continue
        atm_volume = sum(l[3] for l in atm_legs)
        atm_oi = sum(l[4] for l in atm_legs)

        # How far the chosen "ATM" strike actually sits from spot. Far
        # expiries often have sparse strikes, and if the nearest available
        # one is well away from spot then its IV reflects the volatility
        # smile rather than at-the-money vol - which makes it useless for
        # comparing across expiries, and badly misleading if reported as
        # though it were comparable.
        atm_distance_pct = abs(atm - spot) / spot * 100 if spot else None

        days = None
        if exp_ts:
            try:
                days = (datetime.fromtimestamp(int(exp_ts)).date()
                        - date.today()).days
            except (ValueError, OSError):
                days = None
        out.append({
            "expiry_ts": exp_ts, "expiry_date": exp_date,
            "days_to_expiry": days, "atm_strike": atm,
            "atm_iv": sum(ivs) / len(ivs), "spot": spot,
            "legs": len(legs), "atm_distance_pct": atm_distance_pct,
            "strike_count": len(strikes),
            "atm_volume": atm_volume, "atm_oi": atm_oi,
            "liquid": (atm_volume >= MIN_ATM_VOLUME and atm_oi >= MIN_ATM_OI),
        })
    return out


def describe_shape(points):
    """Classify the curve and say what it implies.

    Only expiries with a genuinely at-the-money strike are used. Comparing
    an ATM IV against a strike 5% away from spot measures the smile, not
    the term structure, and would produce confident nonsense.
    """
    usable = [p for p in points if p["atm_iv"]
              and (p.get("atm_distance_pct") is None
                   or p["atm_distance_pct"] <= MAX_ATM_DISTANCE_PCT)
              and p.get("liquid", True)]
    if len(usable) < 2:
        rejected = [p for p in points if p not in usable and p["atm_iv"]]
        if rejected:
            illiquid = [p for p in rejected if not p.get("liquid", True)]
            if illiquid:
                w = min(illiquid, key=lambda p: p.get("atm_volume", 0))
                return None, (
                    f"Not enough comparable expiries. {len(illiquid)} were "
                    f"excluded for illiquidity at the money (worst: "
                    f"{w.get('atm_volume', 0):,} contracts traded, "
                    f"{w.get('atm_oi', 0):,} OI). With almost no trades the "
                    f"last price is stale, and the IV derived from it is not "
                    f"a real volatility reading.")
            worst = max(rejected, key=lambda p: p.get("atm_distance_pct") or 0)
            return None, (
                f"Not enough comparable expiries. {len(rejected)} were "
                f"excluded because their nearest strike sits too far from "
                f"spot (worst: {worst.get('atm_distance_pct', 0):.1f}% away).")
        return None, ("Only one expiry available - term structure needs at "
                      "least two. Run the collector with multiple expiries.")

    front, back = usable[0], usable[-1]
    diff = back["atm_iv"] - front["atm_iv"]
    rel = diff / front["atm_iv"] * 100 if front["atm_iv"] else 0

    if abs(rel) < 3:
        shape = "Flat"
        meaning = ("Near-term and longer-dated vol priced alike. No strong "
                   "signal either way about where to sell.")
    elif diff > 0:
        shape = "Contango"
        meaning = ("Normal shape - longer expiries priced higher for the "
                   "extra uncertainty they carry. Front-week selling collects "
                   "less premium but decays faster; the trade-off is genuine "
                   "rather than one side being obviously better.")
    else:
        shape = "Backwardation"
        meaning = ("Inverted - the market is pricing near-term risk above "
                   "longer-term. Front-week premium looks generous because "
                   "something is expected to happen: an event, a result, a "
                   "shock. This is the shape where selling the front week "
                   "has historically been most punishing.")

    return {"shape": shape, "front_iv": front["atm_iv"],
            "back_iv": back["atm_iv"], "diff": diff, "rel_pct": rel,
            "meaning": meaning}, None


def analyse(conn, underlying):
    ts = latest_snapshot(conn, underlying)
    if not ts:
        print(f"No chain data for {underlying}.")
        return
    points = atm_iv_by_expiry(conn, underlying, ts)
    if not points:
        print(f"No IV data for {underlying} - Fyers omits IV for deep ITM "
              "and OTM strikes, so a very thin chain can have none at all.")
        return

    print("=" * 76)
    print(f"  VOLATILITY TERM STRUCTURE - {underlying}")
    print(f"  snapshot {ts}")
    print("=" * 76)
    spot = points[0]["spot"]
    print(f"\nSpot: {spot:,.2f}\n")

    print(f"{'Expiry':<12} {'Days':>5} {'ATM':>10} {'off':>7} "
          f"{'ATM vol':>10} {'ATM OI':>11} {'IV':>8}")
    print("-" * 76)
    excluded = []
    for p in points:
        d = p.get("atm_distance_pct")
        far = d is not None and d > MAX_ATM_DISTANCE_PCT
        illiq = not p.get("liquid", True)
        if far or illiq:
            excluded.append((p, "illiquid" if illiq else "off-ATM"))
        flag = ""
        if illiq:
            flag = "  <- illiquid, excluded"
        elif far:
            flag = "  <- off-ATM, excluded"
        print(f"{p['expiry_date'] or '-':<12} "
              f"{(p['days_to_expiry'] if p['days_to_expiry'] is not None else '-'):>5} "
              f"{p['atm_strike']:>10,.0f} {(f'{d:.2f}%' if d is not None else '-'):>7} "
              f"{p.get('atm_volume', 0):>10,} {p.get('atm_oi', 0):>11,} "
              f"{p['atm_iv']:>7.2f}%{flag}")
    print("-" * 76)
    if excluded:
        print(f"\n{len(excluded)} expiry/expiries excluded from the comparison.")
        print("An option can sit exactly at the money and still carry a")
        print("meaningless IV: with almost no trades the last price is stale,")
        print("and IV is derived from that price. Volume and OI are the test,")
        print("not distance from spot.")

    shape, err = describe_shape(points)
    if err:
        print(f"\n{err}\n")
        return

    print(f"\n  {shape['shape'].upper()}")
    print(f"\n  front {shape['front_iv']:.2f}%  ->  back {shape['back_iv']:.2f}%"
          f"   ({shape['diff']:+.2f} points, {shape['rel_pct']:+.1f}%)")
    print(f"\n  {shape['meaning']}")

    # The practical comparison: premium per day of exposure.
    if len(points) >= 2:
        print(f"\nIV per remaining day (rough guide to decay pace)")
        for p in points:
            if p["days_to_expiry"] and p["days_to_expiry"] > 0:
                print(f"  {p['expiry_date'] or '-':<14} "
                      f"{p['atm_iv']/p['days_to_expiry']:>8.3f}% per day")
        print("\n  Higher is not automatically better - it reflects both "
              "faster\n  decay and less time for a position to recover from "
              "a bad move.")

    print("\nATM IV only. Fyers returns one IV per strike rather than one per")
    print("leg, so this cannot separate call and put vol.\n")


def cmd_all(conn):
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot ORDER BY underlying")]
    if not unds:
        print("No chain data. Run:  py -3.12 option_chain.py --once")
        return
    print("=" * 78)
    print("  TERM STRUCTURE SUMMARY")
    print("=" * 78)
    print(f"\n{'Underlying':<24} {'Front IV':>10} {'Back IV':>10} "
          f"{'Diff':>9}  Shape")
    print("-" * 78)
    single = 0
    for u in unds:
        ts = latest_snapshot(conn, u)
        pts = atm_iv_by_expiry(conn, u, ts)
        shape, err = describe_shape(pts)
        if err:
            single += 1
            iv = f"{pts[0]['atm_iv']:.2f}%" if pts else "-"
            reason = ("one expiry only" if len(pts) < 2
                      else "no comparable ATM")
            print(f"{u:<24} {iv:>10} {'-':>10} {'-':>9}  {reason}")
            continue
        print(f"{u:<24} {shape['front_iv']:>9.2f}% {shape['back_iv']:>9.2f}% "
              f"{shape['diff']:>+8.2f}  {shape['shape']}")
    print("-" * 78)
    if single:
        print(f"\n{single} underlying(s) could not be compared - usually")
        print("because their far expiries barely trade, so the IV derived")
        print("from a stale last price is not a real reading. That is itself")
        print("worth knowing: term structure is only measurable where the")
        print("back months have genuine liquidity. Run --symbol on one to")
        print("see the volume and OI behind the exclusion.")
    print()


def main():
    ap = argparse.ArgumentParser(description="Volatility term structure")
    ap.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not CHAIN_DB.exists():
        print("option_chains.db not found. Run:  py -3.12 option_chain.py --once")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{CHAIN_DB}?mode=ro", uri=True)
    if args.all:
        cmd_all(conn)
    else:
        analyse(conn, args.symbol)
    conn.close()


if __name__ == "__main__":
    main()
