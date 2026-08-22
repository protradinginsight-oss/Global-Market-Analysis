# Copy this file to config_local.py and fill in your real credentials.
# config_local.py is gitignored - it will never be committed or pushed,
# even though this repo is public.
#
# Get these from https://myapi.fyers.in/dashboard for each app you've created.
# The redirect_uri must EXACTLY match what's registered on the Fyers app,
# character for character, or the login will fail with a vague error.
#
# The "label" is just a name you choose - it identifies the account in the
# token file and in the collector's shard assignment. Keep them stable once
# chosen, since tokens are stored against these labels.

ACCOUNTS = [
    {
        "label": "acc1",
        "client_id": "XXXXXXXXXX-100",
        "secret_key": "your-secret-here",
        "redirect_uri": "https://google.com",
    },
    {
        "label": "acc2",
        "client_id": "XXXXXXXXXX-100",
        "secret_key": "your-secret-here",
        "redirect_uri": "https://google.com",
    },
    {
        "label": "acc3",
        "client_id": "XXXXXXXXXX-100",
        "secret_key": "your-secret-here",
        "redirect_uri": "https://google.com",
    },
    {
        "label": "acc4",
        "client_id": "XXXXXXXXXX-100",
        "secret_key": "your-secret-here",
        "redirect_uri": "https://google.com",
    },
]
