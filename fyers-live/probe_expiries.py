#!/usr/bin/env python3
"""
Probe: what expiries does the option chain endpoint expose?

The collector currently fetches only the current expiry, which means there
is nothing to compare across and no term structure to measure. The response
carries an "expiryData" block listing what else is available; this prints it
so the collector can be extended against the real format rather than a
guess.

Usage:
    py -3.12 probe_expiries.py
    py -3.12 probe_expiries.py --symbol NSE:NIFTYBANK-INDEX
"""

import sys
import json
import argparse
from datetime import date, datetime
from pathlib import Path

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3.")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
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

    print("=" * 74)
    print(f"  EXPIRY DISCOVERY - {args.symbol}")
    print("=" * 74)

    resp = client.optionchain({"symbol": args.symbol, "strikecount": 1,
                               "timestamp": "", "greeks": 1})
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        print(f"\nRequest failed: {resp}")
        sys.exit(1)

    data = resp.get("data") or {}
    print(f"\nTop-level keys: {list(data.keys())}")

    ed = data.get("expiryData")
    print(f"\nexpiryData type: {type(ed).__name__}")
    if isinstance(ed, list):
        print(f"Entries: {len(ed)}\n")
        print(f"  {'#':>3}  {'raw':<40} interpreted")
        print("  " + "-" * 66)
        for i, e in enumerate(ed):
            interp = ""
            if isinstance(e, dict):
                for k, v in e.items():
                    if str(v).isdigit() and len(str(v)) >= 10:
                        try:
                            interp = datetime.fromtimestamp(int(v)).date().isoformat()
                        except (ValueError, OSError):
                            pass
            print(f"  {i:>3}  {str(e)[:40]:<40} {interp}")
    else:
        print(f"  {ed}")

    # Try fetching a non-default expiry to confirm the parameter works
    if isinstance(ed, list) and len(ed) > 1:
        second = ed[1]
        stamp = None
        if isinstance(second, dict):
            for k, v in second.items():
                if str(v).isdigit() and len(str(v)) >= 10:
                    stamp = str(v)
                    key_used = k
                    break
        if stamp:
            print(f"\nTrying the second expiry with timestamp={stamp} "
                  f"(from field '{key_used}')...")
            r2 = client.optionchain({"symbol": args.symbol, "strikecount": 2,
                                     "timestamp": stamp, "greeks": 1})
            if isinstance(r2, dict) and r2.get("s") == "ok":
                chain = (r2.get("data") or {}).get("optionsChain") or []
                opts = [o for o in chain if o.get("option_type") in ("CE", "PE")]
                print(f"  OK - {len(opts)} contracts returned")
                if opts:
                    o = opts[0]
                    gk = o.get("greeks") or {}
                    print(f"  sample: {o.get('symbol')}  "
                          f"IV {gk.get('iv')}  strike {o.get('strike_price')}")
                print("\n  Multiple expiries are fetchable - term structure "
                      "is possible.")
            else:
                print(f"  FAILED: {r2 if not isinstance(r2, dict) else r2.get('message')}")
                print("\n  If this fails, only the front expiry is available "
                      "and term structure cannot be built from this feed.")

    print("\nSend this output back so the collector can be extended.\n")


if __name__ == "__main__":
    main()
