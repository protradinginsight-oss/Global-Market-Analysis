#!/usr/bin/env python3
"""
Gamma exposure (GEX) and the gamma flip level.

The idea: market makers who sell options hedge their delta in the underlying,
and the direction of that hedging depends on their net gamma position.

  Net dealer gamma POSITIVE - hedging works against price. Dealers sell into
  strength and buy into weakness, which dampens moves. Ranges tend to hold.
  Historically the friendlier state for short options.

  Net dealer gamma NEGATIVE - hedging works with price. Dealers buy into
  strength and sell into weakness, amplifying moves. Ranges break, moves
  extend. The state where short option positions get hurt.

The gamma flip is the spot level where the sign changes.

Two things to be clear about before using any of this:

First, dealer positioning is ASSUMED, not observed. The standard convention -
dealers long calls, short puts against retail flow - is a heuristic. The chain
shows contracts outstanding, not who holds which side. If the assumption is
wrong for a given day, the sign of the whole calculation is wrong.

Second, GEX is well-established on US index options and much less validated
on Indian indices. The mechanism is the same; the evidence base is not.

Treat this as one input among several, not a level to trade off.

Usage:
    py -3.12 gamma.py --symbol NSE:NIFTY50-INDEX
    py -3.12 gamma.py --symbol NSE:NIFTY50-INDEX --profile
    py -3.12 gamma.py --all
"""

import sys
import sqlite3
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHAIN_DB = BASE_DIR / "option_chains.db"

# Fyers reports open interest in UNITS, not contracts. A Nifty ATM strike
# showing 6,811,805 OI is roughly 90,000 lots at a lot size of 75 - which is
# a sensible number, where 6.8 million contracts would not be.
#
# An earlier version multiplied by lot size anyway, inflating GEX 75-fold and
# producing figures like 3.9 trillion rupees of hedging flow per 1% move on
# Nifty, against a total Indian market cap around 400 trillion. Implausible
# on its face, which is how the error surfaced.
#
# Lot sizes are kept here for reference and for converting OI to contracts
# when that is what you want to read, but they are NOT applied to GEX.
LOT_SIZES = {
    "NSE:NIFTY50-INDEX": 75,
    "NSE:NIFTYBANK-INDEX": 30,
    "NSE:FINNIFTY-INDEX": 65,
    "NSE:MIDCPNIFTY-INDEX": 120,
    "NSE:NIFTYNXT50-INDEX": 25,
    "BSE:SENSEX-INDEX": 20,
    "BSE:BANKEX-INDEX": 30,
}
DEFAULT_LOT = 1

# Same lesson as the other modules: a strike with no trading behind it
# contributes a number without contributing information.
MIN_STRIKE_OI = 1_000


def latest_snapshot(conn, underlying):
    r = conn.execute("SELECT MAX(snapshot_ts) FROM chain_snapshot "
                     "WHERE underlying=?", (underlying,)).fetchone()
    return r[0] if r and r[0] else None


def front_expiry(conn, underlying, ts):
    """Gamma is dominated by the nearest expiry, so use only that."""
    r = conn.execute(
        "SELECT MIN(expiry_ts) FROM chain_snapshot "
        "WHERE underlying=? AND snapshot_ts=? AND expiry_ts IS NOT NULL",
        (underlying, ts)).fetchone()
    return r[0] if r and r[0] else None


def load(conn, underlying, ts, expiry_ts=None):
    q = ("SELECT strike, option_type, oi, gamma, spot, volume, expiry_date, "
         "iv, expiry_ts "
         "FROM chain_snapshot WHERE underlying=? AND snapshot_ts=? "
         "AND gamma IS NOT NULL")
    params = [underlying, ts]
    if expiry_ts:
        q += " AND expiry_ts=?"
        params.append(expiry_ts)
    q += " ORDER BY strike"
    rows = conn.execute(q, params).fetchall()

    spot = None
    expiry = None
    expiry_epoch = None
    legs = []
    for strike, otype, oi, gamma, sp, vol, exp_d, iv, exp_ts in rows:
        if sp:
            spot = sp
        if exp_d:
            expiry = exp_d
        if exp_ts:
            expiry_epoch = exp_ts
        legs.append({"strike": strike, "type": otype, "oi": oi or 0,
                     "gamma": gamma or 0.0, "volume": vol or 0,
                     "iv": iv})

    days = None
    if expiry_epoch:
        from datetime import datetime, date as _date
        try:
            days = (datetime.fromtimestamp(int(expiry_epoch)).date()
                    - _date.today()).days
            days = max(days, 1)      # an expiring option still has some life
        except (ValueError, OSError):
            days = None
    return legs, spot, expiry, days


def _norm_pdf(x):
    from math import exp, pi, sqrt
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def bs_gamma(spot, strike, iv_pct, days, rate=0.065):
    """Black-Scholes gamma at a hypothetical spot.

    The gamma stored in a chain snapshot is computed at the CURRENT spot, so
    it is a single fixed number. Scanning it across hypothetical prices would
    only scale it by price squared and never change its sign - which means a
    gamma flip could never be found, and any level reported would be an
    artefact.

    Gamma actually depends on moneyness: a far out-of-the-money option has
    almost none, and it rises as spot approaches the strike. Profiling GEX
    therefore requires recomputing gamma at each price, which is what this
    does.
    """
    from math import log, sqrt, exp
    if spot <= 0 or strike <= 0 or days <= 0 or not iv_pct or iv_pct <= 0:
        return 0.0
    sigma = iv_pct / 100.0
    t = days / 365.0
    denom = sigma * sqrt(t)
    if denom <= 0:
        return 0.0
    try:
        d1 = (log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / denom
    except ValueError:
        return 0.0
    return _norm_pdf(d1) / (spot * denom)


def gex_at(legs, price, lot, days=None):
    """Net dealer gamma exposure if spot were at `price`.

    Convention: dealers are long calls and short puts. Call gamma therefore
    contributes positively, put gamma negatively. This is the standard
    assumption and the weakest link in the whole calculation.

    Where IV and days-to-expiry are available, gamma is recomputed at
    `price` rather than reusing the snapshot value - see bs_gamma for why
    that matters. Falls back to the stored gamma if not.

    Expressed as currency value per 1% move.
    """
    total = 0.0
    for l in legs:
        if l["oi"] < MIN_STRIKE_OI:
            continue
        if days and l.get("iv"):
            g = bs_gamma(price, l["strike"], l["iv"], days)
        else:
            g = l["gamma"]
        # OI is already in units, so no lot multiplier here.
        contribution = (g * l["oi"] * price * price * 0.01)
        total += contribution if l["type"] == "CE" else -contribution
    return total


def find_flip(legs, spot, lot, days=None, span_pct=6.0, steps=120):
    """Scan a band around spot for where net GEX changes sign."""
    if not spot:
        return None, []
    lo = spot * (1 - span_pct / 100)
    hi = spot * (1 + span_pct / 100)
    step = (hi - lo) / steps
    profile = []
    for i in range(steps + 1):
        p = lo + i * step
        profile.append((p, gex_at(legs, p, lot, days)))

    flip = None
    for i in range(1, len(profile)):
        prev_p, prev_g = profile[i - 1]
        cur_p, cur_g = profile[i]
        if prev_g == 0:
            flip = prev_p
            break
        if (prev_g > 0) != (cur_g > 0):
            # Linear interpolation between the two sample points.
            if cur_g != prev_g:
                frac = abs(prev_g) / abs(cur_g - prev_g)
                flip = prev_p + frac * (cur_p - prev_p)
            else:
                flip = cur_p
            break
    return flip, profile


def analyse(conn, underlying, show_profile=False):
    ts = latest_snapshot(conn, underlying)
    if not ts:
        print(f"No chain data for {underlying}.")
        return
    exp_ts = front_expiry(conn, underlying, ts)
    legs, spot, expiry, days = load(conn, underlying, ts, exp_ts)
    if not legs or not spot:
        print(f"No gamma data for {underlying}. Fyers omits Greeks for deep "
              "ITM and OTM strikes, so a thin chain may have none.")
        return

    lot = LOT_SIZES.get(underlying, DEFAULT_LOT)
    usable = [l for l in legs if l["oi"] >= MIN_STRIKE_OI]

    print("=" * 76)
    print(f"  GAMMA EXPOSURE - {underlying}")
    print(f"  snapshot {ts}" + (f"   expiry {expiry}" if expiry else ""))
    print("=" * 76)
    print(f"\nSpot          : {spot:,.2f}")
    total_oi = sum(l["oi"] for l in legs)
    print(f"Total OI      : {total_oi:,} units "
          f"(~{total_oi//lot:,} lots at lot size {lot})")
    print(f"Strikes used  : {len(usable)} of {len(legs)} "
          f"(OI floor {MIN_STRIKE_OI:,})")
    print(f"Days to expiry: {days if days else 'unknown'}"
          + ("" if days else "   (gamma cannot be recomputed - using "
                             "snapshot values, so the flip level is "
                             "unreliable)"))

    if len(usable) < 6:
        print("\nToo few strikes with meaningful OI to build a gamma profile.")
        print("This happens on thin chains and outside market hours.\n")
        return

    current = gex_at(legs, spot, lot, days)
    flip, profile = find_flip(legs, spot, lot, days)

    print(f"\nNet GEX at spot: {current:+,.0f} per 1% move")
    state = "POSITIVE" if current > 0 else "NEGATIVE"
    print(f"Dealer gamma   : {state}")

    if current > 0:
        print("\n  Positive gamma. Under the assumed dealer positioning,")
        print("  hedging leans against price - selling strength, buying")
        print("  weakness. That dampens moves and tends to hold ranges,")
        print("  which is the friendlier state for short options.")
    else:
        print("\n  Negative gamma. Under the assumed dealer positioning,")
        print("  hedging leans with price - buying strength, selling")
        print("  weakness. That amplifies moves and breaks ranges, which")
        print("  is where short option positions get hurt.")

    if flip:
        dist = (flip - spot) / spot * 100
        print(f"\nGamma flip     : {flip:,.2f}   ({dist:+.2f}% from spot)")
        if current > 0 and flip < spot:
            print(f"  Below {flip:,.0f} the regime flips negative and moves")
            print("  would start being amplified rather than damped.")
        elif current < 0 and flip > spot:
            print(f"  Above {flip:,.0f} the regime flips positive and moves")
            print("  would start being damped.")
    else:
        print("\nGamma flip     : not found within 6% of spot")
        print("  The sign does not change across that band, so there is no")
        print("  nearby level where dealer behaviour would switch.")

    # Where the gamma actually sits - often more useful than the total
    by_strike = {}
    for l in usable:
        g = (bs_gamma(spot, l["strike"], l["iv"], days)
             if (days and l.get("iv")) else l["gamma"])
        c = (g * l["oi"] * spot * spot * 0.01)
        by_strike[l["strike"]] = by_strike.get(l["strike"], 0) + (
            c if l["type"] == "CE" else -c)
    top = sorted(by_strike.items(), key=lambda x: -abs(x[1]))[:8]
    print(f"\nLargest gamma concentrations")
    print(f"  {'Strike':>10} {'vs spot':>9} {'GEX per 1%':>16}")
    print("  " + "-" * 40)
    for k, g in top:
        print(f"  {k:>10,.0f} {(k-spot)/spot*100:>+8.2f}% {g:>+16,.0f}")
    print("\n  Large positive concentrations act as magnets and resistance to")
    print("  movement; the market often pins near them into expiry.")

    if show_profile:
        print(f"\nGEX profile across spot")
        print(f"  {'Price':>10} {'GEX per 1%':>16}  ")
        print("  " + "-" * 52)
        peak = max(abs(g) for _, g in profile) or 1
        for i in range(0, len(profile), 6):
            p, g = profile[i]
            width = int(abs(g) / peak * 22)
            bar = ("+" if g > 0 else "-") * max(1, width)
            mark = "  <- spot" if abs(p - spot) < (profile[1][0] - profile[0][0]) * 3 else ""
            print(f"  {p:>10,.0f} {g:>+16,.0f}  {bar}{mark}")

    print("\nDealer positioning is assumed (long calls, short puts), not")
    print("observed - the chain shows contracts, not who holds them. If that")
    print("assumption is wrong, the sign of everything above is wrong. GEX is")
    print("also well-established on US indices and much less validated on")
    print("Indian ones. One input among several, not a level to trade off.\n")


def cmd_all(conn):
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot ORDER BY underlying")]
    print("=" * 78)
    print("  GAMMA EXPOSURE SUMMARY")
    print("=" * 78)
    print(f"\n{'Underlying':<24} {'Spot':>11} {'Net GEX':>18} {'Flip':>11} "
          f"{'State':>10}")
    print("-" * 78)
    for u in unds:
        ts = latest_snapshot(conn, u)
        exp_ts = front_expiry(conn, u, ts)
        legs, spot, _, days = load(conn, u, ts, exp_ts)
        if not legs or not spot:
            print(f"{u:<24} {'-':>11} {'no gamma data':>18} {'-':>11} {'-':>10}")
            continue
        lot = LOT_SIZES.get(u, DEFAULT_LOT)
        usable = [l for l in legs if l["oi"] >= MIN_STRIKE_OI]
        if len(usable) < 6:
            print(f"{u:<24} {spot:>11,.0f} {'too few strikes':>18} "
                  f"{'-':>11} {'-':>10}")
            continue
        g = gex_at(legs, spot, lot, days)
        flip, _ = find_flip(legs, spot, lot, days)
        print(f"{u:<24} {spot:>11,.0f} {g:>+18,.0f} "
              f"{(f'{flip:,.0f}' if flip else '-'):>11} "
              f"{('positive' if g > 0 else 'negative'):>10}")
    print("-" * 78)
    print("\nGEX is currency value per 1% move. Fyers reports OI in units,")
    print("so no lot multiplier is applied. Positive suggests dampened moves,")
    print("negative amplified - under an assumed dealer positioning that")
    print("cannot be verified.\n")


def main():
    ap = argparse.ArgumentParser(description="Gamma exposure and flip level")
    ap.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--profile", action="store_true",
                    help="print GEX across a band of spot prices")
    args = ap.parse_args()

    if not CHAIN_DB.exists():
        print("option_chains.db not found. Run:  py -3.12 option_chain.py --once")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{CHAIN_DB}?mode=ro", uri=True)
    if args.all:
        cmd_all(conn)
    else:
        analyse(conn, args.symbol, args.profile)
    conn.close()


if __name__ == "__main__":
    main()
