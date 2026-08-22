#!/usr/bin/env python3
"""
Indian equity/F&O transaction cost model.

Costs decide whether a thin edge is real. A strategy showing +0.08R before
costs is not a strategy until you know what a round trip actually takes off
that number.

Rates below are the standard Indian intraday equity structure. They change
periodically - check against your own contract notes before trusting these
for anything that matters, and update RATES if they've moved.

Usage:
    py -3.12 cost_model.py                       # show cost breakdown
    py -3.12 cost_model.py --position 200000     # for a given position size
    py -3.12 cost_model.py --compare             # across position sizes
"""

import argparse

# Standard Indian intraday equity rates.
RATES = {
    # STT: intraday equity is charged on the SELL side only.
    "stt_sell": 0.00025,          # 0.025%
    # NSE exchange transaction charge, both sides.
    "exchange": 0.0000297,        # 0.00297%
    # Stamp duty: BUY side only.
    "stamp_buy": 0.00003,         # 0.003%
    # SEBI turnover fee, both sides.
    "sebi": 0.000001,             # Rs 10 per crore
    # GST applies to brokerage + exchange + SEBI charges.
    "gst": 0.18,
    # Discount-broker style: flat fee or a percentage, whichever is lower.
    "brokerage_flat": 20.0,
    "brokerage_pct": 0.0003,      # 0.03%
}

# Slippage is not a fee but it is a real cost, and for breakout entries it is
# usually the largest one. Entering as price pushes through a level means
# taking the worse side of the spread, often with momentum against you.
DEFAULT_SLIPPAGE_PCT = 0.0005     # 0.05% per side


def brokerage(turnover):
    return min(RATES["brokerage_flat"], turnover * RATES["brokerage_pct"])


def round_trip_cost(entry_price, exit_price, quantity,
                    slippage_pct=DEFAULT_SLIPPAGE_PCT):
    """Total cost of one buy + one sell, in rupees.

    Returns a dict broken down by component so nothing is hidden in a single
    number.
    """
    buy_turnover = entry_price * quantity
    sell_turnover = exit_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brok = brokerage(buy_turnover) + brokerage(sell_turnover)
    stt = sell_turnover * RATES["stt_sell"]
    exch = total_turnover * RATES["exchange"]
    stamp = buy_turnover * RATES["stamp_buy"]
    sebi = total_turnover * RATES["sebi"]
    gst = (brok + exch + sebi) * RATES["gst"]
    slip = total_turnover * slippage_pct

    total = brok + stt + exch + stamp + sebi + gst + slip
    return {
        "brokerage": brok, "stt": stt, "exchange": exch, "stamp": stamp,
        "sebi": sebi, "gst": gst, "slippage": slip, "total": total,
        "turnover": total_turnover,
        "pct_of_position": total / buy_turnover * 100 if buy_turnover else 0,
    }


def cost_in_R(entry_price, stop_price, quantity, slippage_pct=DEFAULT_SLIPPAGE_PCT):
    """Express round-trip cost as a fraction of the trade's risk (1R).

    This is the number that matters for a backtest: if costs are 0.2R and
    the strategy's edge is 0.08R, the strategy loses money.
    """
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0 or quantity <= 0:
        return None
    risk_rupees = risk_per_share * quantity
    costs = round_trip_cost(entry_price, entry_price, quantity, slippage_pct)
    return costs["total"] / risk_rupees


def main():
    ap = argparse.ArgumentParser(description="Indian transaction cost model")
    ap.add_argument("--position", type=float, default=100000,
                    help="position size in rupees (default 100000)")
    ap.add_argument("--price", type=float, default=1000,
                    help="share price (default 1000)")
    ap.add_argument("--risk-pct", type=float, default=0.8,
                    help="risk per trade as %% of price (default 0.8)")
    ap.add_argument("--slippage", type=float, default=0.05,
                    help="slippage per side in %% (default 0.05)")
    ap.add_argument("--compare", action="store_true",
                    help="compare across position sizes")
    args = ap.parse_args()

    slip = args.slippage / 100

    if args.compare:
        print("=" * 74)
        print("  ROUND-TRIP COST BY POSITION SIZE")
        print(f"  (price Rs {args.price:,.0f}, slippage {args.slippage}% per side)")
        print("=" * 74)
        print(f"\n{'Position':>12} {'Qty':>8} {'Cost Rs':>10} {'% of pos':>10} "
              f"{'Cost in R':>11}")
        print("-" * 74)
        for pos in [25000, 50000, 100000, 200000, 500000, 1000000]:
            qty = int(pos / args.price)
            if qty < 1:
                continue
            c = round_trip_cost(args.price, args.price, qty, slip)
            stop = args.price * (1 - args.risk_pct / 100)
            r = cost_in_R(args.price, stop, qty, slip)
            print(f"{pos:>12,.0f} {qty:>8,} {c['total']:>10,.0f} "
                  f"{c['pct_of_position']:>9.3f}% {r:>10.3f}R")
        print("-" * 74)
        print("\nSmaller positions carry proportionally more cost because the")
        print("flat brokerage fee does not scale down. Above roughly Rs 70,000")
        print("the percentage fee takes over and cost stabilises.")
        return

    qty = int(args.position / args.price)
    c = round_trip_cost(args.price, args.price, qty, slip)
    stop = args.price * (1 - args.risk_pct / 100)
    r = cost_in_R(args.price, stop, qty, slip)

    print("=" * 74)
    print("  ROUND-TRIP COST BREAKDOWN")
    print("=" * 74)
    print(f"\nPosition   : Rs {args.position:,.0f}  ({qty:,} shares at Rs {args.price:,.2f})")
    print(f"Risk       : {args.risk_pct}% of price = Rs {abs(args.price-stop)*qty:,.0f} (1R)")
    print(f"Slippage   : {args.slippage}% per side")
    print()
    print(f"  {'Component':<24} {'Rupees':>12}  {'% of position':>14}")
    print("  " + "-" * 54)
    for key in ["brokerage", "stt", "exchange", "stamp", "sebi", "gst", "slippage"]:
        v = c[key]
        print(f"  {key.capitalize():<24} {v:>12,.2f}  "
              f"{v/args.position*100:>13.4f}%")
    print("  " + "-" * 54)
    print(f"  {'TOTAL':<24} {c['total']:>12,.2f}  "
          f"{c['pct_of_position']:>13.4f}%")
    print()
    print(f"Cost expressed as risk: {r:.3f}R")
    print()
    print("Meaning: every round trip starts this far behind. A strategy needs")
    print(f"expectancy above +{r:.3f}R just to break even.")
    print()
    print("Note the largest single component is usually slippage, which is an")
    print("estimate rather than a published rate. For breakout entries it is")
    print("often worse than 0.05% because you are buying into momentum.")
    print()


if __name__ == "__main__":
    main()
