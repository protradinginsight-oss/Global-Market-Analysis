#!/usr/bin/env python3
"""
Market regime classification.

The same strategy behaves very differently depending on conditions. Selling
premium is comfortable in a quiet, range-bound market and dangerous in a
volatile trending one; a breakout system is the reverse. Knowing which
regime you are in is more useful than most indicators, because it tells you
which tools to reach for rather than what to predict.

Regime here is built from three things already computed elsewhere:
  - volatility  : India VIX proxy from ATM IV, or realised volatility
  - trend       : where the index sits against its own moving averages
  - breadth     : how many stocks are participating

Deliberately simple and rule-based rather than statistical. A clustering
model would produce regimes nobody can interpret, and the point of this is
to be legible enough to argue with.

Usage:
    py -3.12 regime.py
    py -3.12 regime.py --history 30
    py -3.12 regime.py --strategies
"""

import sys
import sqlite3
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HIST_DB = BASE_DIR / "market_history.db"
CHAIN_DB = BASE_DIR / "option_chains.db"

try:
    from indicators import sma, atr
except ImportError:
    print("indicators.py not found - it must sit next to this script.")
    sys.exit(1)

INDEX = "NSE:NIFTY50-INDEX"

# Thresholds. These are judgement, not measurement - there is no history of
# regime labels to fit them against. They are set where the descriptions
# stop being true rather than optimised, and they are worth revisiting once
# there is enough live data to check them.
VOL_LOW = 12.0          # annualised realised vol, %
VOL_HIGH = 20.0
TREND_FLAT_PCT = 2.0    # index within this % of its 50-DMA counts as flat
BREADTH_WEAK = 40.0     # % of stocks above 50-DMA
BREADTH_STRONG = 60.0


def realised_vol(closes, window=20):
    """Annualised standard deviation of daily returns.

    Used in place of India VIX, which Fyers does not expose directly.
    Realised vol is backward-looking where VIX is forward-looking, so it
    reacts after a shock rather than before it - a real limitation worth
    remembering when the reading looks calm.
    """
    if len(closes) < window + 1:
        return None
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - window, len(closes))
            if closes[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5) * 100


def classify(vol, trend_pct, above50_pct, above200_pct):
    """Assign a regime label plus the reasoning behind it."""
    reasons = []

    if vol is None:
        vol_state = "unknown"
    elif vol < VOL_LOW:
        vol_state = "low"
        reasons.append(f"realised vol {vol:.1f}% is below {VOL_LOW}%")
    elif vol > VOL_HIGH:
        vol_state = "high"
        reasons.append(f"realised vol {vol:.1f}% is above {VOL_HIGH}%")
    else:
        vol_state = "normal"
        reasons.append(f"realised vol {vol:.1f}% is mid-range")

    if trend_pct is None:
        trend_state = "unknown"
    elif abs(trend_pct) < TREND_FLAT_PCT:
        trend_state = "flat"
        reasons.append(f"index within {abs(trend_pct):.1f}% of its 50-DMA")
    elif trend_pct > 0:
        trend_state = "up"
        reasons.append(f"index {trend_pct:.1f}% above its 50-DMA")
    else:
        trend_state = "down"
        reasons.append(f"index {abs(trend_pct):.1f}% below its 50-DMA")

    if above50_pct is None:
        breadth_state = "unknown"
    elif above50_pct >= BREADTH_STRONG:
        breadth_state = "broad"
        reasons.append(f"{above50_pct:.0f}% of stocks above their 50-DMA")
    elif above50_pct <= BREADTH_WEAK:
        breadth_state = "narrow"
        reasons.append(f"only {above50_pct:.0f}% of stocks above their 50-DMA")
    else:
        breadth_state = "mixed"
        reasons.append(f"{above50_pct:.0f}% of stocks above their 50-DMA")

    # Name the combination. Divergences get their own labels because they
    # behave differently from the simple cases.
    if trend_state == "up" and breadth_state == "narrow":
        label = "Narrow advance"
        summary = ("Index rising but most stocks are not. The move rests on "
                   "a few names, which makes it fragile in a way the index "
                   "level does not show.")
    elif trend_state == "down" and breadth_state == "broad":
        label = "Concentrated selling"
        summary = ("Index falling while most stocks hold up - the weakness "
                   "is in heavyweights rather than the market.")
    elif trend_state == "flat" and vol_state == "low":
        label = "Quiet range"
        summary = ("Low volatility, no trend. Historically the friendliest "
                   "conditions for premium selling, though also when premium "
                   "is cheapest.")
    elif trend_state == "flat" and vol_state == "high":
        label = "Volatile chop"
        summary = ("Movement without direction. Hard for both trend systems "
                   "and premium sellers - large swings, no follow-through.")
    elif trend_state == "up" and vol_state == "low":
        label = "Steady uptrend"
        summary = "Rising on contained volatility - the calmest bullish state."
    elif trend_state == "up" and vol_state == "high":
        label = "Volatile uptrend"
        summary = ("Rising but unstable. Gains are real; so is the risk of "
                   "sharp reversals.")
    elif trend_state == "down" and vol_state == "high":
        label = "Volatile downtrend"
        summary = ("Falling with elevated volatility - the most dangerous "
                   "state for short options positions.")
    elif trend_state == "down":
        label = "Orderly decline"
        summary = "Falling without panic."
    else:
        label = "Mixed"
        summary = "No clear regime - the signals disagree."

    return {
        "label": label, "summary": summary, "reasons": reasons,
        "vol_state": vol_state, "trend_state": trend_state,
        "breadth_state": breadth_state, "vol": vol,
        "trend_pct": trend_pct, "above50": above50_pct, "above200": above200_pct,
    }


# How each strategy family tends to fare. These are priors from how the
# strategies work, not measured results - the journal is what will
# eventually replace them with evidence.
STRATEGY_FIT = {
    "Quiet range": {
        "favoured": ["Short strangle", "Iron condor", "Range mean-reversion"],
        "avoid": ["Breakout systems", "Long straddle"],
        "caution": "Premium is cheapest exactly when selling feels safest.",
    },
    "Steady uptrend": {
        "favoured": ["Bull put spread", "Covered call", "Trend following"],
        "avoid": ["Short call spreads", "Counter-trend shorts"],
        "caution": "Chasing late in a trend is where most of the damage happens.",
    },
    "Volatile uptrend": {
        "favoured": ["Bull put spread with wider strikes", "Reduced size"],
        "avoid": ["Naked short options", "Full-size directional"],
        "caution": "Rising IV helps sellers but sharp reversals hurt more.",
    },
    "Volatile chop": {
        "favoured": ["Standing aside", "Very small size"],
        "avoid": ["Breakouts", "Naked short options", "Trend following"],
        "caution": "Both trend and premium approaches struggle here.",
    },
    "Volatile downtrend": {
        "favoured": ["Long puts", "Bear call spread", "Cash"],
        "avoid": ["Short puts", "Naked strangles", "Buying dips mechanically"],
        "caution": "The regime that does most damage to short option books.",
    },
    "Orderly decline": {
        "favoured": ["Bear call spread", "Reduced long exposure"],
        "avoid": ["Aggressive dip buying"],
        "caution": "Orderly declines can turn disorderly without warning.",
    },
    "Narrow advance": {
        "favoured": ["Index over single stocks", "Reduced size"],
        "avoid": ["Broad long baskets", "Assuming the rally is healthy"],
        "caution": "Narrow rallies can persist for months - divergence is "
                   "not a timing signal.",
    },
    "Concentrated selling": {
        "favoured": ["Stock-specific over index"],
        "avoid": ["Index shorts on the assumption of broad weakness"],
        "caution": "The index is misrepresenting the market here.",
    },
    "Mixed": {
        "favoured": ["Smaller size until the picture clarifies"],
        "avoid": ["Committing heavily either way"],
        "caution": "No regime read is itself information - conditions are "
                   "genuinely unclear.",
    },
}


def gather(conn, offset=0):
    """Compute regime inputs as of `offset` sessions back from the latest."""
    rows = conn.execute(
        "SELECT ts, close FROM history WHERE symbol=? AND resolution='1D' "
        "AND close IS NOT NULL ORDER BY epoch", (INDEX,)).fetchall()
    if len(rows) < 60:
        return None
    if offset:
        rows = rows[:-offset]
    dates = [r[0][:10] for r in rows]
    closes = [r[1] for r in rows]

    vol = realised_vol(closes)
    s50 = sma(closes, 50)
    trend_pct = ((closes[-1] - s50[-1]) / s50[-1] * 100) if s50[-1] else None

    # Breadth for the same date, computed from the stock universe.
    try:
        import breadth as br
        data = br.load_all(conn)
        series = br.compute_breadth(data, lookback=max(offset + 60, 60))
        target = dates[-1]
        match = next((r for r in reversed(series) if r["date"] == target), None)
        above50 = match["pct_above_50"] if match else None
        above200 = match["pct_above_200"] if match else None
    except Exception:
        above50 = above200 = None

    return {"date": dates[-1], "close": closes[-1], "vol": vol,
            "trend_pct": trend_pct, "above50": above50, "above200": above200}


def cmd_latest(conn, show_strategies):
    g = gather(conn)
    if not g:
        print("Not enough index history. Run:  py -3.12 backfill.py "
              "--days 365 --resolution 1D")
        return
    r = classify(g["vol"], g["trend_pct"], g["above50"], g["above200"])

    print("=" * 76)
    print(f"  MARKET REGIME - {g['date']}")
    print("=" * 76)
    print(f"\n  {r['label'].upper()}")
    print(f"\n  {r['summary']}")

    print(f"\nInputs")
    print(f"  Nifty close        {g['close']:>12,.2f}")
    print(f"  Realised vol (20d) {(f'{g['vol']:.1f}%' if g['vol'] else '-'):>12}"
          f"   {r['vol_state']}")
    print(f"  vs 50-DMA          {(f'{g['trend_pct']:+.1f}%' if g['trend_pct'] is not None else '-'):>12}"
          f"   {r['trend_state']}")
    print(f"  Above 50-DMA       {(f'{g['above50']:.0f}%' if g['above50'] is not None else '-'):>12}"
          f"   {r['breadth_state']}")
    print(f"  Above 200-DMA      {(f'{g['above200']:.0f}%' if g['above200'] is not None else '-'):>12}")

    print(f"\nWhy")
    for reason in r["reasons"]:
        print(f"  - {reason}")

    if show_strategies:
        fit = STRATEGY_FIT.get(r["label"], STRATEGY_FIT["Mixed"])
        print(f"\nStrategy fit for this regime")
        print(f"  Favoured : {', '.join(fit['favoured'])}")
        print(f"  Avoid    : {', '.join(fit['avoid'])}")
        print(f"  Caution  : {fit['caution']}")
        print("\n  These are priors from how the strategies work, not measured")
        print("  results. Once the journal has enough trades tagged by regime,")
        print("  they can be replaced with what actually happened.")

    print("\nVolatility here is realised (backward-looking), not implied.")
    print("It reacts after a shock rather than before one, so a calm reading")
    print("does not mean calm conditions ahead.\n")


def cmd_history(conn, n):
    print("=" * 78)
    print(f"  REGIME HISTORY - last {n} sessions")
    print("=" * 78)
    print(f"\n{'Date':<12} {'Close':>10} {'Vol':>7} {'vs50':>8} {'>50DMA':>8}  Regime")
    print("-" * 78)
    prev_label = None
    for off in range(n - 1, -1, -1):
        g = gather(conn, off)
        if not g:
            continue
        r = classify(g["vol"], g["trend_pct"], g["above50"], g["above200"])
        marker = "  <- changed" if prev_label and r["label"] != prev_label else ""
        print(f"{g['date']:<12} {g['close']:>10,.0f} "
              f"{(f'{g['vol']:.1f}' if g['vol'] else '-'):>7} "
              f"{(f'{g['trend_pct']:+.1f}%' if g['trend_pct'] is not None else '-'):>8} "
              f"{(f'{g['above50']:.0f}%' if g['above50'] is not None else '-'):>8}  "
              f"{r['label']}{marker}")
        prev_label = r["label"]
    print("-" * 78)
    print("\nFrequent regime changes usually mean the thresholds are too tight")
    print("for current conditions rather than that the market is genuinely")
    print("flipping daily.\n")


def main():
    ap = argparse.ArgumentParser(description="Market regime classification")
    ap.add_argument("--history", type=int, metavar="N")
    ap.add_argument("--strategies", action="store_true",
                    help="show which strategies suit this regime")
    args = ap.parse_args()

    if not HIST_DB.exists():
        print("market_history.db not found. Run backfill.py first.")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{HIST_DB}?mode=ro", uri=True)

    if args.history:
        cmd_history(conn, args.history)
    else:
        cmd_latest(conn, args.strategies)
    conn.close()


if __name__ == "__main__":
    main()
