from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import rundown, wildcards
from audience_of_one.adapters.phone import PhoneUncertainError
from audience_of_one.adapters.spotify import SpotifyError
from audience_of_one.desktop import DesktopEngine, MacSpotifyFader, PlayoutError
from audience_of_one.transactions import Journal


def config() -> dict:
    return {
        "station": {"mode": "desktop"},
        "spotify": {"adapter": "web_api", "device": "Desktop"},
        "desktop": {
            "duck_percent": 35,
            "fade_down_seconds": 0.8,
            "fade_up_seconds": 1.0,
            "intro_delay_seconds": 0,
            "blackout_gap_seconds": 0,
        },
        "tts": {"chinese": "fake", "english": "fake", "providers": {"fake": {}}},
    }


class FakeSpotify:
    def __init__(self, events: list[str], *, fail_play: bool = False):
        self.events = events
        self.fail_play = fail_play
        self.device = {
            "id": "desktop-id", "name": "Desktop", "type": "Computer", "is_active": False,
        }

    def ready_device(self, spec=None):
        self.events.append(f"device:{spec}")
        return self.device

    def resolve(self, value):
        self.events.append(f"resolve:{value}")
        return "spotify:track:resolved", {"name": "Resolved"}

    def play_uri(self, uri, device):
        self.events.append("play")
        if self.fail_play:
            raise PlayoutError("injected track failure")
        return {"accepted": True, "uri": uri, "device": device}

    def wait_for_playback(self, uri, device_id, timeout):
        self.events.append("track-confirmed")
        return {
            "uri": uri, "is_playing": True, "progress_ms": 50,
            "device": {"id": device_id, "name": "Desktop"},
        }

    def set_repeat_on_device(self, mode, device):
        self.events.append(f"repeat:{mode}")
        return {"accepted": True, "mode": mode, "device": device}

    def wait_for_repeat(self, mode, device_id, timeout, expected_uri=None):
        self.events.append("repeat-confirmed")
        return {
            "repeat_state": mode, "uri": expected_uri, "device": {"id": device_id},
        }

    def music_bed_probe(self, window, min_advance_ms,
                        expected_uri=None, expected_device_id=None):
        self.events.append("bed-confirmed")
        return {
            "confirmed": True, "proof": "position_motion", "advance_ms": 1200,
            "uri": expected_uri, "device": {"id": expected_device_id},
        }


class ForbiddenSpotify:
    def __getattr__(self, name):
        raise AssertionError(f"voice-only playout touched Spotify.{name}")


class FakeVoice:
    def __init__(self, events: list[str], *, fail: bool = False):
        self.events = events
        self.fail = fail
        self.plays = 0

    def play(self, path, duration):
        self.plays += 1
        self.events.append(f"voice:{Path(path).name}")
        if self.fail:
            raise PlayoutError("injected voice failure")
        return {
            "player": "fake", "pid": 7, "started_at": 10.0,
            "finished_at": 11.0, "duration_seconds": duration,
        }


class FakeFader:
    def __init__(self, events: list[str]):
        self.events = events
        self.current = 70

    def volume(self):
        self.events.append("volume")
        return self.current

    @staticmethod
    def local_computer_name():
        return "Desktop"

    def fade(self, target, seconds):
        start = self.current
        self.current = target
        self.events.append(f"fade:{target}:{seconds}")
        return {"from_percent": start, "to_percent": target, "steps": [target]}


class DesktopPlayoutTests(unittest.TestCase):
    def engine(
        self, state: Path, events: list[str], *, spotify=None, voice=None,
        phone=None, phone_music=None, fader=None,
    ):
        def synthesize(data, text, output):
            events.append("tts")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fake mp3")
            return "fake"

        return DesktopEngine(
            config(), state,
            spotify=spotify or FakeSpotify(events),
            synthesize=synthesize,
            verify_audio=lambda path: 2.5,
            voice_player=voice or FakeVoice(events),
            phone_player=phone,
            phone_music=phone_music,
            fader=fader or FakeFader(events),
            sleep=lambda seconds: events.append(f"sleep:{seconds}"),
        )

    def test_overlap_prepares_before_play_and_receipts_before_ducking(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(
                state, track="Song by Artist", say="This one is for you.",
                transition="overlap", device="Desktop",
            )
            result = self.engine(state, events).execute(item)
            self.assertEqual(result["state"], "played")
            self.assertLess(events.index("device:Desktop"), events.index("tts"))
            self.assertLess(events.index("tts"), events.index("play"))
            self.assertLess(events.index("bed-confirmed"), events.index("fade:35:0.8"))
            self.assertLess(events.index("fade:35:0.8"), events.index(
                f"voice:{item['id']}.mp3"
            ))
            self.assertEqual(events[-1], "fade:70:1.0")
            self.assertTrue(result["track"]["confirmed"])
            self.assertTrue(result["track"]["music_bed"]["confirmed"])
            self.assertTrue(result["voice"]["played"])
            self.assertFalse((state / "queue" / item["filename"]).exists())
            self.assertTrue((state / "played" / item["filename"]).exists())

    def test_repeat_is_normalized_after_cold_target_is_activated(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, track="spotify:track:one")
            self.engine(state, events).execute(item)
            self.assertLess(events.index("track-confirmed"), events.index("repeat:off"))
            self.assertLess(events.index("repeat:off"), events.index("repeat-confirmed"))

    def test_clean_failure_after_spoken_announcement_plays_one_recovery_cover(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            (state / "covers").mkdir()
            (state / "covers" / "cover-1.mp3").write_bytes(b"cover")
            spotify = FakeSpotify(events, fail_play=True)
            voice = FakeVoice(events)
            item = rundown.append(
                state, track="Song", say="Here comes Song.", transition="clean"
            )
            with self.assertRaisesRegex(PlayoutError, "injected track failure"):
                self.engine(state, events, spotify=spotify, voice=voice).execute(item)
            transaction = Journal(state).load(item["filename"])
            self.assertEqual(transaction["state"], "failed")
            self.assertEqual(voice.plays, 2)
            self.assertTrue(transaction["recovery"]["played"])
            self.assertTrue((state / "queue" / item["filename"]).exists())

    def test_legacy_dark_queue_item_executes_as_blackout(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(
                state, track="Song", say="An isolated line.", transition="blackout"
            )
            queue_path = state / "queue" / item["filename"]
            legacy = json.loads(queue_path.read_text())
            legacy["transition"] = "dark"
            queue_path.write_text(json.dumps(legacy))
            result = self.engine(state, events).execute(rundown.get(state, item["id"]))
            self.assertEqual(result["state"], "played")
            self.assertLess(events.index(f"voice:{item['id']}.mp3"), events.index("play"))

    def test_voice_failure_still_restores_spotify_volume(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            fader = FakeFader(events)
            item = rundown.append(
                state, track="Song", say="Line", transition="overlap"
            )
            with self.assertRaisesRegex(PlayoutError, "injected voice failure"):
                self.engine(
                    state, events, voice=FakeVoice(events, fail=True), fader=fader
                ).execute(item)
            self.assertEqual(fader.current, 70)
            self.assertIn("fade:35:0.8", events)
            self.assertEqual(events[-1], "fade:70:1.0")

    def test_voice_only_never_requires_spotify(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, say="A station identification.")
            result = self.engine(state, events, spotify=ForbiddenSpotify()).execute(item)
            self.assertEqual(result["state"], "played")
            self.assertTrue(result["voice"]["played"])

    def test_tail_is_rejected_without_consuming_item(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, track="Song", transition="tail")
            with self.assertRaisesRegex(PlayoutError, "running rundown boundary"):
                self.engine(state, events).execute(item)
            self.assertEqual(Journal(state).load(item["filename"])["state"], "failed")
            self.assertTrue((state / "queue" / item["filename"]).exists())

    def test_direct_opener_rejects_playlist_uri_before_tts_or_play(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            spotify = FakeSpotify(events)
            spotify.resolve = lambda value: ("spotify:playlist:one", None)
            item = rundown.append(state, track="spotify:playlist:one", say="A line")
            with self.assertRaisesRegex(PlayoutError, "needs one exact track"):
                self.engine(state, events, spotify=spotify).execute(item)
            self.assertNotIn("tts", events)
            self.assertNotIn("play", events)

    def test_combined_opener_requires_exact_computer_device(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(
                state, track="Song", say="Line", transition="overlap", device="Desk"
            )
            with self.assertRaisesRegex(PlayoutError, "exact target device"):
                self.engine(state, events).execute(item)

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events = []
            spotify = FakeSpotify(events)
            spotify.device["type"] = "Smartphone"
            spotify.device["name"] = "Phone"
            item = rundown.append(
                state, track="Song", say="Line", transition="overlap", device="Phone"
            )
            with self.assertRaisesRegex(PlayoutError, "Spotify Computer"):
                self.engine(state, events, spotify=spotify).execute(item)

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events = []
            spotify = FakeSpotify(events)
            spotify.device["name"] = "Remote Mac"
            item = rundown.append(
                state, track="Song", say="Line", transition="overlap",
                device="Remote Mac",
            )
            with self.assertRaisesRegex(PlayoutError, "is not this Mac"):
                self.engine(state, events, spotify=spotify).execute(item)

    def test_phone_opener_targets_same_connect_device_and_uses_phone_duck_receipts(self):
        class ReceiptedPhone:
            def __init__(self, events):
                self.events = events

            def play(self, path, duration, *, remote_id, duck=True):
                self.events.append(f"phone:{Path(path).name}:{remote_id}")
                self.duck = duck
                return {
                    "voice_confirmed": True,
                    "duck_confirmed": True,
                    "ack_order_valid": True,
                    "events": {
                        "duck_started": {"at": 1},
                        "voice_started": {"at": 2},
                        "duck_finished": {"at": 3},
                        "voice_finished": {"at": 4},
                    },
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            spotify = FakeSpotify(events)
            spotify.device.update({
                "id": "phone-id", "name": "Android Phone", "type": "Smartphone",
            })
            data = config()
            data["spotify"]["device"] = "Android Phone"
            data["android"] = {"enabled": True, "device": "Android Phone"}
            item = rundown.append(
                state, track="Song", say="Line", transition="overlap",
                phone=True, device="Android Phone",
            )
            engine = self.engine(
                state, events, spotify=spotify, phone=ReceiptedPhone(events)
            )
            engine.config = data
            result = engine.execute(item)
            self.assertEqual(result["state"], "played")
            self.assertTrue(result["voice"]["played"])
            self.assertTrue(result["voice"]["duck_confirmed"])
            self.assertFalse(any(event.startswith("fade:") for event in events))
            self.assertTrue(any(event.startswith("phone:") for event in events))

    def test_qqmusic_phone_opener_uses_receiver_instead_of_local_mpv(self):
        class LocalMusic:
            def resolve(self, value):
                events.append(f"resolve:{value}")
                return "qqmusic:MID123", {"name": "Phone Record"}

            @staticmethod
            def supports_track_uri(uri):
                return uri.startswith("qqmusic:")

            @staticmethod
            def phone_stream(uri):
                return {
                    "uri": uri,
                    "url": "https://cdn.example/record.m4a?ticket=short",
                    "url_expiration_seconds": 7200,
                    "duration_ms": 240_000,
                }

            def ready_device(self, *_args, **_kwargs):
                raise AssertionError("phone QQ opener touched local mpv")

        class PhoneMusic:
            def play(self, stream, *, remote_id):
                events.append(f"phone-music:{stream['uri']}:{remote_id}")
                return {
                    "accepted": True,
                    "confirmed": True,
                    "transport": "qqmusic_api_to_phone_mpv",
                    "url_stored": False,
                    "playback": {
                        "uri": stream["uri"], "is_playing": True,
                        "progress_ms": 1250, "duration_ms": 240_000,
                        "repeat_state": "off", "volume_percent": 100,
                    },
                    "music_bed": {
                        "confirmed": True, "proof": "position_motion",
                        "advance_ms": 1200, "uri": stream["uri"],
                    },
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["music"] = {
                "backend": "local", "library_root": str(state),
                "device": "local_mac",
            }
            data["android"] = {"enabled": True, "device": "Android Phone"}
            item = rundown.append(
                state, track="qqmusic:MID123", phone=True, device="Android Phone"
            )
            engine = self.engine(
                state, events, spotify=LocalMusic(), phone_music=PhoneMusic()
            )
            engine.config = data
            result = engine.execute(item)
            self.assertEqual(result["state"], "played")
            self.assertEqual(
                result["track"]["transport"], "qqmusic_api_to_phone_mpv"
            )
            self.assertTrue(result["track"]["music_bed"]["confirmed"])
            self.assertTrue(any(event.startswith("phone-music:") for event in events))

    def test_phone_intercom_can_request_ducking_over_an_existing_bed(self):
        class ReceiptedPhone:
            def play(self, path, duration, *, remote_id, duck=True):
                self.duck = duck
                return {
                    "voice_confirmed": True,
                    "duck_confirmed": True,
                    "ack_order_valid": True,
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            phone = ReceiptedPhone()
            item = rundown.append(
                state, say="A live line.", transition="clean", phone=True, duck=True,
            )
            engine = self.engine(state, events, phone=phone)
            engine.config["android"] = {"enabled": True, "device": "Android Phone"}
            result = engine.execute(item)
            self.assertEqual(result["state"], "played")
            self.assertTrue(phone.duck)
            self.assertTrue(result["voice"]["duck_requested"])

    def test_phone_opener_rejects_music_and_voice_device_mismatch_before_tts(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            spotify = FakeSpotify(events)
            spotify.device.update({
                "id": "phone-id", "name": "Other Phone", "type": "Smartphone",
            })
            data = config()
            data["android"] = {"enabled": True, "device": "Expected Phone"}
            item = rundown.append(
                state, track="Song", say="Line", phone=True, device="Other Phone"
            )
            engine = self.engine(state, events, spotify=spotify)
            engine.config = data
            with self.assertRaisesRegex(PlayoutError, "does not match Spotify target"):
                engine.execute(item)
            self.assertNotIn("tts", events)

    def test_phone_hard_transition_explicitly_disables_audio_focus_duck(self):
        class ReceiptedPhone:
            def play(self, path, duration, *, remote_id, duck=True):
                self.duck = duck
                return {
                    "voice_confirmed": True,
                    "duck_confirmed": False,
                    "ack_order_valid": False,
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            spotify = FakeSpotify(events)
            spotify.device.update({
                "id": "phone-id", "name": "Android Phone", "type": "Smartphone",
            })
            data = config()
            data["spotify"]["device"] = "Android Phone"
            data["android"] = {"enabled": True, "device": "Android Phone"}
            item = rundown.append(
                state, track="Song", say="Line", transition="hard",
                phone=True, device="Android Phone",
            )
            phone = ReceiptedPhone()
            engine = self.engine(state, events, spotify=spotify, phone=phone)
            engine.config = data
            result = engine.execute(item)
            self.assertEqual(result["state"], "played")
            self.assertFalse(phone.duck)

    def test_uncertain_phone_voice_is_journalled_and_retry_refuses_to_repeat_it(self):
        class UncertainPhone:
            def play(self, path, duration, *, remote_id, duck=True):
                raise PhoneUncertainError(
                    "voice began but finish was lost",
                    {"voice_started": {"at": 2, "detail": "phone-player"}},
                )

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["android"] = {"enabled": True, "device": "Android Phone"}
            item = rundown.append(state, say="Do not repeat me.", phone=True)
            engine = self.engine(state, events, phone=UncertainPhone())
            engine.config = data
            with self.assertRaisesRegex(PlayoutError, "voice began"):
                engine.execute(item)
            transaction = Journal(state).load(item["filename"])
            self.assertTrue(transaction["voice"]["delivery_uncertain"])
            with self.assertRaisesRegex(PlayoutError, "refusing automatic replay"):
                engine.retry(item)

    def test_retry_resume_flag_starts_track_without_repeating_completed_voice(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(
                state, track="Song", say="Already delivered.",
                transition="clean", device="Desktop",
            )
            journal = Journal(state)
            journal.receipt(item["filename"], "voice", {"played": True})
            journal.set_state(item["filename"], "failed", "track failed")
            result = self.engine(state, events).retry(item)
            self.assertEqual(result["state"], "played")
            self.assertNotIn("tts", events)
            self.assertFalse(any(event.startswith("voice:") for event in events))
            self.assertIn("play", events)
            self.assertTrue(result["recovery"]["applied"])

    def test_engine_lock_rejects_duplicate_without_advancing_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, say="One at a time.")
            lock = state / "desktop-playout.lock"
            with lock.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(PlayoutError, "owns the station lock"):
                    self.engine(state, events).execute(item)
            self.assertEqual(Journal(state).load(item["filename"])["state"], "queued")

    def test_interrupted_transaction_can_retry_after_lock_owner_is_gone(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, say="Resume safely.")
            Journal(state).begin(item["filename"])
            result = self.engine(state, events).retry(item)
            self.assertEqual(result["state"], "played")
            self.assertTrue(result.get("previous_attempt_id"))

    def test_subprocess_timeout_is_journalled_as_failed(self):
        def timed_out(*_args):
            raise subprocess.TimeoutExpired("fake", 1)

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            item = rundown.append(state, say="Timeout.")
            engine = DesktopEngine(
                config(), state, spotify=ForbiddenSpotify(), synthesize=timed_out,
                verify_audio=lambda _path: 1.0, voice_player=FakeVoice(events),
                fader=FakeFader(events), sleep=lambda _seconds: None,
            )
            with self.assertRaises(PlayoutError):
                engine.execute(item)
            self.assertEqual(Journal(state).load(item["filename"])["state"], "failed")

    def test_applescript_timeout_is_wrapped_as_playout_error(self):
        fader = MacSpotifyFader.__new__(MacSpotifyFader)
        fader.executable = "/usr/bin/osascript"
        fader.sleep = lambda _seconds: None
        with mock.patch(
            "audience_of_one.desktop.subprocess.run",
            side_effect=subprocess.TimeoutExpired("osascript", 5),
        ), self.assertRaisesRegex(PlayoutError, "timed out"):
            fader.volume()

    def test_applescript_fader_rejects_a_target_that_does_not_stick(self):
        fader = MacSpotifyFader.__new__(MacSpotifyFader)
        fader.executable = "/usr/bin/osascript"
        fader.sleep = lambda _seconds: None
        with mock.patch.object(
            fader, "_run", side_effect=["100", "", "100"]
        ), self.assertRaisesRegex(PlayoutError, "did not stick"):
            fader.fade(40, 0)

    def test_clean_bed_failure_uses_cover_and_retry_will_not_repeat_voice(self):
        class StalledSpotify(FakeSpotify):
            def music_bed_probe(self, **_kwargs):
                self.events.append("bed-stalled")
                return {"confirmed": False, "reason": "position_stalled"}

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            (state / "covers").mkdir()
            (state / "covers" / "cover-1.mp3").write_bytes(b"cover")
            voice = FakeVoice(events)
            item = rundown.append(
                state, track="Song", say="Already delivered.",
                transition="clean", device="Desktop",
            )
            with self.assertRaisesRegex(PlayoutError, "playback stability failed"):
                self.engine(
                    state, events, spotify=StalledSpotify(events), voice=voice
                ).execute(item)
            failed = Journal(state).load(item["filename"])
            self.assertNotEqual((failed["track"] or {}).get("confirmed"), True)
            self.assertEqual(voice.plays, 2)  # announcement plus one recovery cover
            self.assertTrue(failed["recovery"]["played"])

    def test_superseded_track_records_stable_actual_and_liner_decision(self):
        class SupersededSpotify(FakeSpotify):
            stability_window_seconds = 8

            def __init__(self, events):
                super().__init__(events)
                self.probes = 0

            def music_bed_probe(self, **kwargs):
                self.probes += 1
                self.events.append(f"probe:{kwargs.get('expected_uri')}")
                if self.probes == 1:
                    return {
                        "confirmed": False,
                        "reason": "unexpected_track",
                        "actual_track": {
                            "uri": "spotify:track:actual",
                            "name": "Actual Song",
                            "artists": ["Actual Artist"],
                            "progress_ms": 500,
                            "duration_ms": 200000,
                        },
                    }
                return {
                    "confirmed": True,
                    "uri": "spotify:track:actual",
                    "advance_ms": 8000,
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["improv"] = {
                "enabled": True,
                "liner_chance": 1.0,
                "lines": ["One.", "Two."],
            }
            item = rundown.append(state, track="spotify:track:wanted")
            engine = self.engine(state, events, spotify=SupersededSpotify(events))
            engine.config = data
            with mock.patch(
                "audience_of_one.desktop.wildcards.choose",
                return_value={
                    "selected": True,
                    "played": False,
                    "liner_id": "liner-1",
                    "audio": "/tmp/liner-1.mp3",
                },
            ), self.assertRaisesRegex(PlayoutError, "playback stability failed"):
                engine.execute(item)
            transaction = Journal(state).load(item["filename"])
            superseded = transaction["track"]["superseded"]
            self.assertEqual(superseded["requested_uri"], "spotify:track:resolved")
            self.assertEqual(superseded["actual"]["uri"], "spotify:track:actual")
            self.assertTrue(superseded["stable"])
            decision = transaction["improv"]["wildcard_liner"]
            self.assertTrue(decision["selected"])
            self.assertFalse(decision["played"])
            self.assertEqual(decision["delivery_status"], "android_disabled")
            self.assertEqual(decision["requested_uri"], "spotify:track:resolved")
            self.assertEqual(decision["actual_track"]["uri"], "spotify:track:actual")

    def test_stable_supersession_does_not_play_dead_air_recovery_cover(self):
        class SupersededSpotify(FakeSpotify):
            stability_window_seconds = 8

            def music_bed_probe(self, **kwargs):
                self.events.append(f"probe:{kwargs.get('expected_uri')}")
                if kwargs.get("expected_uri") == "spotify:track:resolved":
                    return {
                        "confirmed": False,
                        "reason": "unexpected_track",
                        "actual_track": {
                            "uri": "spotify:track:actual",
                            "name": "Actual Song",
                            "artists": ["Actual Artist"],
                        },
                    }
                return {"confirmed": True, "uri": "spotify:track:actual"}

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            voice = FakeVoice(events)
            data = config()
            data["improv"] = {
                "enabled": False,
                "liner_chance": 0.0,
                "lines": ["One.", "Two."],
            }
            (state / "covers").mkdir()
            (state / "covers" / "cover-1.mp3").write_bytes(b"cover")
            item = rundown.append(
                state,
                say="Requested introduction.",
                track="spotify:track:wanted",
                transition="clean",
            )
            engine = self.engine(
                state, events, spotify=SupersededSpotify(events), voice=voice
            )
            engine.config = data
            with self.assertRaisesRegex(PlayoutError, "playback stability failed"):
                engine.execute(item)
            transaction = Journal(state).load(item["filename"])
            self.assertTrue(transaction["track"]["superseded"]["stable"])
            self.assertEqual(voice.plays, 1)
            self.assertEqual(transaction["recovery"], {})

    def test_supersession_is_captured_when_identity_wait_fails_first(self):
        class EarlySupersededSpotify(FakeSpotify):
            stability_window_seconds = 8

            def wait_for_playback(self, uri, device_id, timeout):
                raise SpotifyError("wanted track never became current")

            def music_bed_probe(self, **kwargs):
                if kwargs.get("expected_uri") == "spotify:track:resolved":
                    return {
                        "confirmed": False,
                        "reason": "unexpected_track",
                        "actual_track": {
                            "uri": "spotify:track:actual",
                            "name": "Actual Song",
                            "artists": ["Actual Artist"],
                        },
                    }
                return {"confirmed": True, "uri": "spotify:track:actual"}

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["improv"] = {
                "enabled": False,
                "liner_chance": 0.25,
                "lines": ["One.", "Two."],
            }
            item = rundown.append(state, track="spotify:track:wanted")
            engine = self.engine(state, events, spotify=EarlySupersededSpotify(events))
            engine.config = data
            with self.assertRaisesRegex(PlayoutError, "never became current"):
                engine.execute(item)
            transaction = Journal(state).load(item["filename"])
            self.assertEqual(transaction["track"]["failed_stage"], "identity")
            self.assertTrue(transaction["track"]["superseded"]["stable"])

    def test_superseded_liner_is_played_only_after_phone_receipts(self):
        class SupersededSpotify(FakeSpotify):
            stability_window_seconds = 8

            def music_bed_probe(self, **kwargs):
                if kwargs.get("expected_uri") == "spotify:track:resolved":
                    return {
                        "confirmed": False,
                        "reason": "unexpected_track",
                        "actual_track": {
                            "uri": "spotify:track:actual",
                            "name": "Actual Song",
                            "artists": ["Actual Artist"],
                        },
                    }
                return {"confirmed": True, "uri": "spotify:track:actual"}

        class ReceiptedPhone:
            def play(self, path, duration, *, remote_id):
                self.call = (path, duration, remote_id)
                return {
                    "remote_id": remote_id,
                    "voice_confirmed": True,
                    "duck_confirmed": True,
                    "ack_order_valid": True,
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["android"] = {"enabled": True}
            data["improv"] = {
                "enabled": True,
                "liner_chance": 1.0,
                "lines": ["One.", "Two."],
            }
            item = rundown.append(state, track="spotify:track:wanted")
            phone = ReceiptedPhone()
            engine = self.engine(
                state, events, spotify=SupersededSpotify(events), phone=phone
            )
            engine.config = data
            with mock.patch(
                "audience_of_one.desktop.wildcards.choose",
                return_value={
                    "selected": True,
                    "played": False,
                    "liner_id": "liner-2",
                    "audio": "/tmp/liner-2.mp3",
                    "duration_seconds": 1.5,
                },
            ), self.assertRaisesRegex(PlayoutError, "playback stability failed"):
                engine.execute(item)
            transaction = Journal(state).load(item["filename"])
            decision = transaction["improv"]["wildcard_liner"]
            self.assertTrue(decision["played"])
            self.assertEqual(decision["delivery_status"], "played_with_duck")
            self.assertEqual(phone.call[2], f"wildcard-{transaction['attempt_id']}")
            self.assertEqual(wildcards.last_played_id(state), "liner-2")

    def test_repeat_guard_write_failure_does_not_erase_real_phone_playout(self):
        class SupersededSpotify(FakeSpotify):
            stability_window_seconds = 8

            def music_bed_probe(self, **kwargs):
                if kwargs.get("expected_uri") == "spotify:track:resolved":
                    return {
                        "confirmed": False,
                        "reason": "unexpected_track",
                        "actual_track": {"uri": "spotify:track:actual"},
                    }
                return {"confirmed": True, "uri": "spotify:track:actual"}

        class ReceiptedPhone:
            def play(self, path, duration, *, remote_id):
                return {
                    "voice_confirmed": True,
                    "duck_confirmed": True,
                    "ack_order_valid": True,
                }

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            events: list[str] = []
            data = config()
            data["android"] = {"enabled": True}
            data["improv"] = {
                "enabled": True, "liner_chance": 1.0, "lines": ["One.", "Two."],
            }
            item = rundown.append(state, track="spotify:track:wanted")
            engine = self.engine(
                state, events, spotify=SupersededSpotify(events), phone=ReceiptedPhone()
            )
            engine.config = data
            with mock.patch(
                "audience_of_one.desktop.wildcards.choose",
                return_value={
                    "selected": True,
                    "played": False,
                    "liner_id": "liner-1",
                    "audio": "/tmp/liner-1.mp3",
                    "duration_seconds": 1.0,
                },
            ), mock.patch(
                "audience_of_one.desktop.wildcards.mark_played",
                side_effect=OSError("read-only state"),
            ), self.assertRaises(PlayoutError):
                engine.execute(item)
            decision = Journal(state).load(item["filename"])["improv"]["wildcard_liner"]
            self.assertTrue(decision["played"])
            self.assertFalse(decision["repeat_guard_saved"])
            self.assertIn("read-only state", decision["repeat_guard_error"])


if __name__ == "__main__":
    unittest.main()
