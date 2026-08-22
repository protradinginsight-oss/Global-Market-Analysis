# Global Market Analysis

A local-first market analysis and trading-signal system: data collection,
storage, and analytics run on your own machine; a public dashboard layer
gets added later as components are built. See the deployment architecture
notes (from the planning conversation) for the overall design - local
processing now, cloud migration once individual pieces are tested.

Repo root corresponds to `E:\Global Trading Analysis` on the build machine.
Each major piece lives in its own subfolder, self-contained with its own
setup instructions, so new components can be added as siblings without
disturbing existing ones.

## Components

### fii-dii-tracker/
Fetches NSE's daily provisional FII/DII cash-market flow data and stores it
locally in SQLite. Sends a Telegram alert on success or failure. Runs once a
day via a scheduler and is safe to trigger more than once - it checks
whether today's data is already stored before doing anything.

Currently deployed on Windows (Task Scheduler). A macOS version is kept in
the folder in case this ever moves back.

See `fii-dii-tracker/SETUP.md` for setup steps.

## Adding new components
Same pattern each time: a self-contained subfolder, its own
`config_local.example.py` if it needs credentials (never commit the real
`config_local.py` - already covered by the root `.gitignore`), and its own
SETUP.md.
