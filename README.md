# TV Episode Tracker

Automatically adds upcoming TV episodes to a Google Calendar. Zero ongoing cost — no LLM, no subscriptions. Runs as a Docker container on any homelab or server.

Episode data comes from [TVmaze](https://www.tvmaze.com) (free, no API key required). Your show list can be managed via a Home Assistant todo list or a plain text file.

---

## Features

- Checks TVmaze for upcoming episodes within a configurable lookahead window
- Creates all-day Google Calendar events for each episode
- Skips duplicates — safe to run as often as you like
- Pulls show list from Home Assistant (`todo` entity) or a local `shows.txt` file
- Built-in web UI showing live logs with a manual trigger button
- Runs on a schedule (default every 6 hours) inside the container — no external cron needed
- Token auto-refreshes; after first-time auth it runs unattended indefinitely

---

## Prerequisites

- Docker + Docker Compose
- A Google Cloud project with the **Google Calendar API** enabled
- A Google Calendar ID to write events into

---

## Quick Start

### Step 1 — Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. Enable the **Google Calendar API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON → save as `./config/credentials.json`

### Step 2 — Configure

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

| Variable | Description |
|---|---|
| `CALENDAR_ID` | Google Calendar ID to write events to |
| `TIMEZONE` | Your local timezone (e.g. `America/Chicago`) |
| `HA_URL` + `HA_TOKEN` | Home Assistant URL and long-lived token (optional) |
| `HA_TODO_ENTITY` | HA todo entity name (default: `todo.tvshows`) |
| `PORT` | External port for the web UI (default: `7840`) |
| `LOOKAHEAD_DAYS` | How many days ahead to check (default: `14`) |
| `RUN_INTERVAL_HOURS` | How often to run (default: `6`; set to `0` for one-shot) |
| `COLOR_ID` | Google Calendar color ID (default: `10` = Basil green) |

### Step 3 — Authenticate (one time only)

Port `8080` is required for the OAuth redirect during this step. It is already defined in `docker-compose.yml` but commented out by default — uncomment it before running auth, then comment it back out afterwards.

```yaml
ports:
  - "8080:8080"   # ← uncomment for auth, comment out again after
```

Run this on a machine with a browser (your desktop):

```bash
docker compose run --rm --service-ports tv-tracker python auth.py
```

A URL will be printed — open it in your browser, sign in, approve access. `config/token.json` is created and the token auto-refreshes forever after.

> After auth succeeds, comment `8080:8080` back out in `docker-compose.yml` — it is not needed for normal operation.

### Step 4 — Start the tracker

```bash
docker compose up -d
```

---

## Web UI

Open **http://localhost:7840** (or whatever `PORT` is set to) for a live log view and a **▶ Run Now** button to trigger an immediate run.

---

## Show list

### Option A — Home Assistant (recommended)

Set `HA_URL`, `HA_TOKEN`, and `HA_TODO_ENTITY` in `.env`. The tracker reads your todo list each run — add or remove shows in HA and changes are picked up automatically.

### Option B — shows.txt

Copy the example and edit it:

```bash
cp config/shows.txt.example config/shows.txt
```

Format — one show per line:

```
Fire Country|37921
The Bear|44217
When Calls The Heart
```

- `ShowName|TVmazeID` — preferred, exact match, no search needed
- `ShowName` — tracker searches TVmaze automatically (slightly slower)
- Find IDs at: `https://www.tvmaze.com/search?q=show+name` (the number in the URL)

Changes to `shows.txt` are picked up on the next run — no restart needed.

---

## Project structure

```
.env                        # Your local config (not committed)
.env.example                # Config template
docker-compose.yml
Dockerfile
requirements.txt
tracker.py                  # Main tracker + web UI
auth.py                     # One-time OAuth setup
config/
  credentials.json          # Google OAuth client secret — downloaded from Google Cloud Console
  token.json                # Google OAuth access/refresh token — written by auth.py (auto-refreshes)
  shows.txt                 # Your show list (if not using HA; TVmaze requires no auth)
  shows.txt.example
```

---

## Updating

```bash
git pull
docker compose up -d --build
```

---

## Logs

```bash
docker compose logs -f
```

Or use the web UI at `http://localhost:<PORT>`.
