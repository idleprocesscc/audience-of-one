from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from audience_of_one.adapters.spotify import SpotifyClient, SpotifyError
from audience_of_one.adapters.spotify_resolver import SpotifyResolveError


def client() -> SpotifyClient:
    return SpotifyClient({
        "client_id_env": "SPOTIFY_CLIENT_ID",
    }, Path(tempfile.gettempdir()) / "station-spotify-test")


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.client = client()
        self.devices = [
            {"id": "desktop-id", "name": "Desktop", "is_active": True},
            {"id": "phone-id", "name": "Android Phone", "is_active": False},
        ]

    def test_default_requires_active_device(self):
        with mock.patch.object(self.client, "devices", return_value=self.devices):
            self.assertEqual(self.client.ready_device()["id"], "desktop-id")

    def test_explicit_inactive_device_is_valid_target(self):
        with mock.patch.object(self.client, "devices", return_value=self.devices):
            target = self.client.ready_device("Android")
        self.assertEqual(target["id"], "phone-id")
        self.assertFalse(target["is_active"])

    def test_ambiguous_partial_device_name_fails_closed(self):
        ambiguous = [
            {"id": "one", "name": "Desktop Player", "is_active": False},
            {"id": "two", "name": "Desktop Speaker", "is_active": False},
        ]
        with mock.patch.object(self.client, "devices", return_value=ambiguous), \
             self.assertRaisesRegex(SpotifyError, "ambiguous"):
            self.client.ready_device("Desktop")

    def test_exact_device_name_wins_over_partial_names(self):
        devices = [
            {"id": "one", "name": "Mac", "is_active": False},
            {"id": "two", "name": "MacBook Pro", "is_active": False},
        ]
        with mock.patch.object(self.client, "devices", return_value=devices):
            self.assertEqual(self.client.ready_device("Mac")["id"], "one")

    def test_duplicate_exact_device_names_require_an_id(self):
        devices = [
            {"id": "one", "name": "Desktop", "is_active": False},
            {"id": "two", "name": "Desktop", "is_active": False},
        ]
        with mock.patch.object(self.client, "devices", return_value=devices), \
             self.assertRaisesRegex(SpotifyError, "use an id"):
            self.client.ready_device("Desktop")

    def test_default_rejects_only_inactive_devices(self):
        inactive = [dict(device, is_active=False) for device in self.devices]
        with mock.patch.object(self.client, "devices", return_value=inactive), \
             self.assertRaisesRegex(SpotifyError, "no active Spotify device"):
            self.client.ready_device()

    def test_play_targets_resolved_device_and_returns_acceptance_not_playback(self):
        calls = []
        with mock.patch.object(
            self.client, "resolve", return_value=("spotify:track:resolved", {"name": "Song"})
        ), mock.patch.object(
            self.client, "ready_device", return_value=self.devices[1]
        ), mock.patch.object(
            self.client, "request", side_effect=lambda *args: calls.append(args)
        ):
            receipt = self.client.play("Song", "Android")
        self.assertTrue(receipt["accepted"])
        self.assertNotIn("confirmed", receipt)
        self.assertIn((
            "PUT", "/v1/me/player/play", {"device_id": "phone-id"},
            {"uris": ["spotify:track:resolved"]},
        ), calls)

    def test_resume_targets_device_without_replacing_its_queue(self):
        calls = []
        with mock.patch.object(
            self.client, "ready_device", return_value=self.devices[0]
        ), mock.patch.object(
            self.client, "request", side_effect=lambda *args: calls.append(args)
        ):
            receipt = self.client.resume("Desktop")
        self.assertTrue(receipt["accepted"])
        self.assertIn((
            "PUT", "/v1/me/player/play", {"device_id": "desktop-id"},
        ), calls)

    def test_scope_guard_names_reauthorization_instead_of_calling_api(self):
        with tempfile.TemporaryDirectory() as raw:
            isolated = SpotifyClient({"client_id_env": "SPOTIFY_CLIENT_ID"}, Path(raw))
            isolated._save_tokens({
                "access_token": "test", "expires_at": 9999999999,
                "scope": "user-read-playback-state",
            })
            with self.assertRaisesRegex(SpotifyError, "user-top-read.*authorize again"):
                isolated.require_scopes(
                    {"user-read-playback-state", "user-top-read"},
                    action="authorize again",
                )

    def test_resolver_failure_is_reported_through_client_contract(self):
        with mock.patch(
            "audience_of_one.adapters.spotify.spotify_resolver.resolve_uri",
            side_effect=SpotifyResolveError("no confident match"),
        ), self.assertRaisesRegex(SpotifyError, "no confident match"):
            self.client.resolve("ambiguous thing")


class PlaybackReceiptTests(unittest.TestCase):
    def setUp(self):
        self.client = client()

    @staticmethod
    def snapshot(position: int, playing: bool = True, uri: str = "spotify:track:bed",
                 device: str = "desktop-id") -> dict:
        return {
            "uri": uri,
            "progress_ms": position,
            "is_playing": playing,
            "device": {"id": device, "name": "Desktop"},
        }

    def test_wait_requires_exact_uri_device_and_playing(self):
        snapshots = [
            self.snapshot(100, uri="spotify:track:wrong"),
            self.snapshot(200, device="other"),
            self.snapshot(300, playing=False),
            self.snapshot(400),
        ]
        with mock.patch.object(self.client, "snapshot", side_effect=snapshots), \
             mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.wait_for_playback(
                "spotify:track:bed", "desktop-id", timeout=5
            )
        self.assertEqual(receipt["progress_ms"], 400)

    def test_resume_receipt_requires_playing_on_the_same_device(self):
        snapshots = [
            self.snapshot(100, playing=False),
            self.snapshot(200, device="other"),
            self.snapshot(300),
        ]
        with mock.patch.object(self.client, "snapshot", side_effect=snapshots), \
             mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.wait_for_resumed("desktop-id", timeout=2)
        self.assertTrue(receipt["is_playing"])

    def test_music_bed_confirms_position_motion(self):
        with mock.patch.object(
            self.client, "snapshot", side_effect=[self.snapshot(1000), self.snapshot(2200)]
        ), mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.music_bed_probe(1.2, 400)
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["advance_ms"], 1200)

    def test_music_bed_rejects_track_change(self):
        with mock.patch.object(
            self.client, "snapshot",
            side_effect=[self.snapshot(1000), self.snapshot(200, uri="spotify:track:new")],
        ), mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.music_bed_probe()
        self.assertFalse(receipt["confirmed"])
        self.assertEqual(receipt["reason"], "track_changed")

    def test_repeat_confirmation_requires_same_device_and_mode(self):
        snapshots = [
            {**self.snapshot(50, uri="spotify:track:other"), "repeat_state": "off"},
            {**self.snapshot(100), "repeat_state": "track"},
            {**self.snapshot(200, device="other"), "repeat_state": "off"},
            {**self.snapshot(300), "repeat_state": "off"},
        ]
        with mock.patch.object(self.client, "snapshot", side_effect=snapshots), \
             mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.wait_for_repeat(
                "off", "desktop-id", timeout=2, expected_uri="spotify:track:bed"
            )
        self.assertEqual(receipt["repeat_state"], "off")

    def test_music_bed_rejects_requested_uri_or_device_drift(self):
        with mock.patch.object(
            self.client, "snapshot",
            return_value=self.snapshot(1000, uri="spotify:track:other"),
        ):
            receipt = self.client.music_bed_probe(
                expected_uri="spotify:track:bed", expected_device_id="desktop-id"
            )
        self.assertFalse(receipt["confirmed"])
        self.assertEqual(receipt["reason"], "unexpected_track")

        with mock.patch.object(
            self.client, "snapshot",
            return_value=self.snapshot(1000, device="other"),
        ):
            receipt = self.client.music_bed_probe(
                expected_uri="spotify:track:bed", expected_device_id="desktop-id"
            )
        self.assertFalse(receipt["confirmed"])
        self.assertEqual(receipt["reason"], "unexpected_device")

    def test_pause_confirmation_requires_same_device_and_paused_state(self):
        snapshots = [
            self.snapshot(100, playing=True),
            self.snapshot(110, playing=False, device="other"),
            self.snapshot(120, playing=False),
        ]
        with mock.patch.object(self.client, "snapshot", side_effect=snapshots), \
             mock.patch("audience_of_one.adapters.spotify.time.sleep"):
            receipt = self.client.wait_for_paused("desktop-id", timeout=2)
        self.assertFalse(receipt["is_playing"])

    def test_pause_targets_preselected_device_without_resolving_again(self):
        device = {"id": "desktop-id", "name": "Desktop"}
        with mock.patch.object(self.client, "request") as request:
            receipt = self.client.pause_on_device(device)
        request.assert_called_once_with(
            "PUT", "/v1/me/player/pause", {"device_id": "desktop-id"}
        )
        self.assertTrue(receipt["accepted"])


class HTTPReceiptTests(unittest.TestCase):
    class Response:
        def __init__(self, status: int, body: bytes):
            self.status = status
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def setUp(self):
        self.client = client()

    def request_with(self, response, method="PUT"):
        with mock.patch.object(self.client, "access_token", return_value="test-token"), \
             mock.patch(
                 "audience_of_one.adapters.spotify.urllib.request.urlopen",
                 return_value=response,
             ):
            return self.client.request(method, "/v1/me/player/repeat", {"state": "off"})

    def test_204_is_success_without_body(self):
        self.assertIsNone(self.request_with(self.Response(204, b"")))

    def test_whitespace_2xx_is_success_without_body(self):
        self.assertIsNone(self.request_with(self.Response(200, b"\n")))

    def test_opaque_mutation_body_is_accepted_by_http_status(self):
        self.assertIsNone(self.request_with(self.Response(200, b"opaque-request-id")))

    def test_opaque_read_body_is_rejected(self):
        with self.assertRaisesRegex(SpotifyError, "non-JSON"):
            self.request_with(self.Response(200, b"opaque"), method="GET")

    def test_429_respects_retry_after_and_then_succeeds(self):
        rate_limit = urllib.error.HTTPError(
            "https://api.spotify.com/test", 429, "Too Many Requests",
            {"Retry-After": "2"}, io.BytesIO(b'{"error":{"message":"slow down"}}'),
        )
        success = self.Response(204, b"")
        with mock.patch.object(self.client, "access_token", return_value="test-token"), \
             mock.patch(
                 "audience_of_one.adapters.spotify.urllib.request.urlopen",
                 side_effect=[rate_limit, success],
             ) as urlopen, mock.patch(
                 "audience_of_one.adapters.spotify.time.sleep"
             ) as sleep:
            self.assertIsNone(
                self.client.request("PUT", "/v1/me/player/repeat", {"state": "off"})
            )
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
