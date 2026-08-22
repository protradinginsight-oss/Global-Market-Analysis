#!/usr/bin/env python3
"""
Global pre-market panel.

Pulls the overnight global cues that matter for the Indian open - US indices,
dollar index, crude, gold, USDINR - stores each snapshot, and prints a
compact read of what they collectively suggest for 9:15.

Deliberately NOT sourced from Fyers: Fyers covers NSE/BSE and MCX, so it's
the right tool for Nifty/BankNifty/stocks/MCX contracts, but it does not
carry S&P futures, DXY, Brent or COMEX. Those need an external provider.
Indian instruments stay on Fyers in a separate component.

Config (API key) lives in config_local.py, which is gitignored - see
config_local.example.py. Never put a real key in this file; it's public.

First run: do this manually before scheduling anything.
    py global_premarket.py
"""

import sqlite3
import requests
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from config_local import TWELVE_DATA_API_KEY
except ImportError:
    print(
        "Missing config_local.py.\n"
        "Copy config_local.example.py to config_local.py and add your own "
        "Twelve Data API key (see SETUP.md)."
    )
    sys.exit(1)

# Everything lives next to this script, so moving the folder needs no edits.
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "global_premarket.db"
LOG_PATH = BASE_DIR / "global_premarket.log"

API_URL = "https://api.twelvedata.com/quote"

# What we track, and the weight each cue carries.
#
# These weights are MEASURED, not assumed. They come from the lagged
# correlation of each market's overnight close against the next day's Nifty
# move, over 5 years of daily data (see global-history/query_global.py
# --lagged). Earlier versions of this file weighted every cue equally, which
# gave crude the same influence as the S&P despite crude showing no
# measurable relationship at all.
#
# weight = measured correlation. Sign carries direction: positive means the
# instrument moving up is supportive for Nifty.
#
# Instruments measured at |r| < 0.15 are tracked for context but contribute
# nothing to the verdict - listing them as drivers would overstate what the
# record supports.
INSTRUMENTS = [
    # symbol,  label,                  weight,  note
    ("SPY",    "S&P 500 (SPY)",         +0.244, "Strongest measured overnight lead"),
    ("QQQ",    "Nasdaq 100 (QQQ)",      +0.241, "Nearly as strong; leads Indian IT"),
    ("EEM",    "EM equities (EEM)",     +0.209, "EM flow proxy, tracks FII appetite"),
    # Spot VIX is premium-tier on Twelve Data, so this uses the VIX futures
    # ETF instead. It tracks VIX direction day to day, but not its level:
    # VIXY bleeds value over time through futures roll, so only treat the
    # daily percentage move as meaningful, never the price itself.
    ("VIXY",   "VIX futures ETF",       -0.203, "Fear gauge proxy, inverse"),
    # Twelve Data uses a slash for crypto pairs, not Yahoo's hyphen.
    ("BTC/USD","Bitcoin",               +0.174, "Fast risk-appetite read, weaker but real"),
    ("UUP",    "Dollar Index (UUP)",    -0.149, "Marginal - just above the noise floor"),
    # --- below the threshold: shown, but not counted ---
    ("USO",    "WTI Crude (USO)",        0.000, "No measurable daily lead (r=-0.03)"),
    ("GLD",    "Gold (GLD)",             0.000, "No measurable daily lead (r=+0.05)"),
    ("USD/INR","USDINR",                 0.000, "No measurable daily lead (r=-0.13 same-day)"),
]

# Cues below this absolute correlation are treated as context only.
WEIGHT_FLOOR = 0.15

# Free tier is rate limited per minute, so space the calls out a little.
SECONDS_BETWEEN_CALLS = 8

BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("global_premarket")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_snapshot (
            captured_at   TEXT NOT NULL,   -- UTC ISO timestamp of this run
            symbol        TEXT NOT NULL,
            label         TEXT NOT NULL,
            price         REAL,
            change_pct    REAL,
            previous_close REAL,
            PRIMARY KEY (captured_at, symbol)
        )
        """
    )
    conn.commit()
    return conn


def fetch_quote(symbol):
    """Fetch one instrument. Returns a dict or raises.

    Errors are re-raised without the request URL: requests puts the full URL
    (including the apikey query param) into HTTPError messages, and those
    messages end up in logs and printed output.
    """
    try:
        resp = requests.get(
            API_URL,
            params={"symbol": symbol, "apikey": TWELVE_DATA_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 404:
            raise ValueError(
                f"{symbol}: not found (404) - likely a premium-tier symbol "
                "not available on the free plan"
            ) from None
        raise ValueError(f"{symbol}: HTTP {code}") from None
    except requests.exceptions.RequestException as e:
        raise ValueError(f"{symbol}: request failed ({type(e).__name__})") from None

    data = resp.json()

    # Twelve Data returns HTTP 200 with an error body on bad key / bad symbol /
    # rate limit, so status code alone is not enough to trust the response.
    if isinstance(data, dict) and data.get("status") == "error":
        raise ValueError(f"{symbol}: {data.get('message', 'unknown API error')}")
    if "close" not in data:
        raise ValueError(f"{symbol}: unexpected response shape: {str(data)[:200]}")

    return data


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect(conn):
    """Fetch every instrument. Partial failure is tolerated and reported."""
    captured_at = datetime.now(timezone.utc).isoformat()
    results = []
    failures = []

    for i, (symbol, label, weight, note) in enumerate(INSTRUMENTS):
        if i > 0:
            time.sleep(SECONDS_BETWEEN_CALLS)
        try:
            raw = fetch_quote(symbol)
            row = {
                "symbol": symbol,
                "label": label,
                "weight": weight,
                "note": note,
                "price": to_float(raw.get("close")),
                "change_pct": to_float(raw.get("percent_change")),
                "previous_close": to_float(raw.get("previous_close")),
            }
            results.append(row)
            conn.execute(
                """
                INSERT OR REPLACE INTO global_snapshot
                    (captured_at, symbol, label, price, change_pct, previous_close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    symbol,
                    label,
                    row["price"],
                    row["change_pct"],
                    row["previous_close"],
                ),
            )
        except Exception as e:
            log.warning("Failed to fetch %s (%s): %s", label, symbol, e)
            failures.append((label, str(e)))

    conn.commit()
    return captured_at, results, failures


def build_read(results, expected_directional=None):
    """Turn overnight moves into a weighted read for the Indian open.

    Each cue contributes its move multiplied by its measured correlation with
    next-day Nifty. A 1% move in the S&P (r=0.244) therefore counts for far
    more than a 1% move in crude (r~0.00), which is what the historical
    record supports and what an equal-weighted count got wrong.
    """
    lines = []
    weighted_sum = 0.0
    weight_used = 0.0
    counted = 0

    for r in sorted(results, key=lambda x: abs(x.get("weight", 0)), reverse=True):
        if r["change_pct"] is None:
            continue
        w = r.get("weight", 0)
        move = r["change_pct"]

        if abs(w) >= WEIGHT_FLOOR:
            contribution = move * w
            weighted_sum += contribution
            weight_used += abs(w)
            counted += 1
            lean = "supportive" if contribution > 0 else "headwind"
            tag = f" -> {lean} (w={w:+.2f})"
        else:
            tag = "  [context only, no measurable lead]"

        lines.append(f"  {r['label']:<22} {move:+.2f}%{tag}")

    if weight_used == 0:
        return "No weighted cues available - no read.", []

    # Normalise so the score is comparable day to day regardless of how many
    # cues were retrievable.
    score = weighted_sum / weight_used

    if abs(score) < 0.15:
        verdict = (f"Net signal {score:+.2f}% - essentially flat. Overnight cues "
                   "give no meaningful lead either way.")
    elif score > 0:
        strength = "modestly" if score < 0.5 else "clearly"
        verdict = (f"Net signal {score:+.2f}% - {strength} supportive from "
                   f"{counted} weighted cues.")
    else:
        strength = "modestly" if score > -0.5 else "clearly"
        verdict = (f"Net signal {score:+.2f}% - {strength} negative from "
                   f"{counted} weighted cues.")

    if expected_directional is not None and counted < expected_directional:
        missing = expected_directional - counted
        verdict = (f"[PARTIAL - {missing} of {expected_directional} weighted "
                   f"cues missing] " + verdict)

    return verdict, lines


def main():
    conn = init_db()
    try:
        log.info("Collecting global pre-market snapshot...")
        captured_at, results, failures = collect(conn)

        if not results:
            raise RuntimeError(
                "Every instrument failed to fetch - check API key and connectivity. "
                f"First error: {failures[0][1] if failures else 'none recorded'}"
            )

        expected_directional = sum(1 for i in INSTRUMENTS if abs(i[2]) >= WEIGHT_FLOOR)
        verdict, lines = build_read(results, expected_directional)

        report = [
            "",
            f"GLOBAL PRE-MARKET SNAPSHOT  ({captured_at} UTC)",
            "-" * 72,
        ]
        report.extend(lines)
        report.append("-" * 72)
        report.append(verdict)
        if failures:
            report.append("")
            report.append(f"NOTE: {len(failures)} instrument(s) failed to fetch:")
            for label, err in failures:
                report.append(f"  - {label}: {err}")
            report.append("The read above is based only on what was retrieved.")
        report.append("")
        report.append("Weights are measured lagged correlations against next-day")
        report.append("Nifty over 5 years. The strongest is only 0.24, so global")
        report.append("cues explain roughly 6% of Nifty's daily variance - useful")
        report.append("context, not a directional call.")
        report.append("")

        output = "\n".join(report)
        print(output)
        log.info("Snapshot stored (%d ok, %d failed). %s",
                 len(results), len(failures), verdict)

    except Exception as e:
        log.exception("Global pre-market collection failed")
        print(f"FAILED: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
