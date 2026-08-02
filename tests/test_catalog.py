from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audience_of_one import catalog


def track(uri: str, name: str, artist: str, duration: int = 200_000) -> dict:
    return {
        "type": "track", "uri": uri, "name": name,
        "artists": [{"name": artist}], "duration_ms": duration,
        "album": {"name": "An Album"}, "is_playable": True,
    }


class FakeSpotify:
    def __init__(self):
        self.required = None

    def require_scopes(self, scopes, *, action):
        self.required = (scopes, action)

    def request(self, method, path, params=None):
        self.assert_get(method)
        if path == "/v1/me/playlists":
            return {
                "total": 2,
                "items": [
                    {
                        "id": "library", "name": "Free Plan",
                        "uri": "spotify:playlist:library", "tracks": {"total": 2},
                    },
                    {
                        "id": "weekly", "name": "Discover Weekly",
                        "uri": "spotify:playlist:weekly", "tracks": {"total": 1},
                    },
                ],
            }
        if path == "/v1/playlists/library/items":
            return {
                "total": 2,
                "items": [
                    {"track": track(
                        "spotify:track:playlist-version", "Love My Way",
                        "The Psychedelic Furs", 213_200,
                    )},
                    {"track": track("spotify:track:recent", "Quiet", "One Artist")},
                ],
            }
        if path == "/v1/playlists/weekly/items":
            return {
                "total": 1,
                "items": [{"track": track(
                    "spotify:track:weekly", "A New Record", "New Artist"
                )}],
            }
        if path == "/v1/me/top/tracks":
            return {"items": [track(
                "spotify:track:top-version", "Love My Way",
                "The Psychedelic Furs", 213_900,
            )]}
        if path == "/v1/me/player/recently-played":
            recent = track("spotify:track:recent", "Quiet", "One Artist")
            return {"items": [
                {"track": recent, "played_at": "2026-08-01T01:00:00Z"},
                {"track": recent, "played_at": "2026-08-01T00:00:00Z"},
            ]}
        raise AssertionError((method, path, params))

    @staticmethod
    def assert_get(method):
        if method != "GET":
            raise AssertionError(method)


class SpotifyShelfTests(unittest.TestCase):
    def test_primary_selection_never_guesses_across_multiple_ordinary_shelves(self):
        playlists = [
            {"id": "one", "name": "One"},
            {"id": "weekly", "name": "Discover Weekly"},
        ]
        self.assertEqual(catalog.select_playlist(playlists)["id"], "one")
        playlists.append({"id": "two", "name": "Two"})
        self.assertIsNone(catalog.select_playlist(playlists))
        self.assertEqual(catalog.select_playlist(playlists, "Two")["id"], "two")

    def test_shelf_combines_affinity_recent_footprints_and_weekly(self):
        client = FakeSpotify()
        payload = catalog.spotify_shelf(client, {"adapter": "web_api"})
        sections = payload["sections"]
        preferred = sections["long_term_preference"][0]
        self.assertEqual(preferred["name"], "Love My Way")
        self.assertEqual(preferred["long_term_match"], "identity")
        recent = sections["unranked_playlist_order"][0]
        self.assertEqual(recent["recent_positions"], [1, 2])
        self.assertEqual(sections["spotify_weekly"][0]["name"], "A New Record")
        self.assertFalse(payload["playback_touched"])
        self.assertFalse(payload["rundown_touched"])
        self.assertEqual(client.required[0], catalog.SPOTIFY_LIST_SCOPES)

    def test_unavailable_weekly_does_not_erase_the_primary_shelf(self):
        class WeeklyDenied(FakeSpotify):
            def request(self, method, path, params=None):
                if path == "/v1/playlists/weekly/items":
                    from audience_of_one.adapters.spotify import SpotifyError
                    raise SpotifyError("Spotify API error 403: forbidden")
                return super().request(method, path, params)

        payload = catalog.spotify_shelf(WeeklyDenied(), {"adapter": "web_api"})
        self.assertEqual(payload["playlist"]["id"], "library")
        self.assertTrue(payload["sections"]["long_term_preference"])
        self.assertEqual(payload["sections"]["spotify_weekly"], [])
        self.assertIn("403", payload["weekly_error"])


class LocalShelfTests(unittest.TestCase):
    def test_local_shelf_reads_embedded_metadata_without_starting_mpv(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            song = root / "Artist" / "Song.flac"
            song.parent.mkdir()
            song.write_bytes(b"audio")
            probe = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"format": {
                    "duration": "183.5",
                    "tags": {"TITLE": "A Song", "ARTIST": "An Artist", "ALBUM": "A Record"},
                }}),
            )
            with mock.patch("audience_of_one.catalog.shutil.which", return_value="ffprobe"), \
                 mock.patch("audience_of_one.catalog.subprocess.run", return_value=probe):
                payload = catalog.local_shelf({"library_root": str(root)})
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["tracks"][0]["name"], "A Song")
            self.assertEqual(payload["tracks"][0]["artists"], ["An Artist"])
            self.assertEqual(payload["tracks"][0]["uri"], "local:Artist/Song.flac")
            self.assertEqual(payload["tracks"][0]["duration_ms"], 183_500)
            self.assertFalse(payload["playback_touched"])

    def test_local_shelf_falls_back_to_filename_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Bare Name.mp3").write_bytes(b"audio")
            with mock.patch("audience_of_one.catalog.shutil.which", return_value=None):
                payload = catalog.local_shelf({"library_root": str(root)})
            self.assertEqual(payload["tracks"][0]["name"], "Bare Name")
            self.assertEqual(payload["metadata_probe"], "filename_only")

    def test_local_shelf_does_not_publish_symlinks_outside_the_record_box(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "records"
            root.mkdir()
            outside = base / "private.mp3"
            outside.write_bytes(b"audio")
            (root / "escape.mp3").symlink_to(outside)
            with mock.patch("audience_of_one.catalog.shutil.which", return_value=None):
                payload = catalog.local_shelf({"library_root": str(root)})
            self.assertEqual(payload["tracks"], [])


if __name__ == "__main__":
    unittest.main()
