#!/usr/bin/env python3
"""
Merge MCX contracts into the collector's subscription universe.

The NSE universe is built by build_universe.py and the MCX one by
mcx_setup.py. This combines them so the live collector picks up both,
assigning MCX to a single shard rather than spreading it around.

Why one shard: MCX trades until 23:30 while NSE closes at 15:30. Keeping
commodities together means exactly one worker needs to stay alive into the
evening; the other three can stop at the NSE close as they do now. Spreading
17 MCX contracts across four shards would force all four to run eight hours
longer for very little data.

Usage:
    py -3.12 merge_universe.py            # merge, assigning MCX to the last shard
    py -3.12 merge_universe.py --shard acc4-mdkansari
    py -3.12 merge_universe.py --status   # what's currently merged
    py -3.12 merge_universe.py --remove   # strip MCX back out
"""

import sys
import json
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
MCX_FILE = BASE_DIR / "mcx_universe.json"
BSE_FILE = BASE_DIR / "bse_universe.json"

MAX_PER_CONNECTION = 200


def load(path, what):
    if not path.exists():
        print(f"{path.name} not found - run {what} first.")
        sys.exit(1)
    return json.loads(path.read_text())


def cmd_status(uni):
    print("=" * 74)
    print("  UNIVERSE COMPOSITION")
    print("=" * 74)
    total_mcx = total_bse = 0
    print(f"\n{'Shard':<26} {'Total':>7} {'NSE':>6} {'MCX':>6} {'BSE':>6}  Capacity")
    print("-" * 74)
    for label, syms in uni["shards"].items():
        mcx = sum(1 for s in syms if s.startswith("MCX:"))
        bse = sum(1 for s in syms if s.startswith("BSE:"))
        nse = len(syms) - mcx - bse
        total_mcx += mcx
        total_bse += bse
        head = MAX_PER_CONNECTION - len(syms)
        print(f"{label:<26} {len(syms):>7} {nse:>6} {mcx:>6} {bse:>6}  "
              f"{head} slots free")
    print("-" * 74)
    total = sum(len(s) for s in uni["shards"].values())
    print(f"{'TOTAL':<26} {total:>7} {total-total_mcx-total_bse:>6} "
          f"{total_mcx:>6} {total_bse:>6}")
    if total_mcx:
        print(f"\nMCX is merged. The shard holding it must run until 23:30;")
        print("the others can stop at the NSE close.")
    else:
        print("\nNo MCX contracts merged yet.")
    print()


def main():
    ap = argparse.ArgumentParser(description="Merge MCX into the collector universe")
    ap.add_argument("--shard", help="which shard gets MCX (default: the last one)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--remove", action="store_true", help="strip MCX back out")
    args = ap.parse_args()

    uni = load(UNIVERSE_FILE, "build_universe.py")

    if args.status:
        cmd_status(uni)
        return

    if args.remove:
        removed = 0
        for label, syms in uni["shards"].items():
            before = len(syms)
            uni["shards"][label] = [s for s in syms
                                    if not (s.startswith("MCX:") or s.startswith("BSE:"))]
            removed += before - len(uni["shards"][label])
        uni["all"] = [a for a in uni["all"]
                      if not (a["ticker"].startswith("MCX:")
                              or a["ticker"].startswith("BSE:"))]
        uni["total_symbols"] = sum(len(s) for s in uni["shards"].values())
        UNIVERSE_FILE.write_text(json.dumps(uni, indent=2))
        print(f"Removed {removed} MCX contracts from the universe.")
        cmd_status(uni)
        return

    extra = []          # (ticker, underlying, kind)
    if MCX_FILE.exists():
        for c in json.loads(MCX_FILE.read_text())["contracts"]:
            extra.append((c["ticker"], c["underlying"], "commodity"))
    if BSE_FILE.exists():
        for c in json.loads(BSE_FILE.read_text())["contracts"]:
            extra.append((c["ticker"], c["underlying"], c.get("kind", "index")))
    if not extra:
        print("Neither mcx_universe.json nor bse_universe.json found.")
        print("Run mcx_setup.py --build and/or bse_setup.py --build first.")
        sys.exit(1)
    mcx_tickers = [t for t, _, _ in extra]

    # Strip any previous merge so re-running doesn't accumulate duplicates
    # or leave stale expired contracts behind.
    def is_extra(sym):
        return sym.startswith("MCX:") or sym.startswith("BSE:")
    for label, syms in uni["shards"].items():
        uni["shards"][label] = [s for s in syms if not is_extra(s)]
    uni["all"] = [a for a in uni["all"] if not is_extra(a["ticker"])]

    target = args.shard or list(uni["shards"].keys())[-1]
    if target not in uni["shards"]:
        print(f"No shard named '{target}'.")
        print("Shards are: " + ", ".join(uni["shards"].keys()))
        sys.exit(1)

    room = MAX_PER_CONNECTION - len(uni["shards"][target])
    if len(mcx_tickers) > room:
        print(f"Shard '{target}' has only {room} free slots but MCX needs "
              f"{len(mcx_tickers)}.")
        print("Pick a different shard with --shard, or trim the WANTED list "
              "in mcx_setup.py.")
        sys.exit(1)

    uni["shards"][target].extend(mcx_tickers)
    for ticker, underlying, kind in extra:
        uni["all"].append({"underlying": underlying, "ticker": ticker,
                           "kind": kind})
    uni["total_symbols"] = sum(len(s) for s in uni["shards"].values())
    uni["mcx_shard"] = target
    UNIVERSE_FILE.write_text(json.dumps(uni, indent=2))

    n_mcx = sum(1 for t in mcx_tickers if t.startswith("MCX:"))
    n_bse = sum(1 for t in mcx_tickers if t.startswith("BSE:"))
    print(f"Merged {n_mcx} MCX + {n_bse} BSE entries into '{target}'.\n")
    cmd_status(uni)
    print(f"IMPORTANT: '{target}' now needs to run until 23:30, not 15:30.")
    print("The collector reads market hours per symbol, so this is handled -")
    print("but that worker will legitimately stay alive into the evening.\n")
    print("MCX contracts expire frequently. Re-run mcx_setup.py --explore")
    print("--build and then this script whenever they roll over.\n")


if __name__ == "__main__":
    main()
