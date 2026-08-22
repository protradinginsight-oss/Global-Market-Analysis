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

# What we track, and how each one is read for an Indian-open view.
# "direction" says which way this instrument moving UP pushes Indian equities.
#   +1 = risk-on for Nifty, -1 = headwind for Nifty, 0 = context only
#
# Note on symbols: index and futures symbols (SPX, IXIC, DXY, BRENT) are
# premium-tier on Twelve Data and 404 on the free plan. These ETF proxies are
# ordinary US-listed equities and work on free tiers. They track their
# underlying closely enough for a directional pre-market read, though they
# are not identical - ETFs carry expense ratios, can trade at a small premium
# or discount, and only move during US market hours, so an overnight move in
# the actual future may not be reflected until the US open.
INSTRUMENTS = [
    # symbol,  label,                    direction, note
    ("SPY",    "S&P 500 (SPY)",               1, "US risk appetite"),
    ("QQQ",    "Nasdaq 100 (QQQ)",            1, "Tech/IT sector read"),
    ("UUP",    "Dollar Index (UUP)",         -1, "Strong USD pressures EM flows"),
    ("USD/INR", "USDINR",                    -1, "Weak INR pressures FII flows"),
    ("USO",    "WTI Crude (USO)",            -1, "India is a net oil importer"),
    ("XAU/USD", "Gold",                       0, "Risk-off hedge, context"),
]

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

    for i, (symbol, label, direction, note) in enumerate(INSTRUMENTS):
        if i > 0:
            time.sleep(SECONDS_BETWEEN_CALLS)
        try:
            raw = fetch_quote(symbol)
            row = {
                "symbol": symbol,
                "label": label,
                "direction": direction,
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
    """Turn the raw moves into a plain-language pre-market read.

    This is a weight-of-evidence summary, not a prediction. It counts how many
    tracked instruments lean risk-on vs risk-off for Indian equities, and
    deliberately surfaces disagreement rather than hiding it behind one number.
    """
    scored = [r for r in results if r["direction"] != 0 and r["change_pct"] is not None]
    if not scored:
        return "No directional instruments available - no read.", []

    lines = []
    positive = 0
    negative = 0

    for r in sorted(results, key=lambda x: abs(x["change_pct"] or 0), reverse=True):
        if r["change_pct"] is None:
            continue
        arrow = "up" if r["change_pct"] >= 0 else "down"
        lean = ""
        if r["direction"] != 0:
            # Instrument's move, translated into its effect on Indian equities
            effect = r["change_pct"] * r["direction"]
            if effect > 0:
                positive += 1
                lean = "supportive"
            else:
                negative += 1
                lean = "headwind"
            lean = f" -> {lean} for Nifty"
        lines.append(
            f"  {r['label']:<20} {r['change_pct']:+.2f}% ({arrow}){lean}  [{r['note']}]"
        )

    total = positive + negative
    if total == 0:
        verdict = "No directional signal."
    elif positive == total:
        verdict = f"All {total} directional cues lean supportive - broadly risk-on overnight."
    elif negative == total:
        verdict = f"All {total} directional cues lean negative - broadly risk-off overnight."
    else:
        verdict = (
            f"Mixed: {positive} supportive vs {negative} headwind. "
            "Cues disagree - treat direction as unresolved rather than picking a side."
        )

    # If some instruments failed to fetch, say so inside the verdict itself.
    # A footnote below is easy to skim past, and "all cues agree" reads far
    # more confident than it should when it's really "all cues that worked".
    if expected_directional is not None and total < expected_directional:
        missing = expected_directional - total
        verdict = (
            f"[PARTIAL DATA - {missing} of {expected_directional} directional "
            f"instruments missing] " + verdict
        )

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

        expected_directional = sum(1 for i in INSTRUMENTS if i[2] != 0)
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
