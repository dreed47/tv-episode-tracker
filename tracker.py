#!/usr/bin/env python3
"""
TV Episode Tracker
Checks TVmaze for upcoming episodes and adds them to Google Calendar.
Runs on a configurable interval — no LLM required.
"""

import os
import re
import time
import logging
import threading
from collections import deque
import requests
import requests.packages.urllib3
import schedule
from flask import Flask, jsonify, request as flask_request

# HA uses a self-signed cert on an IP — suppress the resulting warning
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_BUFFER: deque = deque(maxlen=1000)

class _BufferHandler(logging.Handler):
    def emit(self, record):
        from datetime import datetime
        LOG_BUFFER.append({
            "t": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "l": record.levelname,
            "m": self.format(record),
        })

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger().addHandler(_BufferHandler())
log = logging.getLogger(__name__)

# ── Config (from environment) ─────────────────────────────────────────────────
CALENDAR_ID          = os.environ["CALENDAR_ID"]
LOOKAHEAD_DAYS       = int(os.getenv("LOOKAHEAD_DAYS", "14"))
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MINUTES", "60"))
RUN_INTERVAL_HOURS   = int(os.getenv("RUN_INTERVAL_HOURS", "6"))
COLOR_ID             = os.getenv("COLOR_ID", "10")          # Basil green
TIMEZONE             = os.getenv("TIMEZONE", "America/New_York")
ENV_FILE             = os.getenv("ENV_FILE", "/app/.env")

HA_URL         = os.getenv("HA_URL", "")
HA_TOKEN       = os.getenv("HA_TOKEN", "")
HA_TODO_ENTITY = os.getenv("HA_TODO_ENTITY", "todo.tvshows")

TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "/config/token.json")
SHOWS_FILE = os.getenv("SHOWS_FILE",        "/config/shows.txt")

SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ── Google Calendar auth ──────────────────────────────────────────────────────
def get_calendar_service():
    if not Path(TOKEN_FILE).exists():
        raise RuntimeError(
            f"No token found at {TOKEN_FILE}. "
            "Run 'docker compose run --rm tv-tracker python auth.py' to authenticate."
        )
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(TOKEN_FILE).write_text(creds.to_json())
        else:
            raise RuntimeError("Credentials expired and can't be refreshed. Re-run auth.py.")
    return build("calendar", "v3", credentials=creds)


# ── Show list ─────────────────────────────────────────────────────────────────
def _parse_line(line):
    """Parse 'Show Name|tvmaze_id' or 'Show Name' → (name, id_or_None)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "|" in line:
        name, tvid = line.split("|", 1)
        return name.strip(), tvid.strip()
    return line, None


def _get_shows_from_ha():
    """Fetch shows from Home Assistant todo list via service call."""
    if not HA_URL or not HA_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{HA_URL}/api/services/todo/get_items",
            params={"return_response": "true"},
            headers=headers,
            json={"entity_id": HA_TODO_ENTITY},
            timeout=10,
            verify=False,
        )
        log.debug(f"HA service call status: {r.status_code}")
        if r.ok:
            data = r.json()
            items = data.get("service_response", {}).get(HA_TODO_ENTITY, {}).get("items", [])
            if items:
                log.info(f"Loaded {len(items)} shows from Home Assistant")
                return items
            log.warning(f"HA service call ok but no items found. Keys: {list(data.keys())}")
        else:
            log.warning(f"HA service call failed: {r.status_code} {r.text[:500]}")
    except Exception as e:
        log.warning(f"HA service call exception: {e}")
    return None


def load_shows():
    """Return list of (show_name, tvmaze_id_or_None)."""
    ha_items = _get_shows_from_ha()
    if ha_items:
        parsed = [_parse_line(i.get("summary", "")) for i in ha_items]
        return [p for p in parsed if p]

    shows_path = Path(SHOWS_FILE)
    if not shows_path.exists():
        if HA_URL:
            log.error(f"No shows file at {SHOWS_FILE} and HA returned no shows (check HA_URL/HA_TOKEN and HA connectivity).")
        else:
            log.error(f"No shows file at {SHOWS_FILE} and HA is not configured.")
        return []
    parsed = [_parse_line(line) for line in shows_path.read_text().splitlines()]
    shows = [p for p in parsed if p]
    log.info(f"Loaded {len(shows)} shows from {SHOWS_FILE}")
    return shows


# ── TVmaze ─────────────────────────────────────────────────────────────────────
def _tvmaze_search(show_name):
    """Search TVmaze by name, return show ID or None."""
    # Clean up characters that break URL / search
    q = re.sub(r"[''']", "", show_name)
    q = re.sub(r"&", "and", q)
    try:
        r = requests.get(
            "https://api.tvmaze.com/singlesearch/shows",
            params={"q": q},
            timeout=10,
        )
        if r.ok:
            return str(r.json().get("id"))
    except Exception as e:
        log.warning(f"TVmaze search error for '{show_name}': {e}")
    return None


def get_show_info(tvmaze_id):
    """Return dict with canonical name + provider, or None."""
    try:
        r = requests.get(f"https://api.tvmaze.com/shows/{tvmaze_id}", timeout=10)
        if r.ok:
            d = r.json()
            web = (d.get("webChannel") or {}).get("name", "")
            net = (d.get("network")    or {}).get("name", "")
            return {
                "name":     d.get("name", ""),
                "provider": web or net or "Unknown",
            }
    except Exception as e:
        log.warning(f"TVmaze show info error (id={tvmaze_id}): {e}")
    return None


def get_upcoming_episodes(tvmaze_id):
    """Return episodes airing in the next LOOKAHEAD_DAYS days."""
    try:
        r = requests.get(
            f"https://api.tvmaze.com/shows/{tvmaze_id}/episodes",
            timeout=15,
        )
        if not r.ok:
            return []
    except Exception as e:
        log.warning(f"TVmaze episodes error (id={tvmaze_id}): {e}")
        return []

    today  = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)

    return [
        ep for ep in r.json()
        if ep.get("airdate") and today <= _parse_date(ep["airdate"]) <= cutoff
    ]


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return datetime.min.date()


# ── Calendar helpers ──────────────────────────────────────────────────────────
def get_existing_event_keys(service):
    """Return (set of episode IDs, set of 'Title_YYYY-MM-DD' for old events)."""
    now    = datetime.now(timezone.utc)
    end    = now + timedelta(days=LOOKAHEAD_DAYS)
    start  = now - timedelta(days=1)  # Look back to include recently created events
    ids    = set()
    keys   = set()
    token  = None

    while True:
        try:
            result = service.events().list(
                calendarId=CALENDAR_ID,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                maxResults=250,
                pageToken=token,
            ).execute()
        except HttpError as e:
            log.error(f"Calendar list error: {e}")
            break

        for ev in result.get("items", []):
            extended = ev.get("extendedProperties", {}).get("private", {})
            ep_id = extended.get("tvmaze_episode_id")
            if ep_id:
                ids.add(ep_id)
            else:
                # Old event without ID, use title+date
                title = ev.get("summary", "")
                start_ev = ev.get("start", {})
                date  = (start_ev.get("date") or start_ev.get("dateTime", ""))[:10]
                if title and date:
                    keys.add(f"{title}_{date}")

        token = result.get("nextPageToken")
        if not token:
            break

    return ids, keys


def create_event(service, show_name, provider, episode):
    """Insert a calendar event for one episode."""
    title    = f"{show_name} on {provider}"
    season   = episode.get("season", "?")
    ep_num   = episode.get("number", "?")
    ep_name  = episode.get("name", "")
    airdate  = episode.get("airdate", "")
    airtime  = episode.get("airtime", "")  # e.g. "21:00"

    desc = (
        f"New episode of {show_name} on {provider}.\n"
        f"Season {season}, Episode {ep_num}"
        + (f" - {ep_name}" if ep_name else "")
        + "\n\nSource: TVmaze"
    )

    if airtime:
        start_str = f"{airdate}T{airtime}:00"
        end_str   = (
            datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S")
            + timedelta(minutes=DEFAULT_DURATION_MIN)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        start = {"dateTime": start_str, "timeZone": TIMEZONE}
        end   = {"dateTime": end_str,   "timeZone": TIMEZONE}
    else:
        start = {"date": airdate}
        end   = {"date": airdate}

    body = {
        "summary":     title,
        "description": desc,
        "start":       start,
        "end":         end,
        "colorId":     COLOR_ID,
        "extendedProperties": {
            "private": {
                "tvmaze_episode_id": str(episode["id"])
            }
        }
    }

    try:
        service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
        log.info(f"  ✓  Created: {title}  ({airdate})")
        return True
    except HttpError as e:
        log.error(f"  ✗  Failed to create '{title}': {e}")
        return False


# ── Main run ──────────────────────────────────────────────────────────────────
def run():
    global _last_run_time
    from zoneinfo import ZoneInfo
    _last_run_time = datetime.now(ZoneInfo(TIMEZONE)).strftime("%b %-d, %I:%M %p")
    log.info("━" * 60)
    log.info("TV Episode Tracker — starting run")
    log.info("━" * 60)

    try:
        service = get_calendar_service()
    except RuntimeError as e:
        log.error(str(e))
        return

    shows = load_shows()
    if not shows:
        log.warning("No shows loaded — nothing to do.")
        return

    existing_ids, existing_keys = get_existing_event_keys(service)
    log.info(f"Found {len(existing_ids)} events with IDs and {len(existing_keys)} old events in the window")

    created = skipped = errors = 0

    for show_name, tvmaze_id in shows:
        try:
            if not tvmaze_id:
                tvmaze_id = _tvmaze_search(show_name)
                if not tvmaze_id:
                    log.warning(f"  ✗  Can't find '{show_name}' on TVmaze")
                    errors += 1
                    continue

            info = get_show_info(tvmaze_id)
            if not info:
                log.warning(f"  ✗  Can't get info for id={tvmaze_id}")
                errors += 1
                continue

            canonical = info["name"]
            provider  = info["provider"]
            episodes  = get_upcoming_episodes(tvmaze_id)

            if not episodes:
                log.info(f"  —  {canonical}: no upcoming episodes")
                continue

            for ep in episodes:
                ep_id = str(ep["id"])
                airdate = ep.get("airdate", "")
                key     = f"{canonical} on {provider}_{airdate}"
                if ep_id in existing_ids or key in existing_keys:
                    log.info(f"  —  {canonical} on {provider} ({airdate}): already on calendar")
                    skipped += 1
                else:
                    if create_event(service, canonical, provider, ep):
                        existing_ids.add(ep_id)
                        created += 1
                    else:
                        errors += 1

                time.sleep(0.15)   # gentle rate limiting

        except Exception as e:
            log.error(f"  ✗  Unexpected error for '{show_name}': {e}", exc_info=True)
            errors += 1

    log.info(f"\nDone — created: {created}  skipped: {skipped}  errors: {errors}")
    log.info("━" * 60)


# ── Web UI ────────────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_last_run_time: str = ""
_web_app  = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📺</text></svg>">
<title>TV Episode Tracker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f0f; color: #d4d4d4; font-family: monospace; font-size: 13px; }
  header { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
           background: #1a1a2e; border-bottom: 1px solid #333; }
  header h1 { font-size: 15px; color: #7eb8f7; flex: 1; }
  select { background: #222; color: #d4d4d4; border: 1px solid #444; border-radius: 4px;
           padding: 5px 8px; font-size: 13px; font-family: monospace; cursor: pointer; }
  label  { font-size: 12px; color: #888; }
  button { padding: 7px 18px; border: none; border-radius: 4px; cursor: pointer;
           font-size: 13px; font-family: monospace; }
  #btn-run  { background: #4caf50; color: #000; }
  #btn-run:disabled { background: #555; color: #888; cursor: default; }
  #btn-clear { background: #444; color: #ccc; }
  #status { font-size: 12px; color: #888; }
  #last-run { font-size: 12px; color: #888; }
  header.manual-mode { background: #1e1608 !important; border-bottom-color: #b8860b; }
  #manual-badge { display:none; font-size:11px; font-weight:bold; color:#f0a500;
                  background:#3a2a00; border:1px solid #b8860b; border-radius:4px;
                  padding:3px 8px; letter-spacing:.05em; }
  header.manual-mode #manual-badge { display:inline; }
  #log { padding: 10px 16px; height: calc(100vh - 50px); overflow-y: auto;
         white-space: pre-wrap; word-break: break-all; }
  .INFO    { color: #d4d4d4; }
  .WARNING { color: #e5c07b; }
  .ERROR   { color: #e06c75; }
  .DEBUG   { color: #666; }
  .CRITICAL{ color: #ff79c6; }
</style>
</head>
<body>
<header>
  <h1>📺 TV Episode Tracker</h1>
  <span id="manual-badge">⏸ SCHEDULER DISABLED</span>
  <span id="last-run"></span>
  <span id="status">connecting…</span>
  <a id="ha-link" href="#" target="_blank" rel="noopener"
     style="display:none;color:#7eb8f7;font-size:12px;text-decoration:none"
     title="Open HA todo list">🏠 HA Shows</a>
  <label>Lookahead
    <select id="sel-lookahead" onchange="saveConfig()">
      <option value="7">7 days</option>
      <option value="14" selected>14 days</option>
      <option value="21">21 days</option>
      <option value="30">30 days</option>
    </select>
  </label>
  <label>Schedule
    <select id="sel-interval" onchange="saveConfig()">
      <option value="1">Every 1h</option>
      <option value="2">Every 2h</option>
      <option value="4">Every 4h</option>
      <option value="6" selected>Every 6h</option>
      <option value="12">Every 12h</option>
      <option value="24">Every 24h</option>
      <option value="0">Manual only</option>
    </select>
  </label>
  <button id="btn-clear" onclick="clearLog()">Clear</button>
  <button id="btn-run"   onclick="triggerRun()">▶ Run Now</button>
</header>
<div id="log"></div>
<script>
  let offset = 0, polling = null;
  const logEl    = document.getElementById('log');
  const statusEl = document.getElementById('status');
  const lastRunEl= document.getElementById('last-run');
  const btnRun   = document.getElementById('btn-run');
  const haLink   = document.getElementById('ha-link');
  const selLook = document.getElementById('sel-lookahead');
  const selInt  = document.getElementById('sel-interval');

  async function loadConfig() {
    try {
      const r = await fetch('/config');
      const d = await r.json();
      selLook.value = String(d.lookahead_days);
      selInt.value  = String(d.run_interval_hours);
      updateHeaderStyle();
    } catch(e) {}
  }

  function updateHeaderStyle() {
    document.querySelector('header').classList.toggle('manual-mode', parseInt(selInt.value) === 0);
  }

  async function saveConfig() {
    updateHeaderStyle();
    await fetch('/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lookahead_days:    parseInt(selLook.value),
        run_interval_hours: parseInt(selInt.value)
      })
    });
  }

  function setHaLink(url, entity) {
    if (!url) return;
    // HA todo UI path: /todo?entity_id=todo.tvshows
    haLink.href = url.replace(/\/$/, '') + '/todo?entity_id=' + encodeURIComponent(entity);
    haLink.style.display = 'inline';
  }

  function appendLines(lines) {
    lines.forEach(l => {
      const span = document.createElement('span');
      span.className = l.l;
      span.textContent = l.m + '\\n';
      logEl.appendChild(span);
    });
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function poll() {
    try {
      const r = await fetch('/logs?since=' + offset);
      const d = await r.json();
      if (d.ha_url && haLink.style.display === 'none') setHaLink(d.ha_url, d.ha_entity);
      if (d.lines.length) { appendLines(d.lines); offset = d.total; }
      lastRunEl.textContent = d.last_run ? 'Last run: ' + d.last_run : '';
      statusEl.textContent = d.running ? 'running…' : 'idle';
      btnRun.disabled = d.running;
    } catch(e) { statusEl.textContent = 'disconnected'; }
  }

  function clearLog() { logEl.innerHTML = ''; }

  async function triggerRun() {
    btnRun.disabled = true;
    statusEl.textContent = 'starting…';
    await fetch('/run', {method:'POST'});
  }

  poll();
  loadConfig();
  setInterval(poll, 2000);
</script>
</body>
</html>"""

@_web_app.route("/")
def _index():
    return _HTML

@_web_app.route("/logs")
def _logs():
    since = int(flask_request.args.get("since", 0))
    buf   = list(LOG_BUFFER)
    return jsonify({"lines": buf[since:], "total": len(buf), "running": _run_lock.locked(),
                    "ha_url": HA_URL, "ha_entity": HA_TODO_ENTITY,
                    "last_run": _last_run_time})

@_web_app.route("/config", methods=["GET"])
def _get_config():
    return jsonify({
        "lookahead_days":    LOOKAHEAD_DAYS,
        "run_interval_hours": RUN_INTERVAL_HOURS,
    })

def _update_env_file(key: str, value: str):
    """Rewrite a KEY=... line in the mounted .env file."""
    p = Path(ENV_FILE)
    if not p.exists():
        return
    lines = p.read_text().splitlines(keepends=True)
    found = False
    for i, line in enumerate(lines):
        if re.match(rf"^{key}\s*=", line):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    p.write_text("".join(lines))

@_web_app.route("/config", methods=["POST"])
def _set_config():
    global LOOKAHEAD_DAYS, RUN_INTERVAL_HOURS
    data = flask_request.get_json(force=True)
    if "lookahead_days" in data:
        val = int(data["lookahead_days"])
        LOOKAHEAD_DAYS = val
        _update_env_file("LOOKAHEAD_DAYS", str(val))
    if "run_interval_hours" in data:
        val = int(data["run_interval_hours"])
        RUN_INTERVAL_HOURS = val
        _update_env_file("RUN_INTERVAL_HOURS", str(val))
        # reschedule
        schedule.clear()
        if RUN_INTERVAL_HOURS > 0:
            schedule.every(RUN_INTERVAL_HOURS).hours.do(run)
            log.info(f"Schedule updated — running every {RUN_INTERVAL_HOURS}h")
        else:
            log.info("Schedule cleared — manual runs only")
    return jsonify({"lookahead_days": LOOKAHEAD_DAYS, "run_interval_hours": RUN_INTERVAL_HOURS})

@_web_app.route("/run", methods=["POST"])
def _trigger_run():
    if _run_lock.locked():
        return jsonify({"status": "already running"}), 409
    def _do():
        with _run_lock:
            run()
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"status": "started"})

WEB_PORT = int(os.getenv("PORT", "7840"))    # external port (for log message only)
_INTERNAL_PORT = 5000                          # container-internal port (docker-compose maps PORT -> 5000)

def _start_web():
    _web_app.run(host="0.0.0.0", port=_INTERNAL_PORT, debug=False, use_reloader=False)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_start_web, daemon=True).start()
    log.info(f"Web UI available at http://localhost:{WEB_PORT}")

    if RUN_INTERVAL_HOURS > 0:
        log.info(f"Scheduling runs every {RUN_INTERVAL_HOURS}h (no immediate run on startup)")
        schedule.every(RUN_INTERVAL_HOURS).hours.do(run)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        log.info("RUN_INTERVAL_HOURS=0 — manual runs only")
        while True:
            time.sleep(60)
