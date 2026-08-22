#!/usr/bin/env python3
"""
The global instrument universe - what gets collected and why.

Kept separate from the collector so the list is easy to read and edit
without touching working code.

Yahoo Finance tickers. Index tickers start with '^', futures end with '=F',
currencies with '=X', crypto with '-USD'.
"""

# Each entry: (ticker, label, category, why it matters for an Indian trader)
GLOBAL_UNIVERSE = [

    # --- Indices: the overnight lead for the Indian open ---
    ("^GSPC",     "S&P 500",            "index_us",     "US risk appetite, the primary global cue"),
    ("^IXIC",     "Nasdaq Composite",   "index_us",     "Tech direction, leads Indian IT"),
    ("^DJI",      "Dow Jones",          "index_us",     "Old-economy US read"),
    ("^RUT",      "Russell 2000",       "index_us",     "US small caps, risk appetite at the margin"),
    ("^VIX",      "CBOE VIX",           "volatility",   "Global fear gauge"),
    ("^FTSE",     "FTSE 100",           "index_eu",     "UK/Europe session"),
    ("^GDAXI",    "DAX",                "index_eu",     "German industrials, Europe bellwether"),
    ("^FCHI",     "CAC 40",             "index_eu",     "France"),
    ("^STOXX50E", "Euro Stoxx 50",      "index_eu",     "Broad eurozone"),
    ("^N225",     "Nikkei 225",         "index_asia",   "Asia open, leads Indian session"),
    ("^HSI",      "Hang Seng",          "index_asia",   "China proxy, EM sentiment"),
    ("000001.SS", "Shanghai Composite", "index_asia",   "Mainland China"),
    ("^KS11",     "KOSPI",              "index_asia",   "Korea, semiconductor cycle read"),
    ("^AXJO",     "ASX 200",            "index_asia",   "Australia, commodity-linked"),
    ("^BSESN",    "BSE Sensex",         "index_india",  "Indian benchmark, for cross-reference"),
    ("^NSEI",     "Nifty 50",           "index_india",  "Indian benchmark, for cross-reference"),
    ("^NSEBANK",  "Nifty Bank",         "index_india",  "Indian banking"),

    # --- Commodities ---
    ("CL=F",      "WTI Crude",          "commodity",    "India is a net oil importer"),
    ("BZ=F",      "Brent Crude",        "commodity",    "The benchmark India actually prices against"),
    ("GC=F",      "COMEX Gold",         "commodity",    "Risk-off hedge, drives MCX gold"),
    ("SI=F",      "COMEX Silver",       "commodity",    "Industrial plus precious"),
    ("HG=F",      "Copper",             "commodity",    "Global growth proxy"),
    ("NG=F",      "Natural Gas",        "commodity",    "Energy input costs"),

    # --- Currencies ---
    ("DX-Y.NYB",  "Dollar Index",       "currency",     "Strong USD pressures EM flows"),
    ("USDINR=X",  "USDINR",             "currency",     "Directly drives FII flow economics"),
    ("EURUSD=X",  "EURUSD",             "currency",     "Major pair"),
    ("USDJPY=X",  "USDJPY",             "currency",     "Carry trade barometer"),
    ("GBPUSD=X",  "GBPUSD",             "currency",     "Major pair"),
    ("USDCNY=X",  "USDCNY",             "currency",     "China policy signal"),

    # --- Rates (yield indices) ---
    ("^TNX",      "US 10Y Yield",       "rates",        "Global discount rate"),
    ("^TYX",      "US 30Y Yield",       "rates",        "Long-end expectations"),
    ("^FVX",      "US 5Y Yield",        "rates",        "Belly of the curve"),
    ("^IRX",      "US 13W T-Bill",      "rates",        "Short end, Fed expectations"),

    # --- Crypto: fastest-reacting risk sentiment ---
    ("BTC-USD",   "Bitcoin",            "crypto",       "Risk appetite, reacts before equities"),
    ("ETH-USD",   "Ethereum",           "crypto",       "Second crypto read"),

    # --- Sector ETFs: what's leading globally ---
    ("XLE",       "US Energy ETF",      "sector_us",    "Energy sector rotation"),
    ("XLF",       "US Financials ETF",  "sector_us",    "Financials rotation"),
    ("XLK",       "US Tech ETF",        "sector_us",    "Tech rotation, leads Indian IT"),
    ("SMH",       "Semiconductor ETF",  "sector_us",    "Chip cycle, leads Indian tech"),
    ("EEM",       "EM Equity ETF",      "flows",        "EM flow proxy, correlates with FII"),
    ("INDA",      "India ETF (US)",     "flows",        "Direct foreign positioning in India"),
    ("GLD",       "Gold ETF",           "commodity",    "Retail gold demand proxy"),
]

# FRED series for macro data that price feeds don't carry. Free, no API key
# needed for CSV downloads.
FRED_SERIES = [
    ("FEDFUNDS",     "Fed Funds Rate",           "Policy rate, monthly, from 1954"),
    ("DFF",          "Fed Funds (daily)",        "Daily effective rate"),
    ("DGS10",        "US 10Y Treasury",          "Daily constant maturity, from 1962"),
    ("DGS2",         "US 2Y Treasury",           "Short end"),
    ("DGS30",        "US 30Y Treasury",          "Long end"),
    ("T10Y2Y",       "10Y-2Y Spread",            "Recession/regime signal"),
    ("T10Y3M",       "10Y-3M Spread",            "The curve inversion the Fed watches"),
    ("CPIAUCSL",     "US CPI",                   "Inflation, from 1947"),
    ("UNRATE",       "US Unemployment",          "Labour market, from 1948"),
    ("VIXCLS",       "VIX (daily close)",        "Volatility, from 1990"),
    ("DTWEXBGS",     "Broad Dollar Index",       "Trade-weighted USD"),
    ("BAMLH0A0HYM2", "US High Yield Spread",     "Credit risk appetite"),
    ("WALCL",        "Fed Balance Sheet",        "Liquidity expansion/contraction"),
    ("DCOILWTICO",   "WTI Spot",                 "Oil, from 1986"),
    # Long-history exchange rates. DEXINUS is particularly useful - daily
    # USDINR going back to 1973, far deeper than most price feeds carry.
    ("DEXINUS",      "USDINR (FRED)",            "Daily rupee rate from 1973"),
    ("DEXCHUS",      "USDCNY (FRED)",            "China rate, long history"),
    ("DEXJPUS",      "USDJPY (FRED)",            "Yen, long history"),
    ("M2SL",         "US M2 Money Supply",       "Monetary expansion"),
    ("UMCSENT",      "Consumer Sentiment",       "Michigan survey"),
    # Note: GOLDAMGBD228NLBM (London gold fixing) was in an earlier version
    # but FRED discontinued it. Gold is covered by GC=F and GLD from Yahoo.
]

# Major market-moving events, curated by hand. There is no reliable free
# feed of "significant global events", and an automated one would produce
# noise. A short curated list is more useful than a long scraped one -
# these are the dates you'd actually want to check a strategy against.
MAJOR_EVENTS = [
    ("2008-09-15", "Lehman Brothers collapse",        "crisis"),
    ("2013-05-22", "Taper tantrum begins",            "policy"),
    ("2014-05-16", "Indian general election result",  "india_politics"),
    ("2015-08-11", "China yuan devaluation",          "crisis"),
    ("2016-06-23", "Brexit referendum",               "geopolitics"),
    ("2016-11-08", "US election / Indian demonetisation", "policy"),
    ("2018-02-05", "Volmageddon (VIX spike)",         "volatility"),
    ("2019-05-23", "Indian general election result",  "india_politics"),
    ("2020-01-30", "WHO declares COVID emergency",    "pandemic"),
    ("2020-03-12", "COVID crash begins in earnest",   "pandemic"),
    ("2020-03-23", "COVID market bottom",             "pandemic"),
    ("2020-04-20", "WTI crude goes negative",         "commodity"),
    ("2021-01-27", "GameStop squeeze peak",           "market_structure"),
    ("2022-02-24", "Russia invades Ukraine",          "geopolitics"),
    ("2022-03-16", "Fed begins hiking cycle",         "policy"),
    ("2023-03-10", "SVB collapse",                    "crisis"),
    ("2023-10-07", "Israel-Hamas conflict begins",    "geopolitics"),
    ("2024-06-04", "Indian general election result",  "india_politics"),
    ("2024-08-05", "Yen carry trade unwind",          "crisis"),
]


def by_category():
    out = {}
    for ticker, label, cat, note in GLOBAL_UNIVERSE:
        out.setdefault(cat, []).append((ticker, label, note))
    return out


if __name__ == "__main__":
    print(f"Global universe: {len(GLOBAL_UNIVERSE)} instruments\n")
    for cat, items in sorted(by_category().items()):
        print(f"{cat}  ({len(items)})")
        for ticker, label, note in items:
            print(f"   {ticker:<14} {label:<22} {note}")
        print()
    print(f"FRED macro series: {len(FRED_SERIES)}")
    print(f"Curated major events: {len(MAJOR_EVENTS)}")
