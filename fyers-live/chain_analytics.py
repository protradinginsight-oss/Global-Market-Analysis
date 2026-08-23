#!/usr/bin/env python3
"""
Option chain analytics - PCR, Max Pain, IV percentile, OI buildup.

Reads the snapshots collected by option_chain.py and computes the metrics in
section 3 of the checklist.

Read-only.

Usage:
    py -3.12 chain_analytics.py --symbol NSE:NIFTY50-INDEX
    py -3.12 chain_analytics.py --all
    py -3.12 chain_analytics.py --symbol NSE:NIFTY50-INDEX --chain
    py -3.12 chain_analytics.py --iv-rank
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
CHAIN_DB = BASE_DIR / "option_chains.db"


def latest_snapshot(conn, underlying):
    row = conn.execute(
        "SELECT MAX(snapshot_ts) FROM chain_snapshot WHERE underlying=?",
        (underlying,)).fetchone()
    return row[0] if row and row[0] else None


def load_chain(conn, underlying, snapshot_ts):
    rows = conn.execute(
        """SELECT strike, option_type, ltp, volume, oi, oi_change, iv,
                  delta, gamma, theta, vega, spot
           FROM chain_snapshot WHERE underlying=? AND snapshot_ts=?
           ORDER BY strike""", (underlying, snapshot_ts)).fetchall()
    chain = defaultdict(dict)
    spot = None
    for st, ot, ltp, vol, oi, oich, iv, d, g, t, v, sp in rows:
        chain[st][ot] = {"ltp": ltp, "volume": vol, "oi": oi, "oi_change": oich,
                         "iv": iv, "delta": d, "gamma": g, "theta": t, "vega": v}
        if sp:
            spot = sp
    return dict(chain), spot


def pcr(chain):
    """Put-call ratio by open interest and by volume."""
    ce_oi = sum(v["CE"]["oi"] or 0 for v in chain.values() if "CE" in v)
    pe_oi = sum(v["PE"]["oi"] or 0 for v in chain.values() if "PE" in v)
    ce_vol = sum(v["CE"]["volume"] or 0 for v in chain.values() if "CE" in v)
    pe_vol = sum(v["PE"]["volume"] or 0 for v in chain.values() if "PE" in v)
    return {
        "pcr_oi": pe_oi / ce_oi if ce_oi else None,
        "pcr_volume": pe_vol / ce_vol if ce_vol else None,
        "total_ce_oi": ce_oi, "total_pe_oi": pe_oi,
    }


def max_pain(chain):
    """The strike where total option writer payout is smallest.

    For each candidate expiry price, sum what all outstanding calls and puts
    would pay out. The minimum is 'max pain' - the level that hurts option
    buyers most, and which price is sometimes said to gravitate toward near
    expiry. Treat it as a reference level, not a prediction.
    """
    strikes = sorted(chain)
    if not strikes:
        return None, {}
    pain = {}
    for expiry_price in strikes:
        total = 0
        for k, legs in chain.items():
            ce_oi = (legs.get("CE") or {}).get("oi") or 0
            pe_oi = (legs.get("PE") or {}).get("oi") or 0
            if expiry_price > k:
                total += (expiry_price - k) * ce_oi
            if expiry_price < k:
                total += (k - expiry_price) * pe_oi
        pain[expiry_price] = total
    best = min(pain, key=pain.get)
    return best, pain


def atm_strike(chain, spot):
    if not chain or spot is None:
        return None
    return min(chain, key=lambda k: abs(k - spot))


def atm_iv(chain, spot):
    k = atm_strike(chain, spot)
    if k is None:
        return None
    legs = chain[k]
    ivs = [legs[t]["iv"] for t in ("CE", "PE")
           if t in legs and legs[t]["iv"] is not None]
    return sum(ivs) / len(ivs) if ivs else None


def iv_percentile(conn, underlying, current_iv, days=252):
    """Where today's ATM IV sits against its own recent history.

    This is the number that actually governs whether selling premium is worth
    it - a 14% IV means nothing without knowing whether that's high or low for
    this instrument. Needs weeks of history before it's meaningful.
    """
    if current_iv is None:
        return None, 0
    rows = conn.execute(
        """SELECT snapshot_ts, AVG(iv) FROM chain_snapshot
           WHERE underlying=? AND iv IS NOT NULL AND iv > 0
           GROUP BY substr(snapshot_ts,1,10)
           ORDER BY snapshot_ts DESC LIMIT ?""", (underlying, days)).fetchall()
    history = [r[1] for r in rows if r[1]]
    if len(history) < 10:
        return None, len(history)
    below = sum(1 for h in history if h < current_iv)
    return below / len(history) * 100, len(history)


def buildup(legs):
    """Classify each leg by the price/OI combination.

    Rising price with rising OI is fresh buying; falling price with rising OI
    is fresh selling; rising price with falling OI is short covering; falling
    price with falling OI is long unwinding.
    """
    out = {}
    for t in ("CE", "PE"):
        d = legs.get(t)
        if not d:
            continue
        oich, ltp = d.get("oi_change"), d.get("ltp")
        if oich is None or ltp is None:
            out[t] = "-"
            continue
        # Without a previous LTP we approximate direction from oi_change sign
        # combined with whether the leg is priced above a nominal floor.
        if oich > 0:
            out[t] = "OI added"
        elif oich < 0:
            out[t] = "OI reduced"
        else:
            out[t] = "flat"
    return out


def analyse(conn, underlying, show_chain=False):
    ts = latest_snapshot(conn, underlying)
    if not ts:
        print(f"No chain data for {underlying}.")
        return None

    chain, spot = load_chain(conn, underlying, ts)
    if not chain:
        print(f"Empty chain for {underlying} at {ts}.")
        return None

    p = pcr(chain)
    mp, pain = max_pain(chain)
    atm = atm_strike(chain, spot)
    aiv = atm_iv(chain, spot)
    ivp, ivn = iv_percentile(conn, underlying, aiv)

    print("=" * 76)
    print(f"  {underlying}")
    print(f"  snapshot {ts}")
    print("=" * 76)
    print(f"\nSpot          : {spot:,.2f}" if spot else "\nSpot          : -")
    print(f"ATM strike    : {atm:,.2f}" if atm else "ATM strike    : -")
    print(f"Strikes       : {len(chain)}")
    print()
    print(f"PCR (OI)      : {p['pcr_oi']:.3f}" if p['pcr_oi'] else "PCR (OI)      : -")
    print(f"PCR (volume)  : {p['pcr_volume']:.3f}" if p['pcr_volume'] else "PCR (volume)  : -")
    print(f"Total CE OI   : {p['total_ce_oi']:,}")
    print(f"Total PE OI   : {p['total_pe_oi']:,}")
    print(f"Max Pain      : {mp:,.2f}" if mp else "Max Pain      : -")
    if mp and spot:
        diff = (mp - spot) / spot * 100
        print(f"              : {diff:+.2f}% from spot")
    print()
    print(f"ATM IV        : {aiv:.2f}%" if aiv else "ATM IV        : -")
    if ivp is not None:
        zone = ("high - premium selling favourable" if ivp >= 70 else
                "low - premium selling unattractive" if ivp <= 30 else
                "middling")
        print(f"IV percentile : {ivp:.0f}%  ({ivn} days of history) - {zone}")
    else:
        print(f"IV percentile : not enough history yet ({ivn} days, need 10+)")
        print("                This is the key number for premium selling.")
        print("                It becomes usable after a few weeks of collection.")

    if show_chain:
        print(f"\n{'Strike':>10} | {'CE OI':>11} {'CE IV':>7} {'CE LTP':>9} "
              f"| {'PE LTP':>9} {'PE IV':>7} {'PE OI':>11}")
        print("-" * 76)
        def iv_str(v):
            # Fyers returns 0 for strikes where it doesn't compute IV - deep
            # ITM (nearly all intrinsic) and deep OTM (priced at tick size).
            # Printing "0.0%" would read as measured zero volatility, which
            # is a very different claim from "not computed".
            return f"{v:>6.1f}%" if v else "     -"
        for k in sorted(chain):
            ce = chain[k].get("CE", {})
            pe = chain[k].get("PE", {})
            mark = " <ATM" if k == atm else ""
            print(f"{k:>10,.0f} | {ce.get('oi') or 0:>11,} "
                  f"{iv_str(ce.get('iv'))} {ce.get('ltp') or 0:>9,.2f} "
                  f"| {pe.get('ltp') or 0:>9,.2f} {iv_str(pe.get('iv'))} "
                  f"{pe.get('oi') or 0:>11,}{mark}")

        with_iv = sum(1 for k in chain
                      for t in ("CE", "PE")
                      if chain[k].get(t, {}).get("iv"))
        total_legs = sum(len(chain[k]) for k in chain)
        print(f"\n  IV computed for {with_iv} of {total_legs} legs - Fyers omits")
        print("  it deep ITM and deep OTM, where it cannot be inverted reliably.")
        print("  Note CE IV and PE IV are identical at each strike: Fyers")
        print("  returns one IV per strike, not one per leg, so put-call skew")
        print("  cannot be measured from this feed.")

    print("\nPCR above ~1.3 is often read as bearish positioning (heavy put")
    print("writing), below ~0.7 as bullish. Max Pain is a reference level from")
    print("outstanding OI, not a forecast. Neither is a signal on its own.\n")

    return {"pcr_oi": p["pcr_oi"], "max_pain": mp, "atm_iv": aiv,
            "iv_percentile": ivp, "spot": spot}


def cmd_all(conn):
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot ORDER BY underlying")]
    if not unds:
        print("No chain data yet. Run:  py -3.12 option_chain.py --once")
        return
    print("=" * 76)
    print("  ALL UNDERLYINGS - LATEST SNAPSHOT")
    print("=" * 76)
    print(f"\n{'Underlying':<24} {'Spot':>11} {'PCR':>7} {'Max Pain':>11} "
          f"{'ATM IV':>8} {'IV %ile':>8}")
    print("-" * 76)
    for u in unds:
        ts = latest_snapshot(conn, u)
        chain, spot = load_chain(conn, u, ts)
        if not chain:
            continue
        p = pcr(chain)
        mp, _ = max_pain(chain)
        aiv = atm_iv(chain, spot)
        ivp, _ = iv_percentile(conn, u, aiv)
        print(f"{u:<24} {spot or 0:>11,.2f} "
              f"{p['pcr_oi'] or 0:>7.2f} {mp or 0:>11,.0f} "
              f"{aiv or 0:>7.1f}% {(f'{ivp:.0f}%' if ivp is not None else '-'):>8}")
    print()


def cmd_iv_rank(conn):
    """Rank underlyings by IV percentile - where premium is richest."""
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot")]
    results = []
    for u in unds:
        ts = latest_snapshot(conn, u)
        chain, spot = load_chain(conn, u, ts)
        if not chain:
            continue
        aiv = atm_iv(chain, spot)
        ivp, n = iv_percentile(conn, u, aiv)
        if ivp is not None:
            results.append((u, aiv, ivp, n))

    print("=" * 76)
    print("  IV PERCENTILE RANKING")
    print("=" * 76)
    if not results:
        print("\nNot enough history to rank yet. IV percentile compares today's")
        print("IV to its own past, so it needs a few weeks of collection.")
        print("\nKeep option_chain.py running daily and this becomes the most")
        print("useful screen you have for premium selling.\n")
        return
    results.sort(key=lambda r: -r[2])
    print(f"\n{'Underlying':<24} {'ATM IV':>9} {'IV %ile':>9} {'Days':>7}")
    print("-" * 76)
    for u, aiv, ivp, n in results:
        print(f"{u:<24} {aiv:>8.1f}% {ivp:>8.0f}% {n:>7}")
    print("\nHigh percentile means IV is elevated versus this instrument's own")
    print("history - the setup premium sellers look for. Low means the opposite.\n")


def main():
    ap = argparse.ArgumentParser(description="Option chain analytics")
    ap.add_argument("--symbol")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--chain", action="store_true", help="print the full ladder")
    ap.add_argument("--iv-rank", action="store_true")
    args = ap.parse_args()

    if not CHAIN_DB.exists():
        print("option_chains.db not found. Run:  py -3.12 option_chain.py --once")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{CHAIN_DB}?mode=ro", uri=True)

    if args.iv_rank:
        cmd_iv_rank(conn)
    elif args.all:
        cmd_all(conn)
    elif args.symbol:
        analyse(conn, args.symbol, args.chain)
    else:
        print("Pick one of: --symbol TICKER, --all, --iv-rank")
        unds = [r[0] for r in conn.execute(
            "SELECT DISTINCT underlying FROM chain_snapshot LIMIT 8")]
        if unds:
            print("\nAvailable: " + ", ".join(unds))
    conn.close()


if __name__ == "__main__":
    main()
