#!/usr/bin/env python3
"""
Diagnostic: dump the raw option chain response.

IV and Greeks came back as zeros, which means the field names in the real
response differ from what the collector expects. This prints exactly what
Fyers returns so the parsing can be fixed against reality rather than
another guess.

It also tries the greeks parameter as both an integer and a string, since
the SDK docstring describes it as a string while the obvious reading is an
integer.

Usage:
    py -3.12 probe_chain.py
    py -3.12 probe_chain.py --symbol NSE:NIFTYBANK-INDEX
"""

import sys
import json
import argparse
from datetime import date
from pathlib import Path

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3.")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"


def show_variant(client, symbol, greeks_value, label):
    print("=" * 74)
    print(f"  greeks={greeks_value!r}   ({label})")
    print("=" * 74)
    try:
        resp = client.optionchain({
            "symbol": symbol,
            "strikecount": 2,
            "timestamp": "",
            "greeks": greeks_value,
        })
    except Exception as e:
        print(f"  call failed: {type(e).__name__}: {e}\n")
        return None

    if not isinstance(resp, dict):
        print(f"  unexpected type: {type(resp)}\n")
        return None

    print(f"  status  : {resp.get('s')}")
    if resp.get("s") != "ok":
        print(f"  message : {resp.get('message')}\n")
        return None

    data = resp.get("data") or {}
    print(f"  top-level data keys: {list(data.keys())}")

    chain = data.get("optionsChain") or []
    print(f"  entries in chain   : {len(chain)}")
    if not chain:
        print()
        return None

    # Find a real option entry (not the underlying row)
    opt = next((o for o in chain
                if o.get("option_type") in ("CE", "PE")), None)
    if not opt:
        print("  no CE/PE entries found\n")
        return None

    print(f"\n  All keys on one option entry:")
    for k in sorted(opt.keys()):
        v = opt[k]
        shown = json.dumps(v) if not isinstance(v, str) else v
        if len(str(shown)) > 60:
            shown = str(shown)[:57] + "..."
        print(f"    {k:<22} {shown}")

    # Anything that looks like a Greek or IV, wherever it lives
    print(f"\n  Fields that look like IV or Greeks:")
    found = False
    for k, v in opt.items():
        kl = k.lower()
        if any(t in kl for t in ("iv", "greek", "delta", "gamma",
                                 "theta", "vega", "rho", "volat")):
            print(f"    {k:<22} = {v}")
            found = True
        if isinstance(v, dict):
            for k2, v2 in v.items():
                print(f"    {k}.{k2:<16} = {v2}")
                found = True
    if not found:
        print("    none present in this response")

    print()
    return opt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    args = ap.parse_args()

    if not TOKEN_FILE.exists():
        print("fyers_tokens.json not found.")
        sys.exit(1)
    tokens = json.loads(TOKEN_FILE.read_text())
    today = date.today().isoformat()
    fresh = [t for t in tokens.values() if t.get("generated_on") == today]
    if not fresh:
        print("No fresh tokens. Run token_manager.py first.")
        sys.exit(1)

    t = fresh[0]
    client = fyersModel.FyersModel(client_id=t["client_id"],
                                   token=t["access_token"],
                                   log_path=str(BASE_DIR))

    print(f"\nProbing {args.symbol}\n")
    a = show_variant(client, args.symbol, 1, "integer")
    b = show_variant(client, args.symbol, "1", "string")

    print("=" * 74)
    if a and b:
        ka, kb = set(a.keys()), set(b.keys())
        if ka == kb:
            print("Both variants return the same fields.")
        else:
            print(f"String-only fields : {sorted(kb - ka)}")
            print(f"Integer-only fields: {sorted(ka - kb)}")
    print("\nSend this output back so the parsing can be corrected.\n")


if __name__ == "__main__":
    main()
