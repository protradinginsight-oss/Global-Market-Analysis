#!/usr/bin/env python3
"""
FII/DII EOD flow tracker.

Fetches NSE's provisional FII/DII cash-market activity once the data is
published each evening, and stores it locally. Safe to run more than once
a day: it checks whether today's row already exists before doing anything,
so a duplicate run (or your Mac waking from sleep and re-triggering) is a
harmless no-op instead of a duplicate row.

Config (Telegram token/chat ID) lives in config_local.py, which is
gitignored - see config_local.example.py for the template. Never put
real credentials in this file; this one is meant to be public.

First run: do this manually before you ever touch launchd.
    python3 fii_dii_tracker.py
Check the log file and the database afterward to confirm it actually worked.
"""

import sqlite3
import requests
import logging
import sys
from datetime import datetime, date
from pathlib import Path

try:
    from config_local import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    print(
        "Missing config_local.py.\n"
        "Copy config_local.example.py to config_local.py and fill in your "
        "own Telegram bot token and chat ID (see SETUP.md)."
    )
    sys.exit(1)

# ---- Paths ----
# Everything lives next to this script itself, wherever that is - so moving
# this whole folder (e.g. to a different drive) needs zero path edits here.
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "fii_dii.db"
LOG_PATH = BASE_DIR / "fii_dii.log"

NSE_HOME_URL = "https://www.nseindia.com/"
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/fii-dii-trade-react",
}

BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("fii_dii")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fii_dii_flow (
            trade_date TEXT NOT NULL,
            category   TEXT NOT NULL,   -- 'FII' or 'DII'
            buy_value  REAL,
            sell_value REAL,
            net_value  REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, category)
        )
        """
    )
    conn.commit()
    return conn


def have_date(conn, trade_date):
    """Check whether we already have rows for the date NSE actually returned.

    Deliberately not 'today's date' - NSE serves the last *trading* day's
    data, so on a weekend or holiday, or before the evening publish, the
    returned date is an earlier day. Comparing against today would never
    match and we'd refetch on every run.
    """
    cur = conn.execute(
        "SELECT COUNT(*) FROM fii_dii_flow WHERE trade_date = ?",
        (trade_date,),
    )
    return cur.fetchone()[0] > 0


def fetch_fii_dii():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(NSE_HOME_URL, timeout=10)
    resp = session.get(NSE_FII_DII_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def save_rows(conn, rows):
    fetched_at = datetime.now().isoformat()
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO fii_dii_flow
                (trade_date, category, buy_value, sell_value, net_value, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("date"),
                row.get("category"),
                float(row.get("buyValue", 0) or 0),
                float(row.get("sellValue", 0) or 0),
                float(row.get("netValue", 0) or 0),
                fetched_at,
            ),
        )
    conn.commit()


def send_telegram(message: str):
    if TELEGRAM_BOT_TOKEN == "your-bot-token-here":
        log.warning("Telegram not configured yet, skipping alert: %s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10
        )
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def main():
    conn = init_db()
    try:
        log.info("Fetching FII/DII data from NSE...")
        data = fetch_fii_dii()

        if not data:
            raise ValueError("NSE returned an empty response")

        # NSE serves the last *trading* day's data, which on a weekend,
        # holiday, or before the evening publish is an earlier date than
        # today. So dedup against what NSE actually returned.
        returned_date = data[0].get("date")
        if not returned_date:
            raise ValueError("NSE response had no date field")

        if have_date(conn, returned_date):
            log.info(
                "Already have data for %s (the latest NSE is serving) - "
                "nothing new to store.",
                returned_date,
            )
            return

        save_rows(conn, data)

        lines = [
            f"{row.get('category')}: net {row.get('netValue')} cr "
            f"(buy {row.get('buyValue')} / sell {row.get('sellValue')})"
            for row in data
        ]
        summary = f"FII/DII flow updated for {returned_date}:\n" + "\n".join(lines)
        log.info(summary)
        send_telegram(summary)

    except Exception as e:
        log.exception("FII/DII fetch failed")
        send_telegram(f"FII/DII tracker failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
