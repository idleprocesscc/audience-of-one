"""Read-only record shelves for Spotify and a local music library."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from .adapters.local_mpv import SUPPORTED_SUFFIXES
from .adapters.spotify import SpotifyClient, SpotifyError

SPOTIFY_LIST_SCOPES = {
    "playlist-read-collaborative",
    "playlist-read-private",
    "user-read-recently-played",
    "user-top-read",
}


def _spotify_track(item: dict[str, Any]) -> dict[str, Any] | None:
    uri = str(item.get("uri") or "")
    if item.get("type") not in {None, "track"} or not uri.startswith("spotify:track:"):
        return None
    return {
        "uri": uri,
        "name": str(item.get("name") or uri),
        "artists": [
            str(artist.get("name")) for artist in item.get("artists") or []
            if artist.get("name")
        ],
        "album": str((item.get("album") or {}).get("name") or ""),
        "duration_ms": item.get("duration_ms"),
        "is_playable": item.get("is_playable"),
    }


def _pages(client: SpotifyClient, path: str, *, limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        payload = client.request("GET", path, {"limit": limit, "offset": offset}) or {}
        page = list(payload.get("items") or [])
        rows.extend(page)
        offset += len(page)
        total = int(payload.get("total") or len(rows))
        if not page or offset >= total:
            return rows


def _playlist_id(value: str) -> str:
    raw = str(value or "").strip()
    for pattern in (r"spotify:playlist:([A-Za-z0-9]+)", r"playlist/([A-Za-z0-9]+)"):
        match = re.search(pattern, raw)
        if match:
            return match.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9]+", raw) else ""


def select_playlist(playlists: list[dict], configured: str = "") -> dict | None:
    """Choose one shelf from configuration, or one unambiguous ordinary playlist."""
    rows = [row for row in playlists if row.get("id")]
    wanted = str(configured or "").strip()
    if wanted:
        wanted_id = _playlist_id(wanted)
        exact = [
            row for row in rows
            if str(row.get("id")) == wanted_id
            or str(row.get("name") or "").casefold() == wanted.casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        partial = [
            row for row in rows
            if wanted.casefold() in str(row.get("name") or "").casefold()
        ]
        return partial[0] if len(partial) == 1 else None
    ordinary = [
        row for row in rows
        if str(row.get("name") or "").casefold() != "discover weekly"
    ]
    if len(ordinary) == 1:
        return ordinary[0]
    return rows[0] if len(rows) == 1 else None


def _playlist_tracks(client: SpotifyClient, playlist: dict) -> list[dict]:
    rows = _pages(client, f"/v1/playlists/{playlist['id']}/items")
    tracks = []
    for position, row in enumerate(rows, 1):
        track = _spotify_track(row.get("item") or row.get("track") or {})
        if track:
            tracks.append({**track, "playlist_position": position})
    return tracks


def _identity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def _identity(track: dict) -> tuple[str, tuple[str, ...]] | None:
    title = _identity_text(track.get("name"))
    artists = tuple(sorted(filter(None, (_identity_text(a) for a in track.get("artists") or []))))
    return (title, artists) if title and artists else None


def _matched_uri(track: dict, candidates: dict[str, dict], tolerance_ms: int = 2000) -> str:
    uri = str(track.get("uri") or "")
    if uri in candidates:
        return uri
    identity = _identity(track)
    try:
        duration = int(track.get("duration_ms"))
    except (TypeError, ValueError):
        return ""
    matches = []
    for candidate_uri, candidate in candidates.items():
        if _identity(candidate) != identity:
            continue
        try:
            candidate_duration = int(candidate.get("duration_ms"))
        except (TypeError, ValueError):
            continue
        if abs(duration - candidate_duration) <= tolerance_ms:
            matches.append(candidate_uri)
    return matches[0] if len(matches) == 1 else ""


def spotify_shelf(
    client: SpotifyClient, spotify_config: dict[str, Any], *, playlist: str = "",
) -> dict:
    if spotify_config.get("adapter") != "web_api":
        raise SpotifyError("Spotify LIST needs spotify.adapter = web_api")
    client.require_scopes(SPOTIFY_LIST_SCOPES, action="run station spotify auth again")

    playlists = []
    for row in _pages(client, "/v1/me/playlists"):
        playlist_id = str(row.get("id") or "")
        if playlist_id:
            playlists.append({
                "id": playlist_id,
                "name": str(row.get("name") or playlist_id),
                "uri": str(row.get("uri") or f"spotify:playlist:{playlist_id}"),
                "owner": str((row.get("owner") or {}).get("display_name") or ""),
                "total": int(((row.get("items") or row.get("tracks") or {}).get("total")) or 0),
            })

    configured = playlist or str(spotify_config.get("primary_playlist") or "")
    primary = select_playlist(playlists, configured)
    if not primary:
        names = ", ".join(row["name"] for row in playlists) or "none"
        raise SpotifyError(
            "Spotify LIST cannot choose one primary playlist; set "
            f"spotify.primary_playlist. Available: {names}"
        )
    tracks = _playlist_tracks(client, primary)

    top_payload = client.request("GET", "/v1/me/top/tracks", {
        "time_range": "long_term", "limit": 50,
    }) or {}
    top = {}
    for rank, item in enumerate(top_payload.get("items") or [], 1):
        track = _spotify_track(item)
        if track:
            top[track["uri"]] = {**track, "rank": rank}

    recent_payload = client.request("GET", "/v1/me/player/recently-played", {
        "limit": 10,
    }) or {}
    recent: dict[str, dict] = {}
    for position, row in enumerate(recent_payload.get("items") or [], 1):
        track = _spotify_track(row.get("track") or {})
        if not track:
            continue
        receipt = recent.setdefault(track["uri"], {
            **track, "positions": [], "last_played_at": "",
        })
        receipt["positions"].append(position)
        receipt["last_played_at"] = max(
            receipt["last_played_at"], str(row.get("played_at") or "")
        )

    enriched = []
    for track in tracks:
        top_uri = _matched_uri(track, top)
        recent_uri = _matched_uri(track, recent)
        recent_receipt = recent.get(recent_uri) or {}
        enriched.append({
            **track,
            "long_term_rank": (top.get(top_uri) or {}).get("rank"),
            "long_term_match": "uri" if top_uri == track["uri"] else (
                "identity" if top_uri else None
            ),
            "recent_positions": list(recent_receipt.get("positions") or []),
            "recent_count": len(recent_receipt.get("positions") or []),
            "last_played_at": recent_receipt.get("last_played_at") or None,
            "recent_match": "uri" if recent_uri == track["uri"] else (
                "identity" if recent_uri else None
            ),
        })

    long_term = sorted(
        (track for track in enriched if track["long_term_rank"] is not None),
        key=lambda track: (track["long_term_rank"], track["playlist_position"]),
    )
    unranked = [track for track in enriched if track["long_term_rank"] is None]

    weekly_config = str(spotify_config.get("weekly_playlist") or "")
    weekly = select_playlist(playlists, weekly_config) if weekly_config else next(
        (row for row in playlists if row["name"].casefold() == "discover weekly"), None
    )
    weekly_tracks = []
    weekly_error = None
    if weekly and weekly["id"] != primary["id"]:
        try:
            primary_uris = {track["uri"] for track in tracks}
            for track in _playlist_tracks(client, weekly):
                recent_uri = _matched_uri(track, recent)
                recent_receipt = recent.get(recent_uri) or {}
                weekly_tracks.append({
                    **track,
                    "also_in_primary": track["uri"] in primary_uris,
                    "recent_positions": list(recent_receipt.get("positions") or []),
                    "recent_count": len(recent_receipt.get("positions") or []),
                    "last_played_at": recent_receipt.get("last_played_at") or None,
                })
        except SpotifyError as error:
            # Spotify can refuse followed playlists the listener does not own or
            # collaborate on. Keep the already-proven primary shelf intact.
            weekly_error = str(error)

    return {
        "version": 1,
        "source": "spotify",
        "playlist": primary,
        "sort": "long_term_affinity_then_playlist_order",
        "recent_window": 10,
        "sections": {
            "long_term_preference": long_term,
            "unranked_playlist_order": unranked,
            "spotify_weekly": weekly_tracks,
        },
        "weekly_playlist": weekly,
        "weekly_error": weekly_error,
        "playback_touched": False,
        "rundown_touched": False,
    }


def _tag(tags: dict, name: str) -> str:
    wanted = name.casefold()
    for key, value in tags.items():
        if str(key).casefold() == wanted and value is not None:
            return str(value).strip()
    return ""


def _probe(path: Path, executable: str | None) -> dict:
    if not executable:
        return {}
    try:
        result = subprocess.run(
            [
                executable, "-v", "error", "-show_entries",
                "format=duration:format_tags=title,artist,album,album_artist,track,date",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout).get("format") or {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def local_shelf(music_config: dict[str, Any]) -> dict:
    root_value = str(music_config.get("library_root") or "").strip()
    if not root_value:
        raise SpotifyError("MPV LIST needs music.library_root")
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise SpotifyError(f"local music library does not exist: {root}")
    ffprobe = shutil.which("ffprobe")
    tracks = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        relative = path.relative_to(root).as_posix()
        probe = _probe(resolved, ffprobe)
        tags = probe.get("tags") or {}
        try:
            duration_ms = round(float(probe.get("duration")) * 1000)
        except (TypeError, ValueError):
            duration_ms = None
        title = _tag(tags, "title") or path.stem
        artist = _tag(tags, "artist") or _tag(tags, "album_artist")
        tracks.append({
            "position": len(tracks) + 1,
            "uri": f"local:{relative}",
            "relative_path": relative,
            "name": title,
            "artists": [artist] if artist else [],
            "album": _tag(tags, "album"),
            "track_number": _tag(tags, "track"),
            "date": _tag(tags, "date"),
            "duration_ms": duration_ms,
            "format": path.suffix.casefold().removeprefix("."),
            "bytes": resolved.stat().st_size,
            "metadata_source": "embedded" if tags else "filename",
        })
    return {
        "version": 1,
        "source": "mpv",
        "library_root": str(root),
        "metadata_probe": "ffprobe" if ffprobe else "filename_only",
        "count": len(tracks),
        "tracks": tracks,
        "playback_touched": False,
        "rundown_touched": False,
    }
