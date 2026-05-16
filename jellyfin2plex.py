#!/usr/bin/env python3
"""
Sync a Jellyfin playlist to Plex.

Supports any media type: music, movies, TV episodes, photos, etc.

Works whether Jellyfin and Plex are installed natively or run inside Docker
containers — the script only needs HTTP reachability to each server and either
shared filenames or an optional PATH_MAP to translate container paths.

Matching strategy for each item, in order:
  1. Exact path (after optional PATH_MAP translation).
  2. Progressive suffix match — longest shared path tail wins. Handles
     containers that mount the same files at different paths without any
     configuration in the common case.
  3. Type-aware metadata fallback: artist for music, show/season/episode for
     TV, single-candidate title match for everything else.

Missing items are reported, not silently skipped.
"""

import subprocess
import sys

# Ensure third-party dependencies are available (needed on Unraid/bare Python)
_REQUIRED = ["requests", "plexapi"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *_REQUIRED])

import os
from urllib.parse import urljoin

import requests
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound


# ---- CONFIG ------------------------------------------------------------------
# Fill in values directly here, or leave as "" to read from environment vars.

JELLYFIN_URL     = ""                   # e.g. "http://192.168.1.10:8096"
JELLYFIN_TOKEN   = ""                   # Jellyfin API key
JELLYFIN_USER    = ""                   # Jellyfin username (optional)

PLEX_URL         = ""                   # e.g. "http://192.168.1.10:32400"
PLEX_TOKEN       = ""                   # X-Plex-Token
PLEX_LIBRARIES   = ""                   # Comma-separated library names to search,
                                        # e.g. "Music,Movies,TV Shows".
                                        # Leave blank to search ALL libraries.

SOURCE_PLAYLIST  = ""                   # Playlist name in Jellyfin
TARGET_PLAYLIST  = ""                   # Playlist name in Plex (defaults to SOURCE_PLAYLIST)

# Translate Jellyfin file paths to what Plex sees. Format:
#   "/media:/mnt/media,/data:/srv/data"
# Optional — suffix matching usually works without it.
PATH_MAP_SPEC    = ""

# Fall back to environment variables for any value left blank above.
JELLYFIN_URL     = JELLYFIN_URL     or os.environ.get("JELLYFIN_URL",    "http://localhost:8096")
JELLYFIN_TOKEN   = JELLYFIN_TOKEN   or os.environ.get("JELLYFIN_TOKEN",  "")
JELLYFIN_USER    = JELLYFIN_USER    or os.environ.get("JELLYFIN_USER",   "")
PLEX_URL         = PLEX_URL         or os.environ.get("PLEX_URL",        "http://localhost:32400")
PLEX_TOKEN       = PLEX_TOKEN       or os.environ.get("PLEX_TOKEN",      "")
PLEX_LIBRARIES   = PLEX_LIBRARIES   or os.environ.get("PLEX_LIBRARIES",  "")
SOURCE_PLAYLIST  = SOURCE_PLAYLIST  or os.environ.get("SOURCE_PLAYLIST", "")
TARGET_PLAYLIST  = TARGET_PLAYLIST  or os.environ.get("TARGET_PLAYLIST", SOURCE_PLAYLIST)
PATH_MAP_SPEC    = PATH_MAP_SPEC    or os.environ.get("PATH_MAP",        "")

if not JELLYFIN_TOKEN:
    sys.exit("ERROR: JELLYFIN_TOKEN is required — set it in the CONFIG section above.")
if not PLEX_TOKEN:
    sys.exit("ERROR: PLEX_TOKEN is required — set it in the CONFIG section above.")
if not SOURCE_PLAYLIST:
    sys.exit("ERROR: SOURCE_PLAYLIST is required — set it in the CONFIG section above.")

# ------------------------------------------------------------------------------


def jf_get(path, params=None):
    """GET an arbitrary Jellyfin endpoint and return parsed JSON."""
    headers = {"X-Emby-Token": JELLYFIN_TOKEN, "Accept": "application/json"}
    url = urljoin(JELLYFIN_URL.rstrip("/") + "/", path.lstrip("/"))
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def resolve_jellyfin_user(name):
    users = jf_get("/Users")
    if not name:
        return users[0]["Id"]
    for u in users:
        if u["Name"].lower() == name.lower():
            return u["Id"]
    sys.exit(f"Jellyfin user not found: {name}")


def fetch_jellyfin_playlist(user_id, playlist_name):
    """Return all items in the named playlist (any media type)."""
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


def get_plex_sections(plex):
    """Return Plex library sections to search, per PLEX_LIBRARIES config."""
    if PLEX_LIBRARIES:
        names = [n.strip() for n in PLEX_LIBRARIES.split(",") if n.strip()]
        return [plex.library.section(n) for n in names]
    return plex.library.sections()


def search_candidates(sections, title, jf_type):
    """Search Plex sections for leaf items matching title, suited to jf_type."""
    results = []
    for section in sections:
        try:
            if jf_type == "Audio":
                results.extend(section.searchTracks(title=title))
            elif jf_type == "Episode":
                results.extend(section.searchEpisodes(title=title))
            else:
                # Movies, MusicVideos, Photos, etc. are top-level leaf items.
                results.extend(section.search(title=title))
        except Exception:
            pass
    return results


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


def item_paths(item):
    """All file paths Plex knows for this item."""
    return [p.file for m in item.media for p in m.parts if p.file]


def find_plex_item(sections, jf_item, path_map):
    """Locate a Plex item for a Jellyfin playlist entry. Returns None if no match."""
    title   = jf_item.get("Name", "")
    path    = jf_item.get("Path")
    jf_type = jf_item.get("Type", "")

    candidates = search_candidates(sections, title, jf_type)
    if not candidates:
        return None

    # 1. Exact path match (after optional translation).
    if path:
        translated = apply_path_map(path, path_map)
        for item in candidates:
            if translated in item_paths(item):
                return item

        # 2. Progressive suffix match — longest suffix first so "Artist/Album/song.mp3"
        #    beats bare "song.mp3" when both would match.
        parts = normalize_path(path).split("/")
        for start in range(len(parts)):
            suffix = "/".join(parts[start:])
            for item in candidates:
                for plex_path in item_paths(item):
                    if normalize_path(plex_path).endswith(suffix):
                        return item

    # 3. Type-aware metadata fallback.
    if jf_type == "Audio":
        artist = (jf_item.get("AlbumArtist") or (jf_item.get("Artists") or [""])[0]).strip().lower()
        if not artist:
            return candidates[0] if len(candidates) == 1 else None
        for item in candidates:
            try:
                got = (item.originalTitle or item.artist().title or "").strip().lower()
            except Exception:
                got = ""
            if got == artist:
                return item

    elif jf_type == "Episode":
        series  = jf_item.get("SeriesName", "").strip().lower()
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

    else:
        # Movies, photos, etc. — single title match is enough.
        return candidates[0] if len(candidates) == 1 else None

    return None


def label(jf_item):
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


def main():
    path_map = parse_path_map(PATH_MAP_SPEC)

    print(f"Jellyfin: {JELLYFIN_URL}")
    user_id  = resolve_jellyfin_user(JELLYFIN_USER)
    jf_items = fetch_jellyfin_playlist(user_id, SOURCE_PLAYLIST)
    print(f"  '{SOURCE_PLAYLIST}' -> {len(jf_items)} items")

    print(f"Plex:     {PLEX_URL}")
    plex     = PlexServer(PLEX_URL, PLEX_TOKEN)
    sections = get_plex_sections(plex)
    lib_list = ", ".join(s.title for s in sections)
    print(f"  Searching: {lib_list}")

    matched, missing = [], []
    for it in jf_items:
        item = find_plex_item(sections, it, path_map)
        if item:
            matched.append(item)
        else:
            missing.append(label(it))

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


if __name__ == "__main__":
    main()
