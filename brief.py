#!/usr/bin/env python3
"""
Morning brief - one read across everything.

The dashboard shows what each component holds. This asks the harder
question: do they agree?

Deliberately does NOT produce a single score or a direction to trade. The
measured relationships here are weak - the strongest global cue correlates
0.24 with next-day Nifty, explaining about 6% of daily variance - and
compressing weak signals into one confident number is how a tool starts
lying to you. What it does instead is lay the inputs side by side and say
plainly where they agree, where they conflict, and what is simply unknown.

Disagreement is the useful output. When flow, positioning, breadth and
global cues all point the same way, that is worth noticing. When they
don't, knowing that is more valuable than a number that averages them into
false confidence.

Usage:
    py -3.12 brief.py
    py -3.12 brief.py --symbol NSE:NIFTYBANK-INDEX
"""

import sys
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def find_root(start):
    """Locate the project root by looking for its component folders.

    An earlier version checked whether the folder was literally named
    "Global Trading Analysis", which worked on one machine and nowhere else.
    Detecting by structure means the brief runs from the root or from a
    subfolder without caring what anything is called.
    """
    markers = ("fyers-live", "global-premarket", "fii-dii-tracker")
    for candidate in (start, start.parent, start.parent.parent):
        if sum((candidate / m).is_dir() for m in markers) >= 2:
            return candidate
    return start


ROOT = find_root(BASE_DIR)
IST = timezone(timedelta(hours=5, minutes=30))

FYERS = ROOT / "fyers-live"
for p in (FYERS, ROOT / "global-premarket", ROOT / "events", ROOT / "journal"):
    sys.path.insert(0, str(p))


def ro(path):
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def age_hours(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return (datetime.now(IST) - dt).total_seconds() / 3600
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Each reader returns (verdict, detail, staleness_hours) or None
# verdict is +1 bullish, -1 bearish, 0 neutral, None unknown
# --------------------------------------------------------------------------

def _premarket_weights():
    """Extract INSTRUMENTS and WEIGHT_FLOOR from global_premarket.py source.

    Parsed with ast rather than imported, so the module's credential check
    never runs. Reading a config value should not require the API key that
    config is for.
    """
    src_path = ROOT / "global-premarket" / "global_premarket.py"
    if not src_path.exists():
        return None, 0.15
    try:
        import ast as _ast
        tree = _ast.parse(src_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None, 0.15

    weights, floor = {}, 0.15
    for node in tree.body:
        if not isinstance(node, _ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, _ast.Name):
                continue
            if target.id == "WEIGHT_FLOOR":
                try:
                    floor = _ast.literal_eval(node.value)
                except ValueError:
                    pass
            elif target.id == "INSTRUMENTS":
                try:
                    for entry in _ast.literal_eval(node.value):
                        if len(entry) >= 3:
                            weights[entry[1]] = entry[2]
                except ValueError:
                    return None, floor
    return (weights or None), floor


def read_premarket():
    conn = ro(ROOT / "global-premarket" / "global_premarket.db")
    if not conn:
        return None, "not collected", None
    ts = conn.execute("SELECT MAX(captured_at) FROM global_snapshot").fetchone()
    if not ts or not ts[0]:
        conn.close()
        return None, "no snapshots", None
    rows = conn.execute(
        "SELECT label, change_pct FROM global_snapshot WHERE captured_at=?",
        (ts[0],)).fetchall()
    conn.close()

    # The cue weights live in global_premarket.py, but importing it runs its
    # module-level config check and exits if no API key is present. The brief
    # only reads stored data and must never inherit another component's
    # startup requirements, so the weights are parsed out of the source
    # instead of imported.
    weights, floor = _premarket_weights()
    if weights is None:
        return None, "cue weights unavailable", age_hours(ts[0])

    num = den = 0.0
    used = 0
    for label, chg in rows:
        w = weights.get(label, 0)
        if chg is None or abs(w) < floor:
            continue
        num += chg * w
        den += abs(w)
        used += 1
    if not den:
        return None, "no weighted cues retrieved", age_hours(ts[0])
    score = num / den
    verdict = 0 if abs(score) < 0.15 else (1 if score > 0 else -1)
    return verdict, f"net {score:+.2f}% from {used} weighted cues", age_hours(ts[0])


def read_fii_dii():
    conn = ro(ROOT / "fii-dii-tracker" / "fii_dii.db")
    if not conn:
        return None, "not collected", None
    rows = conn.execute(
        "SELECT trade_date, category, net_value, fetched_at FROM fii_dii_flow "
        "ORDER BY fetched_at DESC LIMIT 4").fetchall()
    conn.close()
    if not rows:
        return None, "no data", None
    latest = rows[0][0]
    items = {r[1]: r[2] for r in rows if r[0] == latest}
    fii = next((v for k, v in items.items() if "FII" in k or "FPI" in k), None)
    dii = items.get("DII")
    if fii is None:
        return None, "no FII figure", age_hours(rows[0][3])
    verdict = 0 if abs(fii) < 500 else (1 if fii > 0 else -1)
    detail = f"FII {fii:+,.0f} cr"
    if dii is not None:
        detail += f", DII {dii:+,.0f} cr"
        if (fii > 0) != (dii > 0) and abs(fii) > 500 and abs(dii) > 500:
            detail += " (opposing)"
    return verdict, detail, age_hours(rows[0][3])


def read_chain(symbol):
    conn = ro(FYERS / "option_chains.db")
    if not conn:
        return None, "not collected", None
    try:
        import chain_analytics as ca
    except ImportError:
        conn.close()
        return None, "chain_analytics unavailable", None
    ts = ca.latest_snapshot(conn, symbol)
    if not ts:
        conn.close()
        return None, f"no data for {symbol}", None
    chain, spot = ca.load_chain(conn, symbol, ts)
    conn.close()
    if not chain:
        return None, "empty chain", age_hours(ts)
    p = ca.pcr(chain)
    mp, _ = ca.max_pain(chain)
    aiv = ca.atm_iv(chain, spot)
    pcr_v = p["pcr_oi"]
    if pcr_v is None:
        return None, "no PCR", age_hours(ts)
    # Convention: high PCR means heavy put writing, read as bullish.
    verdict = 0 if 0.8 <= pcr_v <= 1.2 else (1 if pcr_v > 1.2 else -1)
    detail = f"PCR {pcr_v:.2f}"
    if mp and spot:
        detail += f", max pain {mp:,.0f} ({(mp-spot)/spot*100:+.1f}%)"
    if aiv:
        detail += f", ATM IV {aiv:.1f}%"
    return verdict, detail, age_hours(ts)


def read_buildup(symbol):
    conn = ro(FYERS / "option_chains.db")
    if not conn:
        return None, "not collected", None
    try:
        import buildup as bu
    except ImportError:
        conn.close()
        return None, "buildup unavailable", None
    ts = bu.latest_snapshot(conn, symbol)
    if not ts:
        conn.close()
        return None, "no data", None
    legs, spot = bu.load(conn, symbol, ts)
    conn.close()
    bull = bear = 0
    for leg in legs:
        if (leg.get("volume") or 0) < bu.MIN_LEG_VOLUME:
            leg["category"] = "Untraded"
            continue
        cat = bu.classify(leg["price_chg"], leg["oi_chg_pct"])
        leg["category"] = cat
        lean = bu.implication(cat, leg["type"])
        if lean == "bullish":
            bull += abs(leg["oi_chg"] or 0)
        elif lean == "bearish":
            bear += abs(leg["oi_chg"] or 0)
    usable, reasons, q = bu.assess_quality(legs)
    total = bull + bear
    if total == 0:
        return None, "no classifiable activity", age_hours(ts)
    if not usable:
        return None, f"too thin ({reasons[0] if reasons else ''})", age_hours(ts)
    gap = (bull - bear) / total * 100
    verdict = 0 if abs(gap) < 10 else (1 if gap > 0 else -1)
    return verdict, (f"{'bullish' if gap > 0 else 'bearish'} tilt "
                     f"{abs(gap):.0f}%"), age_hours(ts)


def read_breadth():
    conn = ro(FYERS / "market_history.db")
    if not conn:
        return None, "no history", None
    try:
        import breadth as br
    except ImportError:
        conn.close()
        return None, "breadth unavailable", None
    try:
        data = br.load_all(conn)
        series = br.compute_breadth(data, lookback=10)
    except Exception as e:
        conn.close()
        return None, f"failed: {type(e).__name__}", None
    conn.close()
    if not series:
        return None, "not enough history", None
    r = series[-1]
    ratio = r["ad_ratio"]
    if ratio is None:
        return None, "no A/D data", None
    verdict = 0 if 0.8 <= ratio <= 1.25 else (1 if ratio > 1.25 else -1)
    detail = f"A/D {ratio:.2f} ({r['advances']}/{r['declines']})"
    if r["pct_above_50"] is not None:
        detail += f", {r['pct_above_50']:.0f}% above 50-DMA"
    return verdict, detail, None


def read_regime():
    conn = ro(FYERS / "market_history.db")
    if not conn:
        return None, "no history", None
    try:
        import regime as rg
    except ImportError:
        conn.close()
        return None, "regime unavailable", None
    try:
        g = rg.gather(conn)
    except Exception as e:
        conn.close()
        return None, f"failed: {type(e).__name__}", None
    conn.close()
    if not g:
        return None, "not enough history", None
    r = rg.classify(g["vol"], g["trend_pct"], g["above50"], g["above200"])
    bullish = {"Steady uptrend", "Volatile uptrend"}
    bearish = {"Volatile downtrend", "Orderly decline"}
    verdict = (1 if r["label"] in bullish else
               -1 if r["label"] in bearish else 0)
    detail = r["label"]
    if g["vol"]:
        detail += f", vol {g['vol']:.1f}%"
    return verdict, detail, None


def read_events():
    conn = ro(ROOT / "events" / "events.db")
    if not conn:
        return None, "not collected", None
    today = date.today().isoformat()
    week = (date.today() + timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT symbol, event_date, event_type, purpose FROM events "
        "WHERE event_date>=? AND event_date<=? AND event_type NOT IN "
        "('other','board_meeting') ORDER BY event_date", (today, week)).fetchall()
    conn.close()
    try:
        import events as ev
        uni = ev.fno_universe()
        if uni:
            rows = [r for r in rows if r[0].upper() in uni]
    except Exception:
        pass
    if not rows:
        return 0, "nothing scheduled this week", None
    # Events are a risk flag, not a direction.
    names = ", ".join(f"{r[0]} ({r[2]})" for r in rows[:4])
    if len(rows) > 4:
        names += f" +{len(rows)-4} more"
    return None, names, None


def main():
    ap = argparse.ArgumentParser(description="Morning brief")
    ap.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    args = ap.parse_args()

    now = datetime.now(IST)
    print("=" * 78)
    print(f"  MORNING BRIEF   {now.strftime('%A %d %B %Y, %H:%M')} IST")
    print(f"  {args.symbol}")
    print("=" * 78)

    checks = [
        ("Global cues",   read_premarket()),
        ("FII/DII flow",  read_fii_dii()),
        ("Options chain", read_chain(args.symbol)),
        ("OI buildup",    read_buildup(args.symbol)),
        ("Market breadth", read_breadth()),
        ("Regime",        read_regime()),
    ]

    print(f"\n{'Input':<16} {'Read':<10} Detail")
    print("-" * 78)
    bull = bear = neut = unknown = 0
    stale = []
    for name, (verdict, detail, age) in checks:
        if verdict is None:
            label = "unknown"
            unknown += 1
        elif verdict > 0:
            label = "bullish"
            bull += 1
        elif verdict < 0:
            label = "bearish"
            bear += 1
        else:
            label = "neutral"
            neut += 1
        if age is not None and age > 18:
            stale.append((name, age))
            detail += f"  [{age:.0f}h old]"
        print(f"{name:<16} {label:<10} {detail}")
    print("-" * 78)

    ev_verdict, ev_detail, _ = read_events()
    print(f"\nEvents this week: {ev_detail}")

    # The synthesis - deliberately not a score
    print(f"\n{'=' * 78}")
    directional = bull + bear
    print(f"  {bull} bullish, {bear} bearish, {neut} neutral, "
          f"{unknown} unknown")

    if unknown >= 4:
        print("\n  Too little data to read anything. Most components have not")
        print("  collected yet - run them and try again.")
    elif directional == 0:
        print("\n  No directional lean from any input. That is a real reading,")
        print("  not a missing one: conditions are genuinely undecided.")
    elif bull and not bear:
        print(f"\n  All {bull} directional inputs lean bullish, with {neut}")
        print("  neutral. Agreement across independent measures is worth more")
        print("  than any one of them - though see the caveat below.")
    elif bear and not bull:
        print(f"\n  All {bear} directional inputs lean bearish, with {neut}")
        print("  neutral. Agreement across independent measures is worth more")
        print("  than any one of them - though see the caveat below.")
    else:
        print(f"\n  CONFLICTING. {bull} bullish against {bear} bearish.")
        print("  These measure different things and are disagreeing, which is")
        print("  itself information: whatever edge exists today is not a")
        print("  directional one.")

    if stale:
        print(f"\n  {len(stale)} input(s) running on old data:")
        for name, age in stale:
            print(f"    {name} - {age:.0f} hours")
        print("  Treat those reads as historical rather than current.")

    print("\n  These inputs are weak individually. The strongest measured")
    print("  relationship in this system is 0.24 correlation between global")
    print("  cues and next-day Nifty, which explains about 6% of daily")
    print("  variance. Nothing here forecasts a direction; it describes")
    print("  conditions. Position sizing and risk still do the work.")
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
