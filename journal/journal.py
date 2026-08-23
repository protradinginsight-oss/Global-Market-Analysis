#!/usr/bin/env python3
"""
Trade journal - a record of what you actually did, and what it cost.

Nothing else in this project records real trades. Without that, every
performance question is unanswerable: which setups work, whether the edge
you think you have is real, whether your position sizing is sane.

The single most useful field here is `rationale` - why you took the trade,
written before you knew the outcome. Reading those back after fifty trades
tells you more than any metric, because memory quietly rewrites losing
trades into "I knew that was risky" and winners into "I called that."

Costs are computed automatically using the cost model, so reported P&L is
net rather than the flattering gross figure.

Usage:
    py -3.12 journal.py open --symbol NSE:SBIN-EQ --side LONG --qty 500 \\
        --entry 820.50 --stop 812.00 --target 838.00 \\
        --strategy camarilla-r3 --rationale "opened near R3, volume above avg"

    py -3.12 journal.py close --id 3 --exit 835.25 --note "hit target"
    py -3.12 journal.py list
    py -3.12 journal.py open-positions
    py -3.12 journal.py stats
    py -3.12 journal.py stats --strategy camarilla-r3
    py -3.12 journal.py show --id 3
"""

import sys
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "trade_journal.db"
IST = timezone(timedelta(hours=5, minutes=30))

# The cost model lives in the fyers-live folder; import it if reachable so
# P&L is net of costs rather than gross.
COST_MODEL = None
for candidate in (BASE_DIR.parent / "fyers-live", BASE_DIR):
    sys.path.insert(0, str(candidate))
    try:
        import cost_model as COST_MODEL
        break
    except ImportError:
        sys.path.pop(0)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT NOT NULL,
            side         TEXT NOT NULL,          -- LONG or SHORT
            quantity     INTEGER NOT NULL,
            entry_price  REAL NOT NULL,
            entry_time   TEXT NOT NULL,
            stop_price   REAL,
            target_price REAL,
            strategy     TEXT,
            rationale    TEXT,
            exit_price   REAL,
            exit_time    TEXT,
            exit_note    TEXT,
            gross_pnl    REAL,
            costs        REAL,
            net_pnl      REAL,
            r_multiple   REAL,
            status       TEXT NOT NULL DEFAULT 'OPEN'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy ON trades(strategy)")
    conn.commit()
    return conn


def estimate_costs(entry, exit_price, qty):
    if COST_MODEL is None:
        return 0.0
    try:
        c = COST_MODEL.round_trip_cost(entry, exit_price, qty)
        return c["total"]
    except Exception:
        return 0.0


def cmd_open(conn, a):
    if a.side not in ("LONG", "SHORT"):
        print("side must be LONG or SHORT")
        sys.exit(1)
    if a.stop:
        if a.side == "LONG" and a.stop >= a.entry:
            print(f"For a LONG, the stop ({a.stop}) must be below entry "
                  f"({a.entry}).")
            sys.exit(1)
        if a.side == "SHORT" and a.stop <= a.entry:
            print(f"For a SHORT, the stop ({a.stop}) must be above entry "
                  f"({a.entry}).")
            sys.exit(1)

    now = datetime.now(IST).isoformat()
    cur = conn.execute(
        """INSERT INTO trades (symbol, side, quantity, entry_price, entry_time,
                               stop_price, target_price, strategy, rationale, status)
           VALUES (?,?,?,?,?,?,?,?,?,'OPEN')""",
        (a.symbol, a.side, a.qty, a.entry, now, a.stop, a.target,
         a.strategy, a.rationale))
    conn.commit()
    tid = cur.lastrowid

    print(f"\nTrade #{tid} opened")
    print(f"  {a.side} {a.qty} x {a.symbol} @ {a.entry:,.2f}")
    if a.stop:
        risk = abs(a.entry - a.stop) * a.qty
        print(f"  Stop {a.stop:,.2f}   risk Rs {risk:,.0f} (1R)")
    if a.target:
        reward = abs(a.target - a.entry) * a.qty
        print(f"  Target {a.target:,.2f}   reward Rs {reward:,.0f}")
        if a.stop:
            rr = abs(a.target - a.entry) / abs(a.entry - a.stop)
            print(f"  Reward:risk {rr:.2f}")
            if rr < 1:
                print("  Note: reward is smaller than risk. That can still be")
                print("  correct with a high enough win rate, but it needs one.")
    if not a.stop:
        print("  No stop recorded - R-multiples can't be computed for this trade.")
    print()


def cmd_close(conn, a):
    row = conn.execute(
        "SELECT symbol, side, quantity, entry_price, stop_price, status "
        "FROM trades WHERE id=?", (a.id,)).fetchone()
    if not row:
        print(f"No trade #{a.id}.")
        sys.exit(1)
    symbol, side, qty, entry, stop, status = row
    if status != "OPEN":
        print(f"Trade #{a.id} is already {status}.")
        sys.exit(1)

    gross = ((a.exit - entry) if side == "LONG" else (entry - a.exit)) * qty
    costs = estimate_costs(entry, a.exit, qty)
    net = gross - costs
    r_mult = None
    if stop:
        risk = abs(entry - stop) * qty
        if risk > 0:
            r_mult = net / risk

    conn.execute(
        """UPDATE trades SET exit_price=?, exit_time=?, exit_note=?,
           gross_pnl=?, costs=?, net_pnl=?, r_multiple=?, status='CLOSED'
           WHERE id=?""",
        (a.exit, datetime.now(IST).isoformat(), a.note,
         gross, costs, net, r_mult, a.id))
    conn.commit()

    print(f"\nTrade #{a.id} closed")
    print(f"  {side} {qty} x {symbol}   {entry:,.2f} -> {a.exit:,.2f}")
    print(f"  Gross P&L : Rs {gross:>12,.2f}")
    print(f"  Costs     : Rs {costs:>12,.2f}")
    print(f"  Net P&L   : Rs {net:>12,.2f}")
    if r_mult is not None:
        print(f"  R multiple: {r_mult:>+13.2f}R")
    if costs and gross:
        pct = costs / abs(gross) * 100
        if pct > 25:
            print(f"\n  Costs took {pct:.0f}% of the gross move. Worth noting -")
            print("  small moves rarely survive Indian F&O costs.")
    print()


def cmd_list(conn, a):
    q = "SELECT id, symbol, side, quantity, entry_price, exit_price, " \
        "net_pnl, r_multiple, strategy, status, entry_time FROM trades"
    params = []
    if a.strategy:
        q += " WHERE strategy=?"
        params.append(a.strategy)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(a.limit)
    rows = conn.execute(q, params).fetchall()

    print("=" * 96)
    print("  TRADES")
    print("=" * 96)
    if not rows:
        print("\nNo trades recorded yet.\n")
        return
    print(f"\n{'ID':>4} {'Date':<12} {'Symbol':<20} {'Side':<6} {'Qty':>6} "
          f"{'Entry':>10} {'Exit':>10} {'Net P&L':>12} {'R':>7}  Strategy")
    print("-" * 96)
    for (tid, sym, side, qty, ent, ex, net, r, strat, status, et) in rows:
        print(f"{tid:>4} {et[:10]:<12} {sym[:19]:<20} {side:<6} {qty:>6,} "
              f"{ent:>10,.2f} {(f'{ex:,.2f}' if ex else 'OPEN'):>10} "
              f"{(f'{net:,.0f}' if net is not None else '-'):>12} "
              f"{(f'{r:+.2f}' if r is not None else '-'):>7}  {strat or ''}")
    print("-" * 96)
    print()


def cmd_open_positions(conn, a):
    rows = conn.execute(
        "SELECT id, symbol, side, quantity, entry_price, stop_price, "
        "target_price, entry_time, strategy FROM trades WHERE status='OPEN' "
        "ORDER BY id").fetchall()
    print("=" * 90)
    print("  OPEN POSITIONS")
    print("=" * 90)
    if not rows:
        print("\nNone open.\n")
        return
    total_risk = 0
    print(f"\n{'ID':>4} {'Symbol':<20} {'Side':<6} {'Qty':>6} {'Entry':>10} "
          f"{'Stop':>10} {'Risk Rs':>11}  Opened")
    print("-" * 90)
    for tid, sym, side, qty, ent, stop, tgt, et, strat in rows:
        risk = abs(ent - stop) * qty if stop else None
        if risk:
            total_risk += risk
        print(f"{tid:>4} {sym[:19]:<20} {side:<6} {qty:>6,} {ent:>10,.2f} "
              f"{(f'{stop:,.2f}' if stop else '-'):>10} "
              f"{(f'{risk:,.0f}' if risk else '-'):>11}  {et[:16]}")
    print("-" * 90)
    print(f"\nTotal risk if every stop is hit: Rs {total_risk:,.0f}")
    print("That is the number that matters for position sizing - not the")
    print("capital deployed, but what you lose if everything goes wrong at once.\n")


def cmd_stats(conn, a):
    q = "SELECT net_pnl, r_multiple, strategy, symbol FROM trades " \
        "WHERE status='CLOSED'"
    params = []
    if a.strategy:
        q += " AND strategy=?"
        params.append(a.strategy)
    rows = conn.execute(q, params).fetchall()

    print("=" * 74)
    print("  PERFORMANCE" + (f" - {a.strategy}" if a.strategy else ""))
    print("=" * 74)
    if not rows:
        print("\nNo closed trades yet.\n")
        return

    pnls = [r[0] for r in rows if r[0] is not None]
    rs = [r[1] for r in rows if r[1] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    print(f"\nClosed trades : {len(pnls)}")
    print(f"Net P&L       : Rs {total:>14,.2f}")
    print(f"Win rate      : {win_rate:>14.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Average win   : Rs {avg_win:>14,.2f}")
    print(f"Average loss  : Rs {avg_loss:>14,.2f}")
    if avg_loss:
        print(f"Win/loss ratio: {abs(avg_win/avg_loss):>14.2f}")
    if rs:
        exp = sum(rs) / len(rs)
        print(f"Expectancy    : {exp:>+14.3f}R per trade")
        if exp <= 0:
            print("\n  Negative expectancy: this is losing money per trade.")
        elif exp < 0.1:
            print("\n  Thin positive expectancy - easily erased by a change in")
            print("  costs, slippage, or market regime.")

    # Drawdown from the running equity curve
    equity, peak, max_dd = 0, 0, 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    print(f"Max drawdown  : Rs {max_dd:>14,.2f}")

    if len(pnls) < 30:
        print(f"\n  Only {len(pnls)} trades. Nothing here is statistically")
        print("  meaningful yet - 30 is a bare minimum and 100 is better.")

    if not a.strategy:
        by_strat = {}
        for net, r, strat, sym in rows:
            if net is None:
                continue
            k = strat or "(untagged)"
            by_strat.setdefault(k, []).append(net)
        if len(by_strat) > 1:
            print(f"\n{'Strategy':<24} {'Trades':>8} {'Net P&L':>14} {'Win%':>8}")
            print("-" * 74)
            for k, v in sorted(by_strat.items(), key=lambda x: -sum(x[1])):
                w = sum(1 for p in v if p > 0) / len(v) * 100
                print(f"{k:<24} {len(v):>8} {sum(v):>14,.0f} {w:>7.0f}%")
    print()


def cmd_show(conn, a):
    row = conn.execute("SELECT * FROM trades WHERE id=?", (a.id,)).fetchone()
    if not row:
        print(f"No trade #{a.id}.")
        return
    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    d = dict(zip(cols, row))
    print("=" * 74)
    print(f"  TRADE #{d['id']}")
    print("=" * 74)
    for k in cols:
        if d[k] is None or k == "id":
            continue
        v = d[k]
        if isinstance(v, float):
            v = f"{v:,.2f}"
        print(f"  {k:<14} {v}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Trade journal")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="record a new trade")
    o.add_argument("--symbol", required=True)
    o.add_argument("--side", required=True, choices=["LONG", "SHORT"])
    o.add_argument("--qty", type=int, required=True)
    o.add_argument("--entry", type=float, required=True)
    o.add_argument("--stop", type=float)
    o.add_argument("--target", type=float)
    o.add_argument("--strategy")
    o.add_argument("--rationale", help="why you took it - write this BEFORE "
                                       "you know the outcome")

    c = sub.add_parser("close", help="close an open trade")
    c.add_argument("--id", type=int, required=True)
    c.add_argument("--exit", type=float, required=True)
    c.add_argument("--note")

    l = sub.add_parser("list")
    l.add_argument("--limit", type=int, default=30)
    l.add_argument("--strategy")

    sub.add_parser("open-positions")

    s = sub.add_parser("stats")
    s.add_argument("--strategy")

    sh = sub.add_parser("show")
    sh.add_argument("--id", type=int, required=True)

    args = ap.parse_args()
    conn = init_db()

    {"open": cmd_open, "close": cmd_close, "list": cmd_list,
     "open-positions": cmd_open_positions, "stats": cmd_stats,
     "show": cmd_show}[args.cmd](conn, args)
    conn.close()


if __name__ == "__main__":
    main()
