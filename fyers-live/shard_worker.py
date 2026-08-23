#!/usr/bin/env python3
"""
Shard worker - collects ONE account's slice of symbols.

Runs as its own process. This is not optional: Fyers' FyersDataSocket is a
singleton (its __new__ returns the same object every time), so four sockets
in one process silently collapse into one, and only the last-configured
account's callbacks ever fire. Separate processes give each socket its own
interpreter state - and as a bonus, one shard crashing can't take the
others down.

Normally launched by collector.py rather than run directly.

Usage:
    py -3.12 shard_worker.py --shard acc1 [--ignore-hours]
"""

import sys
import json
import time
import signal
import sqlite3
import logging
import argparse
import threading
from datetime import datetime, timezone, timedelta, time as dtime, date
from pathlib import Path

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    print("Missing fyers-apiv3. Install with:  py -3.12 -m pip install fyers-apiv3")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_FILE = BASE_DIR / "universe.json"
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"
CANDLE_DB = BASE_DIR / "market_candles.db"
TICK_DB = BASE_DIR / "market_ticks.db"

IST = timezone(timedelta(hours=5, minutes=30))
FLUSH_SECONDS = 30

try:
    from market_hours import is_ticker_open, segment_for
except ImportError:
    print("market_hours.py not found - it must sit next to this script.")
    sys.exit(1)

_shutdown = threading.Event()


def any_symbol_open(symbols, now=None):
    """Should this worker still be running?

    A worker keeps going while ANY of its symbols is in a live session. This
    matters once MCX is merged in: a shard holding both NSE stocks and
    commodities has to stay alive until 23:30, long after the equities it
    also carries have closed. Using one fixed NSE window would silently
    discard the entire MCX evening session.
    """
    now = now or datetime.now(IST)
    open_segments = set()
    for sym in symbols:
        ok, _ = is_ticker_open(sym, now)
        if ok:
            open_segments.add(segment_for(sym))
    if open_segments:
        return True, "open: " + ", ".join(sorted(open_segments))
    return False, "all segments closed"


class CandleBuilder:
    """Aggregates ticks into 1-minute OHLCV bars, in memory until complete."""

    def __init__(self):
        self._bars = {}
        self._lock = threading.Lock()

    def add_tick(self, symbol, ltp, volume, ts):
        if ltp is None:
            return
        minute = ts.replace(second=0, microsecond=0)
        key = (symbol, minute)
        with self._lock:
            bar = self._bars.get(key)
            if bar is None:
                self._bars[key] = {"open": ltp, "high": ltp, "low": ltp,
                                   "close": ltp, "volume": volume or 0, "ticks": 1}
            else:
                bar["high"] = max(bar["high"], ltp)
                bar["low"] = min(bar["low"], ltp)
                bar["close"] = ltp
                bar["ticks"] += 1
                # Fyers sends cumulative day volume - take latest, never sum.
                if volume:
                    bar["volume"] = volume

    def pop_completed(self, now):
        current = now.replace(second=0, microsecond=0)
        out = []
        with self._lock:
            for key in [k for k in self._bars if k[1] < current]:
                out.append((key[0], key[1], self._bars.pop(key)))
        return out

    def pending_count(self):
        with self._lock:
            return len(self._bars)


def open_db(path, ddl, index_ddl):
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    # Four processes write to these files. WAL allows concurrent readers and
    # serialises writers; busy_timeout makes a worker wait its turn rather
    # than erroring out when another shard happens to be mid-write.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(ddl)
    conn.execute(index_ddl)
    conn.commit()
    return conn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", required=True, help="account label to run")
    ap.add_argument("--ignore-hours", action="store_true")
    args = ap.parse_args()

    label = args.shard

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{label}] %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(BASE_DIR / f"worker_{label}.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger(label)

    universe = json.loads(UNIVERSE_FILE.read_text())
    tokens = json.loads(TOKEN_FILE.read_text())

    symbols = universe["shards"].get(label)
    if not symbols:
        log.error("No symbols assigned to shard '%s'", label)
        sys.exit(1)

    token_entry = tokens.get(label)
    if not token_entry:
        log.error("No token for '%s' - run token_manager.py", label)
        sys.exit(1)
    if token_entry.get("generated_on") != date.today().isoformat():
        log.error("Token for '%s' is stale - run token_manager.py", label)
        sys.exit(1)

    tick_symbols = set(universe.get("tick_symbols", []))
    builder = CandleBuilder()
    tick_queue = []
    state = {"messages": 0, "connected": False, "last_msg": None}

    cdb = open_db(
        CANDLE_DB,
        """CREATE TABLE IF NOT EXISTS candles_1m (
               symbol TEXT NOT NULL, minute_ts TEXT NOT NULL,
               open REAL, high REAL, low REAL, close REAL,
               volume INTEGER, tick_count INTEGER,
               PRIMARY KEY (symbol, minute_ts))""",
        "CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles_1m(minute_ts)")
    tdb = open_db(
        TICK_DB,
        """CREATE TABLE IF NOT EXISTS ticks (
               symbol TEXT NOT NULL, ts TEXT NOT NULL, ltp REAL,
               volume INTEGER, bid REAL, ask REAL)""",
        "CREATE INDEX IF NOT EXISTS idx_ticks_sym_ts ON ticks(symbol, ts)")

    def on_message(msg):
        state["messages"] += 1
        state["last_msg"] = datetime.now(IST)
        try:
            if not isinstance(msg, dict):
                return
            symbol = msg.get("symbol")
            if not symbol:
                return
            ltp = msg.get("ltp")
            volume = msg.get("vol_traded_today") or msg.get("volume")
            now = datetime.now(IST)
            builder.add_tick(symbol, ltp, volume, now)
            if symbol in tick_symbols:
                tick_queue.append((symbol, now.isoformat(), ltp, volume,
                                   msg.get("bid_price"), msg.get("ask_price")))
        except Exception:
            log.exception("error handling message")

    def on_error(msg):
        log.error("websocket error: %s", msg)

    def on_close(msg):
        state["connected"] = False
        log.warning("websocket closed: %s", msg)

    def on_connect():
        state["connected"] = True
        log.info("connected - subscribing to %d symbols", len(symbols))
        try:
            socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
            socket.keep_running()
        except Exception:
            log.exception("subscribe failed")

    socket = data_ws.FyersDataSocket(
        access_token=token_entry["ws_token"],
        write_to_file=False,
        log_path=str(BASE_DIR),
        litemode=False,
        reconnect=True,
        on_connect=on_connect,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message,
    )

    def handle_stop(signum, frame):
        _shutdown.set()
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    segs = {}
    for sym in symbols:
        segs[segment_for(sym)] = segs.get(segment_for(sym), 0) + 1
    log.info("starting with %d symbols across segments: %s",
             len(symbols), ", ".join(f"{k}={v}" for k, v in sorted(segs.items())))
    threading.Thread(target=socket.connect, daemon=True).start()

    def flush():
        written_c = written_t = 0
        completed = builder.pop_completed(datetime.now(IST))
        if completed:
            cdb.executemany(
                """INSERT OR REPLACE INTO candles_1m
                   (symbol, minute_ts, open, high, low, close, volume, tick_count)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(s, m.isoformat(), b["open"], b["high"], b["low"],
                  b["close"], b["volume"], b["ticks"])
                 for s, m, b in completed])
            cdb.commit()
            written_c = len(completed)
        if tick_queue:
            batch, tick_queue[:] = tick_queue[:], []
            tdb.executemany(
                "INSERT INTO ticks (symbol, ts, ltp, volume, bid, ask) VALUES (?,?,?,?,?,?)",
                batch)
            tdb.commit()
            written_t = len(batch)
        return written_c, written_t

    total_c = total_t = 0
    last_flush = last_status = time.time()

    try:
        while not _shutdown.is_set():
            time.sleep(1)
            now_t = time.time()

            if now_t - last_flush >= FLUSH_SECONDS:
                last_flush = now_t
                try:
                    c, t = flush()
                    total_c += c
                    total_t += t
                except Exception:
                    log.exception("flush failed - data kept in memory, will retry")

            if now_t - last_status >= 60:
                last_status = now_t
                age = ("never" if not state["last_msg"] else
                       f"{(datetime.now(IST) - state['last_msg']).total_seconds():.0f}s ago")
                log.info("connected=%s | %d msgs (last %s) | %d candles | %d ticks | %d pending",
                         state["connected"], state["messages"], age,
                         total_c, total_t, builder.pending_count())
                if not args.ignore_hours:
                    ok, why = any_symbol_open(symbols)
                    if not ok:
                        log.info("%s - stopping", why)
                        break
    finally:
        log.info("flushing before exit...")
        try:
            # Push the clock forward so the in-progress minute is also written
            # rather than discarded on shutdown.
            leftover = builder.pop_completed(datetime.now(IST) + timedelta(minutes=2))
            if leftover:
                cdb.executemany(
                    """INSERT OR REPLACE INTO candles_1m
                       (symbol, minute_ts, open, high, low, close, volume, tick_count)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [(s, m.isoformat(), b["open"], b["high"], b["low"],
                      b["close"], b["volume"], b["ticks"]) for s, m, b in leftover])
                cdb.commit()
                total_c += len(leftover)
            if tick_queue:
                tdb.executemany(
                    "INSERT INTO ticks (symbol, ts, ltp, volume, bid, ask) VALUES (?,?,?,?,?,?)",
                    tick_queue)
                tdb.commit()
                total_t += len(tick_queue)
        except Exception:
            log.exception("final flush failed")
        try:
            socket.close_connection()
        except Exception:
            pass
        cdb.close()
        tdb.close()
        log.info("stopped. %d candles, %d ticks, %d messages",
                 total_c, total_t, state["messages"])


if __name__ == "__main__":
    main()
