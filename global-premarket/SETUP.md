# Global pre-market panel - setup

Pulls overnight global cues (US indices, DXY, USDINR, Brent, gold) and
prints a weight-of-evidence read for the Indian open.

## Why not Fyers?

Fyers covers NSE/BSE and MCX - it's the right source for Nifty, BankNifty,
stocks and MCX contracts, and the 4 Fyers accounts matter a lot for the
live Indian tick-data component. But Fyers does not carry S&P futures, DXY,
Brent or COMEX gold, so this component needs an external provider. The two
are complementary, not redundant.

## 1. Get a free API key
Sign up at https://twelvedata.com/pricing - the free tier gives 800
requests/day and 8/minute. This component uses 6 requests per run, so even
running it several times a day is nowhere near the limit.

## 2. Create your config
```
cd "E:\Global Trading Analysis\global-premarket"
copy config_local.example.py config_local.py
notepad config_local.py
```
Paste your key in, save, close. `config_local.py` is gitignored and will
never be committed.

## 3. Test it manually
```
py global_premarket.py
```
This is the first real test against the live API - it was built and tested
against mocked responses only, because the build sandbox couldn't reach
twelvedata.com. Expect one of:
- **A printed snapshot table** - working.
- **"invalid api key"** - key not activated yet, or a typo. Twelve Data
  sometimes takes a minute after signup.
- **A symbol error on one or two instruments** - some symbols vary by plan
  tier. The script tolerates partial failure and tells you which ones
  failed; send the message and the symbol can be swapped.
- **"PARTIAL DATA" prefix on the verdict** - some instruments fetched, some
  didn't. The read is still shown but explicitly flagged as incomplete.

### A note on the symbols used

Index and futures symbols (SPX, IXIC, DXY, BRENT) are premium-tier on Twelve
Data and return 404 on the free plan. This component therefore uses ETF
proxies - SPY, QQQ, UUP, USO - which are ordinary US-listed equities and
work on free tiers.

They track their underlying closely enough for a directional read, but they
are not identical: ETFs carry expense ratios, can trade at a small premium or
discount to NAV, and only price during US market hours. So an overnight move
in the actual S&P future may not show up in SPY until the US opens. For a
"which way is the wind blowing" read this is fine; for precise levels it is
not. Forex (USD/INR) and metals (XAU/USD) are direct, not proxied.

## 4. Check what got stored
```
py -c "import sqlite3; c=sqlite3.connect('global_premarket.db'); [print(r) for r in c.execute('SELECT * FROM global_snapshot ORDER BY captured_at DESC LIMIT 12')]"
```

## 5. Schedule it
```
setup_scheduled_tasks.bat
```
Runs at 8:15 AM and 9:00 AM IST daily - after the US close, before the
Indian open. Verify:
```
schtasks /query /tn "Global_Premarket_815AM"
```

## How to read the output

Each instrument is tagged with how its move affects Indian equities:
- US indices up -> supportive (global risk appetite)
- DXY up -> headwind (strong dollar pressures EM flows)
- USDINR up -> headwind (weakening rupee pressures FII flows)
- Brent up -> headwind (India is a net oil importer)
- Gold -> context only, no directional tag

The verdict counts agreement across these. Important: when cues disagree it
says so rather than averaging them into a false signal, and when data is
missing it says that too. This is weight of evidence to layer under your own
read - not a prediction, and not a trade signal. Global cues set the tone
for the open; they do not determine the day.

## Not covered yet
- No historical comparison ("is this move unusual for this instrument?")
  - that needs a few weeks of accumulated snapshots first
- No India VIX / GIFT Nifty - GIFT Nifty data is licensing-restricted
  through retail broker APIs, noted in the main checklist
- No alerting - add the same Telegram pattern as the FII/DII tracker if
  wanted
