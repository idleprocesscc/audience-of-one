from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one.adapters.spotify import SpotifyError
from audience_of_one.adapters.spotify_local import (
    CURRENT_SESSION_ID,
    LOCAL_MAC_ID,
    MacOSSpotifyClient,
)


def client(config: dict | None = None, **kwargs) -> MacOSSpotifyClient:
    kwargs.setdefault("sleep", lambda _seconds: None)
    return MacOSSpotifyClient(
        {"adapter": "macos_applescript", **(config or {})},
        Path(tempfile.gettempdir()) / "station-local-test",
        **kwargs,
    )


class ResolutionTests(unittest.TestCase):
    def test_exact_uri_and_public_url_resolve_without_oauth(self):
        local = client()
        uri, receipt = local.resolve("spotify:track:ABC123")
        self.assertEqual(uri, "spotify:track:ABC123")
        self.assertEqual(receipt["source"], "spotify_uri")

        uri, receipt = local.resolve(
            "https://open.spotify.com/track/XYZ789?si=discarded"
        )
        self.assertEqual(uri, "spotify:track:XYZ789")
        self.assertEqual(receipt["source"], "open_spotify_url")

    def test_title_text_fails_closed_until_an_agent_resolves_it(self):
        with self.assertRaisesRegex(SpotifyError, "resolve title and artist first"):
            client().resolve("Space Song Beach House")


class SessionTests(unittest.TestCase):
    def test_current_session_does_not_claim_a_connect_target(self):
        device = client().ready_device("current", require_snapshot=False)
        self.assertEqual(device["id"], CURRENT_SESSION_ID)
        self.assertEqual(device["type"], "Unknown")
        self.assertFalse(device["selectable"])
        self.assertEqual(device["routing_proof"], "not_available_in_applescript")

    def test_local_mac_is_an_explicit_operator_assertion(self):
        local = client()
        with mock.patch.object(local, "_local_computer_name", return_value="Studio Mac"):
            device = local.ready_device(LOCAL_MAC_ID, require_snapshot=False)
        self.assertEqual(device["name"], "Studio Mac")
        self.assertEqual(device["type"], "Computer")
        self.assertEqual(device["routing_proof"], "operator_asserted_local_mac")

    def test_named_connect_device_is_rejected(self):
        with self.assertRaisesRegex(SpotifyError, "cannot select a named Connect device"):
            client().ready_device("My phone", require_snapshot=False)

    def test_snapshot_is_machine_readable_and_marks_unknown_routing(self):
        local = client({"device": "current"})
        payload = {
            "running": True,
            "state": "playing",
            "is_playing": True,
            "progress_ms": 1200,
            "repeat_state": "off",
            "uri": "spotify:track:ABC123",
            "name": "A Song",
            "artists": ["An Artist"],
            "duration_ms": 200000,
            "volume_percent": 70,
        }
        with mock.patch.object(local, "_jxa", return_value=json.dumps(payload)):
            receipt = local.snapshot()
        self.assertEqual(receipt["uri"], "spotify:track:ABC123")
        self.assertEqual(receipt["device"]["id"], CURRENT_SESSION_ID)

    def test_snapshot_preserves_playing_interstitial_without_inventing_a_track(self):
        local = client({"device": "current"})
        payload = {
            "running": True,
            "state": "playing",
            "is_playing": True,
            "content_type": "interstitial",
            "repeat_state": "off",
            "volume_percent": 70,
        }
        with mock.patch.object(local, "_jxa", return_value=json.dumps(payload)):
            receipt = local.snapshot()
        self.assertEqual(receipt["content_type"], "interstitial")
        self.assertNotIn("uri", receipt)

    def test_explicit_session_identity_stays_bound_during_playback_receipts(self):
        local = client({"device": "local_mac"})
        with mock.patch.object(local, "_jxa", return_value='{"running": true}'), \
             mock.patch.object(local, "_local_computer_name", return_value="Studio Mac"):
            selected = local.ready_device("current")
        self.assertEqual(selected["id"], CURRENT_SESSION_ID)

        payload = {
            "running": True,
            "state": "playing",
            "is_playing": True,
            "progress_ms": 1200,
            "repeat_state": "off",
            "uri": "spotify:track:ABC123",
            "name": "A Song",
            "artists": ["An Artist"],
            "duration_ms": 200000,
            "volume_percent": 70,
        }
        with mock.patch.object(local, "_jxa", return_value=json.dumps(payload)):
            receipt = local.snapshot()
        self.assertEqual(receipt["device"]["id"], CURRENT_SESSION_ID)

    def test_play_uses_exact_uri_as_a_script_argument(self):
        local = client()
        device = local.ready_device("current", require_snapshot=False)
        with mock.patch.object(local, "_jxa", return_value="accepted") as run:
            receipt = local.play_uri("spotify:track:ABC123", device)
        self.assertEqual(run.call_args.args[-1], "spotify:track:ABC123")
        self.assertEqual(receipt["transport"], "macos_applescript")

    def test_resume_uses_the_current_spotify_session(self):
        local = client()
        device = {"id": CURRENT_SESSION_ID, "name": "Spotify current session"}
        with mock.patch.object(local, "ready_device", return_value=device), \
             mock.patch.object(local, "_jxa", return_value="accepted") as run:
            receipt = local.resume("current")
        self.assertTrue(receipt["accepted"])
        self.assertIn("s.play()", run.call_args.args[0])


class StablePlaybackTests(unittest.TestCase):
    @staticmethod
    def snapshot(uri: str, position: int) -> dict:
        return {
            "uri": uri,
            "progress_ms": position,
            "is_playing": True,
            "device": {"id": CURRENT_SESSION_ID},
        }

    def test_stability_window_rejects_a_track_replaced_after_initial_success(self):
        local = client({"stability_seconds": 8})
        with mock.patch.object(
            local,
            "snapshot",
            side_effect=[
                self.snapshot("spotify:track:wanted", 1000),
                self.snapshot("spotify:track:replacement", 7000),
            ],
        ):
            receipt = local.music_bed_probe(
                expected_uri="spotify:track:wanted",
                expected_device_id=CURRENT_SESSION_ID,
            )
        self.assertFalse(receipt["confirmed"])
        self.assertEqual(receipt["reason"], "unexpected_track")
        self.assertEqual(receipt["window_ms"], 8000)

    def test_stability_window_confirms_identity_and_position_motion(self):
        local = client({"stability_seconds": 8})
        with mock.patch.object(
            local,
            "snapshot",
            side_effect=[
                self.snapshot("spotify:track:wanted", 1000),
                self.snapshot("spotify:track:wanted", 9000),
            ],
        ):
            receipt = local.music_bed_probe(
                expected_uri="spotify:track:wanted",
                expected_device_id=CURRENT_SESSION_ID,
            )
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["advance_ms"], 8000)
        self.assertEqual(receipt["identity_match"], "exact_uri")

    def test_ad_interstitial_extends_identity_wait_and_is_recorded(self):
        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        local = client(
            {"ad_wait_seconds": 120},
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        interstitial = {
            "content_type": "interstitial",
            "is_playing": True,
            "device": {"id": CURRENT_SESSION_ID},
        }
        wanted = self.snapshot("spotify:track:wanted", 1000)
        with mock.patch.object(local, "snapshot", side_effect=[interstitial, wanted]):
            receipt = local.wait_for_playback(
                "spotify:track:wanted", CURRENT_SESSION_ID, timeout=0.2
            )
        self.assertTrue(receipt["interstitial_observed"])

    def test_bed_probe_names_interstitial_as_ad_break(self):
        local = client({"stability_seconds": 8})
        interstitial = {
            "content_type": "interstitial",
            "is_playing": True,
            "device": {"id": CURRENT_SESSION_ID},
        }
        with mock.patch.object(
            local, "snapshot",
            side_effect=[self.snapshot("spotify:track:wanted", 1000), interstitial],
        ):
            receipt = local.music_bed_probe(
                expected_uri="spotify:track:wanted",
                expected_device_id=CURRENT_SESSION_ID,
            )
        self.assertFalse(receipt["confirmed"])
        self.assertEqual(receipt["reason"], "ad_break")


if __name__ == "__main__":
    unittest.main()
