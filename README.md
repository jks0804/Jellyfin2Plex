# Jellyfin2Plex Sync

Syncs content between Jellyfin and Plex. Three operations are supported:

- **Playlist sync** — mirror a playlist from one server to the other (either direction). Supports music, movies, TV episodes, photos, or mixed playlists.
- **Watch-state sync** — bidirectional sync of watched / unwatched status and resume position across the whole library. Whichever side has more recent activity wins.
- **Daemon mode** — long-running service that listens for webhooks from both servers and pushes watch state across in near-real-time when a user finishes watching something. Multi-user. Includes a periodic full reconciliation as a safety net.

## Requirements

- Python 3.7+
- Network access to both Jellyfin and Plex (HTTP)
- `requests` and `plexapi` are installed automatically on first run

Works on Linux, macOS, and Windows. The script uses only the standard library plus those two pip packages, so anywhere Python runs is fair game.

## Setup

Open `jellyfin2plex.py` and fill in the `CONFIG` section near the top of the file:

```python
OPERATION       = "playlist"           # or "watch_state" or "daemon"
DIRECTION       = "jellyfin_to_plex"   # or "plex_to_jellyfin"  (playlist op only)

JELLYFIN_URL    = "http://192.168.1.10:8096"
JELLYFIN_TOKEN  = "your-jellyfin-api-key"
JELLYFIN_USER   = ""                          # optional — defaults to first user

PLEX_URL        = "http://192.168.1.10:32400"
PLEX_TOKEN      = "your-plex-token"
PLEX_LIBRARIES  = ""                          # optional — blank = all libraries

SOURCE_PLAYLIST = "My Playlist"               # required for playlist op
TARGET_PLAYLIST = ""                          # optional — defaults to SOURCE_PLAYLIST
```

### Getting your Jellyfin API key

Dashboard → API Keys → + (top right)

### Getting your Plex token

Sign in to Plex, then follow the [official instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Running

```bash
python3 jellyfin2plex.py
```

On Unraid with the **User Scripts** plugin, point a script at the file and run it from the UI.

## Configuration reference

| Variable | Required | Description |
|---|---|---|
| `OPERATION` | No | `playlist` (default), `watch_state`, or `daemon` |
| `DIRECTION` | No | `jellyfin_to_plex` (default) or `plex_to_jellyfin`. Ignored for `watch_state` and `daemon` |
| `JELLYFIN_URL` | No | Jellyfin base URL. Defaults to `http://localhost:8096` |
| `JELLYFIN_TOKEN` | **Yes** | Jellyfin API key (admin) |
| `JELLYFIN_USER` | No | Jellyfin username for playlist / watch-state ops. Defaults to the first user on the server. Ignored in `daemon` mode (see `USER_MAP`) |
| `PLEX_URL` | No | Plex base URL. Defaults to `http://localhost:32400` |
| `PLEX_TOKEN` | **Yes** | X-Plex-Token (admin) |
| `PLEX_LIBRARIES` | No | Comma-separated Plex library names to scope to, e.g. `Music,Movies,TV Shows`. Blank = all libraries |
| `SOURCE_PLAYLIST` | Playlist op | Exact playlist name on the source server |
| `TARGET_PLAYLIST` | No | Playlist name to create on the target. Defaults to `SOURCE_PLAYLIST` |
| `PATH_MAP_SPEC` | No | Path translation rules (see below) |
| `DAEMON_HOST` | No | Daemon bind address. Defaults to `0.0.0.0` |
| `DAEMON_PORT` | No | Daemon TCP port. Defaults to `8765` |
| `WEBHOOK_TOKEN` | Daemon op | Shared secret embedded in webhook URLs. ≥8 chars, `[A-Za-z0-9_-]` |
| `USER_MAP` | Daemon op | `PlexUser:JellyfinUser,...` username pairs to sync |
| `PLEX_USER_TOKENS` | Daemon op | `PlexUser:token,...` one X-Plex-Token per user in `USER_MAP` |
| `RECONCILE_INTERVAL_MIN` | No | Minutes between full reconciliations. Defaults to `360` |
| `INDEX_REFRESH_MIN` | No | Minutes between path-index rebuilds. Defaults to `60` |
| `LOG_PATH` | No | Daemon log file. Defaults to `jellyfin2plex.log` inside the system temp directory (`/tmp` on Linux/macOS, `%TEMP%` on Windows) |

All values can also be set as environment variables using the same names (plus `PATH_MAP` for `PATH_MAP_SPEC`). Values hardcoded in the file take priority.

---

## Playlist sync

For each item in the source playlist, the script tries three strategies in order:

1. **Exact path** — the source file path (after any `PATH_MAP` translation) is compared directly against the target server's known file paths.
2. **Suffix match** — if the full path doesn't match, progressively shorter path suffixes are tried (longest first). This handles cases where Jellyfin and Plex mount the same files at different root paths, with no configuration needed.
3. **Metadata fallback** — type-aware last resort:
   - *Music:* matches by title + artist name
   - *TV episodes:* matches by title + show name + season + episode number
   - *Movies / everything else:* matches by title if only one candidate is found

Items that cannot be matched are listed at the end of the run — they are never silently skipped.

Each run **replaces** the target playlist entirely. If the playlist does not exist yet it is created fresh.

### Example output

**Jellyfin → Plex:**
```
Direction: jellyfin_to_plex
Jellyfin:  http://192.168.1.10:8096
Plex:      http://192.168.1.10:32400
  Source playlist 'Road Trip Mix' -> 24 items
  Searching Plex libraries: Music, Movies, TV Shows
Matched 23/24 items.
Missing in Plex:
  - The Beatles - Now And Then
Removed existing Plex playlist 'Road Trip Mix'.
Created Plex playlist 'Road Trip Mix' with 23 items.
```

**Plex → Jellyfin:**
```
Direction: plex_to_jellyfin
Jellyfin:  http://192.168.1.10:8096
Plex:      http://192.168.1.10:32400
  Source playlist 'Weekend Watch List' -> 12 items
Matched 12/12 items.
Created Jellyfin playlist 'Weekend Watch List' with 12 items.
```

---

## Watch-state sync

Set `OPERATION = "watch_state"` to run a bidirectional sync of playback state.

### What it does

For every item that exists on **both** servers (matched by file path), the script reads:

| | Jellyfin | Plex |
|---|---|---|
| Watched flag | `UserData.Played` | `viewCount > 0` |
| Resume position | `UserData.PlaybackPositionTicks` | `viewOffset` |
| Last activity timestamp | `UserData.LastPlayedDate` | `lastViewedAt` |

The side with the **more recent** `LastPlayedDate` / `lastViewedAt` is treated as authoritative, and its state (watched flag + resume position) is pushed to the other side. If only one side has a timestamp, that side wins. If neither side has any activity, the item is skipped.

### Scope

- Covers Movies, TV episodes, and Music tracks.
- Photos and other library types are skipped.
- `PLEX_LIBRARIES` restricts which Plex libraries participate; if blank, all libraries are scanned.

### Example output

```
Operation: watch_state (bidirectional)
Jellyfin:  http://192.168.1.10:8096
Plex:      http://192.168.1.10:32400
  Scanning Plex libraries: Movies, TV Shows, Music
  Plex items:     8,421
  Jellyfin items: 8,397
Matched: 8,310 (Jellyfin items not in Plex: 87)
  Pushed Jellyfin → Plex:    42
  Pushed Plex → Jellyfin:    18
  Already in sync / no data: 8,250
```

### Notes & caveats

- Play counts are **not** synced — only the watched flag and resume position. This prevents counts from drifting on each sync.
- Plex's `markPlayed()` updates `lastViewedAt` to "now", and Jellyfin's `/PlayedItems` endpoint accepts a `datePlayed` parameter (we pass the Plex timestamp through). On the next run, the receiving side's timestamp will usually be slightly newer than the source's — that's fine, the watched flags will already match so no further writes happen.
- For partially watched items pushed Plex → Jellyfin, the resume position is written via a direct `UserData` POST.

---

## Daemon mode

Set `OPERATION = "daemon"` to run a persistent service that listens for webhooks from Plex and Jellyfin and pushes watch state across in near-real-time. Each user's "back to the menu" event on one server is mirrored to the other within a few seconds.

### Required config

```python
OPERATION         = "daemon"
WEBHOOK_TOKEN     = "your-shared-secret-here"      # ≥8 chars [A-Za-z0-9_-]
USER_MAP          = "PlexAlice:JellyfinAlice,PlexBob:JellyfinBob"
PLEX_USER_TOKENS  = "PlexAlice:tok1,PlexBob:tok2"  # one X-Plex-Token per user
```

`JELLYFIN_TOKEN` (admin) is reused for all Jellyfin users. Plex tokens are inherently per-user — the admin token cannot mark items watched for managed users, so each Plex user needs their own token in `PLEX_USER_TOKENS`.

### Webhook URLs

```
http://<this-host>:<DAEMON_PORT>/plex/<WEBHOOK_TOKEN>
http://<this-host>:<DAEMON_PORT>/jellyfin/<WEBHOOK_TOKEN>
```

`GET /health` returns `ok` for liveness probes.

### Plex setup (requires Plex Pass)

Plex web UI → **Settings → Account → Webhooks → Add Webhook** → paste the `/plex/<TOKEN>` URL.

Plex fires webhooks for every user playing on your server, so a single webhook covers all mapped users. The daemon ignores events for users not in `USER_MAP`.

### Jellyfin setup (requires the Webhook plugin)

Install the **Webhook** plugin from the Jellyfin plugin catalog, then:

**Dashboard → Plugins → Webhook → Add Generic Destination**

| Field | Value |
|---|---|
| Webhook URL | `http://<this-host>:<DAEMON_PORT>/jellyfin/<WEBHOOK_TOKEN>` |
| Notification Type | enable **Playback Stop** only |
| Request Content Type | `application/json` |

Template:

```
{
  "NotificationType": "{{NotificationType}}",
  "UserId":           "{{UserId}}",
  "ItemId":           "{{ItemId}}"
}
```

The daemon re-queries Jellyfin for the current `UserData` on every event, so the template is intentionally minimal.

### What it syncs

| Event | Action |
|---|---|
| Plex `media.scrobble` (≥90% watched) | mark watched on Jellyfin |
| Plex `media.stop` with partial position | push resume position to Jellyfin |
| Jellyfin `PlaybackStop` | re-read Jellyfin `UserData` and push watched/resume state to Plex |

Other webhook event types are ignored. Manual mark-as-watched via the Jellyfin or Plex UI (no playback session) is **not** caught by the webhooks themselves but will be picked up by the periodic reconciliation.

### Reconciliation safety net

On startup, and then every `RECONCILE_INTERVAL_MIN` (default 360 = 6 hours), the daemon runs the existing full bidirectional `watch_state` sync per mapped user. This catches:

- Anything that happened while the daemon was down
- Manual UI mark-as-watched actions that didn't trigger a webhook
- Items added to the library after the last index refresh

The path index is rebuilt independently every `INDEX_REFRESH_MIN` (default 60 minutes) so newly-added media is matchable without waiting for the next reconcile.

### Logging

Single rotating log file at `LOG_PATH`, capped at 5 MB with one `.1` backup. The default path is the system temp directory (`/tmp/jellyfin2plex.log` on Linux/macOS, `%TEMP%\jellyfin2plex.log` on Windows). Log output is also written to stderr so it shows up in `journald` under systemd or in your service manager's log view on macOS / Windows.

On Linux installs where `/tmp` is `tmpfs`, the default also keeps log writes off the system disk. On macOS and Windows the temp directory is on the system disk, so set `LOG_PATH` to a different volume if avoiding writes to the system drive matters to you.

### Loop protection

When the daemon writes state to one side, the *other* side may fire an "incoming" webhook for the same change. The daemon tracks recent writes for ~15 seconds per `(side, item)` and drops the echo. In practice Plex doesn't fire webhooks for API-driven state changes (only for real playback), but the suppression is in place defensively for both sides.

### Startup queueing

The first path-index build can take 30+ seconds on large libraries. Webhooks that arrive before the index is ready are queued and processed once it's built — no events are lost during startup.

### Example output

```
2026-05-20 14:11:02 [INFO] Daemon start: Plex http://172.18.0.25:32400, Jellyfin http://172.18.0.26:8096
2026-05-20 14:11:02 [INFO] User mapped: Plex 'PlexAlice' -> Jellyfin 'JellyfinAlice' (a1b2c3...)
2026-05-20 14:11:02 [INFO] Listening on 0.0.0.0:8765
2026-05-20 14:11:02 [INFO] Building path index...
2026-05-20 14:11:34 [INFO] Index built in 31.4s: 22837 Plex, 22910 Jellyfin items.
2026-05-20 14:11:34 [INFO] Startup reconcile...
2026-05-20 14:23:11 [INFO] Plex scrobble [PlexAlice] -> Jellyfin watched: Some Movie (2024)
2026-05-20 14:51:07 [INFO] Jellyfin stop [PlexBob] -> Plex resume 482000ms: Another Show S01E03
```

---

## Path mapping

If Jellyfin and Plex see your media at different paths (common with Docker containers), set `PATH_MAP_SPEC` to translate source paths into target paths:

```python
PATH_MAP_SPEC = "/data/media:/mnt/user/media"
```

Multiple rules are separated by commas:

```python
PATH_MAP_SPEC = "/music:/mnt/user/music,/shows:/mnt/user/tv"
```

For playlist sync the direction follows `DIRECTION` — source prefix on the left, target prefix on the right. For watch-state sync the mapping is applied from Jellyfin → Plex during path lookup (Plex paths on the right side). The first matching prefix is applied. In most setups the suffix matching strategy removes the need for this.

---

## Automation

Dependencies (`requests`, `plexapi`) are installed automatically on the first run and are no-ops on subsequent runs.

### Scheduled runs (playlist / watch_state)

**Unraid User Scripts:**

1. Install the **User Scripts** plugin from Unraid Community Apps.
2. Create a new script and set the schedule (e.g. daily).
3. In the script body, call Python directly:
   ```bash
   #!/bin/bash
   python3 /path/to/jellyfin2plex.py
   ```

**cron (any Linux):**

```cron
0 4 * * *  /usr/bin/python3 /path/to/jellyfin2plex.py >> /var/log/jellyfin2plex.log 2>&1
```

### As a persistent service (daemon mode)

For `OPERATION = "daemon"` you want the script always running. Pick the recipe for your platform.

**Linux (systemd):**

```ini
# /etc/systemd/system/jellyfin2plex.service
[Unit]
Description=Jellyfin <-> Plex watch-state daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/jellyfin2plex/jellyfin2plex.py
Restart=on-failure
RestartSec=10
User=jellyfin2plex
# Or set config via env if you prefer not to edit the script:
# Environment=OPERATION=daemon
# Environment=WEBHOOK_TOKEN=...
# Environment=USER_MAP=...
# Environment=PLEX_USER_TOKENS=...

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jellyfin2plex
sudo journalctl -u jellyfin2plex -f
```

On Unraid, run the daemon as a User Scripts entry set to "Run in background" at array start, or wrap it in a small Docker container.

**macOS (launchd):**

```xml
<!-- ~/Library/LaunchAgents/com.local.jellyfin2plex.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.local.jellyfin2plex</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/you/jellyfin2plex/jellyfin2plex.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/jellyfin2plex.out</string>
    <key>StandardErrorPath</key><string>/tmp/jellyfin2plex.err</string>
    <!-- Optional env-var config in place of editing the script:
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPERATION</key><string>daemon</string>
        <key>WEBHOOK_TOKEN</key><string>...</string>
    </dict>
    -->
</dict>
</plist>
```

```bash
launchctl load  ~/Library/LaunchAgents/com.local.jellyfin2plex.plist
launchctl start com.local.jellyfin2plex
# Tail stderr for live logs:
tail -f /tmp/jellyfin2plex.err
```

Use `~/Library/LaunchAgents` for a per-user service, or `/Library/LaunchDaemons` (owned by root) for a system-wide one.

**Windows:**

The simplest option is [NSSM](https://nssm.cc/) (Non-Sucking Service Manager), which wraps any executable as a Windows service:

```cmd
nssm install Jellyfin2Plex "C:\Python311\python.exe" "C:\jellyfin2plex\jellyfin2plex.py"
nssm set Jellyfin2Plex AppEnvironmentExtra OPERATION=daemon WEBHOOK_TOKEN=... USER_MAP=... PLEX_USER_TOKENS=...
nssm set Jellyfin2Plex Start SERVICE_AUTO_START
nssm start Jellyfin2Plex
```

NSSM restarts the process automatically if it exits and captures stdout/stderr to a file you configure under `nssm edit Jellyfin2Plex` → I/O.

Alternative for non-service use: Task Scheduler → "Create Task" → trigger "At startup", action `python.exe C:\path\jellyfin2plex.py`. Check "Run whether user is logged on or not".
