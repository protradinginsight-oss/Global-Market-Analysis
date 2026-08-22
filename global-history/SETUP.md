# Global historical data - setup

Long-horizon daily history for global indices, commodities, currencies,
rates and crypto, plus macro series and a curated table of major market
events. Indian market data lives in `fyers-live/` and is separate.

## Why Yahoo Finance and not Twelve Data

Twelve Data's free tier is built for recent quotes, not decades of history.
Yahoo needs no API key and reaches back to the 1990s for most indices.

The tradeoff: Yahoo's interface is unofficial and can break without notice.
That is precisely why this stores everything locally rather than querying
live - a break costs you future updates, not your existing history.

## 1. Install
```
py -3.12 -m pip install yfinance requests
```

## 2. Check the plan
```
cd "E:\Global Trading Analysis\global-history"
py -3.12 collect_global.py --dry-run
```

## 3. Collect
```
py -3.12 collect_global.py --years 20
```
Takes 5-15 minutes for 42 instruments plus 12 macro series. Rerunning is
safe and updates in place rather than duplicating.

## 4. Inspect
```
py -3.12 collect_global.py --status
```

## What you get

**Prices** (Yahoo, daily OHLCV): US/EU/Asia indices, VIX, crude, gold,
silver, copper, gas, DXY, major currency pairs, US Treasury yields, BTC/ETH,
and sector/flow ETFs including INDA and EEM.

**Macro** (FRED, free, no key): Fed funds, Treasury yields, the 10Y-2Y
spread, CPI, unemployment, the broad dollar index, high-yield credit
spreads, and the Fed balance sheet. Several of these run back to the 1950s.

**Events**: about 20 curated dates - Lehman, taper tantrum, Brexit, COVID,
negative oil, SVB, the yen carry unwind, Indian election results. Hand-
maintained in `universe.py`.

## What is NOT included, and why

**Historical news archives.** Ten years of searchable financial news is a
commercial product. GDELT is free and covers global event data from 2015,
but it gives event metadata rather than article text, and the volume is
enormous. Worth adding deliberately later if news-based signals turn out to
matter - not worth bolting on speculatively.

**Merger and demerger history.** No reliable free source covers this
historically for Indian or global markets. Corporate action data is sold,
not published. Recording them going forward from exchange filings is
achievable; reconstructing the past is not.

**Anything intraday.** This is daily only. Yahoo's intraday history is
limited to roughly 60 days regardless of what you request.

## A warning about survivorship bias

The Indian F&O universe in `fyers-live/` is *today's* list. Backtesting it
over 10 years quietly assumes you would have been trading exactly the stocks
that survived and still qualify - which you would not have been. Results on
long-horizon tests will be optimistic for that reason.

The global universe here is less affected, since indices and commodities do
not disappear, but the sector ETFs have the same issue in milder form.

If ML work is planned later, this matters more than it might seem: a model
trained on survivors learns patterns that were only visible in hindsight.
Recording the F&O universe as it changes, starting now, is much easier than
reconstructing it afterwards.
