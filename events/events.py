#!/usr/bin/env python3
"""
India events calendar - results, dividends, corporate actions.

Being short options into an unexpected results announcement is one of the
more avoidable ways to lose money. NSE publishes the dates free; this
collects them so the information is available before a position is opened
rather than discovered afterwards.

Collects:
  - board meetings (results dates)
  - corporate actions (dividends, splits, bonuses, buybacks)
  - the forward event calendar

NSE blocks cold requests, so this uses the same session-cookie approach as
the FII/DII tracker: hit the homepage first, then the API.

Usage:
    py -3.12 events.py --fetch            # collect the latest
    py -3.12 events.py --upcoming         # what is coming, all symbols
    py -3.12 events.py --symbol RELIANCE  # one company
    py -3.12 events.py --check-positions  # events affecting open trades
"""

import sys
import json
import sqlite3
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing requests. Install with:  py -3.12 -m pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
DB_PATH = BASE_DIR / "events.db"
JOURNAL_DB = ROOT / "journal" / "trade_journal.db"
UNIVERSE_FILE = ROOT / "fyers-live" / "universe.json"


def fno_universe():
    """The F&O-eligible names, so events can be filtered to what's tradeable.

    NSE's feed covers every listed company, and most of a day's events are
    small caps with no derivatives. They are not wrong, just not actionable
    for anyone trading options - and they bury the handful that matter.
    """
    if not UNIVERSE_FILE.exists():
        return None
    try:
        uni = json.loads(UNIVERSE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    names = set()
    for item in uni.get("all", []):
        u = item.get("underlying")
        if u:
            names.add(u.upper())
    return names or None

NSE_HOME = "https://www.nseindia.com/"

# Without date parameters these endpoints return only a narrow window -
# typically the next few days - which is why an unparameterised fetch looks
# almost empty when filtered to F&O names over a quarter. Each is requested
# with an explicit range and falls back to the bare URL if that is rejected.
ENDPOINTS = {
    "board_meetings": {
        "url": "https://www.nseindia.com/api/event-calendar",
        "date_params": ("from_date", "to_date"),
        "extra": {"index": "equities"},
    },
    "corp_actions": {
        "url": "https://www.nseindia.com/api/corporates-corporateActions",
        "date_params": ("from_date", "to_date"),
        "extra": {"index": "equities"},
    },
    # The corporate-announcements endpoint was tried and dropped. It returns
    # thousands of rows - 3,263 in one fetch - but they are filings that have
    # already happened, not scheduled events. It contributed nothing to
    # forward planning and buried the couple of hundred rows that did.
}

# How far forward and back to request.
#
# 120 days was tried and is misleading: NSE does not publish that far ahead.
# Measured against the live feed, board meetings reach about 16 days out and
# corporate actions about 32. Asking for four months implies data exists
# there and makes a complete calendar look sparse. 45 days covers what the
# source actually provides with a little margin.
LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 45

# Default window for the calendar view, for the same reason.
DEFAULT_CALENDAR_DAYS = 45

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            symbol      TEXT NOT NULL,
            event_date  TEXT NOT NULL,
            event_type  TEXT NOT NULL,   -- results, dividend, split, etc
            purpose     TEXT,
            detail      TEXT,
            source      TEXT,
            fetched_at  TEXT,
            PRIMARY KEY (symbol, event_date, event_type, purpose)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_date ON events(event_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_sym ON events(symbol)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            endpoint   TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            rows       INTEGER,
            status     TEXT,
            detail     TEXT
        )
    """)
    conn.commit()
    return conn


def nse_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    # NSE rejects a cold API call; the homepage sets the cookies it checks.
    s.get(NSE_HOME, timeout=15)
    return s


def classify(purpose):
    """Map NSE's free-text purpose to a coarse event type."""
    p = (purpose or "").lower()
    if "dividend" in p:
        return "dividend"
    if "split" in p or "sub-division" in p or "subdivision" in p:
        return "split"
    if "bonus" in p:
        return "bonus"
    if "buyback" in p or "buy back" in p:
        return "buyback"
    if "rights" in p:
        return "rights"
    if "result" in p or "financial" in p:
        return "results"
    if "meeting" in p:
        return "board_meeting"
    if "amalgamation" in p or "merger" in p or "demerger" in p:
        return "restructuring"
    return "other"


def norm_date(raw):
    """NSE returns several date formats depending on the endpoint."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y",
                "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:len(fmt) + 4], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw[:19]).date().isoformat()
    except ValueError:
        return None


def extract(endpoint_name, payload):
    """Pull (symbol, date, type, purpose, detail) from whatever NSE returned.

    The three endpoints use different field names, and NSE has changed them
    before, so this looks for any of several plausible keys rather than
    assuming one layout.
    """
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    if not isinstance(rows, list):
        return []

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = (r.get("symbol") or r.get("sym") or r.get("Symbol") or "").strip()
        if not sym:
            continue

        raw_date = (r.get("date") or r.get("meetingDate") or r.get("exDate")
                    or r.get("ex_date") or r.get("bm_date")
                    or r.get("an_dt") or r.get("recDate"))
        d = norm_date(raw_date)
        if not d:
            continue

        purpose = (r.get("purpose") or r.get("bm_purpose") or r.get("subject")
                   or r.get("desc") or r.get("attchmntText") or "").strip()
        detail = (r.get("bm_desc") or r.get("comp") or r.get("attchmntFile")
                  or "").strip()

        out.append((sym, d, classify(purpose), purpose[:300],
                    detail[:300], endpoint_name))
    return out


def cmd_fetch(conn):
    print("=" * 74)
    print("  FETCHING NSE EVENTS")
    print("=" * 74)
    try:
        s = nse_session()
    except Exception as e:
        print(f"\nCould not reach NSE: {type(e).__name__}: {e}")
        sys.exit(1)

    now = datetime.now().isoformat()
    frm = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%m-%Y")
    to = (date.today() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%d-%m-%Y")
    print(f"\nRequesting {frm} to {to}")

    total = 0
    for name, cfg in ENDPOINTS.items():
        print(f"\n  {name}")

        attempts = []
        p1 = dict(cfg.get("extra", {}))
        d_from, d_to = cfg["date_params"]
        p1[d_from], p1[d_to] = frm, to
        attempts.append(("with date range", p1))
        attempts.append(("without dates", dict(cfg.get("extra", {}))))

        rows = []
        used = None
        last_err = ""
        for label, params in attempts:
            try:
                resp = s.get(cfg["url"], params=params, timeout=25)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
                print(f"    {label:<18} failed ({type(e).__name__})")
                continue
            got = extract(name, payload)
            print(f"    {label:<18} {len(got)} events")
            if got:
                rows, used = got, label
                break

        if rows:
            conn.executemany(
                """INSERT OR REPLACE INTO events
                   (symbol, event_date, event_type, purpose, detail, source,
                    fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [(*r, now) for r in rows])
            conn.commit()
            total += len(rows)
            dates = sorted({r[1] for r in rows})
            print(f"    -> stored, covering {dates[0]} to {dates[-1]}")
        else:
            print(f"    -> nothing usable. {last_err}")
        conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                     (name, now, len(rows), "ok" if rows else "empty",
                      used or last_err))
        conn.commit()

    print(f"\n{'=' * 74}")
    print(f"Stored {total} events -> {DB_PATH.name}")

    span = conn.execute(
        "SELECT MIN(event_date), MAX(event_date), COUNT(*) FROM events"
    ).fetchone()
    if span and span[0]:
        print(f"Database now holds {span[2]:,} events, {span[0]} to {span[1]}")
        uni = fno_universe()
        if uni:
            fno_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_date >= ?",
                (date.today().isoformat(),)).fetchone()[0]
            future_fno = [r for r in conn.execute(
                "SELECT symbol FROM events WHERE event_date >= ?",
                (date.today().isoformat(),)) if r[0].upper() in uni]
            print(f"Upcoming: {fno_count} total, {len(future_fno)} on F&O names")
            # Measured against the live feed: board meetings publish about
            # 16 days ahead, corporate actions about 32. A short forward
            # list is the source's limit, not a failed fetch.
            print("\nNSE publishes board meetings roughly two weeks ahead and")
            print("corporate actions about a month. A short forward list is")
            print("that limit, not a problem - re-fetch daily and it rolls")
            print("forward as companies announce.")
    if total == 0:
        print("\nNothing collected. NSE changes these endpoints periodically;")
        print("if this keeps failing the field names in extract() may need")
        print("updating against whatever the API now returns.")
    print()


# "Other business matters" is what NSE files when nothing specific is being
# announced. It is most of the feed by volume and almost none of it by use,
# so it is hidden unless asked for.
NOISE_TYPES = {"other", "board_meeting"}


def cmd_calendar(conn, days=DEFAULT_CALENDAR_DAYS, all_symbols=False,
                 include_noise=False):
    """Week-by-week view for planning ahead.

    The flat list answers "what is happening soon". This answers "which
    weeks should I be careful in", which is the question when deciding
    where to put an expiry.
    """
    today = date.today()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT event_date, symbol, event_type, purpose FROM events
           WHERE event_date >= ? AND event_date <= ?
           ORDER BY event_date, symbol""", (today.isoformat(), until)).fetchall()

    universe = None if all_symbols else fno_universe()
    if universe:
        rows = [r for r in rows if r[1].upper() in universe]
    if not include_noise:
        rows = [r for r in rows if r[2] not in NOISE_TYPES]

    print("=" * 78)
    print(f"  EVENT CALENDAR - next {days} days")
    if universe:
        print(f"  F&O names only" +
              ("" if include_noise else ", routine filings hidden"))
    print("=" * 78)
    if not rows:
        print("\nNothing scheduled in that window. Run --fetch to refresh.\n")
        return

    # Group into weeks starting Monday
    weeks = {}
    for d, sym, typ, purpose in rows:
        dt = date.fromisoformat(d)
        monday = dt - timedelta(days=dt.weekday())
        weeks.setdefault(monday, []).append((dt, sym, typ, purpose))

    for monday in sorted(weeks):
        items = weeks[monday]
        sunday = monday + timedelta(days=6)
        weeks_away = (monday - (today - timedelta(days=today.weekday()))).days // 7
        when = ("this week" if weeks_away == 0 else
                "next week" if weeks_away == 1 else
                f"in {weeks_away} weeks")

        by_type = {}
        for _, _, t, _ in items:
            by_type[t] = by_type.get(t, 0) + 1
        summary = ", ".join(f"{n} {t}" for t, n in
                            sorted(by_type.items(), key=lambda x: -x[1]))

        print(f"\n  {monday.strftime('%d %b')} - {sunday.strftime('%d %b')}"
              f"   ({when})   {len(items)} events: {summary}")
        print("  " + "-" * 74)
        for dt, sym, typ, purpose in sorted(items):
            print(f"    {dt.strftime('%a %d %b'):<12} {sym[:14]:<15} "
                  f"{typ:<12} {purpose[:30]}")

    print()
    print("  " + "-" * 74)
    print(f"  {len(rows)} events across {len(weeks)} weeks.")
    # Say plainly where the data runs out, so an empty far week is not
    # mistaken for a quiet one.
    horizon = conn.execute(
        "SELECT MAX(event_date) FROM events WHERE event_date >= ?",
        (today.isoformat(),)).fetchone()
    if horizon and horizon[0]:
        h = date.fromisoformat(horizon[0])
        print(f"  Nothing is scheduled beyond {horizon[0]} "
              f"({(h - today).days} days out) - that is how far NSE")
        print("  publishes, not a quiet period. Re-fetch daily and the")
        print("  calendar extends as companies announce.")
    if not include_noise:
        print("  Routine board meetings and 'other business matters' are")
        print("  hidden - use --noise to include them.")
    print("\n  A week heavy with results is a week where short options carry")
    print("  more risk than the chain suggests. That is the planning use.\n")


def cmd_upcoming(conn, days=30, all_symbols=False, min_type=None,
                 include_noise=False):
    today = date.today().isoformat()
    until = (date.today() + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT event_date, symbol, event_type, purpose FROM events
           WHERE event_date >= ? AND event_date <= ?
           ORDER BY event_date, symbol""", (today, until)).fetchall()

    universe = None if all_symbols else fno_universe()
    total_raw = len(rows)
    if universe:
        rows = [r for r in rows if r[1].upper() in universe]
    if min_type:
        rows = [r for r in rows if r[2] == min_type]
    elif not include_noise:
        rows = [r for r in rows if r[2] not in NOISE_TYPES]

    print("=" * 78)
    print(f"  UPCOMING EVENTS - next {days} days")
    if universe:
        print(f"  F&O names only ({len(rows)} of {total_raw} events)")
    print("=" * 78)
    if not rows:
        if total_raw:
            print(f"\n{total_raw} events stored for this window, but none on")
            print("F&O-eligible names. Use --all to see everything.\n")
        else:
            print("\nNothing stored for that window. Run with --fetch first.\n")
        return

    print(f"\n{'Date':<12} {'Symbol':<14} {'Type':<15} Purpose")
    print("-" * 78)
    for d, sym, typ, purpose in rows[:60]:
        days_away = (date.fromisoformat(d) - date.today()).days
        when = "today" if days_away == 0 else f"{days_away}d"
        print(f"{d:<12} {sym[:13]:<14} {typ:<15} {purpose[:35]}  ({when})")
    if len(rows) > 60:
        print(f"\n... and {len(rows)-60} more")
    print("-" * 78)
    if universe:
        print(f"\nFiltered to the {len(universe)} F&O underlyings"
              + ("" if include_noise else ", routine filings hidden") + ".")
        print("Use --all for every listed company, --noise for routine")
        print("board meetings, --calendar for a week-by-week planning view.")
    print()


def cmd_symbol(conn, symbol):
    rows = conn.execute(
        """SELECT event_date, event_type, purpose, detail FROM events
           WHERE symbol=? ORDER BY event_date DESC LIMIT 30""",
        (symbol.upper(),)).fetchall()
    print("=" * 78)
    print(f"  EVENTS - {symbol.upper()}")
    print("=" * 78)
    if not rows:
        print(f"\nNothing stored for {symbol.upper()}.\n")
        return
    today = date.today()
    print(f"\n{'Date':<12} {'When':>8} {'Type':<15} Purpose")
    print("-" * 78)
    for d, typ, purpose, detail in rows:
        delta = (date.fromisoformat(d) - today).days
        when = f"{delta:+d}d" if delta else "today"
        print(f"{d:<12} {when:>8} {typ:<15} {purpose[:38]}")
    print("-" * 78)
    print()


def cmd_check_positions(conn, days=14):
    """Events affecting anything currently held.

    This is the practical use: an option position running into a results
    date is a different risk from the same position in a quiet week, and
    the chain gives no warning.
    """
    if not JOURNAL_DB.exists():
        print("No trade journal found - nothing to check against.")
        return
    jc = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
    try:
        positions = jc.execute(
            "SELECT id, symbol, side, quantity FROM trades WHERE status='OPEN'"
        ).fetchall()
    except sqlite3.Error:
        positions = []
    jc.close()

    print("=" * 78)
    print(f"  EVENT RISK ON OPEN POSITIONS - next {days} days")
    print("=" * 78)
    if not positions:
        print("\nNo open positions.\n")
        return

    today = date.today().isoformat()
    until = (date.today() + timedelta(days=days)).isoformat()
    found_any = False
    for tid, sym, side, qty in positions:
        base = sym.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")
        rows = conn.execute(
            """SELECT event_date, event_type, purpose FROM events
               WHERE symbol=? AND event_date>=? AND event_date<=?
               ORDER BY event_date""", (base, today, until)).fetchall()
        if not rows:
            continue
        found_any = True
        print(f"\n  #{tid} {side} {qty:,} x {base}")
        for d, typ, purpose in rows:
            delta = (date.fromisoformat(d) - date.today()).days
            when = "TODAY" if delta == 0 else f"in {delta} day{'s' if delta != 1 else ''}"
            print(f"     {d}  {typ:<14} {when:<14} {purpose[:34]}")

    if not found_any:
        print(f"\nNo events stored for any open position in the next {days} days.")
        print("\nThat is not the same as there being none - it depends on what")
        print("has been fetched. Run --fetch to refresh before relying on it.")
    else:
        print("\n  A position running into a results date carries a different")
        print("  risk from the same position in a quiet week. Short options in")
        print("  particular can be repriced sharply by an announcement, and")
        print("  the chain gives no warning of one coming.")
    print()


def cmd_prune(conn):
    """Remove rows from the dropped announcements endpoint and old events.

    The first version collected corporate-announcements, which turned out to
    be historical filings rather than scheduled events - thousands of rows
    that never helped. This clears them out along with anything long past.
    """
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    n_ann = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='announcements'").fetchone()[0]
    conn.execute("DELETE FROM events WHERE source='announcements'")

    cutoff = (date.today() - timedelta(days=30)).isoformat()
    n_old = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_date < ?", (cutoff,)).fetchone()[0]
    conn.execute("DELETE FROM events WHERE event_date < ?", (cutoff,))
    conn.commit()
    conn.execute("VACUUM")

    after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print("=" * 74)
    print("  PRUNE")
    print("=" * 74)
    print(f"\n  Removed {n_ann:,} rows from the dropped announcements feed")
    print(f"  Removed {n_old:,} events older than 30 days")
    print(f"\n  {before:,} -> {after:,} rows")
    span = conn.execute(
        "SELECT MIN(event_date), MAX(event_date) FROM events").fetchone()
    if span and span[0]:
        print(f"  Now covering {span[0]} to {span[1]}")
    print()


def main():
    ap = argparse.ArgumentParser(description="India events calendar")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--upcoming", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--check-positions", action="store_true")
    ap.add_argument("--calendar", action="store_true",
                    help="week-by-week view for planning ahead")
    ap.add_argument("--noise", action="store_true",
                    help="include routine board meetings and 'other'")
    ap.add_argument("--prune", action="store_true",
                    help="clear out the dropped announcements feed and old events")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true",
                    help="include non-F&O names (default: F&O only)")
    ap.add_argument("--type", dest="etype",
                    choices=["results", "dividend", "split", "bonus",
                             "buyback", "rights", "restructuring",
                             "board_meeting", "other"],
                    help="show only this event type")
    args = ap.parse_args()

    conn = init_db()
    if args.prune:
        cmd_prune(conn)
    elif args.fetch:
        cmd_fetch(conn)
    elif args.calendar:
        cmd_calendar(conn,
                     args.days if args.days != 30 else DEFAULT_CALENDAR_DAYS,
                     args.all, args.noise)
    elif args.upcoming:
        cmd_upcoming(conn, args.days, args.all, args.etype, args.noise)
    elif args.symbol:
        cmd_symbol(conn, args.symbol)
    elif args.check_positions:
        cmd_check_positions(conn, args.days)
    else:
        print("Pick one of: --fetch, --upcoming, --calendar, --symbol NAME, "
              "--check-positions, --prune")
    conn.close()


if __name__ == "__main__":
    main()
