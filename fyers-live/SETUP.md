# Fyers live Indian market data - setup

**Status: token manager built and logic-tested. Collector not built yet.**

This is step 1 of the Fyers component: getting authenticated. Nothing else
can be built or tested until daily tokens work reliably, so it's deliberately
separate from the data collector.

## Why the daily login can't be automated away

Fyers access tokens expire daily and require an interactive browser login to
regenerate. This isn't a limitation of this script - it's how their OAuth
flow works, and every Fyers-based system has the same constraint. The token
manager makes it a ~30-second morning task across all 4 accounts rather than
four separate manual processes.

## 1. Install the SDK
```
pip install fyers-apiv3
```

## 2. Fill in your credentials
```
cd "E:\Global Trading Analysis\fyers-live"
copy config_local.example.py config_local.py
notepad config_local.py
```

For each of your 4 accounts, from https://myapi.fyers.in/dashboard:
- `client_id` - looks like `XXXXXXXXXX-100`
- `secret_key`
- `redirect_uri` - must match the registered app EXACTLY, character for
  character. A trailing slash mismatch is a common cause of vague login
  failures.

The `label` is a name you choose. Keep labels stable once set, since tokens
are stored against them.

Save, close. `config_local.py` is gitignored - your keys never leave the
machine.

## Four separate accounts - the setup that trips people up

Four *different* Fyers accounts (not four apps on one account) means:

**Each account needs its own API app.** Log into myapi.fyers.in as that
account and create an app there. An app created under account 1 will only
ever authenticate account 1, regardless of which login screen you see.

**The browser session carries over between logins.** This is the real trap.
After you authorise account 1, your browser stays logged in. When the script
opens the login page for account 2, Fyers may skip the login form entirely
and authorise account 1 again. You end up with four tokens that all belong
to one account, everything appears to work, and you silently get one
account's capacity split four ways instead of four independent connections.

The script now handles this two ways: it pauses before each subsequent login
to remind you to log out or use an incognito window, and after all logins it
checks the `fy_id` behind each token and warns loudly if any two labels
resolved to the same account.

Smoothest approach: use a separate incognito/private window per account, and
close it before starting the next one.

## What four accounts actually buys you

- **4 x 200 = 800 concurrent websocket symbols** instead of 200. This is the
  main win, and it's what makes the full F&O underlying universe reachable.
- **4 separate daily REST quotas**, so backfills and option-chain polling
  don't eat into the budget the live feeds need.
- **Failure isolation** - one account's session dropping doesn't take down
  everything.

What it does *not* do: raise the limit on any single symbol, or bypass
rate limiting on a given account. Requests for the same symbol from four
accounts is still four requests, not a way around throttling.

## 3. First run
```
py token_manager.py
```

For each account it will:
1. Open the Fyers login page in your browser
2. Wait for you to log in
3. Redirect you to a page that may look blank or broken - **this is normal**
4. Ask you to paste that page's full URL back into the terminal

Paste the whole URL from the address bar. It extracts the auth code itself,
and handles the case where the URL contains both `code=200` (an HTTP status)
and the actual `auth_code` - a genuinely easy thing to get wrong by hand.

Each token is validated with a real API call before being saved. A bad paste
fails immediately here rather than silently at 9:15 when the market opens.

## 4. Check status any time
```
py token_manager.py --check
```
Shows FRESH / STALE / MISSING per account. Exit code 0 if all fresh, 1 if
any need attention - useful for scripting later.

## Daily routine

Each morning before the open:
```
refresh_tokens.bat
```
Or `py token_manager.py`, which skips accounts already refreshed today. Use
`--all` to force refresh everything.

## What's next (not built yet)

- **Symbol universe**: fetching the current NSE F&O underlying list
- **Websocket collectors**: sharded across the 4 accounts
- **1-minute aggregation** for the broad universe, tick storage for a
  selected few symbols
- **Watchdog**: feed-freshness monitoring and auto-restart

### An important constraint to know about now

The Fyers v3 data socket supports **200 symbols per connection**. With 4
accounts that's 800 concurrent symbols. That comfortably covers all F&O
underlying stocks (~180-220) plus indices.

It does *not* cover full option chains: roughly 40 contracts per stock
(20 strikes x CE/PE, single expiry) x 200 stocks is around 8,000 contracts,
an order of magnitude beyond the ceiling. Option chain data therefore has
to come from REST polling on demand, not the websocket - the same approach
the existing jumerah.in dashboards use.

## Security note

`fyers_tokens.json` holds live access tokens and is gitignored. Keep this
folder off any synced/shared drive (OneDrive, Dropbox, Google Drive), since
those would copy credentials off the machine.
