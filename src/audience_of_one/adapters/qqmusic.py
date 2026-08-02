"""QQMusicApi HTTP companion without importing its GPL Python package."""

from __future__ import annotations

import base64
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


class QQMusicError(RuntimeError):
    """The local QQMusicApi companion could not provide trustworthy data."""


def _artists(song: dict[str, Any]) -> list[str]:
    singers = song.get("singer") or song.get("artists") or []
    return [
        str(item.get("name") or "").strip()
        for item in singers
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _album(song: dict[str, Any]) -> str:
    album = song.get("album") or {}
    return str(album.get("name") or album.get("title") or "").strip() \
        if isinstance(album, dict) else ""


def _track(song: dict[str, Any], position: int) -> dict[str, Any]:
    file_info = song.get("file") or {}
    if not isinstance(file_info, dict):
        file_info = {}
    mid = str(song.get("mid") or "").strip()
    return {
        "position": position,
        "uri": f"qqmusic:{mid}",
        "mid": mid,
        "media_mid": str(file_info.get("media_mid") or "").strip(),
        "name": str(song.get("title") or song.get("name") or "").strip(),
        "artists": _artists(song),
        "album": _album(song),
        "duration_ms": int(song.get("interval") or 0) * 1000,
        "source": "qqmusic_api",
    }


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return [word for word in re.findall(r"[\w]+", normalized) if word not in {"by"}]


def select_search_result(query: str, tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose only a unique title/artist match; otherwise require an exact MID."""
    query_words = _words(query)
    if not query_words:
        raise QQMusicError("QQ Music search needs a title or artist")
    title_exact = [track for track in tracks if _words(str(track.get("name") or "")) == query_words]
    if len(title_exact) == 1:
        return title_exact[0]
    if len(title_exact) > 1:
        raise QQMusicError("QQ Music found several versions; choose one exact qqmusic:MID")
    anchored = []
    for track in tracks:
        haystack = _words(" ".join([
            str(track.get("name") or ""),
            " ".join(str(value) for value in track.get("artists") or []),
        ]))
        if all(word in haystack for word in query_words):
            anchored.append(track)
    if len(anchored) == 1:
        return anchored[0]
    if len(anchored) > 1:
        raise QQMusicError("QQ Music search is ambiguous; choose one exact qqmusic:MID")
    raise QQMusicError("QQ Music search did not prove the requested title and artist; choose a MID")


class QQMusicClient:
    """Small client for the separately installed QQMusicApi Web service."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        credential_path: Path | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base = str(config.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QQMusicError("qqmusic.base_url must be an http(s) URL")
        self.base_url = base
        self.timeout = float(config.get("timeout_seconds", 8.0))
        self.retries = int(config.get("retries", 2))
        self.credential_path = credential_path
        self.credential = self._load_credential()
        self.opener = opener
        self.sleep = sleep

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = urllib.parse.urlencode({
            key: value for key, value in (params or {}).items() if value not in {None, ""}
        })
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **({"Cookie": self._cookie()} if self.credential else {}),
            },
        )
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict) or decoded.get("code") != 0:
                    message = decoded.get("msg") if isinstance(decoded, dict) else None
                    raise QQMusicError(f"QQMusicApi rejected the request: {message or 'invalid response'}")
                return decoded.get("data")
            except QQMusicError:
                raise
            except (OSError, TimeoutError, json.JSONDecodeError, urllib.error.URLError) as error:
                last = error
                if attempt < self.retries:
                    self.sleep(0.35 * (attempt + 1))
                    continue
        raise QQMusicError(f"QQMusicApi is unavailable at {self.base_url}: {last}") from last

    def doctor(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/openapi.json", headers={"Accept": "application/json"}
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, urllib.error.URLError) as error:
            raise QQMusicError(f"QQMusicApi is unavailable at {self.base_url}: {error}") from error
        paths = data.get("paths") if isinstance(data, dict) else None
        required = {
            "/search/search_by_type", "/song/{mid}/url", "/song/get_cdn_dispatch",
            "/song/query_song",
            "/login/qrcode/{login_type}", "/user/{euin}/fav/songs",
            "/user/{uin}/created_songlists", "/user/{euin}/fav/songlists",
        }
        missing = sorted(required - set(paths or {}))
        if missing:
            raise QQMusicError(f"QQMusicApi is missing required routes: {', '.join(missing)}")
        return {
            "ready": True,
            "base_url": self.base_url,
            "routes_confirmed": sorted(required),
            "signed_in": bool(self.credential),
        }

    def _load_credential(self) -> dict[str, Any] | None:
        if self.credential_path is None:
            return None
        try:
            data = json.loads(self.credential_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("musicid") or not data.get("musickey"):
            return None
        return data

    def _cookie(self) -> str:
        if not self.credential:
            return ""
        fields = (
            "musicid", "musickey", "openid", "refresh_token", "access_token",
            "expired_at", "unionid", "str_musicid", "refresh_key",
        )
        pairs = []
        for field in fields:
            value = self.credential.get(field)
            if value in {None, "", 0} and field not in {"musicid", "musickey"}:
                continue
            pairs.append(
                f"{field}={urllib.parse.quote(str(value), safe='')}"
            )
        return "; ".join(pairs)

    def begin_login(self, login_type: str = "qq") -> dict[str, Any]:
        if login_type not in {"qq", "wx"}:
            raise QQMusicError("QQ Music login type must be qq or wx")
        data = self._request(f"/login/qrcode/{login_type}")
        if not isinstance(data, dict):
            raise QQMusicError("QQMusicApi returned an invalid login QR")
        identifier = str(data.get("identifier") or "")
        encoded = str(data.get("data") or "")
        mimetype = str(data.get("mimetype") or "image/png")
        if not identifier or not encoded:
            raise QQMusicError("QQMusicApi returned an empty login QR")
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise QQMusicError("QQMusicApi returned invalid QR image data") from error
        if not image or len(image) > 2_000_000:
            raise QQMusicError("QQMusicApi returned an invalid QR image size")
        return {
            "identifier": identifier,
            "login_type": login_type,
            "mimetype": mimetype,
            "image": image,
        }

    def login_status(self, identifier: str, login_type: str = "qq") -> dict[str, Any]:
        data = self._request(
            f"/login/qrcode/{login_type}/status", params={"identifier": identifier}
        )
        if not isinstance(data, dict):
            raise QQMusicError("QQMusicApi returned an invalid login status")
        return data

    def save_credential(self, credential: dict[str, Any]) -> dict[str, Any]:
        if self.credential_path is None:
            raise QQMusicError("QQ Music credential path is not configured")
        normalized = dict(credential)
        aliases = {
            "strMusicid": "str_musicid",
            "refreshKey": "refresh_key",
            "refreshToken": "refresh_token",
            "accessToken": "access_token",
            "expiredAt": "expired_at",
            "encryptUin": "encrypt_uin",
            "musickeyCreateTime": "musickey_create_time",
            "keyExpiresIn": "key_expires_in",
            "loginType": "login_type",
        }
        for source, target in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        if not normalized.get("musicid") or not normalized.get("musickey"):
            raise QQMusicError("QQMusicApi login completed without a usable credential")
        self.credential_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credential_path.write_text(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        self.credential_path.chmod(0o600)
        self.credential = normalized
        musicid = str(normalized["musicid"])
        return {
            "saved": True,
            "credential_path": str(self.credential_path),
            "account": ("*" * max(0, len(musicid) - 3)) + musicid[-3:],
            "has_encrypt_uin": bool(normalized.get("encrypt_uin")),
        }

    def private_shelf(self, *, playlist: str = "") -> dict[str, Any]:
        credential = self.credential
        if not credential:
            raise QQMusicError("QQ Music private LIST needs station qqmusic login")
        musicid = int(credential["musicid"])
        euin = str(credential.get("encrypt_uin") or credential.get("encryptUin") or "")
        if not euin:
            raise QQMusicError("QQ Music credential has no encrypted account id; log in again")

        favorite_tracks = self._favorite_tracks(euin)
        created_data = self._request(f"/user/{musicid}/created_songlists")
        created = self._playlists(created_data, "created")
        collected = self._favorite_playlists(euin)
        selected = None
        if playlist:
            selected_summary = self._select_playlist(playlist, [*created, *collected])
            selected = {
                **selected_summary,
                "tracks": self._playlist_tracks(selected_summary),
            }
        return {
            "version": 1,
            "source": "qqmusic_api_private",
            "signed_in": True,
            "account": ("*" * max(0, len(str(musicid)) - 3)) + str(musicid)[-3:],
            "sections": {
                "my_favorites": favorite_tracks,
                "created_playlists": created,
                "collected_playlists": collected,
            },
            "selected_playlist": selected,
            "playback_touched": False,
            "rundown_touched": False,
        }

    def _favorite_tracks(self, euin: str) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        for page in range(1, 11):
            data = self._request(
                f"/user/{urllib.parse.quote(euin)}/fav/songs",
                params={"page": page, "num": 100},
            )
            songs = data.get("songs") if isinstance(data, dict) else None
            if not isinstance(songs, list):
                raise QQMusicError("QQMusicApi returned an invalid favorite-song list")
            tracks.extend(
                _track(song, len(tracks) + index)
                for index, song in enumerate(songs, 1)
                if isinstance(song, dict) and song.get("mid")
            )
            total = int(data.get("total") or len(tracks))
            if not data.get("hasmore") or len(tracks) >= total:
                break
        return tracks

    @staticmethod
    def _playlists(data: Any, source: str) -> list[dict[str, Any]]:
        values = data.get("playlists") if isinstance(data, dict) else None
        if not isinstance(values, list):
            raise QQMusicError(f"QQMusicApi returned an invalid {source} playlist list")
        return [
            {
                "id": int(item.get("id") or 0),
                "dirid": int(item.get("dirid") or 0),
                "name": str(item.get("title") or "").strip(),
                "track_count": int(item.get("songnum") or 0),
                "source": source,
            }
            for item in values if isinstance(item, dict) and item.get("id")
        ]

    def _favorite_playlists(self, euin: str) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        for page in range(1, 11):
            data = self._request(
                f"/user/{urllib.parse.quote(euin)}/fav/songlists",
                params={"page": page, "num": 100},
            )
            batch = self._playlists(data, "collected")
            playlists.extend(batch)
            total = int(data.get("total") or len(playlists)) if isinstance(data, dict) else 0
            if not isinstance(data, dict) or not data.get("hasmore") or len(playlists) >= total:
                break
        return playlists

    @staticmethod
    def _select_playlist(value: str, playlists: list[dict[str, Any]]) -> dict[str, Any]:
        wanted = value.strip().casefold()
        matches = [
            item for item in playlists
            if str(item["id"]) == wanted or item["name"].casefold() == wanted
        ]
        if not matches:
            raise QQMusicError(f"QQ Music playlist not found: {value}")
        if len(matches) > 1:
            raise QQMusicError("QQ Music playlist name is ambiguous; choose its numeric id")
        return matches[0]

    def _playlist_tracks(self, playlist: dict[str, Any]) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        for page in range(1, 11):
            data = self._request(
                f"/songlist/{playlist['id']}/detail",
                params={
                    "dirid": playlist.get("dirid") or 0,
                    "page": page,
                    "num": 100,
                    "onlysong": True,
                    "tag": False,
                    "userinfo": False,
                },
            )
            songs = data.get("songs") if isinstance(data, dict) else None
            if not isinstance(songs, list):
                raise QQMusicError("QQMusicApi returned invalid playlist tracks")
            tracks.extend(
                _track(song, len(tracks) + index)
                for index, song in enumerate(songs, 1)
                if isinstance(song, dict) and song.get("mid")
            )
            total = int(data.get("total") or len(tracks))
            if not data.get("hasmore") or len(tracks) >= total:
                break
        return tracks

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        value = query.strip()
        if not value:
            raise QQMusicError("QQ Music search needs a title or artist")
        data = self._request(
            "/search/search_by_type",
            params={"keyword": value, "search_type": 0, "num": limit, "page": 1},
        )
        songs = data.get("song") if isinstance(data, dict) else None
        if not isinstance(songs, list):
            raise QQMusicError("QQMusicApi search returned no song list")
        return [
            _track(song, index)
            for index, song in enumerate(songs, 1)
            if isinstance(song, dict) and song.get("mid")
        ]

    def resolve(self, value: str) -> tuple[str, dict[str, Any], str]:
        raw = value.strip()
        if raw.startswith("qqmusic:"):
            mid = raw.removeprefix("qqmusic:").strip()
            matches: list[dict[str, Any]] = []
        else:
            query = raw.removeprefix("qqmusic-search:").strip()
            matches = self.search(query, limit=5)
            if not matches:
                raise QQMusicError(f"QQ Music found no track for {query!r}")
            selected = select_search_result(query, matches)
            mid = selected["mid"]
        if not mid or not mid.replace("_", "").isalnum():
            raise QQMusicError("invalid QQ Music MID")
        selected = selected if matches else self._track_by_mid(mid)
        urls = self._request(
            f"/song/{urllib.parse.quote(mid)}/url",
            params={"media_mid": selected.get("media_mid") or None},
        )
        entries = urls.get("data") if isinstance(urls, dict) else None
        if not isinstance(entries, list) or not entries:
            raise QQMusicError("QQMusicApi returned no playback authorization")
        info = entries[0] if isinstance(entries[0], dict) else {}
        purl = str(info.get("purl") or "").strip()
        result = int(info.get("result") or 0)
        if result != 0 or not purl:
            raise QQMusicError(
                f"QQ Music cannot play this track with the current account (result={result})"
            )
        dispatch = self._request("/song/get_cdn_dispatch")
        cdns = dispatch.get("sip") if isinstance(dispatch, dict) else None
        if not isinstance(cdns, list) or not cdns:
            raise QQMusicError("QQMusicApi returned no playable CDN")
        url, selected_host = self._select_cdn(cdns, purl)
        evidence = {
            **selected,
            "uri": f"qqmusic:{mid}",
            "authorization_result": result,
            "url_expiration_seconds": int(urls.get("expiration") or 0),
            "cdn_count": len(cdns),
            "cdn_selected_host": selected_host,
        }
        return evidence["uri"], evidence, url

    def _track_by_mid(self, mid: str) -> dict[str, Any]:
        data = self._request("/song/query_song", params={"value": mid})
        tracks = data.get("tracks") if isinstance(data, dict) else None
        if not isinstance(tracks, list) or len(tracks) != 1 or not isinstance(tracks[0], dict):
            raise QQMusicError(f"QQ Music could not confirm exact MID {mid}")
        selected = _track(tracks[0], 1)
        if selected["mid"] != mid:
            raise QQMusicError("QQ Music returned a different MID during exact lookup")
        return selected

    def _select_cdn(self, cdns: list[Any], purl: str) -> tuple[str, str]:
        attempted = []
        for sweep in range(self.retries + 1):
            for base in cdns:
                url = urllib.parse.urljoin(str(base), purl)
                host = urllib.parse.urlparse(url).hostname or "unknown"
                attempted.append(host)
                request = urllib.request.Request(
                    url,
                    headers={
                        "Range": "bytes=0-0",
                        "Referer": "https://y.qq.com/",
                        "User-Agent": "Mozilla/5.0 Audience-of-One/0.1",
                    },
                )
                try:
                    with self.opener(request, timeout=self.timeout) as response:
                        status = int(getattr(response, "status", 200))
                        response.read(1)
                    if status in {200, 206}:
                        return url, host
                except (OSError, TimeoutError, urllib.error.URLError):
                    continue
            if sweep < self.retries:
                self.sleep(0.35 * (sweep + 1))
        raise QQMusicError(
            "QQ Music authorized the track, but none of its CDN candidates were readable "
            f"({', '.join(dict.fromkeys(attempted))})"
        )
