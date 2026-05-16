# Jellyfin2Plex PlaylistSync

Syncs a playlist between Jellyfin and Plex in either direction. Supports any media type — music, movies, TV episodes, photos, or mixed playlists.

## Requirements

- Python 3.6+
- Network access to both Jellyfin and Plex (HTTP)
- `requests` and `plexapi` are installed automatically on first run

## Setup

Open `jellyfin2plex.py` and fill in the `CONFIG` section near the top of the file:

```python
DIRECTION       = "jellyfin_to_plex"   # or "plex_to_jellyfin"

JELLYFIN_URL    = "http://192.168.1.10:8096"
JELLYFIN_TOKEN  = "your-jellyfin-api-key"
JELLYFIN_USER   = ""                          # optional — defaults to first user

PLEX_URL        = "http://192.168.1.10:32400"
PLEX_TOKEN      = "your-plex-token"
PLEX_LIBRARIES  = ""                          # optional — blank = search all libraries
                                              # only used when direction is jellyfin_to_plex

SOURCE_PLAYLIST = "My Playlist"               # exact name on the source server
TARGET_PLAYLIST = ""                          # optional — defaults to SOURCE_PLAYLIST
```

### Getting your Jellyfin API key

Dashboard → API Keys → + (top right)

### Getting your Plex token

Sign in to Plex, then visit `https://plex.tv/api/v2/user?X-Plex-Token=` — your token is in the URL, or use the [official instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Running

```bash
python3 jellyfin2plex.py
```

On Unraid with the **User Scripts** plugin, paste the full path to the script in the script body and run it from the UI.

## Configuration reference

| Variable | Required | Description |
|---|---|---|
| `DIRECTION` | No | `jellyfin_to_plex` (default) or `plex_to_jellyfin` |
| `JELLYFIN_URL` | No | Jellyfin base URL. Defaults to `http://localhost:8096` |
| `JELLYFIN_TOKEN` | **Yes** | Jellyfin API key |
| `JELLYFIN_USER` | No | Jellyfin username. Defaults to the first user on the server |
| `PLEX_URL` | No | Plex base URL. Defaults to `http://localhost:32400` |
| `PLEX_TOKEN` | **Yes** | X-Plex-Token |
| `PLEX_LIBRARIES` | No | Comma-separated Plex library names to search, e.g. `Music,Movies,TV Shows`. Blank = search all. Only applies when direction is `jellyfin_to_plex` |
| `SOURCE_PLAYLIST` | **Yes** | Exact playlist name on the source server |
| `TARGET_PLAYLIST` | No | Playlist name to create on the target server. Defaults to `SOURCE_PLAYLIST` |
| `PATH_MAP_SPEC` | No | Path translation rules (see below) |

All values can alternatively be set as environment variables using the same names (plus `PATH_MAP` for `PATH_MAP_SPEC`). Values hardcoded in the file take priority.

## How matching works

For each item in the source playlist, the script tries three strategies in order:

1. **Exact path** — the source file path (after any `PATH_MAP` translation) is compared directly against the target server's known file paths.
2. **Suffix match** — if the full path doesn't match, the script tries progressively shorter path suffixes (longest first). This handles cases where Jellyfin and Plex mount the same files at different root paths, with no configuration needed.
3. **Metadata fallback** — type-aware last resort:
   - *Music:* matches by title + artist name
   - *TV episodes:* matches by title + show name + season + episode number
   - *Movies / everything else:* matches by title if only one candidate is found

Items that cannot be matched are listed at the end of the run — they are never silently skipped.

Each run **replaces** the target playlist entirely. If the playlist does not exist yet it is created fresh.

## Path mapping

If Jellyfin and Plex see your media at different paths (common with Docker containers), set `PATH_MAP_SPEC` to translate source paths into target paths:

```python
PATH_MAP_SPEC = "/data/media:/mnt/user/media"
```

Multiple rules are separated by commas:

```python
PATH_MAP_SPEC = "/music:/mnt/user/music,/shows:/mnt/user/tv"
```

The direction of the mapping follows `DIRECTION` — source prefix on the left, target prefix on the right. The first matching prefix is applied. In most setups the suffix matching strategy (step 2 above) removes the need for this.

## Example output

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

## Automating with Unraid User Scripts

1. Install the **User Scripts** plugin from Unraid Community Apps.
2. Create a new script and set the schedule (e.g. daily).
3. In the script body, call Python directly:

```bash
#!/bin/bash
python3 /path/to/jellyfin2plex.py
```

Dependencies (`requests`, `plexapi`) are installed automatically on the first run and are no-ops on subsequent runs.
