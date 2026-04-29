# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start (normal operation)
docker compose up -d

# Rebuild after code changes
docker compose up -d --build

# Logs
docker compose logs -f

# One-time OAuth setup (requires browser on the host machine)
docker compose run --rm --service-ports tv-tracker python auth.py

# Update
git pull && docker compose up -d --build
```

There are no tests or linters configured.

## Architecture

Two Python files, no framework beyond Flask:

- **`tracker.py`** — everything: scheduler, TVmaze client, Google Calendar client, Flask web UI, in-memory log buffer. Entry point runs Flask in a daemon thread and the `schedule` loop in the main thread.
- **`auth.py`** — one-shot OAuth flow; writes `config/token.json`. Run once from a machine with a browser; never used again after that.

**Data flow per run:**
1. `load_shows()` → Home Assistant todo entity (via REST) or `config/shows.txt`
2. For each show: TVmaze search (if no ID) → `get_show_info()` → `get_upcoming_episodes()`
3. `get_existing_event_keys()` fetches current calendar window to deduplicate
4. `create_event()` inserts missing episodes as Google Calendar events

**Port mapping:** Flask always binds internally to port 5000. `docker-compose.yml` maps `${PORT:-7840}` → 5000. Port 8080 is only needed during the one-time OAuth callback; it's commented out by default.

**Live config updates:** The web UI's `/config` POST endpoint mutates `LOOKAHEAD_DAYS` and `RUN_INTERVAL_HOURS` globals at runtime *and* rewrites the `.env` file (mounted at `/app/.env`) so settings survive container restarts. It also clears and reschedules the `schedule` job.

**Config:** All settings are environment variables loaded from `.env` via `env_file` in docker-compose. `CALENDAR_ID` is the only required var. `HA_URL`/`HA_TOKEN`/`HA_TODO_ENTITY` are optional — if absent, falls back to `config/shows.txt`.

**`config/` volume:** mounted read-write. Contains `credentials.json` (Google OAuth client secret, never modified), `token.json` (auto-refreshed by google-auth), and optionally `shows.txt`.
