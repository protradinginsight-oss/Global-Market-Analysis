# Setup (Windows)

Note: this folder also has `com.tracker.fiidii.plist`, a macOS launchd
version, kept in case this ever moves back to a Mac. Everything below is
for Windows, which is the current setup.

Assumes this whole repo lives at `E:\Global Trading Analysis`, so this
component is at `E:\Global Trading Analysis\fii-dii-tracker`.

## 1. Telegram alerts (optional but recommended, ~2 minutes)
- Message **@BotFather** on Telegram, send `/newbot`, follow the prompts — gives you a bot token
- Message your new bot anything once, then visit
  `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser — your
  chat ID is in the response under `message.chat.id`
- In this folder, copy `config_local.example.py` to `config_local.py` and
  fill in both values. **`config_local.py` is gitignored — it will never be
  committed, even in a public repo.**

```
cd "E:\Global Trading Analysis\fii-dii-tracker"
copy config_local.example.py config_local.py
notepad config_local.py
```

## 2. Confirm Python is on your PATH
```
py --version
```
If that fails, install Python from python.org and check "Add python.exe to
PATH" during install.

## 3. Install the one dependency
```
pip install requests
```

## 4. Test it manually — do this before touching Task Scheduler at all
```
cd "E:\Global Trading Analysis\fii-dii-tracker"
py fii_dii_tracker.py
type fii_dii.log
```
This is the part that couldn't be tested from the sandbox this was built in —
its network doesn't reach nseindia.com. So this manual run is the actual
first real test. Possible outcomes:
- **403 / blocked**: NSE's anti-bot check didn't like the request — share
  the log and it can be adjusted
- **Empty response before ~6:30 PM IST**: normal, data isn't published yet
- **KeyError / missing fields**: NSE changed the response shape — share the
  actual JSON from the log and the parsing gets fixed

Don't move to step 6 until this manual run has produced a row in the database
(both `fii_dii.db` and `fii_dii.log` will appear right in this same folder).

## 5. Check the database
```
py -c "import sqlite3; c = sqlite3.connect('fii_dii.db'); print(c.execute('SELECT * FROM fii_dii_flow').fetchall())"
```

## 6. Install the scheduled version
`setup_scheduled_tasks.bat` already assumes the `E:\Global Trading Analysis`
path — if that's really where this is, no editing needed. Just run it once,
as your normal user, no admin required:
```
setup_scheduled_tasks.bat
```

Check it's registered:
```
schtasks /query /tn "FII_DII_Tracker_730PM"
```

Remove a task later if needed:
```
schtasks /delete /tn "FII_DII_Tracker_730PM" /f
```

Not built yet, worth knowing: the basic setup above doesn't retry
automatically if a run fails outright (e.g. no internet at 7:30 PM sharp) —
the 9 PM run and the on-logon run are what catch that in practice for now.
A "retry every N minutes on failure" rule can be added later via Task
Scheduler's Settings tab — not necessary for a once-a-day job, more relevant
once live intraday collectors exist.

## Publishing/updating this repo on GitHub
```
cd "E:\Global Trading Analysis"
git add -A
git commit -m "describe what changed"
git push
```
When prompted for credentials: username is your GitHub username, password is
your personal access token (not your actual GitHub password — that stopped
working for git operations years ago). Double check on GitHub afterward that
`config_local.py` never appears in the repo — only `config_local.example.py`
should be there.

## What this does and doesn't cover yet
- Runs at 7:30 PM and 9 PM IST daily, plus immediately on login (Windows
  Task Scheduler's "at logon" trigger — the auto-resume behavior)
- Won't duplicate data if triggered more than once in a day
- Telegram alert on both success and failure
- Doesn't yet have the watchdog process from the deployment architecture doc
  (feed-freshness checks, auto-restart) — overkill for a once-a-day job,
  matters more once live intraday collectors exist
- Doesn't yet handle "PC asleep at both 7:30 PM and 9 PM" — the on-logon
  trigger covers waking up and logging back in, but if the machine stays
  asleep through both scheduled times and isn't logged into again until the
  next morning, that day's fetch is simply missed
