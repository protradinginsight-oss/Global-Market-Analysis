#!/usr/bin/env python3
"""
Per-segment market hours.

NSE and MCX keep very different hours, so a single hardcoded 9:15-15:30
schedule silently stops MCX collection at 3:30 PM and loses the entire
evening session - which for crude and gold is when a lot of the movement
happens, because that's when US markets are active.

Hours here were verified against the session field in Fyers' own symbol
master files rather than assumed. MCX reports "0900-2330" for almost every
contract and "0900-1800" for a handful of agri ones.

If MCX shifts its close with US daylight saving, the symbol file will say
so - re-run mcx_setup.py --explore and check the session field rather than
adjusting these by guesswork.

Usage as a library:
    from market_hours import is_open, segment_for
    open_now, reason = is_open("MCX")
"""

from datetime import datetime, timezone, timedelta, time as dtime

IST = timezone(timedelta(hours=5, minutes=30))

SEGMENTS = {
    "NSE_CM": {
        "label": "NSE cash",
        "open": dtime(9, 15), "close": dtime(15, 30),
        "weekdays": {0, 1, 2, 3, 4},
    },
    "NSE_FO": {
        "label": "NSE derivatives",
        "open": dtime(9, 15), "close": dtime(15, 30),
        "weekdays": {0, 1, 2, 3, 4},
    },
    "BSE_CM": {
        # BSE keeps the same hours as NSE, but labelling it separately makes
        # the collector's segment summary honest - reporting 218 "NSE_CM"
        # symbols when 5 are BSE is misleading even if the behaviour is right.
        "label": "BSE cash/index",
        "open": dtime(9, 15), "close": dtime(15, 30),
        "weekdays": {0, 1, 2, 3, 4},
    },
    "MCX": {
        # Non-agri commodities: crude, gold, silver, copper, zinc, gas.
        # Hours taken from the session field in Fyers' MCX symbol master,
        # which reports "0900-2330" for the overwhelming majority of
        # contracts. Earlier versions of this file guessed 23:55 based on
        # the summer DST close; the exchange file says otherwise.
        "label": "MCX commodities",
        "open": dtime(9, 0), "close": dtime(23, 30),
        "weekdays": {0, 1, 2, 3, 4},
    },
    "MCX_AGRI": {
        # A small number of MCX contracts report "0900-1800" instead.
        "label": "MCX agri",
        "open": dtime(9, 0), "close": dtime(18, 0),
        "weekdays": {0, 1, 2, 3, 4},
    },
    "NSE_CD": {
        # Currency derivatives.
        "label": "NSE currency",
        "open": dtime(9, 0), "close": dtime(17, 0),
        "weekdays": {0, 1, 2, 3, 4},
    },
}


def segment_for(ticker):
    """Work out which segment a Fyers ticker belongs to."""
    t = (ticker or "").upper()
    if t.startswith("MCX:"):
        # Agri contracts on MCX keep shorter hours than metals and energy.
        agri = ("COTTON", "CPO", "MENTHAOIL", "CASTOR", "KAPAS",
                "RUBBER", "CARDAMOM", "PEPPER")
        return "MCX_AGRI" if any(a in t for a in agri) else "MCX"
    if t.startswith("BSE:"):
        return "BSE_CM"
    if "USDINR" in t or "EURINR" in t or "GBPINR" in t or "JPYINR" in t:
        return "NSE_CD"
    if t.endswith("-INDEX") or t.endswith("-EQ"):
        return "NSE_CM"
    return "NSE_FO"


def is_open(segment, now=None):
    """Is this segment trading right now? Returns (bool, reason)."""
    cfg = SEGMENTS.get(segment)
    if not cfg:
        return False, f"unknown segment '{segment}'"
    now = now or datetime.now(IST)
    if now.weekday() not in cfg["weekdays"]:
        return False, "weekend"
    t = now.time()
    if t < cfg["open"]:
        return False, f"pre-open (opens {cfg['open'].strftime('%H:%M')})"
    if t > cfg["close"]:
        return False, f"closed (closed {cfg['close'].strftime('%H:%M')})"
    return True, "open"


def is_ticker_open(ticker, now=None):
    return is_open(segment_for(ticker), now)


def any_open(segments, now=None):
    """True if any of these segments is trading - used to decide whether a
    collector should keep running at all."""
    for s in segments:
        ok, _ = is_open(s, now)
        if ok:
            return True
    return False


def summary(now=None):
    now = now or datetime.now(IST)
    lines = [f"Market hours at {now.strftime('%Y-%m-%d %H:%M:%S')} IST", "-" * 58]
    for key, cfg in SEGMENTS.items():
        ok, why = is_open(key, now)
        window = f"{cfg['open'].strftime('%H:%M')}-{cfg['close'].strftime('%H:%M')}"
        lines.append(f"  {cfg['label']:<22} {window:<14} "
                     f"{'OPEN' if ok else 'closed':<8} {'' if ok else why}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
    print()
    print("Segment routing for example tickers:")
    for t in ["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "NSE:NIFTY26AUG24000CE",
              "MCX:CRUDEOIL26SEPFUT", "MCX:GOLD26OCTFUT",
              "MCX:COTTON26SEPFUT", "NSE:USDINR26AUGFUT", "BSE:SENSEX-INDEX"]:
        seg = segment_for(t)
        ok, why = is_open(seg)
        print(f"  {t:<28} -> {seg:<10} {'OPEN' if ok else why}")
