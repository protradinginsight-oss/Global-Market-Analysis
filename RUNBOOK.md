# Runbook

How to operate the system day to day, and what to do when something breaks.

Everything lives on `E:\Global Trading Analysis`. Python 3.12 — not 3.14,
which cannot install the Fyers SDK.

---

## Daily, market days

### Before 9:15

| Step | What | Why |
|---|---|---|
| 1 | `fyers-live\refresh_tokens.bat` | Fyers tokens expire nightly. Four browser logins, fresh incognito window for each - the session carries over otherwise and you end up with four tokens for one account. |
| 2 | `fyers-live\run_collector.bat` | Live tick collection, 235 symbols. Leave running. |
| 3 | `fyers-live\run_watchdog.bat` | Catches feeds that go silent while staying connected. Leave running. |
| 4 | `fyers-live\run_chains.bat` | Option chains, 3 expiries per index. Leave running. |
| 5 | `check.bat` | Watch this. The **New** column is the thing that matters. |
| 6 | `brief.py` | One read across everything. |

Steps 1-4 each open their own window and stay open.

### What healthy looks like in `check.bat`

```
live candles          12,450     +230      8s
live ticks            89,220   +1,840      2s
option chains          8,610      +82     45s
```

Numbers climbing every refresh, ages in seconds. The verdict line at the
bottom says so in words.

### After the close

Nothing required. Collectors stop themselves - NSE shards at 15:30, the MCX
shard at 23:30. FII/DII runs itself at 19:30 and 21:00.

---

## Scheduled automatically

| When | What |
|---|---|
| 08:00 daily | events `--fetch` |
| 08:15, 09:00 | global pre-market |
| 08:20 daily | dashboard rebuild |
| 19:30, 21:00 | FII/DII |
| Sunday 18:00 | MCX contract rebuild |
| 1st of month | universe snapshot |

Set these up once with `setup_all_schedules.bat`.

---

## When something looks wrong

**`check.bat` shows a stale feed during market hours.** Look at
`fyers-live\worker_<shard>.log`. The watchdog restarts a stalled shard up to
three times, then stops and says so rather than looping.

**A worker keeps dying.** Its log will say why. The most common causes are a
stale token and a lapsed subscription. `py -3.12 token_manager.py --check`
answers the first in a second.

**Only some symbols are updating.** That is the partial-failure case the
watchdog exists for - it flags a shard as PARTIAL when under 60% of its
symbols are reporting, which a plain freshness check would miss entirely.

**An MCX contract stopped updating.** It probably expired. Run the Sunday
rebuild manually:
```
py -3.12 mcx_setup.py --explore
py -3.12 mcx_setup.py --build
py -3.12 merge_universe.py
```

**A number looks wrong.** Trust that instinct. Four separate bugs during the
build were caught by an implausible figure rather than by anything failing:
a backtest showing +4.6R, back-month IV at 27% against a 10% front, PCR of
5.03 on a barely-traded index, and gamma exposure of ₹3.9 trillion. In every
case the code ran perfectly and produced something that could not be true.

Before acting on any output, ask roughly what the number should be. The tools
flag thin data where they can, but they cannot sanity-check their own
magnitudes.

---

## What each tool answers

**Is everything running?** `healthcheck.py --watch`
**What is the overall picture?** `brief.py`, `dashboard.py`
**What is the options market pricing?** `chain_analytics.py`,
`term_structure.py`, `gamma.py`, `buildup.py`
**What kind of market is this?** `regime.py`, `breadth.py`
**What is scheduled?** `events.py --calendar`
**What am I risking?** `portfolio_risk.py --stress --capital N`
**Did the idea ever work?** `backtest_camarilla.py`, `cost_model.py`
**What did I actually do?** `journal.py stats`

---

## Known limitations

These are properties of the data sources, not bugs:

**FII/DII is evening-only.** There is no intraday feed, for anyone. Index
futures OI is the usual proxy.

**Put-call skew is not measurable.** Fyers returns one IV per strike rather
than one per leg, so call and put vol cannot be separated.

**The events calendar reaches about two to four weeks.** Board meetings are
announced roughly a fortnight ahead, corporate actions about a month. A short
forward list is the source's limit, not a failed fetch.

**Volatility in the regime module is realised, not implied.** India VIX is
not exposed by Fyers, so 20-day realised volatility stands in. It reacts
after a shock rather than before one.

**Gamma exposure assumes dealer positioning.** The convention of dealers long
calls and short puts is a heuristic; the chain shows contracts, not who holds
them. If that assumption is wrong on a given day, the sign is wrong.

**Indian F&O history has survivorship bias.** Today's 213 names are the
survivors. `universe_snapshot.py` records membership monthly so future work
can correct for it, but the record only starts from August 2026.

---

## What the evidence actually says

Worth keeping in view, because it is easy to forget once there are this many
tools:

**Camarilla R3/S3 loses money.** Best configuration was +0.080R before costs
across 18,318 setups; round-trip costs are 0.175-0.258R. Net negative at
every position size, and still negative at optimistic slippage.

**Crude and USDINR do not lead Nifty.** Measured across five years of daily
returns: crude -0.03, USDINR -0.13 same-day. The pre-market panel originally
weighted them equally with the S&P, which is why it produced confident
readings from noise.

**Global cues explain roughly 6% of Nifty's daily variance.** The strongest
lagged correlation is 0.24, from the S&P. Real, usable as context, nowhere
near enough for a directional call.

Nothing here forecasts direction. It describes conditions. Position sizing
and risk management still do the work.
