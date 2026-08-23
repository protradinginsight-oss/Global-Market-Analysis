#!/usr/bin/env python3
"""
Unified dashboard - everything in one view.

Reads the databases the other components produce and renders a single HTML
page. Nothing here collects data or calls an API; if a component hasn't run,
its panel says so rather than showing stale numbers as if they were current.

That last point is the main design constraint. A dashboard that displays a
three-day-old FII/DII figure without saying so is worse than no dashboard,
because you'd act on it. Every panel carries the age of its data.

Usage:
    py -3.12 dashboard.py              # build and open in browser
    py -3.12 dashboard.py --no-open    # build only
    py -3.12 dashboard.py --text       # print to terminal instead
"""

import sys
import json
import sqlite3
import argparse
import webbrowser
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
OUT_HTML = BASE_DIR / "dashboard.html"

IST = timezone(timedelta(hours=5, minutes=30))

SOURCES = {
    "fii_dii":    ROOT / "fii-dii-tracker" / "fii_dii.db",
    "premarket":  ROOT / "global-premarket" / "global_premarket.db",
    "chains":     ROOT / "fyers-live" / "option_chains.db",
    "candles":    ROOT / "fyers-live" / "market_candles.db",
    "history":    ROOT / "fyers-live" / "market_history.db",
    "global":     ROOT / "global-history" / "global_history.db",
    "journal":    ROOT / "journal" / "trade_journal.db",
}


def ro(path):
    """Open read-only so the dashboard can never disturb a live collector."""
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


# How old is too old differs completely by panel. FII/DII is published once
# each evening, so 15 hours is normal. A pre-market read 15 hours old was
# taken before the US close and has missed the move it exists to capture.
# Using one threshold for both would colour them identically.
FRESHNESS_RULES = {
    # panel key: (fresh_mins, recent_mins, aging_mins)
    "premarket":  (90, 240, 600),        # wants to be this morning's
    "fii_dii":    (900, 1440, 2880),     # daily EOD, a day old is fine
    "chains":     (10, 60, 240),         # intraday, should be minutes
    "buildup":    (10, 60, 240),
    "collector":  (3, 15, 60),           # live feed, should be seconds
}


def age_str(ts_str, rule=None):
    """How old is this, in words, judged against what's normal for the panel."""
    if not ts_str:
        return "no data", "stale"
    try:
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
    except ValueError:
        try:
            dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            return ts_str[:19], "unknown"
    delta = datetime.now(IST) - dt
    mins = delta.total_seconds() / 60

    if mins < 2:
        words = "just now"
    elif mins < 90:
        words = f"{mins:.0f} min ago"
    elif mins < 1440:
        words = f"{mins/60:.0f} hr ago"
    else:
        d = mins / 1440
        words = f"{d:.0f} day{'s' if d >= 2 else ''} ago"

    fresh_m, recent_m, aging_m = FRESHNESS_RULES.get(rule, (60, 720, 1440))
    if mins <= fresh_m:
        return words, "fresh"
    if mins <= recent_m:
        return words, "recent"
    if mins <= aging_m:
        return words, "aging"
    return words, "stale"


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

def panel_fii_dii():
    conn = ro(SOURCES["fii_dii"])
    if not conn:
        return {"title": "FII / DII flow", "missing":
                "Not collected yet. Run fii_dii_tracker.py"}
    rows = conn.execute(
        "SELECT trade_date, category, buy_value, sell_value, net_value, fetched_at "
        "FROM fii_dii_flow ORDER BY fetched_at DESC LIMIT 4").fetchall()
    conn.close()
    if not rows:
        return {"title": "FII / DII flow", "missing": "No rows stored yet."}

    latest_date = rows[0][0]
    items = [r for r in rows if r[0] == latest_date]
    age, freshness = age_str(items[0][5], "fii_dii")
    body = []
    for _, cat, buy, sell, net, _ in items:
        body.append({
            "label": cat,
            "value": f"{net:+,.0f} cr",
            "sub": f"buy {buy:,.0f} / sell {sell:,.0f}",
            "tone": "pos" if net > 0 else "neg",
        })
    return {"title": "FII / DII flow", "subtitle": f"for {latest_date}",
            "age": age, "freshness": freshness, "rows": body,
            "note": "NSE publishes provisional figures each evening. There is "
                    "no intraday FII/DII feed for anyone."}


def panel_premarket():
    conn = ro(SOURCES["premarket"])
    if not conn:
        return {"title": "Global pre-market", "missing":
                "Not collected yet. Run global_premarket.py"}
    ts = conn.execute("SELECT MAX(captured_at) FROM global_snapshot").fetchone()
    if not ts or not ts[0]:
        conn.close()
        return {"title": "Global pre-market", "missing": "No snapshots stored."}
    rows = conn.execute(
        "SELECT label, price, change_pct FROM global_snapshot "
        "WHERE captured_at=? ORDER BY ABS(change_pct) DESC", (ts[0],)).fetchall()
    conn.close()
    age, freshness = age_str(ts[0], "premarket")

    # Which cues actually carry weight, from the pre-market component itself.
    # Sorting purely by size of move puts gold at the top on a day it moved
    # 2% - visually dominant, and weighted zero. The distinction has to be
    # visible or the panel undoes the work of measuring the weights.
    weights = {}
    weights_ok = False
    try:
        sys.path.insert(0, str(ROOT / "global-premarket"))
        import global_premarket as gp
        weights = {label: w for _, label, w, _ in gp.INSTRUMENTS}
        floor = gp.WEIGHT_FLOOR
        weights_ok = True
    except Exception:
        floor = 0.15

    def is_weighted(label):
        return abs(weights.get(label, 0)) >= floor

    if not weights_ok:
        # Without the weights every cue would be labelled "context only",
        # which is wrong and would quietly undo the point of measuring them.
        ordered = rows
        body = [{"label": l, "value": f"{c:+.2f}%" if c is not None else "-",
                 "sub": f"{p:,.2f}",
                 "tone": "pos" if (c or 0) > 0 else "neg" if (c or 0) < 0 else ""}
                for l, p, c in rows]
        return {"title": "Global pre-market", "age": age,
                "freshness": freshness, "rows": body,
                "note": "Could not read the cue weights from "
                        "global_premarket.py, so weighted and context-only "
                        "cues are not distinguished here. The moves shown "
                        "are correct; their relative importance is not."}

    ordered = ([r for r in rows if is_weighted(r[0])] +
               [r for r in rows if not is_weighted(r[0])])
    body = []
    for l, p, c in ordered:
        w = weights.get(l)
        if is_weighted(l):
            sub = f"{p:,.2f}   weight {w:+.2f}"
        else:
            sub = f"{p:,.2f}   context only"
        body.append({"label": l, "value": f"{c:+.2f}%" if c is not None else "-",
                     "sub": sub,
                     "tone": "pos" if (c or 0) > 0 else "neg" if (c or 0) < 0 else ""})
    return {"title": "Global pre-market", "age": age, "freshness": freshness,
            "rows": body,
            "note": "Weights are measured lagged correlations with next-day "
                    "Nifty. The strongest is 0.24, so these cues explain "
                    "roughly 6% of daily variance - context, not direction."}


def panel_chains():
    conn = ro(SOURCES["chains"])
    if not conn:
        return {"title": "Option chains", "missing":
                "Not collected yet. Run option_chain.py"}
    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot "
        "WHERE underlying LIKE '%INDEX%' ORDER BY underlying")]
    if not unds:
        conn.close()
        return {"title": "Option chains", "missing": "No chain data stored."}

    sys.path.insert(0, str(ROOT / "fyers-live"))
    try:
        import chain_analytics as ca
    except ImportError:
        conn.close()
        return {"title": "Option chains", "missing":
                "chain_analytics.py not found."}

    body = []
    newest = None
    for u in unds:
        ts = ca.latest_snapshot(conn, u)
        if not ts:
            continue
        newest = max(newest or ts, ts)
        chain, spot = ca.load_chain(conn, u, ts)
        if not chain:
            continue
        p = ca.pcr(chain)
        mp, _ = ca.max_pain(chain)
        aiv = ca.atm_iv(chain, spot)
        short = u.replace("NSE:", "").replace("-INDEX", "")
        pcr_v = p["pcr_oi"]

        # A PCR of 5.03 on a barely-traded index is arithmetic, not
        # positioning. The buildup panel already refuses to call these;
        # showing a confident PCR here would contradict it.
        vol = conn.execute(
            "SELECT SUM(volume) FROM chain_snapshot WHERE underlying=? "
            "AND snapshot_ts=?", (u, ts)).fetchone()[0] or 0
        thin = vol < 1_000_000

        sub = (f"PCR {pcr_v:.2f}" if pcr_v else "PCR -") + \
              (f" | MaxPain {mp:,.0f}" if mp else "") + \
              (f" | IV {aiv:.1f}%" if aiv else " | IV -")
        if thin:
            sub += "  (thin - treat PCR with caution)"

        body.append({
            "label": short + (" *" if thin else ""),
            "value": f"{spot:,.0f}" if spot else "-",
            "sub": sub,
            "tone": "",
        })
    conn.close()
    age, freshness = age_str(newest, "chains")
    return {"title": "Option chains", "age": age, "freshness": freshness,
            "rows": body,
            "note": "PCR above ~1.3 suggests bearish positioning, below ~0.7 "
                    "bullish. Max Pain is a reference level from outstanding "
                    "OI, not a forecast. Entries marked * have too little "
                    "volume for their ratios to mean much."}


def panel_buildup():
    conn = ro(SOURCES["chains"])
    if not conn:
        return {"title": "OI buildup", "missing": "No chain data."}
    sys.path.insert(0, str(ROOT / "fyers-live"))
    try:
        import buildup as bu
    except ImportError:
        conn.close()
        return {"title": "OI buildup", "missing": "buildup.py not found."}

    unds = [r[0] for r in conn.execute(
        "SELECT DISTINCT underlying FROM chain_snapshot "
        "WHERE underlying LIKE '%INDEX%' ORDER BY underlying")]
    body = []
    newest = None
    for u in unds:
        ts = bu.latest_snapshot(conn, u)
        if not ts:
            continue
        newest = max(newest or ts, ts)
        legs, spot = bu.load(conn, u, ts)
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
        short = u.replace("NSE:", "").replace("-INDEX", "")
        if total == 0:
            body.append({"label": short, "value": "-", "sub": "no activity"})
        elif not usable:
            body.append({"label": short, "value": "too thin",
                         "sub": f"vol {q['volume']:,}", "tone": ""})
        else:
            gap = (bull - bear) / total * 100
            body.append({
                "label": short,
                "value": ("balanced" if abs(gap) < 10 else
                          f"{'bull' if gap > 0 else 'bear'} {abs(gap):.0f}%"),
                "sub": f"OI moved {q['oi_change']:,}",
                "tone": "pos" if gap > 10 else "neg" if gap < -10 else "",
            })
    conn.close()
    age, freshness = age_str(newest, "buildup")
    return {"title": "OI buildup", "age": age, "freshness": freshness,
            "rows": body,
            "note": "OI counts contracts, not intent - rising call OI could "
                    "be buying or covered writing. Thin instruments are shown "
                    "as 'too thin' rather than given a false direction."}


def panel_collector():
    conn = ro(SOURCES["candles"])
    if not conn:
        return {"title": "Live collector", "missing": "Has not run yet."}
    row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(minute_ts) "
        "FROM candles_1m").fetchone()
    conn.close()
    if not row or not row[0]:
        return {"title": "Live collector", "missing": "No candles stored."}
    n, syms, latest = row
    age, freshness = age_str(latest, "collector")
    return {"title": "Live collector", "age": age, "freshness": freshness,
            "rows": [
                {"label": "candles stored", "value": f"{n:,}"},
                {"label": "symbols seen", "value": f"{syms}"},
                {"label": "last write", "value": (latest or "")[11:19]},
            ],
            "note": "During market hours this should be seconds old. Anything "
                    "over a few minutes means a feed has stalled."}


def panel_journal():
    conn = ro(SOURCES["journal"])
    if not conn:
        return {"title": "Trade journal", "missing":
                "No trades recorded yet."}
    try:
        closed = conn.execute(
            "SELECT net_pnl, r_multiple FROM trades WHERE status='CLOSED'"
        ).fetchall()
        n_open = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
    except sqlite3.Error:
        conn.close()
        return {"title": "Trade journal", "missing": "No trades recorded yet."}
    conn.close()
    if not closed and not n_open:
        return {"title": "Trade journal", "missing": "No trades recorded yet."}

    pnls = [c[0] for c in closed if c[0] is not None]
    rs = [c[1] for c in closed if c[1] is not None]
    body = [{"label": "open positions", "value": str(n_open)}]
    if pnls:
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        body.append({"label": "closed trades", "value": str(len(pnls))})
        body.append({"label": "net P&L", "value": f"Rs {total:,.0f}",
                     "tone": "pos" if total > 0 else "neg"})
        body.append({"label": "win rate",
                     "value": f"{wins/len(pnls)*100:.0f}%"})
        if rs:
            exp = sum(rs) / len(rs)
            body.append({"label": "expectancy", "value": f"{exp:+.3f}R",
                         "tone": "pos" if exp > 0 else "neg"})
    note = ("P&L is net of modelled costs." if pnls else
            "Record trades with journal.py to populate this.")
    if pnls and len(pnls) < 30:
        note += f" Only {len(pnls)} trades - not statistically meaningful yet."
    return {"title": "Trade journal", "rows": body, "note": note}


def panel_data_coverage():
    body = []
    for name, path in [("India history", SOURCES["history"]),
                       ("Global history", SOURCES["global"])]:
        conn = ro(path)
        if not conn:
            body.append({"label": name, "value": "missing"})
            continue
        try:
            if name.startswith("India"):
                r = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM history"
                ).fetchone()
            else:
                r = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT ticker) FROM prices"
                ).fetchone()
            size = path.stat().st_size / (1024 * 1024)
            body.append({"label": name, "value": f"{r[0]:,} rows",
                         "sub": f"{r[1]} instruments, {size:.0f} MB"})
        except sqlite3.Error:
            body.append({"label": name, "value": "unreadable"})
        conn.close()
    return {"title": "Stored history", "rows": body,
            "note": "Backfilled data, not live. Used by backtests and "
                    "correlation analysis."}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_text(panels):
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    out = ["=" * 74, f"  TRADING DASHBOARD   {now} IST", "=" * 74]
    for p in panels:
        out.append("")
        head = f"  {p['title']}"
        if p.get("subtitle"):
            head += f"  ({p['subtitle']})"
        if p.get("age"):
            head += f"   [{p['age']}]"
        out.append(head)
        out.append("  " + "-" * 70)
        if p.get("missing"):
            out.append(f"    {p['missing']}")
            continue
        for r in p.get("rows", []):
            line = f"    {r['label']:<26} {r['value']:>16}"
            if r.get("sub"):
                line += f"   {r['sub']}"
            out.append(line)
        if p.get("note"):
            out.append(f"    note: {p['note']}")
    out.append("")
    out.append("=" * 74)
    return "\n".join(out)


def render_html(panels):
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    cards = []
    for p in panels:
        fresh_cls = p.get("freshness", "")
        age_html = (f'<span class="age {fresh_cls}">{p["age"]}</span>'
                    if p.get("age") else "")
        if p.get("missing"):
            inner = f'<p class="missing">{p["missing"]}</p>'
        else:
            rows = []
            for r in p.get("rows", []):
                tone = r.get("tone", "")
                sub = f'<div class="sub">{r["sub"]}</div>' if r.get("sub") else ""
                rows.append(
                    f'<div class="row"><div class="lbl">{r["label"]}{sub}</div>'
                    f'<div class="val {tone}">{r["value"]}</div></div>')
            inner = "".join(rows)
        note = f'<p class="note">{p["note"]}</p>' if p.get("note") else ""
        sub_t = f' <span class="subtitle">{p["subtitle"]}</span>' if p.get("subtitle") else ""
        cards.append(f'''<section class="card">
  <header><h2>{p["title"]}{sub_t}</h2>{age_html}</header>
  {inner}{note}
</section>''')

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Trading Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {{
    --bg:#12131a; --card:#1b1d27; --line:#2a2d3a;
    --text:#e6e7ee; --dim:#8b8fa3; --pos:#4ade80; --neg:#f87171;
    --fresh:#4ade80; --recent:#a3e635; --aging:#fbbf24; --stale:#f87171;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text);
    font:14px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace; }}
  h1 {{ font-size:18px; font-weight:600; margin:0 0 4px; letter-spacing:.02em; }}
  .stamp {{ color:var(--dim); font-size:12px; margin-bottom:20px; }}
  .grid {{ display:grid; gap:14px;
    grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }}
  .card {{ background:var(--card); border:1px solid var(--line);
    border-radius:8px; padding:14px 16px; }}
  .card header {{ display:flex; justify-content:space-between;
    align-items:baseline; margin-bottom:10px;
    padding-bottom:8px; border-bottom:1px solid var(--line); }}
  h2 {{ font-size:13px; font-weight:600; margin:0; letter-spacing:.04em;
    text-transform:uppercase; }}
  .subtitle {{ color:var(--dim); font-weight:400; text-transform:none;
    letter-spacing:0; }}
  .age {{ font-size:11px; padding:2px 7px; border-radius:10px;
    background:#00000040; }}
  .age.fresh {{ color:var(--fresh); }} .age.recent {{ color:var(--recent); }}
  .age.aging {{ color:var(--aging); }} .age.stale {{ color:var(--stale); }}
  .row {{ display:flex; justify-content:space-between; align-items:flex-start;
    padding:5px 0; }}
  .row + .row {{ border-top:1px solid #ffffff08; }}
  .lbl {{ color:var(--text); }}
  .sub {{ color:var(--dim); font-size:11px; }}
  .val {{ font-variant-numeric:tabular-nums; white-space:nowrap;
    padding-left:14px; }}
  .val.pos {{ color:var(--pos); }} .val.neg {{ color:var(--neg); }}
  .note {{ color:var(--dim); font-size:11px; margin:10px 0 0;
    padding-top:8px; border-top:1px solid var(--line); }}
  .missing {{ color:var(--dim); font-style:italic; margin:4px 0; }}
  footer {{ color:var(--dim); font-size:11px; margin-top:22px;
    padding-top:12px; border-top:1px solid var(--line); }}
</style></head><body>
<h1>Trading Dashboard</h1>
<div class="stamp">built {now} IST &middot; static snapshot, refresh by re-running</div>
<div class="grid">
{"".join(cards)}
</div>
<footer>
  Every panel shows the age of its data. Nothing here is live - this is a
  snapshot of what the collectors have stored, rendered at build time.
  A panel reading "2 days ago" during market hours means that component
  is not running.
</footer>
</body></html>'''


def main():
    ap = argparse.ArgumentParser(description="Unified dashboard")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--text", action="store_true")
    args = ap.parse_args()

    panels = [
        panel_premarket(),
        panel_fii_dii(),
        panel_chains(),
        panel_buildup(),
        panel_collector(),
        panel_journal(),
        panel_data_coverage(),
    ]

    if args.text:
        print(render_text(panels))
        return

    OUT_HTML.write_text(render_html(panels), encoding="utf-8")
    print(f"Dashboard written to {OUT_HTML}")
    missing = [p["title"] for p in panels if p.get("missing")]
    if missing:
        print(f"\nPanels with no data: {', '.join(missing)}")
        print("Run the corresponding collectors to populate them.")
    if not args.no_open:
        webbrowser.open(OUT_HTML.as_uri())


if __name__ == "__main__":
    main()
