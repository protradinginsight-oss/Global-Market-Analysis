# Global Market Analysis

A local-first market analysis and trading-signal system: data collection,
storage, and analytics run on your own machine; a cloud/dashboard layer gets
added later, once individual pieces are proven locally.

Repo root corresponds to `E:\Global Trading Analysis` on the build machine.
Each component lives in its own self-contained subfolder with its own setup
instructions, so new ones can be added without disturbing what already works.

## Components

### fii-dii-tracker/
Fetches NSE's daily provisional FII/DII cash-market flow and stores it in
SQLite. Runs at 7:30 PM and 9 PM IST via Task Scheduler. Deduplicates against
the trade date NSE actually returns, so weekend/holiday runs don't refetch or
overwrite with stale data. **Status: live and working.**

### global-premarket/
Pulls overnight global cues (US indices, DXY, USDINR, Brent, gold) via Twelve
Data and prints a weight-of-evidence read for the Indian open. Runs at 8:15
and 9:00 AM IST. Tolerates partial API failure and flags incomplete data
rather than presenting a confident-sounding read built on missing inputs.
**Status: built and logic-tested, pending first live API run.**

## Data sources, and why more than one

- **Fyers** (4 accounts) - NSE/BSE and MCX. The source for Indian equities,
  F&O, and MCX commodities, including live tick data. Not yet wired up.
- **Twelve Data** - global indices, DXY, forex, international commodities.
  Fyers does not carry these, so an external provider is required for the
  global picture.
- **NSE public reports** - FII/DII flow, EOD only. There is no real-time
  FII/DII feed available to anyone; the provisional figures publish in the
  evening.

## Conventions for new components

- Self-contained subfolder
- `config_local.example.py` committed, real `config_local.py` gitignored
- Paths resolved from the script's own location, so the project can move
  drives without edits
- A `run_*.bat` wrapper plus a `setup_scheduled_tasks.bat`
- Own `SETUP.md`, including what the component does *not* cover yet
