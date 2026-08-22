#!/usr/bin/env python3
"""
Fyers token manager - handles daily login for all 4 accounts.

Fyers access tokens expire daily and require an interactive browser login;
there is no fully automated path around this, it's how their auth works.
This script makes the daily ritual as short as possible: it opens each
account's login page in turn, you log in, you paste back the redirect URL,
and it stores the token.

Tokens are validated before being saved, so a bad paste fails immediately
here rather than silently at 9:15 when the market opens.

Usage:
    py token_manager.py              # refresh any account whose token is stale
    py token_manager.py --all        # force refresh every account
    py token_manager.py --check      # just report token status, change nothing
"""

import json
import sys
import argparse
import webbrowser
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    print("Missing fyers-apiv3. Install it with:  pip install fyers-apiv3")
    sys.exit(1)

try:
    from config_local import ACCOUNTS
except ImportError:
    print(
        "Missing config_local.py.\n"
        "Copy config_local.example.py to config_local.py and fill in your "
        "4 Fyers accounts' credentials (see SETUP.md)."
    )
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "fyers_tokens.json"


def load_tokens():
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        print(f"WARNING: {TOKEN_FILE.name} is corrupt - starting fresh.")
        return {}


def save_tokens(tokens):
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    # Tokens are credentials. On Windows this file inherits folder perms;
    # it is gitignored, but keep it off shared/synced folders.


def is_fresh(entry):
    """A token is usable only if it was generated today.

    Fyers tokens expire at end of day, so 'generated today' is the practical
    test. Being conservative here is deliberate: a stale token fails at the
    open, which is the worst possible time to discover it.
    """
    if not entry or "generated_on" not in entry:
        return False
    return entry["generated_on"] == date.today().isoformat()


def validate_token(client_id, access_token):
    """Confirm the token actually works before trusting it.

    Catches the common failure where a wrong or truncated URL was pasted:
    the token gets stored, looks fine, and then nothing works during market
    hours with no obvious cause.

    Returns (ok, name, fy_id). fy_id is the actual Fyers account the token
    belongs to - used to detect the browser-session trap where you end up
    authorising the same account several times over.
    """
    try:
        fyers = fyersModel.FyersModel(
            client_id=client_id, token=access_token, log_path=str(BASE_DIR)
        )
        resp = fyers.get_profile()
        if isinstance(resp, dict) and resp.get("s") == "ok":
            data = resp.get("data", {})
            return True, data.get("name", "unknown"), data.get("fy_id", "unknown")
        return False, str(resp)[:200], None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


def extract_auth_code(pasted):
    """Pull auth_code out of whatever the user pasted.

    Accepts the full redirect URL or just the bare code, because after a
    browser redirect it's genuinely unclear which one is wanted.
    """
    pasted = pasted.strip()
    if not pasted:
        return None
    if pasted.startswith("http://") or pasted.startswith("https://"):
        qs = parse_qs(urlparse(pasted).query)
        codes = qs.get("auth_code") or qs.get("code")
        if not codes:
            return None
        return codes[0]
    return pasted


def refresh_account(acct, is_first):
    """Run the interactive login for one account. Returns token dict or None."""
    label = acct["label"]
    print()
    print("=" * 68)
    print(f"  {label}  ({acct['client_id']})")
    print("=" * 68)

    if not is_first:
        print()
        print("  IMPORTANT: this is a DIFFERENT Fyers account from the last one.")
        print("  Your browser is probably still logged in as the previous one,")
        print("  which would silently authorise the wrong account.")
        print()
        print("  Either log out of Fyers in your browser first, or open the")
        print("  URL below in a fresh incognito/private window.")
        input("  Press Enter when ready... ")

    session = fyersModel.SessionModel(
        client_id=acct["client_id"],
        secret_key=acct["secret_key"],
        redirect_uri=acct["redirect_uri"],
        response_type="code",
        grant_type="authorization_code",
    )

    auth_url = session.generate_authcode()
    print("\nOpening login page in your browser...")
    print("If it doesn't open, paste this URL manually:\n")
    print(f"  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("After logging in you'll be redirected to a page that may look")
    print("broken or blank - that's expected. Copy the FULL URL from the")
    print("address bar and paste it here.\n")

    pasted = input("Redirect URL (or blank to skip this account): ")
    auth_code = extract_auth_code(pasted)
    if not auth_code:
        print("  Skipped.")
        return None

    try:
        session.set_token(auth_code)
        resp = session.generate_token()
    except Exception as e:
        print(f"  FAILED to exchange auth code: {type(e).__name__}: {e}")
        return None

    if not isinstance(resp, dict) or "access_token" not in resp:
        print(f"  FAILED - unexpected response: {str(resp)[:300]}")
        return None

    access_token = resp["access_token"]

    print("  Validating token...", end=" ", flush=True)
    ok, detail, fy_id = validate_token(acct["client_id"], access_token)
    if not ok:
        print(f"REJECTED\n  Token did not validate: {detail}")
        return None
    print(f"OK  (account: {detail}, fy_id: {fy_id})")

    return {
        "client_id": acct["client_id"],
        "access_token": access_token,
        "refresh_token": resp.get("refresh_token"),
        "generated_on": date.today().isoformat(),
        "generated_at": datetime.now().isoformat(),
        "fy_id": fy_id,
        "account_name": detail,
        # The websocket wants "appid:accesstoken", not the bare token.
        "ws_token": f"{acct['client_id']}:{access_token}",
    }


def warn_duplicate_accounts(tokens):
    """Flag labels that resolved to the same underlying Fyers account.

    The point of 4 accounts is 4 independent connections and 4 separate
    quotas. If the browser session carried over during login, several labels
    can end up holding tokens for the same account - which looks fine, but
    quietly gives you one account's limits spread across four collectors.
    """
    seen = {}
    for label, entry in tokens.items():
        fy_id = entry.get("fy_id")
        if not fy_id or fy_id == "unknown":
            continue
        seen.setdefault(fy_id, []).append(label)

    dupes = {fy: labels for fy, labels in seen.items() if len(labels) > 1}
    if not dupes:
        return False

    print()
    print("!" * 68)
    print("WARNING: some labels point at the SAME Fyers account:")
    for fy_id, labels in dupes.items():
        print(f"  {fy_id}  <-  {', '.join(labels)}")
    print()
    print("This usually means the browser stayed logged in between logins.")
    print("You'd get one account's limits shared across several collectors,")
    print("rather than the independent capacity you're expecting.")
    print()
    print("Fix: log out of Fyers (or use a fresh incognito window per")
    print("account) and rerun:  py token_manager.py --all")
    print("!" * 68)
    return True


def cmd_check(tokens):
    print()
    print(f"{'Account':<28} {'Status':<12} Generated")
    print("-" * 68)
    stale = 0
    for acct in ACCOUNTS:
        entry = tokens.get(acct["label"])
        if is_fresh(entry):
            status = "FRESH"
        elif entry:
            status = "STALE"
            stale += 1
        else:
            status = "MISSING"
            stale += 1
        gen = entry.get("generated_on", "-") if entry else "-"
        print(f"{acct['label']:<28} {status:<12} {gen}")
    print("-" * 68)
    if stale:
        print(f"{stale} account(s) need refreshing. Run:  py token_manager.py")
    else:
        print("All accounts have fresh tokens.")
    return stale


def main():
    parser = argparse.ArgumentParser(description="Fyers daily token manager")
    parser.add_argument("--all", action="store_true",
                        help="refresh every account, even fresh ones")
    parser.add_argument("--check", action="store_true",
                        help="report status only, change nothing")
    args = parser.parse_args()

    tokens = load_tokens()

    if args.check:
        sys.exit(0 if cmd_check(tokens) == 0 else 1)

    todo = [a for a in ACCOUNTS
            if args.all or not is_fresh(tokens.get(a["label"]))]

    if not todo:
        print("\nAll tokens are already fresh for today. Nothing to do.")
        print("(Use --all to force a refresh anyway.)")
        return

    print(f"\n{len(todo)} account(s) need a token today.")

    refreshed = 0
    for idx, acct in enumerate(todo):
        result = refresh_account(acct, is_first=(idx == 0))
        if result:
            tokens[acct["label"]] = result
            save_tokens(tokens)   # save as we go, so a later failure
            refreshed += 1        # doesn't lose earlier successes

    print()
    print("=" * 68)
    print(f"Refreshed {refreshed} of {len(todo)} account(s).")
    if refreshed < len(todo):
        print("Some accounts still need tokens - rerun to retry just those.")

    warn_duplicate_accounts(load_tokens())
    cmd_check(load_tokens())


if __name__ == "__main__":
    main()
