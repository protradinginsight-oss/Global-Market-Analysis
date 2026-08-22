#!/usr/bin/env python3
"""
History depth probe - find out how far back Fyers data actually goes.

Before committing to a long backfill, this establishes what's genuinely
available per resolution. Brokers usually keep far less intraday history
than daily, and the limit is rarely documented clearly.

Uses a binary search per resolution rather than walking back year by year,
so it costs about a dozen calls instead of hundreds.

Usage:
    py -3.12 probe_history.py
    py -3.12 probe_history.py --symbol NSE:RELIANCE-EQ
    py -3.12 probe_history.py --resolutions 1D,60,15,5,1
"""

import sys
import json
import time
import argparse
from datetime import date, timedelta
from pathlib import Path

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3. Install with:  py -3.12 -m pip install fyers-apiv3")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"
RESULT_FILE = BASE_DIR / "history_depth.json"

SECONDS_BETWEEN_CALLS = 0.6


def has_data(client, symbol, resolution, start, end):
    """True if Fyers returns any candles for this window."""
    try:
        resp = client.history({
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(),
            "cont_flag": "1",
        })
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if not isinstance(resp, dict):
        return None, "unexpected response type"
    if resp.get("s") == "no_data":
        return False, "no_data"
    if resp.get("s") != "ok":
        return None, resp.get("message") or str(resp)[:120]
    return bool(resp.get("candles")), f"{len(resp.get('candles') or [])} candles"


def probe_resolution(client, symbol, resolution, max_years=15):
    """Binary search for the earliest date with data.

    Probes a 10-day window at each candidate date. A short window keeps
    responses small; the point is only whether anything exists there.
    """
    today = date.today()
    oldest = today - timedelta(days=365 * max_years)
    newest = today - timedelta(days=7)

    # First confirm recent data exists at all - if not, the symbol or
    # resolution is unusable and searching further is pointless.
    ok, note = has_data(client, symbol, resolution,
                        today - timedelta(days=10), today)
    time.sleep(SECONDS_BETWEEN_CALLS)
    if ok is None:
        return {"resolution": resolution, "status": "error", "detail": note}
    if not ok:
        # Might just be a quiet recent window; try a slightly longer one
        ok, note = has_data(client, symbol, resolution,
                            today - timedelta(days=30), today)
        time.sleep(SECONDS_BETWEEN_CALLS)
        if not ok:
            return {"resolution": resolution, "status": "no_recent_data",
                    "detail": note}

    lo, hi = oldest, newest      # lo = probably no data, hi = has data
    calls = 1
    while (hi - lo).days > 20:
        mid = lo + (hi - lo) / 2
        ok, note = has_data(client, symbol, resolution,
                            mid, mid + timedelta(days=10))
        calls += 1
        time.sleep(SECONDS_BETWEEN_CALLS)
        if ok is None:
            return {"resolution": resolution, "status": "error",
                    "detail": note, "calls": calls}
        if ok:
            hi = mid
        else:
            lo = mid

    depth_days = (date.today() - hi).days
    return {
        "resolution": resolution,
        "status": "ok",
        "earliest": hi.isoformat(),
        "depth_days": depth_days,
        "depth_years": round(depth_days / 365.25, 1),
        "calls": calls,
    }


def main():
    ap = argparse.ArgumentParser(description="Probe Fyers history depth")
    ap.add_argument("--symbol", default="NSE:RELIANCE-EQ",
                    help="symbol to probe (use an old, liquid one)")
    ap.add_argument("--resolutions", default="1D,240,60,30,15,5,1",
                    help="comma-separated resolutions to test")
    args = ap.parse_args()

    if not TOKEN_FILE.exists():
        print("fyers_tokens.json not found. Run token_manager.py first.")
        sys.exit(1)

    tokens = json.loads(TOKEN_FILE.read_text())
    today = date.today().isoformat()
    fresh = [t for t in tokens.values() if t.get("generated_on") == today]
    if not fresh:
        print("No fresh tokens. Run:  py -3.12 token_manager.py")
        sys.exit(1)

    t = fresh[0]
    client = fyersModel.FyersModel(client_id=t["client_id"],
                                   token=t["access_token"],
                                   log_path=str(BASE_DIR))

    resolutions = [r.strip() for r in args.resolutions.split(",") if r.strip()]

    print("=" * 74)
    print("  FYERS HISTORY DEPTH PROBE")
    print("=" * 74)
    print(f"\nSymbol: {args.symbol}")
    print(f"Testing resolutions: {', '.join(resolutions)}")
    print("\nBinary searching for the earliest available date per resolution.")
    print("This takes a minute or two.\n")

    results = []
    for res in resolutions:
        print(f"  probing {res:>4} ...", end=" ", flush=True)
        r = probe_resolution(client, args.symbol, res)
        results.append(r)
        if r["status"] == "ok":
            print(f"back to {r['earliest']}  ({r['depth_years']} years, "
                  f"{r['calls']} calls)")
        else:
            print(f"{r['status']}: {r.get('detail', '')}")

    RESULT_FILE.write_text(json.dumps(
        {"symbol": args.symbol, "probed_on": today, "results": results},
        indent=2))

    print()
    print("=" * 74)
    print(f"{'Resolution':<12} {'Earliest':<14} {'Depth':<12} Practical use")
    print("-" * 74)
    for r in results:
        if r["status"] != "ok":
            print(f"{r['resolution']:<12} {'-':<14} {'-':<12} {r['status']}")
            continue
        yrs = r["depth_years"]
        if yrs >= 5:
            use = "long-horizon backtests"
        elif yrs >= 1.5:
            use = "multi-regime testing"
        elif yrs >= 0.5:
            use = "recent behaviour only"
        else:
            use = "too short for backtesting"
        print(f"{r['resolution']:<12} {r['earliest']:<14} "
              f"{str(yrs) + ' years':<12} {use}")
    print("-" * 74)

    ok_results = [r for r in results if r["status"] == "ok"]
    if ok_results:
        deepest = max(ok_results, key=lambda r: r["depth_days"])
        print(f"\nDeepest history: {deepest['resolution']} back to "
              f"{deepest['earliest']} ({deepest['depth_years']} years)")

        intraday = [r for r in ok_results if r["resolution"] != "1D"]
        if intraday:
            best_intra = max(intraday, key=lambda r: r["depth_days"])
            print(f"Deepest intraday: {best_intra['resolution']}-minute back to "
                  f"{best_intra['earliest']} ({best_intra['depth_years']} years)")
            if best_intra["depth_years"] < 2:
                print("\nIntraday history is short. Any strategy needing bar")
                print("sequencing within the day is limited to this window,")
                print("regardless of how much daily history exists.")

    print(f"\nSaved: {RESULT_FILE.name}")
    print("\nNote: depth can vary by symbol. Recently listed stocks will have")
    print("less regardless of what this liquid name shows.\n")


if __name__ == "__main__":
    main()
