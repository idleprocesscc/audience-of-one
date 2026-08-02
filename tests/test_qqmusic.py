from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from audience_of_one.adapters.qqmusic import (
    QQMusicClient,
    QQMusicError,
    select_search_result,
)


class Response:
    def __init__(self, payload: dict, *, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        value = json.dumps(self.payload).encode()
        return value if size < 0 else value[:size]


class QQMusicTests(unittest.TestCase):
    def test_qr_login_saves_private_credential_and_sends_it_only_as_cookie(self):
        qr_bytes = b"fake-png"
        requests = []

        def opener(request, timeout):
            self.assertGreater(timeout, 0)
            requests.append(request)
            if "/login/qrcode/qq/status" in request.full_url:
                return Response({"code": 0, "msg": "ok", "data": {
                    "event": 0,
                    "done": True,
                    "credential": {
                        "musicid": 123456789,
                        "musickey": "test-music-key",
                        "encryptUin": "encrypted-user",
                    },
                }})
            return Response({"code": 0, "msg": "ok", "data": {
                "identifier": "qr-id",
                "mimetype": "image/png",
                "data": base64.b64encode(qr_bytes).decode(),
            }})

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "secrets" / "qqmusic-credential.json"
            client = QQMusicClient({"retries": 0}, credential_path=path, opener=opener)
            started = client.begin_login("qq")
            self.assertEqual(started["image"], qr_bytes)
            status = client.login_status(started["identifier"], "qq")
            receipt = client.save_credential(status["credential"])
            self.assertTrue(receipt["saved"])
            self.assertTrue(receipt["has_encrypt_uin"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reloaded = QQMusicClient({"retries": 0}, credential_path=path, opener=opener)
            self.assertTrue(reloaded.credential)
            self.assertIn("musicid=123456789", reloaded._cookie())
            self.assertIn("musickey=test-music-key", reloaded._cookie())
            self.assertNotIn("musickey", json.dumps(receipt))
        self.assertIsNone(requests[0].get_header("Cookie"))

    def test_private_shelf_combines_likes_and_playlist_summaries(self):
        def song(mid, title, artist):
            return {
                "mid": mid, "title": title, "interval": 180,
                "singer": [{"name": artist}], "album": {"name": "Album"},
                "file": {"media_mid": mid},
            }

        def opener(request, timeout):
            self.assertGreater(timeout, 0)
            self.assertIn("musicid=123456789", request.get_header("Cookie") or "")
            url = request.full_url
            if "/fav/songs" in url:
                data = {"songs": [song("LIKE1", "Liked Song", "Artist")],
                        "total": 1, "hasmore": 0}
            elif "/created_songlists" in url:
                data = {"playlists": [{
                    "id": 10, "dirid": 20, "title": "Morning", "songnum": 1,
                }]}
            elif "/fav/songlists" in url:
                data = {"playlists": [{
                    "id": 30, "dirid": 0, "title": "Night", "songnum": 2,
                }], "total": 1, "hasmore": 0}
            elif "/songlist/10/detail" in url:
                data = {"songs": [song("PLAY1", "Playlist Song", "Host")],
                        "total": 1, "hasmore": 0}
            else:
                raise AssertionError(url)
            return Response({"code": 0, "msg": "ok", "data": data})

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "credential.json"
            path.write_text(json.dumps({
                "musicid": 123456789,
                "musickey": "test-music-key",
                "encrypt_uin": "encrypted-user",
            }))
            path.chmod(0o600)
            client = QQMusicClient({"retries": 0}, credential_path=path, opener=opener)
            shelf = client.private_shelf(playlist="Morning")
        self.assertEqual(shelf["sections"]["my_favorites"][0]["uri"], "qqmusic:LIKE1")
        self.assertEqual(shelf["sections"]["created_playlists"][0]["name"], "Morning")
        self.assertEqual(shelf["sections"]["collected_playlists"][0]["name"], "Night")
        self.assertEqual(shelf["selected_playlist"]["tracks"][0]["uri"], "qqmusic:PLAY1")
        self.assertNotIn("test-music-key", json.dumps(shelf))

    def test_search_selection_requires_unique_title_and_artist_evidence(self):
        tracks = [
            {"mid": "A", "name": "Something Stupid", "artists": ["Lola Marsh"]},
            {"mid": "B", "name": "Something Stupid", "artists": ["Other Artist"]},
        ]
        self.assertEqual(
            select_search_result("Something Stupid Lola Marsh", tracks)["mid"], "A"
        )
        with self.assertRaisesRegex(QQMusicError, "several versions"):
            select_search_result("Something Stupid", tracks)
        with self.assertRaisesRegex(QQMusicError, "did not prove"):
            select_search_result("Different Song Lola Marsh", tracks)

    def test_search_and_resolve_keep_the_expiring_url_out_of_evidence(self):
        def opener(request, timeout):
            self.assertEqual(timeout, 3)
            if request.full_url.startswith("https://cdn.example/"):
                return Response({}, status=206)
            if request.full_url.endswith("/song/get_cdn_dispatch"):
                return Response({"code": 0, "msg": "ok", "data": {
                    "sip": ["https://cdn.example/"],
                }})
            if "/song/MID123/url" in request.full_url:
                return Response({"code": 0, "msg": "ok", "data": {
                    "expiration": 7200,
                    "data": [{
                        "mid": "MID123", "result": 0,
                        "purl": "song.mp3?ticket=test-only",
                    }],
                }})
            return Response({"code": 0, "msg": "ok", "data": {
                "song": [{
                    "mid": "MID123", "title": "Something Stupid", "interval": 262,
                    "singer": [{"name": "Lola Marsh"}],
                    "album": {"name": "Better Call Saul"},
                    "file": {"media_mid": "MEDIA123"},
                }],
            }})

        client = QQMusicClient(
            {"base_url": "http://127.0.0.1:8080", "timeout_seconds": 3, "retries": 0},
            opener=opener,
        )
        tracks = client.search("Something Stupid Lola Marsh")
        self.assertEqual(tracks[0]["uri"], "qqmusic:MID123")
        uri, evidence, url = client.resolve("qqmusic-search:Something Stupid Lola Marsh")
        self.assertEqual(uri, "qqmusic:MID123")
        self.assertEqual(url, "https://cdn.example/song.mp3?ticket=test-only")
        self.assertNotIn("test-only", json.dumps(evidence))
        self.assertEqual(evidence["url_expiration_seconds"], 7200)
        self.assertEqual(evidence["cdn_selected_host"], "cdn.example")

    def test_transient_network_errors_receive_a_bounded_retry(self):
        attempts = 0

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise urllib.error.URLError("temporary")
            return Response({"code": 0, "msg": "ok", "data": {"song": []}})

        client = QQMusicClient(
            {"base_url": "http://127.0.0.1:8080", "retries": 2},
            opener=opener, sleep=lambda _seconds: None,
        )
        self.assertEqual(client.search("test"), [])
        self.assertEqual(attempts, 3)

    def test_transient_cdn_probe_reuses_the_same_bounded_retry_budget(self):
        attempts = 0
        sleeps = []

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.URLError("temporary CDN route")
            return Response({}, status=206)

        client = QQMusicClient(
            {"retries": 1}, opener=opener, sleep=sleeps.append,
        )
        url, host = client._select_cdn(
            ["https://cdn.example/"], "record.mp3?ticket=one",
        )
        self.assertEqual(url, "https://cdn.example/record.mp3?ticket=one")
        self.assertEqual(host, "cdn.example")
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [0.35])

    def test_no_playback_authorization_fails_closed(self):
        calls = 0

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            nonlocal calls
            calls += 1
            if calls == 1:
                return Response({"code": 0, "msg": "ok", "data": {"tracks": [{
                    "mid": "MID123", "title": "Protected Song", "interval": 200,
                    "singer": [{"name": "Artist"}], "album": {"name": "Album"},
                    "file": {"media_mid": "MEDIA123"},
                }]}})
            if calls == 2:
                return Response({"code": 0, "msg": "ok", "data": {
                    "expiration": 0,
                    "data": [{"mid": "MID123", "result": 104003, "purl": ""}],
                }})
            raise AssertionError("CDN should not be requested")

        client = QQMusicClient({"retries": 0}, opener=opener)
        with self.assertRaisesRegex(QQMusicError, "current account"):
            client.resolve("qqmusic:MID123")


if __name__ == "__main__":
    unittest.main()
