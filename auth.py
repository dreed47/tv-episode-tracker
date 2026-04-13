#!/usr/bin/env python3
"""
One-time Google OAuth setup.

Run this ONCE on a machine with a browser (your desktop, not the container):

    docker compose run --rm --service-ports tv-tracker python auth.py

Or locally (outside Docker):

    pip install google-auth-oauthlib
    GOOGLE_CREDENTIALS_FILE=./config/credentials.json \
    GOOGLE_TOKEN_FILE=./config/token.json \
    python auth.py

After this succeeds, token.json is written to ./config/ and the main
tracker container will use it automatically.
"""

import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES      = ["https://www.googleapis.com/auth/calendar"]
CREDS_FILE  = os.getenv("GOOGLE_CREDENTIALS_FILE", "/config/credentials.json")
TOKEN_FILE  = os.getenv("GOOGLE_TOKEN_FILE",        "/config/token.json")


def main():
    creds_path = Path(CREDS_FILE)
    if not creds_path.exists():
        print(f"\n❌  credentials.json not found at: {CREDS_FILE}")
        print("    Download it from Google Cloud Console → APIs & Services → Credentials")
        print("    (OAuth 2.0 Client ID → Desktop app → Download JSON)")
        print(f"    Then place it at: {CREDS_FILE}\n")
        sys.exit(1)

    print(f"\nUsing credentials: {CREDS_FILE}")
    print(f"Token will be saved to: {TOKEN_FILE}\n")

    flow  = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)

    # host='localhost' keeps the redirect_uri as http://localhost:8080/ (required
    # by Google).  bind_addr='0.0.0.0' makes the server reachable from outside
    # the container so Docker's port-forward can deliver the callback.
    # open_browser=False prints the URL for the user to open on the host.
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="0.0.0.0",
        port=8080,
        open_browser=False,
    )

    token_path = Path(TOKEN_FILE)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    print(f"\n✓  Token saved to {TOKEN_FILE}")
    print("   You can now start the tracker: docker compose up -d\n")


if __name__ == "__main__":
    main()
