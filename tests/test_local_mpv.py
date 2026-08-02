from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one.adapters.local_mpv import LocalMPVClient
from audience_of_one.adapters.spotify import SpotifyError


class FakeMPV(LocalMPVClient):
    def __init__(self, root: Path, state: Path):
        super().__init__(
            {"library_root": str(root), "stability_seconds": 0}, state,
            executable="/fake/mpv", sleep=lambda _seconds: None,
        )
        self.properties = {
            "path": None,
            "pause": True,
            "time-pos": 0.0,
            "duration": 120.0,
            "media-title": "Test Record",
            "metadata": {"ARTIST": "Test Artist", "album": "Test Album"},
            "loop-file": "no",
            "volume": 70,
        }

    def _ensure_process(self):
        return None

    def _command(self, *command):
        if command[0] == "get_property":
            return self.properties.get(command[1])
        if command[0] == "set_property":
            self.properties[command[1]] = command[2]
            return None
        if command[0] == "loadfile":
            self.properties["path"] = command[1]
            self.properties["time-pos"] = 0.1
            return None
        raise AssertionError(command)

    @staticmethod
    def _computer_name():
        return "Test Mac"


class LocalMPVTests(unittest.TestCase):
    def test_ipc_command_ignores_async_events_before_its_response(self):
        class Connection:
            def __init__(self):
                self.chunks = [
                    (
                        b'{"event":"start-file"}\n'
                        b'{"request_id":1,"error":"success","data":null}\n'
                    ),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            client = FakeMPV(root, Path(raw) / "state")
            client._command = LocalMPVClient._command.__get__(client, LocalMPVClient)
            with mock.patch.object(client, "_ensure_process"), \
                 mock.patch("audience_of_one.adapters.local_mpv.socket.socket",
                            return_value=Connection()):
                self.assertIsNone(client._command("set_property", "pause", False))

    def test_ipc_identity_is_stable_across_process_environments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            state = Path(raw) / "state"
            root.mkdir()
            client = FakeMPV(root, state)
            self.assertEqual(client.socket_path, state / "local-mpv.sock")

    def test_resolve_accepts_only_existing_audio_below_library(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            (root / "A Song.flac").write_bytes(b"audio")
            client = FakeMPV(root, Path(raw) / "state")
            uri, receipt = client.resolve("A Song.flac")
            self.assertEqual(uri, "local:A Song.flac")
            self.assertEqual(receipt["source"], "local_library")
            with self.assertRaisesRegex(SpotifyError, "stay below"):
                client.resolve("local:../outside.mp3")
            with self.assertRaisesRegex(SpotifyError, "does not exist"):
                client.resolve("missing.mp3")

    def test_play_snapshot_repeat_pause_and_fader_share_one_receipt_surface(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            song = root / "A Song.flac"
            song.write_bytes(b"audio")
            client = FakeMPV(root, Path(raw) / "state")
            device = client.ready_device("local_mac")
            accepted = client.play_uri("local:A Song.flac", device)
            self.assertTrue(accepted["accepted"])
            snapshot = client.wait_for_playback(
                "local:A Song.flac", "local_mac", timeout=1
            )
            self.assertTrue(snapshot["is_playing"])
            self.assertEqual(snapshot["artists"], ["Test Artist"])
            self.assertEqual(snapshot["album"], "Test Album")
            client.set_repeat_on_device("context", device)
            self.assertEqual(
                client.wait_for_repeat(
                    "context", "local_mac", expected_uri="local:A Song.flac"
                )["repeat_state"],
                "context",
            )
            receipt = client.fader().fade(35, 0)
            self.assertEqual(receipt["to_percent"], 35)
            client.pause("local_mac")
            self.assertFalse(client.wait_for_paused("local_mac")["is_playing"])
            resumed = client.resume("local_mac")
            self.assertEqual(resumed["uri"], "local:A Song.flac")
            self.assertTrue(client.wait_for_resumed("local_mac")["is_playing"])

    def test_resume_refuses_to_invent_a_record(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            client = FakeMPV(root, Path(raw) / "state")
            with self.assertRaisesRegex(SpotifyError, "no record"):
                client.resume("local_mac")

    def test_boolean_false_from_mpv_means_repeat_off(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            song = root / "A Song.flac"
            song.write_bytes(b"audio")
            client = FakeMPV(root, Path(raw) / "state")
            client.properties["path"] = str(song)
            client.properties["pause"] = False
            client.properties["loop-file"] = False

            snapshot = client.snapshot()

            self.assertEqual(snapshot["repeat_state"], "off")

    def test_playback_waits_until_local_media_probe_is_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            song = root / "A Song.flac"
            song.write_bytes(b"audio")
            client = FakeMPV(root, Path(raw) / "state")
            device = client.ready_device("local_mac")
            client.play_uri("local:A Song.flac", device)
            original_command = client._command
            duration_reads = 0

            def delayed_duration(*command):
                nonlocal duration_reads
                if command == ("get_property", "duration"):
                    duration_reads += 1
                    return 0 if duration_reads == 1 else 120
                return original_command(*command)

            client._command = delayed_duration
            snapshot = client.wait_for_playback(
                "local:A Song.flac", "local_mac", timeout=1
            )

            self.assertGreaterEqual(duration_reads, 2)
            self.assertEqual(snapshot["duration_ms"], 120_000)
            self.assertGreater(snapshot["progress_ms"], 0)

    def test_qqmusic_stream_uses_mpv_and_persists_only_a_url_hash(self):
        class Resolver:
            def resolve(self, value):
                self.value = value
                return (
                    "qqmusic:MID123",
                    {"name": "Something Stupid", "artists": ["Lola Marsh"],
                     "album": "Better Call Saul"},
                    "https://cdn.example/song.mp3?ticket=test-only",
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            state = Path(raw) / "state"
            root.mkdir()
            client = FakeMPV(root, state)
            client.stream_resolver = Resolver()
            uri, evidence = client.resolve("qqmusic-search:Something Stupid Lola Marsh")
            self.assertEqual(uri, "qqmusic:MID123")
            self.assertEqual(evidence["artists"], ["Lola Marsh"])
            phone = client.phone_stream(uri)
            self.assertEqual(phone["uri"], uri)
            self.assertIn("ticket=test-only", phone["url"])
            device = client.ready_device("local_mac")
            receipt = client.play_uri(uri, device)
            self.assertEqual(receipt["transport"], "qqmusic_api_to_mpv")
            self.assertEqual(client.snapshot()["uri"], uri)
            stored = client.remote_state_path.read_text()
            self.assertNotIn("test-only", stored)
            self.assertNotIn("cdn.example", stored)
            self.assertIn("Something Stupid", stored)

    def test_remote_stream_restarts_one_degraded_mpv_instance(self):
        class Resolver:
            def resolve(self, _value):
                return (
                    "qqmusic:MID123",
                    {"name": "Record", "artists": ["Artist"], "album": "Album"},
                    "https://cdn.example/song.mp3?ticket=test-only",
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            client = FakeMPV(root, Path(raw) / "state")
            client.stream_resolver = Resolver()
            loads = 0
            quits = 0
            original_command = client._command

            def degraded_once(*command):
                nonlocal loads
                if command[0] == "loadfile":
                    loads += 1
                    if loads == 1:
                        return None
                return original_command(*command)

            def fake_quit():
                nonlocal quits
                quits += 1
                client.properties["path"] = None
                client.remote_state_path.unlink(missing_ok=True)
                return {"accepted": True, "stopped": True, "already_stopped": False}

            client._command = degraded_once
            client.quit = fake_quit
            uri, _ = client.resolve("qqmusic:MID123")
            receipt = client.play_uri(uri, client.ready_device("local_mac"))
            self.assertEqual(loads, 2)
            self.assertEqual(quits, 1)
            self.assertTrue(receipt["mpv_restarted"])


if __name__ == "__main__":
    unittest.main()
