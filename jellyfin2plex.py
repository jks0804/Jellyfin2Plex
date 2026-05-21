#!/usr/bin/env python3
"""
Sync content between Jellyfin and Plex.

Three operations are supported:

  * OPERATION = "playlist"     (default)
        Mirror a playlist from one server to the other. DIRECTION controls
        which side is the source. Supports any media type: music, movies,
        TV episodes, photos, etc.

  * OPERATION = "watch_state"
        Bidirectional sync of watched / unwatched status and playback
        position across the whole library. For each item present on both
        servers, the side with the more recent activity wins and its state
        is pushed to the other side.

  * OPERATION = "daemon"
        Run as a persistent service. Listens for webhooks from Plex
        (requires Plex Pass) and Jellyfin (requires the Webhook plugin)
        and pushes watch state across in near-real-time when a user
        finishes watching something. Also runs a periodic full
        reconciliation as a safety net. Multi-user; see USER_MAP and
        PLEX_USER_TOKENS in the CONFIG section.

Works whether Jellyfin and Plex are installed natively or run inside Docker
containers — the script only needs HTTP reachability to each server and either
shared filenames or an optional PATH_MAP to translate container paths.

Item matching strategy, in order:
  1. Exact path (after optional PATH_MAP translation).
  2. Progressive suffix match — longest shared path tail wins. Handles
     containers that mount the same files at different paths without any
     configuration in the common case.
  3. Type-aware metadata fallback: artist for music, show/season/episode for
     TV, single-candidate title match for everything else. (playlist op only)

Missing items are reported, not silently skipped.
"""

import subprocess
import sys

# Ensure third-party dependencies are available (needed on Unraid/bare Python)
_REQUIRED = ["requests", "plexapi"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *_REQUIRED])

import json
import logging
import logging.handlers
import os
import re
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as _email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

import requests
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound


# ---- CONFIG ------------------------------------------------------------------
# Fill in values directly here, or leave as "" to read from environment vars.

OPERATION        = ""                   # "playlist" (default) or "watch_state"
DIRECTION        = ""                   # "jellyfin_to_plex" or "plex_to_jellyfin"
                                        # Only used when OPERATION == "playlist".

JELLYFIN_URL     = ""                   # e.g. "http://192.168.1.10:8096"
JELLYFIN_TOKEN   = ""                   # Jellyfin API key
JELLYFIN_USER    = ""                   # Jellyfin username (optional)

PLEX_URL         = ""                   # e.g. "http://192.168.1.10:32400"
PLEX_TOKEN       = ""                   # X-Plex-Token
PLEX_LIBRARIES   = ""                   # Comma-separated library names to search,
                                        # e.g. "Music,Movies,TV Shows".
                                        # Leave blank to search ALL libraries.
                                        # Used for jellyfin_to_plex playlist sync
                                        # and to scope watch_state sync.

SOURCE_PLAYLIST  = ""                   # Playlist name on the source server
                                        # (required for OPERATION=playlist)
TARGET_PLAYLIST  = ""                   # Playlist name on the target server
                                        # (defaults to SOURCE_PLAYLIST)

# Translate source file paths to what the target server sees. Format:
#   "/media:/mnt/media,/data:/srv/data"
# Optional — suffix matching usually works without it.
# In jellyfin_to_plex:  Jellyfin path prefix → Plex path prefix
# In plex_to_jellyfin:  Plex path prefix → Jellyfin path prefix
PATH_MAP_SPEC    = ""

# ---- Daemon mode (OPERATION = "daemon") --------------------------------------
# All ignored unless OPERATION is "daemon".

DAEMON_HOST            = ""             # Bind address (default 0.0.0.0)
DAEMON_PORT            = ""             # TCP port (default 8765)

# Shared secret embedded in the webhook URLs. Required. ≥8 chars,
# [A-Za-z0-9_-] only. Both services POST to:
#   http://<this-host>:<DAEMON_PORT>/plex/<WEBHOOK_TOKEN>
#   http://<this-host>:<DAEMON_PORT>/jellyfin/<WEBHOOK_TOKEN>
WEBHOOK_TOKEN          = ""

# Map Plex usernames to Jellyfin usernames. Required.
#   "PlexAlice:JellyfinAlice,PlexBob:JellyfinBob"
USER_MAP               = ""

# Per-user X-Plex-Token (Plex tokens are per-user; the admin token can't
# write watch state for managed users). One entry per user in USER_MAP.
#   "PlexAlice:xxxxxxxx,PlexBob:yyyyyyyy"
PLEX_USER_TOKENS       = ""

RECONCILE_INTERVAL_MIN = ""             # Full reconcile every N minutes (default 360)
INDEX_REFRESH_MIN      = ""             # Rebuild path index every N minutes (default 60)
LOG_PATH               = ""             # Daemon log file. Defaults to "jellyfin2plex.log"
                                        # inside the system temp dir
                                        # (/tmp, %TEMP%, $TMPDIR — whichever applies).

# Fall back to environment variables for any value left blank above.
OPERATION        = OPERATION        or os.environ.get("OPERATION",        "playlist")
DIRECTION        = DIRECTION        or os.environ.get("DIRECTION",        "jellyfin_to_plex")
JELLYFIN_URL     = JELLYFIN_URL     or os.environ.get("JELLYFIN_URL",     "http://localhost:8096")
JELLYFIN_TOKEN   = JELLYFIN_TOKEN   or os.environ.get("JELLYFIN_TOKEN",   "")
JELLYFIN_USER    = JELLYFIN_USER    or os.environ.get("JELLYFIN_USER",    "")
PLEX_URL         = PLEX_URL         or os.environ.get("PLEX_URL",         "http://localhost:32400")
PLEX_TOKEN       = PLEX_TOKEN       or os.environ.get("PLEX_TOKEN",       "")
PLEX_LIBRARIES   = PLEX_LIBRARIES   or os.environ.get("PLEX_LIBRARIES",   "")
SOURCE_PLAYLIST  = SOURCE_PLAYLIST  or os.environ.get("SOURCE_PLAYLIST",  "")
TARGET_PLAYLIST  = TARGET_PLAYLIST  or os.environ.get("TARGET_PLAYLIST",  SOURCE_PLAYLIST)
PATH_MAP_SPEC    = PATH_MAP_SPEC    or os.environ.get("PATH_MAP",         "")

DAEMON_HOST            = DAEMON_HOST            or os.environ.get("DAEMON_HOST",            "0.0.0.0")
DAEMON_PORT            = DAEMON_PORT            or os.environ.get("DAEMON_PORT",            "8765")
WEBHOOK_TOKEN          = WEBHOOK_TOKEN          or os.environ.get("WEBHOOK_TOKEN",          "")
USER_MAP               = USER_MAP               or os.environ.get("USER_MAP",               "")
PLEX_USER_TOKENS       = PLEX_USER_TOKENS       or os.environ.get("PLEX_USER_TOKENS",       "")
RECONCILE_INTERVAL_MIN = RECONCILE_INTERVAL_MIN or os.environ.get("RECONCILE_INTERVAL_MIN", "360")
INDEX_REFRESH_MIN      = INDEX_REFRESH_MIN      or os.environ.get("INDEX_REFRESH_MIN",      "60")
LOG_PATH               = LOG_PATH               or os.environ.get("LOG_PATH",               os.path.join(tempfile.gettempdir(), "jellyfin2plex.log"))

if OPERATION not in ("playlist", "watch_state", "daemon"):
    sys.exit("ERROR: OPERATION must be 'playlist', 'watch_state', or 'daemon'.")
if OPERATION == "playlist" and DIRECTION not in ("jellyfin_to_plex", "plex_to_jellyfin"):
    sys.exit("ERROR: DIRECTION must be 'jellyfin_to_plex' or 'plex_to_jellyfin'.")
if not JELLYFIN_TOKEN:
    sys.exit("ERROR: JELLYFIN_TOKEN is required — set it in the CONFIG section above.")
if not PLEX_TOKEN:
    sys.exit("ERROR: PLEX_TOKEN is required — set it in the CONFIG section above.")
if OPERATION == "playlist" and not SOURCE_PLAYLIST:
    sys.exit("ERROR: SOURCE_PLAYLIST is required — set it in the CONFIG section above.")
if OPERATION == "daemon":
    if not WEBHOOK_TOKEN or not re.match(r"^[A-Za-z0-9_\-]{8,}$", WEBHOOK_TOKEN):
        sys.exit("ERROR: WEBHOOK_TOKEN must be ≥8 chars, [A-Za-z0-9_-] only.")
    if not USER_MAP:
        sys.exit("ERROR: USER_MAP is required for daemon mode.")
    if not PLEX_USER_TOKENS:
        sys.exit("ERROR: PLEX_USER_TOKENS is required for daemon mode.")

# ------------------------------------------------------------------------------


# ---- Jellyfin API helpers ----------------------------------------------------

def jf_get(path, params=None, timeout=60):
    headers = {"X-Emby-Token": JELLYFIN_TOKEN, "Accept": "application/json"}
    url = urljoin(JELLYFIN_URL.rstrip("/") + "/", path.lstrip("/"))
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def jf_post(path, json=None, params=None):
    headers = {
        "X-Emby-Token": JELLYFIN_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = urljoin(JELLYFIN_URL.rstrip("/") + "/", path.lstrip("/"))
    r = requests.post(url, headers=headers, json=json, params=params, timeout=15)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


def jf_delete(path):
    headers = {"X-Emby-Token": JELLYFIN_TOKEN}
    url = urljoin(JELLYFIN_URL.rstrip("/") + "/", path.lstrip("/"))
    requests.delete(url, headers=headers, timeout=15).raise_for_status()


# ---- Shared utilities --------------------------------------------------------

def resolve_jellyfin_user(name):
    users = jf_get("/Users")
    if not name:
        return users[0]["Id"]
    for u in users:
        if u["Name"].lower() == name.lower():
            return u["Id"]
    sys.exit(f"Jellyfin user not found: {name}")


def parse_path_map(spec):
    """Parse 'src1:dst1,src2:dst2' into a list of (src, dst) tuples."""
    pairs = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            sys.exit(f"Bad PATH_MAP entry (need src:dst): {chunk!r}")
        src, dst = chunk.split(":", 1)
        pairs.append((src.strip(), dst.strip()))
    return pairs


def apply_path_map(p, mapping):
    if not p:
        return p
    for src, dst in mapping:
        if p.startswith(src):
            return dst + p[len(src):]
    return p


def normalize_path(p):
    return p.replace("\\", "/").lstrip("/")


def item_paths(plex_item):
    """All file paths Plex knows for this item."""
    return [p.file for m in plex_item.media for p in m.parts if p.file]


def _jf_paths(jf_item):
    p = jf_item.get("Path")
    return [p] if p else []


# ---- Type maps & media-type constants ----------------------------------------

_JF_TO_PLEX_TYPE = {
    "Audio":   "track",
    "Movie":   "movie",
    "Episode": "episode",
    "Photo":   "photo",
}
_PLEX_TO_JF_TYPE = {v: k for k, v in _JF_TO_PLEX_TYPE.items()}

# Jellyfin item types fetched in bulk for each operation.
_JF_MEDIA_TYPES_PLAYLIST    = ("Movie", "Episode", "Audio", "Photo")
_JF_MEDIA_TYPES_WATCH_STATE = ("Movie", "Episode", "Audio")


# ---- Bulk fetch & indexing ---------------------------------------------------

def get_plex_sections(plex):
    """Return Plex library sections to search, per PLEX_LIBRARIES config."""
    if PLEX_LIBRARIES:
        names = [n.strip() for n in PLEX_LIBRARIES.split(",") if n.strip()]
        return [plex.library.section(n) for n in names]
    return plex.library.sections()


def fetch_plex_all_media(sections, include_photos=True):
    """All leaf items (movies, episodes, tracks, optionally photos) across sections."""
    items = []
    for section in sections:
        st = section.type
        try:
            if st == "movie":
                items.extend(section.all())
            elif st == "show":
                items.extend(section.searchEpisodes())
            elif st == "artist":
                items.extend(section.searchTracks())
            elif st == "photo" and include_photos:
                items.extend(section.search(libtype="photo"))
        except Exception:
            pass
    return items


def fetch_jellyfin_all_media(user_id, types=_JF_MEDIA_TYPES_WATCH_STATE, fields="Path"):
    """All items of the given Jellyfin types, returned with the requested Fields.

    Paged to keep individual responses small enough to return within the request
    timeout on large libraries.
    """
    page_size = 500
    items = []
    for media_type in types:
        start = 0
        while True:
            result = jf_get(
                f"/Users/{user_id}/Items",
                params={
                    "IncludeItemTypes": media_type,
                    "Recursive":        "true",
                    "Fields":           fields,
                    "StartIndex":       start,
                    "Limit":            page_size,
                },
                timeout=120,
            )
            batch = result.get("Items", [])
            items.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
    return items


def build_path_index(items, paths_fn):
    """(by_exact, by_basename) indexes for path lookup. paths_fn(item) -> [paths]."""
    by_exact = {}
    by_basename = {}
    for item in items:
        for path in paths_fn(item):
            np = normalize_path(path)
            by_exact[np] = item
            base = np.rsplit("/", 1)[-1]
            by_basename.setdefault(base, []).append((np, item))
    return by_exact, by_basename


def build_plex_title_index(plex_items):
    """(plex_TYPE, lowercase title) → list of Plex items, for metadata fallback."""
    idx = {}
    for item in plex_items:
        title = (item.title or "").strip().lower()
        if not title:
            continue
        idx.setdefault((item.TYPE, title), []).append(item)
    return idx


def build_jellyfin_title_index(jf_items):
    """(JF Type, lowercase Name) → list of Jellyfin items, for metadata fallback."""
    idx = {}
    for item in jf_items:
        title = (item.get("Name") or "").strip().lower()
        if not title:
            continue
        idx.setdefault((item.get("Type", ""), title), []).append(item)
    return idx


def find_by_path(source_path, by_exact, by_basename, path_map):
    """Look up an item by file path, after optional translation. None if no match."""
    if not source_path:
        return None
    translated = normalize_path(apply_path_map(source_path, path_map))
    if translated in by_exact:
        return by_exact[translated]

    base = translated.rsplit("/", 1)[-1]
    candidates = by_basename.get(base)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    # Multiple files share the basename — pick the one with the longest matching suffix.
    parts = translated.split("/")
    best, best_len = None, 0
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        for cand_np, item in candidates:
            if cand_np.endswith(suffix) and len(suffix) > best_len:
                best, best_len = item, len(suffix)
    return best


# ---- Playlist matching -------------------------------------------------------

def fetch_jellyfin_playlist(user_id, playlist_name):
    """Return all items in the named Jellyfin playlist."""
    pls = jf_get(
        f"/Users/{user_id}/Items",
        params={"IncludeItemTypes": "Playlist", "Recursive": "true"},
    )["Items"]
    pl = next((p for p in pls if p["Name"] == playlist_name), None)
    if not pl:
        sys.exit(f"Playlist not found in Jellyfin: {playlist_name}")

    return jf_get(
        f"/Playlists/{pl['Id']}/Items",
        params={
            "UserId": user_id,
            "Fields": "Path,AlbumArtist,Artists,SeriesName,IndexNumber,ParentIndexNumber",
        },
    )["Items"]


def fetch_plex_playlist(plex, name):
    """Return all items in the named Plex playlist."""
    try:
        return plex.playlist(name).items()
    except NotFound:
        sys.exit(f"Playlist not found in Plex: {name}")


def find_plex_item(by_exact, by_basename, by_title, jf_item, path_map):
    """Locate a Plex item for a Jellyfin playlist entry. Returns None if no match."""
    found = find_by_path(jf_item.get("Path"), by_exact, by_basename, path_map)
    if found is not None:
        return found

    title = (jf_item.get("Name") or "").strip().lower()
    if not title:
        return None

    jf_type   = jf_item.get("Type", "")
    plex_type = _JF_TO_PLEX_TYPE.get(jf_type)
    if plex_type:
        candidates = by_title.get((plex_type, title), [])
    else:
        candidates = [c for (pt, t), cs in by_title.items() if t == title for c in cs]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if jf_type == "Audio":
        artist = (jf_item.get("AlbumArtist") or (jf_item.get("Artists") or [""])[0]).strip().lower()
        if not artist:
            return None
        for item in candidates:
            # originalTitle = track artist override; grandparentTitle = album artist.
            # Both are cached on the Track object, so no extra HTTP call.
            got = (
                getattr(item, "originalTitle", "") or
                getattr(item, "grandparentTitle", "") or ""
            ).strip().lower()
            if got == artist:
                return item

    elif jf_type == "Episode":
        series  = (jf_item.get("SeriesName") or "").strip().lower()
        season  = jf_item.get("ParentIndexNumber")
        episode = jf_item.get("IndexNumber")
        for item in candidates:
            try:
                show_ok = not series  or item.grandparentTitle.strip().lower() == series
                sea_ok  = season  is None or item.parentIndex == season
                ep_ok   = episode is None or item.index == episode
                if show_ok and sea_ok and ep_ok:
                    return item
            except Exception:
                pass

    return None


def find_jellyfin_item(by_exact, by_basename, by_title, plex_item, path_map):
    """Locate a Jellyfin item for a Plex playlist entry. Returns None if no match."""
    for plex_path in item_paths(plex_item):
        found = find_by_path(plex_path, by_exact, by_basename, path_map)
        if found is not None:
            return found

    title = (plex_item.title or "").strip().lower()
    if not title:
        return None

    plex_type = plex_item.TYPE
    jf_type   = _PLEX_TO_JF_TYPE.get(plex_type)
    if jf_type:
        candidates = by_title.get((jf_type, title), [])
    else:
        candidates = [c for (jt, t), cs in by_title.items() if t == title for c in cs]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if plex_type == "track":
        artist = (
            getattr(plex_item, "originalTitle", "") or
            getattr(plex_item, "grandparentTitle", "") or ""
        ).strip().lower()
        if not artist:
            return None
        for jf_item in candidates:
            jf_artist = (jf_item.get("AlbumArtist") or (jf_item.get("Artists") or [""])[0]).strip().lower()
            if jf_artist == artist:
                return jf_item

    elif plex_type == "episode":
        try:
            show   = plex_item.grandparentTitle.strip().lower()
            season = plex_item.parentIndex
            ep_num = plex_item.index
        except Exception:
            return None
        for jf_item in candidates:
            jf_show = (jf_item.get("SeriesName") or "").strip().lower()
            jf_sea  = jf_item.get("ParentIndexNumber")
            jf_ep   = jf_item.get("IndexNumber")
            if jf_show == show and jf_sea == season and jf_ep == ep_num:
                return jf_item

    return None


def create_jellyfin_playlist(user_id, name, item_ids):
    """Delete the existing Jellyfin playlist if present, then create a fresh one."""
    pls = jf_get(
        f"/Users/{user_id}/Items",
        params={"IncludeItemTypes": "Playlist", "Recursive": "true"},
    )["Items"]
    existing = next((p for p in pls if p["Name"] == name), None)
    if existing:
        jf_delete(f"/Items/{existing['Id']}")
        print(f"Removed existing Jellyfin playlist '{name}'.")

    jf_post("/Playlists", json={"Name": name, "Ids": item_ids, "UserId": user_id})


def jf_label(jf_item):
    """Human-readable label for a Jellyfin item."""
    jf_type = jf_item.get("Type", "")
    name    = jf_item.get("Name", "?")
    if jf_type == "Audio":
        artist = jf_item.get("AlbumArtist") or (jf_item.get("Artists") or [""])[0]
        return f"{artist} - {name}"
    if jf_type == "Episode":
        series  = jf_item.get("SeriesName", "")
        season  = jf_item.get("ParentIndexNumber")
        episode = jf_item.get("IndexNumber")
        if isinstance(season, int) and isinstance(episode, int):
            return f"{series} S{season:02d}E{episode:02d} - {name}"
        return f"{series} - {name}"
    return name


def plex_label(item):
    """Human-readable label for a Plex item."""
    t = item.TYPE
    if t == "track":
        artist = getattr(item, "originalTitle", "") or getattr(item, "grandparentTitle", "") or ""
        return f"{artist} - {item.title}" if artist else item.title
    if t == "episode":
        try:
            return f"{item.grandparentTitle} S{item.parentIndex:02d}E{item.index:02d} - {item.title}"
        except Exception:
            pass
    return item.title


# ---- Watch-state sync (bidirectional) ----------------------------------------

def _parse_jf_timestamp(s):
    """Parse a Jellyfin ISO 8601 string to an aware UTC datetime. None if absent/bad."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        tz_idx = max(tail.rfind("+"), tail.rfind("-"))
        if tz_idx > 0:
            s = f"{head}.{tail[:tz_idx][:6]}{tail[tz_idx:]}"
        else:
            s = f"{head}.{tail[:6]}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_utc(dt):
    """Coerce a possibly-naive datetime to UTC. None passes through."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pick_source(jf_item, plex_item):
    """
    Decide which side's state is authoritative for this pair.
    Returns "jellyfin", "plex", or None (nothing to do).
    """
    ud = jf_item.get("UserData") or {}
    jf_watched   = bool(ud.get("Played"))
    jf_pos_ticks = int(ud.get("PlaybackPositionTicks") or 0)
    jf_time      = _parse_jf_timestamp(ud.get("LastPlayedDate"))

    plex_watched = (plex_item.viewCount or 0) > 0
    plex_pos_ms  = int(plex_item.viewOffset or 0)
    plex_time    = _to_utc(plex_item.lastViewedAt)

    if not (jf_watched or jf_pos_ticks or jf_time or
            plex_watched or plex_pos_ms or plex_time):
        return None
    if jf_time and plex_time:
        if jf_time > plex_time:
            return "jellyfin"
        if plex_time > jf_time:
            return "plex"
        return None
    if jf_time:
        return "jellyfin"
    if plex_time:
        return "plex"
    if (jf_watched or jf_pos_ticks) and not (plex_watched or plex_pos_ms):
        return "jellyfin"
    if (plex_watched or plex_pos_ms) and not (jf_watched or jf_pos_ticks):
        return "plex"
    return None


def _push_state_to_plex(plex_item, jf_item):
    """Apply Jellyfin's watch state to a Plex item."""
    ud = jf_item.get("UserData") or {}
    watched   = bool(ud.get("Played"))
    pos_ms    = int(ud.get("PlaybackPositionTicks") or 0) // 10000

    if watched:
        plex_item.markPlayed()
    else:
        plex_item.markUnplayed()
        if pos_ms > 0 and plex_item.TYPE in ("movie", "episode"):
            try:
                plex_item.updateProgress(pos_ms)
            except Exception:
                pass


def _push_state_to_jellyfin(user_id, jf_item, plex_item):
    """Apply Plex's watch state to a Jellyfin item."""
    watched = (plex_item.viewCount or 0) > 0
    pos_ms  = int(plex_item.viewOffset or 0)
    jf_id   = jf_item["Id"]

    if watched:
        played_at = _to_utc(plex_item.lastViewedAt)
        params = {}
        if played_at:
            params["datePlayed"] = played_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        jf_post(f"/Users/{user_id}/PlayedItems/{jf_id}", params=params)
    else:
        try:
            jf_delete(f"/Users/{user_id}/PlayedItems/{jf_id}")
        except Exception:
            pass
        if pos_ms > 0:
            jf_post(
                f"/Users/{user_id}/Items/{jf_id}/UserData",
                json={"PlaybackPositionTicks": pos_ms * 10000},
            )


def sync_watch_state(user_id, plex, path_map):
    sections = get_plex_sections(plex)
    print(f"  Scanning Plex libraries: {', '.join(s.title for s in sections)}")

    plex_items = fetch_plex_all_media(sections, include_photos=False)
    print(f"  Plex items:     {len(plex_items)}")
    by_exact, by_basename = build_path_index(plex_items, item_paths)

    jf_items = fetch_jellyfin_all_media(user_id, types=_JF_MEDIA_TYPES_WATCH_STATE)
    print(f"  Jellyfin items: {len(jf_items)}")

    matched = updated_to_plex = updated_to_jellyfin = skipped = errors = 0
    unmatched = 0

    for jf_item in jf_items:
        plex_item = find_by_path(
            jf_item.get("Path"), by_exact, by_basename, path_map
        )
        if plex_item is None:
            unmatched += 1
            continue
        matched += 1

        source = _pick_source(jf_item, plex_item)
        if source is None:
            skipped += 1
            continue

        try:
            if source == "jellyfin":
                _push_state_to_plex(plex_item, jf_item)
                updated_to_plex += 1
            else:
                _push_state_to_jellyfin(user_id, jf_item, plex_item)
                updated_to_jellyfin += 1
        except Exception as e:
            errors += 1
            print(f"  ! {jf_item.get('Name', '?')}: {e}")

    print(f"Matched: {matched} (Jellyfin items not in Plex: {unmatched})")
    print(f"  Pushed Jellyfin → Plex:    {updated_to_plex}")
    print(f"  Pushed Plex → Jellyfin:    {updated_to_jellyfin}")
    print(f"  Already in sync / no data: {skipped}")
    if errors:
        print(f"  Errors:                    {errors}")


# ---- Daemon mode -------------------------------------------------------------
#
# Webhook setup:
#
# Plex (Plex Pass): Settings → Account → Webhooks → Add
#   http://<this-host>:<DAEMON_PORT>/plex/<WEBHOOK_TOKEN>
#
# Jellyfin (Webhook plugin): Dashboard → Plugins → Webhook → Add Generic
#   Webhook URL:           http://<this-host>:<DAEMON_PORT>/jellyfin/<WEBHOOK_TOKEN>
#   Notification Type:     enable "Playback Stop" only
#   Request Content Type:  application/json
#   Template:
#       {
#         "NotificationType": "{{NotificationType}}",
#         "UserId":           "{{UserId}}",
#         "ItemId":           "{{ItemId}}"
#       }
#
# Both servers must be able to reach <this-host>:<DAEMON_PORT>.


class _IndexSnapshot:
    """Immutable index pair; swap by reassigning the field on DaemonState."""
    __slots__ = ("plex_exact", "plex_base", "jf_exact", "jf_base",
                 "plex_count", "jf_count")

    def __init__(self):
        self.plex_exact = {}
        self.plex_base  = {}
        self.jf_exact   = {}
        self.jf_base    = {}
        self.plex_count = 0
        self.jf_count   = 0


class _StdoutToLog:
    """File-like that forwards complete lines to a logger (line-buffered)."""
    def __init__(self, log, level):
        self._log = log
        self._level = level
        self._buf = ""

    def write(self, msg):
        self._buf += msg
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._log.log(self._level, line)
        return len(msg)

    def flush(self):
        if self._buf.strip():
            self._log.log(self._level, self._buf.rstrip())
        self._buf = ""

    def isatty(self):
        return False


def _setup_daemon_log(path):
    log = logging.getLogger("jellyfin2plex")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=1)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log.propagate = False
    return log


def _parse_pairs(spec, name):
    out = {}
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            sys.exit(f"Bad {name} entry (need key:value): {chunk!r}")
        k, v = chunk.split(":", 1)
        out[k.strip()] = v.strip()
    return out


class DaemonState:
    def __init__(self, path_map, log):
        self.path_map = path_map
        self.log = log

        self.user_map         = _parse_pairs(USER_MAP, "USER_MAP")
        self.plex_user_tokens = _parse_pairs(PLEX_USER_TOKENS, "PLEX_USER_TOKENS")
        missing = [u for u in self.user_map if u not in self.plex_user_tokens]
        if missing:
            sys.exit(f"ERROR: PLEX_USER_TOKENS missing entries for users: {missing}")

        log.info(f"Connecting to Plex admin: {PLEX_URL}")
        self.plex_admin = PlexServer(PLEX_URL, PLEX_TOKEN)

        self.jf_user_id = {}
        for plex_user, jf_name in self.user_map.items():
            self.jf_user_id[plex_user] = resolve_jellyfin_user(jf_name)
            log.info(
                f"User mapped: Plex {plex_user!r} -> "
                f"Jellyfin {jf_name!r} ({self.jf_user_id[plex_user]})"
            )

        self.plex_user_server = {}
        for plex_user, tok in self.plex_user_tokens.items():
            if plex_user in self.user_map:
                self.plex_user_server[plex_user] = PlexServer(PLEX_URL, tok)

        self.snap = _IndexSnapshot()
        self.index_built = threading.Event()
        self.refresh_lock = threading.Lock()

        self.suppress = {}
        self.suppress_lock = threading.Lock()

        self.pending = []
        self.pending_lock = threading.Lock()

    def build_index(self):
        if not self.refresh_lock.acquire(blocking=False):
            self.log.info("Index refresh already running; skipping.")
            return
        try:
            self.log.info("Building path index...")
            t0 = time.time()
            new = _IndexSnapshot()

            sections = get_plex_sections(self.plex_admin)
            plex_items = fetch_plex_all_media(sections, include_photos=False)
            new.plex_exact, new.plex_base = build_path_index(plex_items, item_paths)
            new.plex_count = len(plex_items)

            any_jf_user = next(iter(self.jf_user_id.values()))
            jf_items = fetch_jellyfin_all_media(any_jf_user, types=_JF_MEDIA_TYPES_WATCH_STATE)
            new.jf_exact, new.jf_base = build_path_index(jf_items, _jf_paths)
            new.jf_count = len(jf_items)

            self.snap = new
            self.log.info(
                f"Index built in {time.time()-t0:.1f}s: "
                f"{new.plex_count} Plex, {new.jf_count} Jellyfin items."
            )
            if not self.index_built.is_set():
                self.index_built.set()
                self._drain_pending()
        finally:
            self.refresh_lock.release()

    def _drain_pending(self):
        with self.pending_lock:
            queued, self.pending = self.pending, []
        if queued:
            self.log.info(f"Draining {len(queued)} queued webhook(s)")
        for fn in queued:
            try:
                fn()
            except Exception:
                self.log.exception("Queued webhook handler failed")

    def run_or_queue(self, fn):
        if self.index_built.is_set():
            fn()
            return
        with self.pending_lock:
            if self.index_built.is_set():
                fn()
                return
            self.pending.append(fn)
            self.log.info("Webhook queued (initial index still building)")

    def should_suppress(self, side, key):
        with self.suppress_lock:
            now = time.time()
            self.suppress = {k: v for k, v in self.suppress.items() if v > now}
            return self.suppress.get((side, key), 0) > now

    def mark_write(self, side, key, seconds=15):
        with self.suppress_lock:
            self.suppress[(side, key)] = time.time() + seconds


def _parse_plex_payload(body, content_type):
    ct = (content_type or "").lower()
    if "application/json" in ct:
        return json.loads(body or b"{}")
    if "multipart/form-data" not in ct:
        return {}
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    msg = BytesParser(policy=_email_default_policy).parsebytes(raw)
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition") or ""
        if 'name="payload"' in cd:
            data = part.get_payload(decode=True) or b""
            return json.loads(data or b"{}")
    return {}


def _handle_plex_event(state, payload):
    event = payload.get("event")
    if event not in ("media.scrobble", "media.stop"):
        return

    account = ((payload.get("Account") or {}).get("title") or "").strip()
    if account not in state.user_map:
        state.log.info(f"Plex {event}: user {account!r} not in USER_MAP; ignored")
        return

    metadata = payload.get("Metadata") or {}
    paths = [p.get("file") for m in (metadata.get("Media") or [])
                          for p in (m.get("Part") or []) if p.get("file")]
    if not paths:
        state.log.warning(f"Plex {event}: no file path in payload")
        return

    snap = state.snap
    jf_item = None
    for p in paths:
        jf_item = find_by_path(p, snap.jf_exact, snap.jf_base, state.path_map)
        if jf_item:
            break
    if not jf_item:
        state.log.warning(f"Plex {event}: no Jellyfin match for {paths[0]}")
        return

    jf_user_id = state.jf_user_id[account]
    jf_id = jf_item["Id"]

    if state.should_suppress("jellyfin", (jf_user_id, jf_id)):
        state.log.info(f"Plex {event}: suppressed (recent write to Jellyfin item)")
        return

    title = metadata.get("title") or jf_item.get("Name", "?")
    state.mark_write("jellyfin", (jf_user_id, jf_id))

    if event == "media.scrobble":
        state.log.info(f"Plex scrobble [{account}] -> Jellyfin watched: {title}")
        try:
            jf_post(f"/Users/{jf_user_id}/PlayedItems/{jf_id}")
        except Exception:
            state.log.exception("Failed marking Jellyfin played")
        return

    view_offset = int(metadata.get("viewOffset") or 0)
    duration    = int(metadata.get("duration") or 0)
    if view_offset <= 0:
        return
    if duration and view_offset >= duration * 0.9:
        return
    state.log.info(f"Plex stop [{account}] -> Jellyfin resume {view_offset}ms: {title}")
    try:
        jf_post(
            f"/Users/{jf_user_id}/Items/{jf_id}/UserData",
            json={"PlaybackPositionTicks": view_offset * 10000},
        )
    except Exception:
        state.log.exception("Failed setting Jellyfin resume position")


def _handle_jellyfin_event(state, payload):
    event = payload.get("NotificationType") or payload.get("Event")
    if event != "PlaybackStop":
        return

    user_id = payload.get("UserId")
    plex_user = next((pu for pu, jid in state.jf_user_id.items() if jid == user_id), None)
    if not plex_user:
        state.log.info(f"Jellyfin {event}: user {user_id!r} not in USER_MAP; ignored")
        return

    item_id = payload.get("ItemId")
    if not item_id:
        state.log.warning(f"Jellyfin {event}: no ItemId in payload")
        return

    if state.should_suppress("plex", (plex_user, item_id)):
        state.log.info(f"Jellyfin {event}: suppressed (recent write to Plex item)")
        return

    try:
        item = jf_get(f"/Users/{user_id}/Items/{item_id}")
    except Exception:
        state.log.exception(f"Failed fetching Jellyfin item {item_id}")
        return

    path = item.get("Path")
    if not path:
        state.log.warning(f"Jellyfin {event}: no path for item {item_id}")
        return

    snap = state.snap
    plex_item = find_by_path(path, snap.plex_exact, snap.plex_base, state.path_map)
    if not plex_item:
        state.log.warning(f"Jellyfin {event}: no Plex match for {path}")
        return

    try:
        user_plex_item = state.plex_user_server[plex_user].fetchItem(plex_item.ratingKey)
    except Exception:
        state.log.exception(
            f"Failed fetching Plex item {plex_item.ratingKey} as {plex_user}"
        )
        return

    state.mark_write("plex", (plex_user, item_id))

    ud = item.get("UserData") or {}
    title = item.get("Name", "?")
    try:
        _push_state_to_plex(user_plex_item, item)
        if ud.get("Played"):
            state.log.info(f"Jellyfin stop [{plex_user}] -> Plex watched: {title}")
        else:
            pos = int(ud.get("PlaybackPositionTicks") or 0) // 10000
            state.log.info(f"Jellyfin stop [{plex_user}] -> Plex resume {pos}ms: {title}")
    except Exception:
        state.log.exception("Failed pushing state to Plex")


def _make_webhook_handler(state, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return  # silence default access log; we log our own events

        def _read(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(n) if n > 0 else b""

        def _reply(self, code, msg="ok"):
            data = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.strip("/") == "health":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) < 2 or parts[1] != token:
                self._reply(404, "not found")
                return
            kind = parts[0]
            try:
                body = self._read()
                if kind == "plex":
                    payload = _parse_plex_payload(body, self.headers.get("Content-Type", ""))
                    state.run_or_queue(lambda p=payload: _handle_plex_event(state, p))
                elif kind == "jellyfin":
                    payload = json.loads(body or b"{}")
                    state.run_or_queue(lambda p=payload: _handle_jellyfin_event(state, p))
                else:
                    self._reply(404, "not found")
                    return
                self._reply(200, "ok")
            except Exception:
                state.log.exception(f"Error handling {kind} webhook")
                self._reply(500, "error")

    return Handler


def _periodic(label, log, interval_s, fn):
    while True:
        time.sleep(interval_s)
        try:
            log.info(f"Periodic task: {label}")
            fn()
        except Exception:
            log.exception(f"Periodic task {label!r} failed")


def _reconcile_all(state):
    for plex_user, jf_user_id in state.jf_user_id.items():
        state.log.info(f"Reconcile: {plex_user} / {jf_user_id}")
        try:
            sync_watch_state(jf_user_id, state.plex_user_server[plex_user], state.path_map)
        except Exception:
            state.log.exception(f"Reconcile failed for {plex_user}")


def _run_daemon(path_map):
    log = _setup_daemon_log(LOG_PATH)
    sys.stdout = _StdoutToLog(log, logging.INFO)
    sys.stderr = _StdoutToLog(log, logging.WARNING)

    log.info(f"Daemon start: Plex {PLEX_URL}, Jellyfin {JELLYFIN_URL}")
    state = DaemonState(path_map, log)

    refresh_s   = max(60, int(INDEX_REFRESH_MIN) * 60)
    reconcile_s = max(60, int(RECONCILE_INTERVAL_MIN) * 60)

    threading.Thread(target=state.build_index, daemon=True, name="initial-index").start()
    threading.Thread(
        target=_periodic, args=("index-refresh", log, refresh_s, state.build_index),
        daemon=True, name="index-refresh",
    ).start()
    threading.Thread(
        target=_periodic, args=("reconcile", log, reconcile_s, lambda: _reconcile_all(state)),
        daemon=True, name="reconcile",
    ).start()

    def startup_reconcile():
        state.index_built.wait()
        log.info("Startup reconcile...")
        _reconcile_all(state)
    threading.Thread(target=startup_reconcile, daemon=True, name="startup-reconcile").start()

    port = int(DAEMON_PORT)
    server = ThreadingHTTPServer(
        (DAEMON_HOST, port), _make_webhook_handler(state, WEBHOOK_TOKEN)
    )
    log.info(f"Listening on {DAEMON_HOST}:{port}")

    def shutdown(signum, _frame):
        log.info(f"Signal {signum} received; shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()
    for _sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        # SIGTERM/SIGBREAK availability varies by platform (Windows has SIGBREAK
        # for Ctrl+Break; SIGTERM is defined but only fires for forced termination).
        _sig = getattr(signal, _sig_name, None)
        if _sig is None:
            continue
        try:
            signal.signal(_sig, shutdown)
        except (ValueError, OSError):
            pass

    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("Stopped")


# ---- Entry point -------------------------------------------------------------

def _run_playlist_sync(path_map):
    print(f"Direction: {DIRECTION}")
    print(f"Jellyfin:  {JELLYFIN_URL}")
    print(f"Plex:      {PLEX_URL}")

    if DIRECTION == "jellyfin_to_plex":
        user_id  = resolve_jellyfin_user(JELLYFIN_USER)
        jf_items = fetch_jellyfin_playlist(user_id, SOURCE_PLAYLIST)
        print(f"  Source playlist '{SOURCE_PLAYLIST}' -> {len(jf_items)} items")

        plex     = PlexServer(PLEX_URL, PLEX_TOKEN)
        sections = get_plex_sections(plex)
        print(f"  Scanning Plex libraries: {', '.join(s.title for s in sections)}")
        plex_items = fetch_plex_all_media(sections, include_photos=True)
        print(f"  Indexed {len(plex_items)} Plex items.")

        by_exact, by_basename = build_path_index(plex_items, item_paths)
        by_title              = build_plex_title_index(plex_items)

        matched, missing = [], []
        for it in jf_items:
            item = find_plex_item(by_exact, by_basename, by_title, it, path_map)
            if item:
                matched.append(item)
            else:
                missing.append(jf_label(it))

        print(f"Matched {len(matched)}/{len(jf_items)} items.")
        if missing:
            print("Missing in Plex:")
            for m in missing:
                print(f"  - {m}")

        if not matched:
            sys.exit("Nothing to add; aborting.")

        try:
            plex.playlist(TARGET_PLAYLIST).delete()
            print(f"Removed existing Plex playlist '{TARGET_PLAYLIST}'.")
        except NotFound:
            pass

        plex.createPlaylist(TARGET_PLAYLIST, items=matched)
        print(f"Created Plex playlist '{TARGET_PLAYLIST}' with {len(matched)} items.")

    else:  # plex_to_jellyfin
        plex           = PlexServer(PLEX_URL, PLEX_TOKEN)
        playlist_items = fetch_plex_playlist(plex, SOURCE_PLAYLIST)
        print(f"  Source playlist '{SOURCE_PLAYLIST}' -> {len(playlist_items)} items")

        user_id = resolve_jellyfin_user(JELLYFIN_USER)
        jf_lib_items = fetch_jellyfin_all_media(
            user_id,
            types=_JF_MEDIA_TYPES_PLAYLIST,
            fields="Path,AlbumArtist,Artists,SeriesName,IndexNumber,ParentIndexNumber",
        )
        print(f"  Indexed {len(jf_lib_items)} Jellyfin items.")

        by_exact, by_basename = build_path_index(jf_lib_items, _jf_paths)
        by_title              = build_jellyfin_title_index(jf_lib_items)

        matched, missing = [], []
        for it in playlist_items:
            item = find_jellyfin_item(by_exact, by_basename, by_title, it, path_map)
            if item:
                matched.append(item["Id"])
            else:
                missing.append(plex_label(it))

        print(f"Matched {len(matched)}/{len(playlist_items)} items.")
        if missing:
            print("Missing in Jellyfin:")
            for m in missing:
                print(f"  - {m}")

        if not matched:
            sys.exit("Nothing to add; aborting.")

        create_jellyfin_playlist(user_id, TARGET_PLAYLIST, matched)
        print(f"Created Jellyfin playlist '{TARGET_PLAYLIST}' with {len(matched)} items.")


def _run_watch_state_sync(path_map):
    print(f"Operation: watch_state (bidirectional)")
    print(f"Jellyfin:  {JELLYFIN_URL}")
    print(f"Plex:      {PLEX_URL}")

    user_id = resolve_jellyfin_user(JELLYFIN_USER)
    plex    = PlexServer(PLEX_URL, PLEX_TOKEN)
    sync_watch_state(user_id, plex, path_map)


def main():
    path_map = parse_path_map(PATH_MAP_SPEC)
    if OPERATION == "playlist":
        _run_playlist_sync(path_map)
    elif OPERATION == "watch_state":
        _run_watch_state_sync(path_map)
    else:
        _run_daemon(path_map)


if __name__ == "__main__":
    main()
