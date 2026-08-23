#!/usr/bin/env python3
"""
Technical indicator library.

Pure functions over price series - no dependencies beyond the standard
library, so nothing breaks when a package updates. Each takes lists of
prices and returns a list the same length, with None where there isn't
enough history to compute a value yet.

That None-padding matters: silently returning a shorter list is how
indicators end up misaligned with their dates, which produces backtests
that look great and are wrong.

Usage as a library:
    from indicators import rsi, macd, vwap, atr
    values = rsi(closes, period=14)

Run directly to self-test against hand-worked values:
    py -3.12 indicators.py
"""


def sma(values, period):
    """Simple moving average."""
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values, period):
    """Exponential moving average, seeded with an SMA.

    Seeding with the SMA of the first `period` values is the standard
    convention; seeding with the first value alone makes early readings
    depend heavily on one data point.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder_smooth(values, period):
    """Wilder's smoothing - used by RSI, ATR and ADX.

    Distinct from EMA: uses 1/period rather than 2/(period+1), which makes it
    slower. Mixing the two up is a common source of indicator values that
    almost match a charting platform but not quite.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes, period=14):
    """Relative Strength Index, Wilder's original formulation."""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def to_rsi(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = to_rsi(avg_g, avg_l)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = to_rsi(avg_g, avg_l)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, and histogram."""
    ef = ema(closes, fast)
    es = ema(closes, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]

    valid = [v for v in line if v is not None]
    sig_vals = ema(valid, signal) if valid else []
    sig = [None] * len(closes)
    offset = len(closes) - len(valid)
    for i, v in enumerate(sig_vals):
        sig[offset + i] = v

    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def true_range(highs, lows, closes):
    out = [None] * len(highs)
    if not highs:
        return out
    out[0] = highs[0] - lows[0]
    for i in range(1, len(highs)):
        out[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    return out


def atr(highs, lows, closes, period=14):
    """Average True Range - Wilder smoothed."""
    tr = true_range(highs, lows, closes)
    vals = [t for t in tr if t is not None]
    smoothed = wilder_smooth(vals, period)
    out = [None] * len(highs)
    offset = len(highs) - len(vals)
    for i, v in enumerate(smoothed):
        out[offset + i] = v
    return out


def bollinger(closes, period=20, num_std=2.0):
    """Bollinger Bands: middle SMA plus/minus a standard deviation multiple."""
    mid = sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        upper[i] = m + num_std * sd
        lower[i] = m - num_std * sd
    return upper, mid, lower


def keltner(highs, lows, closes, period=20, mult=2.0):
    """Keltner Channels: EMA centre, ATR-based width.

    Unlike Bollinger, width tracks true range rather than closing-price
    standard deviation, so gaps widen Keltner but not Bollinger.
    """
    mid = ema(closes, period)
    a = atr(highs, lows, closes, period)
    upper = [None if (m is None or x is None) else m + mult * x
             for m, x in zip(mid, a)]
    lower = [None if (m is None or x is None) else m - mult * x
             for m, x in zip(mid, a)]
    return upper, mid, lower


def vwap(highs, lows, closes, volumes):
    """Volume Weighted Average Price, cumulative from the start of the series.

    Feed this one session at a time. VWAP resets each day in practice, so
    running it across multiple days gives a number no trader uses.
    """
    out = [None] * len(closes)
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(len(closes)):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        v = volumes[i] or 0
        cum_pv += typical * v
        cum_v += v
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def stochastic(highs, lows, closes, k_period=14, d_period=3):
    """Stochastic oscillator %K and %D."""
    k = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        hh = max(highs[i - k_period + 1:i + 1])
        ll = min(lows[i - k_period + 1:i + 1])
        k[i] = 100.0 * (closes[i] - ll) / (hh - ll) if hh > ll else 50.0
    valid = [v for v in k if v is not None]
    d_vals = sma(valid, d_period)
    d = [None] * len(closes)
    offset = len(closes) - len(valid)
    for i, v in enumerate(d_vals):
        d[offset + i] = v
    return k, d


def adx(highs, lows, closes, period=14):
    """ADX with +DI and -DI (Wilder's Directional Movement system)."""
    n = len(highs)
    out_adx = [None] * n
    plus_di = [None] * n
    minus_di = [None] * n
    if n < period * 2:
        return out_adx, plus_di, minus_di

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))

    sm_p = wilder_smooth(plus_dm, period)
    sm_m = wilder_smooth(minus_dm, period)
    sm_t = wilder_smooth(trs, period)

    dx = []
    for i in range(len(sm_t)):
        if sm_t[i] is None or sm_t[i] == 0:
            dx.append(None)
            continue
        p = 100.0 * sm_p[i] / sm_t[i]
        m = 100.0 * sm_m[i] / sm_t[i]
        plus_di[i + 1] = p
        minus_di[i + 1] = m
        dx.append(100.0 * abs(p - m) / (p + m) if (p + m) else 0.0)

    dx_valid = [d for d in dx if d is not None]
    adx_vals = wilder_smooth(dx_valid, period)
    offset = n - len(dx_valid)
    for i, v in enumerate(adx_vals):
        out_adx[offset + i] = v
    return out_adx, plus_di, minus_di


def supertrend(highs, lows, closes, period=10, mult=3.0):
    """Supertrend: ATR bands that flip with trend direction.

    Returns (line, direction) where direction is +1 for uptrend, -1 for down.
    """
    n = len(closes)
    a = atr(highs, lows, closes, period)
    line = [None] * n
    direction = [None] * n
    prev_upper = prev_lower = None
    prev_dir = 1

    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        upper = hl2 + mult * a[i]
        lower = hl2 - mult * a[i]

        if prev_upper is not None:
            upper = min(upper, prev_upper) if closes[i - 1] <= prev_upper else upper
            lower = max(lower, prev_lower) if closes[i - 1] >= prev_lower else lower

        if prev_upper is None:
            d = 1 if closes[i] > hl2 else -1
        elif closes[i] > prev_upper:
            d = 1
        elif closes[i] < prev_lower:
            d = -1
        else:
            d = prev_dir

        line[i] = lower if d == 1 else upper
        direction[i] = d
        prev_upper, prev_lower, prev_dir = upper, lower, d

    return line, direction


# ---------------------------------------------------------------------------
# Self-tests, run when this file is executed directly.
# ---------------------------------------------------------------------------

def _selftest():
    ok = True

    def check(name, got, expected, tol=1e-6):
        nonlocal ok
        if got is None:
            passed = expected is None
        elif expected is None:
            passed = False
        else:
            passed = abs(got - expected) < tol
        if not passed:
            ok = False
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: got {got}, expected {expected}")

    print("SMA")
    s = sma([1, 2, 3, 4, 5], 3)
    check("first two are None", s[0], None)
    check("sma[2] = (1+2+3)/3", s[2], 2.0)
    check("sma[4] = (3+4+5)/3", s[4], 4.0)
    check("length preserved", len(s), 5)

    print("\nEMA")
    e = ema([1, 2, 3, 4, 5], 3)
    check("seeded with SMA at index 2", e[2], 2.0)
    # k = 2/(3+1) = 0.5;  e[3] = 4*0.5 + 2*0.5 = 3.0
    check("e[3] = 4*0.5 + 2*0.5", e[3], 3.0)
    check("e[4] = 5*0.5 + 3*0.5", e[4], 4.0)

    print("\nRSI - all gains should give 100")
    r = rsi([float(i) for i in range(1, 20)], 14)
    check("straight uptrend -> 100", r[14], 100.0)
    print("RSI - all losses should give 0")
    r2 = rsi([float(i) for i in range(20, 1, -1)], 14)
    check("straight downtrend -> 0", r2[14], 0.0)

    print("\nTrue Range")
    tr = true_range([10, 12], [8, 9], [9, 11])
    check("first bar = high-low", tr[0], 2.0)
    # max(12-9, |12-9|, |9-9|) = 3
    check("second bar", tr[1], 3.0)

    print("\nVWAP")
    v = vwap([10, 10], [10, 10], [10, 10], [100, 100])
    check("flat prices -> same vwap", v[1], 10.0)
    v2 = vwap([10, 20], [10, 20], [10, 20], [100, 300])
    # (10*100 + 20*300) / 400 = 7000/400 = 17.5
    check("volume weighted", v2[1], 17.5)

    print("\nBollinger")
    u, m, l = bollinger([2, 4, 6, 8, 10], 5, 2.0)
    check("middle = mean", m[4], 6.0)
    # population sd of 2,4,6,8,10 = sqrt(8) = 2.828427
    check("upper = mean + 2sd", u[4], 6.0 + 2 * (8 ** 0.5))

    print("\nStochastic")
    k, d = stochastic([10] * 13 + [20], [0] * 14, [15] * 13 + [20], 14, 3)
    # last close 20, hh 20, ll 0 -> 100
    check("close at high of range -> 100", k[13], 100.0)

    print("\nNone-padding consistency (misalignment guard)")
    closes = [float(i) for i in range(50)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    vols = [1000] * 50
    for name, series in [
        ("sma", sma(closes, 20)), ("ema", ema(closes, 20)),
        ("rsi", rsi(closes, 14)), ("atr", atr(highs, lows, closes, 14)),
        ("vwap", vwap(highs, lows, closes, vols)),
    ]:
        check(f"{name} length == input", len(series), 50)
    line, sig, hist = macd(closes)
    check("macd length", len(line), 50)
    check("signal length", len(sig), 50)
    a, p, mn = adx(highs, lows, closes, 14)
    check("adx length", len(a), 50)
    st, dr = supertrend(highs, lows, closes, 10, 3.0)
    check("supertrend length", len(st), 50)

    print()
    print("ALL PASS" if ok else "SOME TESTS FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
