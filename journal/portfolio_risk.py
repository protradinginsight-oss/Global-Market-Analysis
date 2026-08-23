#!/usr/bin/env python3
"""
Portfolio risk - what happens to everything at once.

Per-trade stops tell you what one position can lose. They say nothing about
what the book loses if the market gaps against all of it simultaneously,
which is the loss that actually ends accounts.

This computes:
  - total risk if every stop is hit
  - beta-weighted exposure to Nifty, in rupees per 1% index move
  - concentration by sector and by single underlying
  - stress scenarios against historical shocks
  - days-to-liquidate per position

Reads the trade journal and the stored price history. Read-only.

Usage:
    py -3.12 portfolio_risk.py
    py -3.12 portfolio_risk.py --stress
    py -3.12 portfolio_risk.py --capital 1000000
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
JOURNAL_DB = ROOT / "journal" / "trade_journal.db"
HIST_DB = ROOT / "fyers-live" / "market_history.db"
INDEX = "NSE:NIFTY50-INDEX"

# Rough sector mapping for concentration checks. Not exhaustive - anything
# unmapped falls into "other", which is honest rather than guessing.
SECTORS = {
    "banking": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
                "INDUSINDBK", "BANKBARODA", "PNB", "CANBK", "FEDERALBNK",
                "IDFCFIRSTB", "AUBANK", "BANKINDIA", "INDIANB", "UNIONBANK",
                "RBLBANK", "YESBANK", "BANDHANBNK"],
    "it": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTM", "MPHASIS",
           "COFORGE", "PERSISTENT", "OFSS", "KPITTECH", "TATAELXSI"],
    "energy": ["RELIANCE", "ONGC", "IOC", "BPCL", "HINDPETRO", "GAIL",
               "PETRONET", "OIL", "COALINDIA", "NTPC", "POWERGRID",
               "TATAPOWER", "ADANIPOWER", "JSWENERGY", "ADANIGREEN"],
    "auto": ["MARUTI", "TATAMOTORS", "TMPV", "M&M", "BAJAJ-AUTO", "EICHERMOT",
             "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY", "MOTHERSON", "BOSCHLTD",
             "BHARATFORG", "SONACOMS", "UNOMINDA"],
    "pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA",
               "TORNTPHARM", "ALKEM", "GLENMARK", "ZYDUSLIFE", "BIOCON",
               "LAURUSLABS", "MANKIND", "ABBOTINDIA"],
    "fmcg": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
             "GODREJCP", "COLPAL", "TATACONSUM", "UNITDSPR", "RADICO",
             "VBL", "PATANJALI"],
    "metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL",
               "NATIONALUM", "SAIL", "NMDC", "HINDZINC"],
    "financials": ["BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE",
                   "ICICIPRULI", "ICICIGI", "LICI", "SHRIRAMFIN", "CHOLAFIN",
                   "MUTHOOTFIN", "MANAPPURAM", "PFC", "RECLTD", "IRFC",
                   "LICHSGFIN", "SBICARD", "POLICYBZR", "JIOFIN"],
}
SECTOR_OF = {sym: sec for sec, syms in SECTORS.items() for sym in syms}

# Historical single-day index moves, used as stress scenarios. Real events
# rather than round numbers, because "what if Nifty falls 5%" is easier to
# dismiss than "what if this happens again".
SCENARIOS = [
    ("COVID crash, 23 Mar 2020",      -13.0),
    ("Election shock, 4 Jun 2024",     -5.9),
    ("Budget selloff, typical",        -3.0),
    ("Sharp down day",                 -2.0),
    ("Ordinary down day",              -1.0),
    ("Ordinary up day",                 1.0),
    ("Sharp up day",                    2.0),
    ("Election rally, 3 Jun 2024",      3.3),
]


def base_symbol(ticker):
    """NSE:SBIN-EQ -> SBIN"""
    t = ticker.split(":")[-1]
    for suffix in ("-EQ", "-INDEX", "-A"):
        if t.endswith(suffix):
            return t[:-len(suffix)]
    return t


def open_positions(conn):
    try:
        rows = conn.execute(
            "SELECT id, symbol, side, quantity, entry_price, stop_price, "
            "target_price, strategy, entry_time FROM trades WHERE status='OPEN'"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r[0], "symbol": r[1], "side": r[2], "qty": r[3],
             "entry": r[4], "stop": r[5], "target": r[6], "strategy": r[7],
             "opened": r[8]} for r in rows]


def compute_beta(hconn, symbol, days=120):
    """Beta of a stock against Nifty, from stored daily history.

    Returns None when there isn't enough overlapping history - better than
    defaulting to 1.0, which would silently claim knowledge we don't have.
    """
    def rets(sym):
        rows = hconn.execute(
            "SELECT ts, close FROM history WHERE symbol=? AND resolution='1D' "
            "AND close IS NOT NULL ORDER BY epoch DESC LIMIT ?",
            (sym, days + 1)).fetchall()
        rows.reverse()
        out = {}
        for i in range(1, len(rows)):
            p, c = rows[i - 1][1], rows[i][1]
            if p:
                out[rows[i][0][:10]] = (c - p) / p
        return out

    sr, ir = rets(symbol), rets(INDEX)
    shared = sorted(set(sr) & set(ir))
    if len(shared) < 40:
        return None
    xs = [ir[d] for d in shared]
    ys = [sr[d] for d in shared]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)
    var = sum((x - mx) ** 2 for x in xs) / (len(xs) - 1)
    return cov / var if var else None


def avg_volume(hconn, symbol, days=20):
    rows = hconn.execute(
        "SELECT volume FROM history WHERE symbol=? AND resolution='1D' "
        "AND volume IS NOT NULL ORDER BY epoch DESC LIMIT ?",
        (symbol, days)).fetchall()
    vols = [r[0] for r in rows if r[0]]
    return sum(vols) / len(vols) if vols else None


def analyse(positions, hconn, capital=None):
    if not positions:
        print("=" * 76)
        print("  PORTFOLIO RISK")
        print("=" * 76)
        print("\nNo open positions.")
        print("\nRecord trades with journal.py and this becomes live. The")
        print("numbers that matter here - total risk if every stop is hit,")
        print("beta-weighted exposure, concentration - only exist once there")
        print("is a book to measure.\n")
        return

    print("=" * 76)
    print("  PORTFOLIO RISK")
    print("=" * 76)

    enriched = []
    for p in positions:
        base = base_symbol(p["symbol"])
        beta = compute_beta(hconn, p["symbol"]) if hconn else None
        notional = p["entry"] * p["qty"]
        risk = (abs(p["entry"] - p["stop"]) * p["qty"]) if p["stop"] else None
        signed = notional if p["side"] == "LONG" else -notional
        enriched.append({**p, "base": base, "beta": beta,
                         "notional": notional, "signed": signed, "risk": risk,
                         "sector": SECTOR_OF.get(base, "other")})

    print(f"\n{'ID':>4} {'Symbol':<20} {'Side':<6} {'Notional':>13} "
          f"{'Risk':>11} {'Beta':>6}  Sector")
    print("-" * 76)
    for e in enriched:
        print(f"{e['id']:>4} {e['base'][:19]:<20} {e['side']:<6} "
              f"{e['notional']:>13,.0f} "
              f"{(f'{e['risk']:,.0f}' if e['risk'] else 'no stop'):>11} "
              f"{(f'{e['beta']:.2f}' if e['beta'] else '-'):>6}  {e['sector']}")
    print("-" * 76)

    gross = sum(e["notional"] for e in enriched)
    net = sum(e["signed"] for e in enriched)
    total_risk = sum(e["risk"] for e in enriched if e["risk"])
    no_stop = [e for e in enriched if not e["risk"]]

    print(f"\nEXPOSURE")
    print(f"  Gross notional     Rs {gross:>15,.0f}")
    print(f"  Net (long - short) Rs {net:>15,.0f}")
    if capital:
        lev = gross / capital
        print(f"  Gross / capital       {lev*100:>14.1f}%")
        if lev > 1.0:
            print(f"\n  Gross exposure is {lev:.1f}x capital. That is leverage:")
            print("  losses scale with the notional, not with what you put up.")

    print(f"\nRISK IF EVERY STOP IS HIT")
    print(f"  Total              Rs {total_risk:>15,.0f}")
    if capital:
        pct = total_risk / capital * 100
        print(f"  As % of capital       {pct:>14.2f}%")
        if pct > 6:
            print(f"\n  That is {pct:.1f}% of capital at risk simultaneously.")
            print("  Common guidance caps total open risk around 2-6%; above")
            print("  that a single bad session does lasting damage.")
    if no_stop:
        print(f"\n  {len(no_stop)} position(s) have no stop recorded, so their")
        print("  loss is unbounded and excluded from the total above.")
        for e in no_stop:
            print(f"    #{e['id']} {e['base']} - Rs {e['notional']:,.0f} notional")

    # Beta-weighted exposure - the single number for "what does 1% do to me"
    with_beta = [e for e in enriched if e["beta"] is not None]
    if with_beta:
        # Beta-adjusted notional, then scaled to a 1% move. An earlier
        # version printed the notional itself and labelled it "per 1%",
        # overstating the figure a hundredfold.
        bw_notional = sum(e["signed"] * e["beta"] for e in with_beta)
        bw = bw_notional * 0.01
        print(f"\nBETA-WEIGHTED EXPOSURE TO NIFTY")
        print(f"  Beta-adjusted notional  Rs {bw_notional:+,.0f}")
        print(f"  P&L per 1% index move   Rs {bw:+,.0f}")
        missing = len(enriched) - len(with_beta)
        if missing:
            print(f"  ({missing} position(s) excluded - not enough price")
            print("   history to compute a beta)")
        print("\n  This is the number per-trade stops hide: it is what the")
        print("  whole book gains or loses when the index moves, regardless")
        print("  of where individual stops sit.")
        if capital:
            print(f"  A 1% move is {abs(bw)/capital*100:.2f}% of capital; "
                  f"a 5% move is {abs(bw)*5/capital*100:.2f}%.")

    # Concentration
    by_sector = defaultdict(float)
    by_symbol = defaultdict(float)
    for e in enriched:
        by_sector[e["sector"]] += e["notional"]
        by_symbol[e["base"]] += e["notional"]

    print(f"\nCONCENTRATION")
    print(f"  {'Sector':<16} {'Notional':>14} {'Share':>8}")
    print("  " + "-" * 42)
    for sec, val in sorted(by_sector.items(), key=lambda x: -x[1]):
        share = val / gross * 100
        flag = "  <- heavy" if share > 40 else ""
        print(f"  {sec:<16} {val:>14,.0f} {share:>7.1f}%{flag}")

    top_sym, top_val = max(by_symbol.items(), key=lambda x: x[1])
    top_share = top_val / gross * 100
    if top_share > 30:
        print(f"\n  Largest single name: {top_sym} at {top_share:.0f}% of gross.")
        print("  Concentration is not automatically wrong, but it means the")
        print("  book's outcome depends heavily on one company's news.")

    # Liquidity
    if hconn:
        print(f"\nDAYS TO LIQUIDATE  (at 20% of average daily volume)")
        print(f"  {'Symbol':<20} {'Qty':>10} {'Avg vol':>14} {'Days':>7}")
        print("  " + "-" * 55)
        slow = []
        for e in enriched:
            av = avg_volume(hconn, e["symbol"])
            if not av:
                print(f"  {e['base'][:19]:<20} {e['qty']:>10,} "
                      f"{'no data':>14} {'-':>7}")
                continue
            days = e["qty"] / (av * 0.2)
            if days > 1:
                slow.append((e["base"], days))
            # Sub-day figures round to 0.00 and look like missing data, so
            # show them as a fraction of a session instead.
            shown = (f"{days:.2f}" if days >= 0.01
                     else f"<{0.01:.2f}")
            print(f"  {e['base'][:19]:<20} {e['qty']:>10,} {av:>14,.0f} "
                  f"{shown:>7}")
        if slow:
            print(f"\n  {len(slow)} position(s) would take over a day to exit")
            print("  without moving the price. A stop-loss on an illiquid")
            print("  position is a hope, not a plan - in a fast market you")
            print("  get filled well past your level.")


def stress(positions, hconn, capital=None):
    if not positions:
        print("No open positions to stress.")
        return
    enriched = []
    for p in positions:
        beta = compute_beta(hconn, p["symbol"]) if hconn else None
        notional = p["entry"] * p["qty"]
        signed = notional if p["side"] == "LONG" else -notional
        enriched.append({**p, "beta": beta, "signed": signed})

    usable = [e for e in enriched if e["beta"] is not None]
    if not usable:
        print("Cannot stress test - no position has enough price history")
        print("for a beta estimate. Backfill more daily data first.")
        return

    bw_notional = sum(e["signed"] * e["beta"] for e in usable)
    bw = bw_notional * 0.01

    print("=" * 76)
    print("  STRESS SCENARIOS")
    print("=" * 76)
    print(f"\nBeta-adjusted notional: Rs {bw_notional:+,.0f}")
    print(f"P&L per 1% index move : Rs {bw:+,.0f}")
    if len(usable) < len(enriched):
        print(f"({len(enriched)-len(usable)} position(s) excluded for lack of "
              "history)")
    print(f"\n{'Scenario':<34} {'Index':>8} {'P&L':>16}" +
          (f" {'% cap':>8}" if capital else ""))
    print("-" * 76)
    for name, move in SCENARIOS:
        pnl = bw * move
        line = f"{name:<34} {move:>+7.1f}% {pnl:>+16,.0f}"
        if capital:
            line += f" {pnl/capital*100:>+7.2f}%"
        print(line)
    print("-" * 76)
    print("\nBeta is estimated from recent history and is least reliable")
    print("exactly when it matters most: in a crash, correlations converge")
    print("toward one and almost everything falls together. Treat the")
    print("severe scenarios as optimistic.\n")


def main():
    ap = argparse.ArgumentParser(description="Portfolio risk")
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--capital", type=float,
                    help="trading capital, to express risk as a percentage")
    args = ap.parse_args()

    if not JOURNAL_DB.exists():
        print("trade_journal.db not found. Record trades with journal.py first.")
        sys.exit(1)
    jconn = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
    hconn = (sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)
             if HIST_DB.exists() else None)
    if hconn is None:
        print("Note: market_history.db not found, so beta and liquidity")
        print("cannot be computed. Run backfill.py to enable them.\n")

    positions = open_positions(jconn)
    if args.stress:
        stress(positions, hconn, args.capital)
    else:
        analyse(positions, hconn, args.capital)

    jconn.close()
    if hconn:
        hconn.close()


if __name__ == "__main__":
    main()
