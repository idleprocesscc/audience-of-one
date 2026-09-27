from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import cli, clips, rundown
from audience_of_one import config as station_config
from audience_of_one.desktop import DesktopEngine, PlayoutError
from audience_of_one.transactions import Journal
from tests.test_cli_commands import ROOT, SuccessfulEngine
from tests.test_desktop_playout import (
    FakeFader,
    FakeSpotify,
    FakeVoice,
    ForbiddenSpotify,
)
from tests.test_desktop_playout import config as desktop_config


class ClipResolutionTests(unittest.TestCase):
    def test_clip_resolves_only_below_the_configured_shelf(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "clips"
            (root / "idents").mkdir(parents=True)
            (root / "idents" / "name.wav").write_bytes(b"RIFF")
            (Path(raw) / "outside.wav").write_bytes(b"RIFF")
            config = {"clips": {"root": str(root)}}
            self.assertEqual(
                clips.resolve(config, "idents/name.wav"),
                (root / "idents" / "name.wav").resolve(),
            )
            for name in ("", "/etc/passwd.wav", "../outside.wav", "idents/../../outside.wav"):
                with self.assertRaisesRegex(clips.ClipError, "below clips.root"):
                    clips.resolve(config, name)
            (root / "escape.wav").symlink_to(Path(raw) / "outside.wav")
            with self.assertRaisesRegex(clips.ClipError, "escaped"):
                clips.resolve(config, "escape.wav")
            (root / "notes.txt").write_text("not audio")
            with self.assertRaisesRegex(clips.ClipError, "unsupported"):
                clips.resolve(config, "notes.txt")
            with self.assertRaisesRegex(clips.ClipError, "does not exist"):
                clips.resolve(config, "idents/missing.wav")

    def test_unconfigured_shelf_is_named(self):
        with self.assertRaisesRegex(clips.ClipError, "clips.root is not configured"):
            clips.resolve({}, "name.wav")


class ClipRenderTests(unittest.TestCase):
    def fake_ffmpeg(self, calls, returncode=0):
        def run(command, **kwargs):
            calls.append(command)
            if returncode == 0:
                Path(command[-1]).write_bytes(b"mp3")
            return subprocess.CompletedProcess(command, returncode, "", "boom")
        return run

    def test_voice_enters_at_the_requested_offset_without_normalizing_levels(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "prepared" / "item.mp3"
            calls: list[list[str]] = []
            with mock.patch("audience_of_one.clips.shutil.which", return_value="/bin/ffmpeg"), \
                 mock.patch("audience_of_one.clips.subprocess.run", self.fake_ffmpeg(calls)):
                clips.render(Path("echo.wav"), Path("voice.mp3"), 5.74, output)
            command = calls[0]
            self.assertEqual(command[command.index("-i") + 1], "echo.wav")
            self.assertIn("voice.mp3", command)
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("adelay=5740|5740", graph)
            self.assertIn("normalize=0", graph)
            self.assertIn("alimiter", graph)
            self.assertEqual(output.read_bytes(), b"mp3")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_clip_alone_is_rendered_to_the_same_voice_format(self):
        with tempfile.TemporaryDirectory() as raw:
            calls: list[list[str]] = []
            with mock.patch("audience_of_one.clips.shutil.which", return_value="/bin/ffmpeg"), \
                 mock.patch("audience_of_one.clips.subprocess.run", self.fake_ffmpeg(calls)):
                clips.render(Path("ident.wav"), None, 0.0, Path(raw) / "item.mp3")
            command = calls[0]
            self.assertEqual(command.count("-i"), 1)
            self.assertNotIn("amix", command[command.index("-filter_complex") + 1])
            self.assertEqual(command[command.index("-f") + 1], "mp3")

    def test_failed_render_leaves_no_partial_voice_file(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "item.mp3"
            calls: list[list[str]] = []
            with mock.patch("audience_of_one.clips.shutil.which", return_value="/bin/ffmpeg"), \
                 mock.patch("audience_of_one.clips.subprocess.run",
                            self.fake_ffmpeg(calls, returncode=1)), \
                 self.assertRaisesRegex(clips.ClipError, "clip render failed: boom"):
                clips.render(Path("ident.wav"), None, 0.0, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_render_requires_ffmpeg_and_a_bounded_offset(self):
        with mock.patch("audience_of_one.clips.shutil.which", return_value=None), \
             self.assertRaisesRegex(clips.ClipError, "ffmpeg is required"):
            clips.render(Path("a.wav"), None, 0.0, Path("out.mp3"))
        with mock.patch("audience_of_one.clips.shutil.which", return_value="/bin/ffmpeg"), \
             self.assertRaisesRegex(clips.ClipError, "0 to 600"):
            clips.render(Path("a.wav"), Path("v.mp3"), -1.0, Path("out.mp3"))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe"
    )
    def test_real_mix_lasts_until_the_voice_finishes_over_the_clip(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)

            def tone(name, frequency, seconds):
                path = base / name
                subprocess.run([
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:duration={seconds}", str(path),
                ], check=True)
                return path

            clip = tone("echo.wav", 440, 3.0)
            voice = tone("voice.mp3", 220, 2.0)
            output = base / "prepared" / "item.mp3"
            clips.render(clip, voice, 2.5, output)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(output),
            ], capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(probe.stdout), 4.5, delta=0.15)


class ClipProgrammeTests(unittest.TestCase):
    DURATIONS = {"echo.wav": 9.0, "ident.wav": 4.6}

    def engine(self, state: Path, events: list[str], renders: list, *, spotify=None):
        data = desktop_config()
        data["clips"] = {"root": str(state / "clips")}

        def synthesize(_data, text, output):
            events.append(f"tts:{output.name}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"voice")
            return "fake"

        def verify_audio(path):
            if path.name in self.DURATIONS:
                return self.DURATIONS[path.name]
            return 7.8 if path.name.endswith(".voice.mp3") else 12.0

        def render_clip(clip, voice, voice_at, output):
            events.append(f"render:{clip.name}")
            renders.append((clip.name, voice.name if voice else None, voice_at, output.name))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"mixed")

        return DesktopEngine(
            data, state,
            spotify=spotify or FakeSpotify(events),
            synthesize=synthesize,
            verify_audio=verify_audio,
            render_clip=render_clip,
            voice_player=FakeVoice(events),
            fader=FakeFader(events),
            sleep=lambda seconds: events.append(f"sleep:{seconds}"),
        )

    def shelf(self, state: Path) -> None:
        (state / "clips").mkdir(parents=True)
        for name in self.DURATIONS:
            (state / "clips" / name).write_bytes(b"RIFF")

    def test_echo_and_line_become_one_voice_file_before_the_next_record(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            renders: list = []
            item = rundown.append(
                state, track="Next Song", say="A line over the last chord.",
                clip="echo.wav", clip_voice_at=5.74, transition="clean", device="Desktop",
            )
            result = self.engine(state, events, renders).execute(item)
            self.assertEqual(result["state"], "played")
            self.assertEqual(renders, [(
                "echo.wav", f"{item['id']}.voice.mp3", 5.74, f"{item['id']}.mp3",
            )])
            self.assertLess(events.index(f"tts:{item['id']}.voice.mp3"),
                            events.index("render:echo.wav"))
            self.assertLess(events.index(f"voice:{item['id']}.mp3"), events.index("play"))
            clip = result["voice"]["clip"]
            self.assertEqual(clip["requested"], "echo.wav")
            self.assertEqual(clip["duration_seconds"], 9.0)
            self.assertEqual(clip["voice_at_seconds"], 5.74)
            self.assertEqual(clip["voice_duration_seconds"], 7.8)
            self.assertEqual(result["voice"]["duration_seconds"], 12.0)
            self.assertTrue(result["voice"]["played"])

    def test_line_follows_the_clip_when_no_offset_is_given(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            renders: list = []
            item = rundown.append(state, say="After the ident.", clip="ident.wav")
            self.engine(state, [], renders, spotify=ForbiddenSpotify()).execute(item)
            self.assertEqual(renders[0][2], 4.6)

    def test_ident_alone_needs_no_speech_and_no_music_backend(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            renders: list = []
            item = rundown.append(state, clip="ident.wav", transition="clean")
            result = self.engine(
                state, events, renders, spotify=ForbiddenSpotify()
            ).execute(item)
            self.assertEqual(result["state"], "played")
            self.assertFalse(any(event.startswith("tts:") for event in events))
            self.assertEqual(renders, [("ident.wav", None, 0.0, f"{item['id']}.mp3")])
            self.assertNotIn("voice_at_seconds", result["voice"]["clip"])
            self.assertNotIn("provider", result["voice"])

    def test_overlap_ducks_the_record_under_the_clip(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            item = rundown.append(
                state, track="Song", clip="ident.wav", transition="overlap", device="Desktop",
            )
            self.engine(state, events, []).execute(item)
            self.assertLess(events.index("play"), events.index("fade:35:0.8"))
            self.assertLess(events.index("fade:35:0.8"), events.index(f"voice:{item['id']}.mp3"))
            self.assertEqual(events[-1], "fade:70:1.0")

    def test_missing_clip_fails_before_paying_for_speech(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            item = rundown.append(state, say="Never spoken.", clip="gone.wav")
            with self.assertRaisesRegex(PlayoutError, "clip does not exist"):
                self.engine(state, events, [], spotify=ForbiddenSpotify()).execute(item)
            self.assertFalse(any(event.startswith("tts:") for event in events))
            self.assertEqual(Journal(state).load(item["filename"])["state"], "failed")
            self.assertTrue((state / "queue" / item["filename"]).exists())

    def test_retry_after_a_played_clip_starts_only_the_record(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            renders: list = []
            item = rundown.append(
                state, track="Song", say="Already heard.", clip="echo.wav",
                transition="clean", device="Desktop",
            )
            journal = Journal(state)
            journal.receipt(item["filename"], "voice", {"played": True})
            journal.set_state(item["filename"], "failed", "track failed")
            result = self.engine(state, events, renders).retry(item)
            self.assertEqual(result["state"], "played")
            self.assertEqual(renders, [])
            self.assertFalse(any(event.startswith(("tts:", "voice:")) for event in events))
            self.assertIn("play", events)

    def test_echo_keeps_its_clip_and_line_after_the_record_it_follows(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            renders: list = []
            record = rundown.append(state, track="City of Stars", device="Desktop")
            Journal(state).set_state(record["filename"], "played")
            rundown.archive_played(state, record["filename"])
            echo = rundown.append(
                state, track="Next", say="Over the last chord.", clip="echo.wav",
                transition="clean", device="Desktop", follows="previous",
            )
            self.assertEqual(echo["data"]["follows"], record["id"])
            result = self.engine(state, events, renders).execute(echo)
            self.assertEqual(result["state"], "played")
            self.assertEqual(len(renders), 1)
            self.assertEqual(result["recovery"], {})

    def test_echo_behind_a_failed_record_drops_its_seam_and_keeps_the_next_record(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            renders: list = []
            record = rundown.append(state, track="Unavailable version", device="Desktop")
            echo = rundown.append(
                state, track="Next", say="City of Stars. He asked the stars...",
                clip="echo.wav", clip_voice_at=5.74, transition="clean",
                device="Desktop", follows="previous",
            )
            self.assertEqual(echo["data"]["follows"], record["id"])
            Journal(state).set_state(record["filename"], "failed", "account cannot stream")
            result = self.engine(state, events, renders).execute(echo)
            self.assertEqual(result["state"], "played")
            self.assertEqual(renders, [])
            self.assertFalse(any(event.startswith(("tts:", "voice:")) for event in events))
            self.assertIn("play", events)
            self.assertEqual(result["recovery"], {
                "follows": record["id"], "follows_state": "failed",
                "dropped": ["clip", "say"],
            })
            # The archived programme still says what was planned for this seam.
            archived = json.loads((state / "played" / echo["filename"]).read_text())
            self.assertEqual(archived["clip"], "echo.wav")

    def test_seam_without_a_record_fails_when_its_record_did_not_play(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            self.shelf(state)
            events: list[str] = []
            record = rundown.append(state, track="Unavailable version", device="Desktop")
            Journal(state).set_state(record["filename"], "failed", "account cannot stream")
            echo = rundown.append(state, clip="echo.wav", follows=record["id"])
            with self.assertRaisesRegex(PlayoutError, f"item {record['id']} did not play"):
                self.engine(state, events, [], spotify=ForbiddenSpotify()).execute(echo)
            self.assertFalse(any(event.startswith(("tts:", "render:", "voice:")) for event in events))
            self.assertEqual(Journal(state).load(echo["filename"])["state"], "failed")


class ClipRundownAndCLITests(unittest.TestCase):
    def test_rundown_accepts_a_clip_alone_and_bounds_the_voice_offset(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, clip="ident.wav")
            self.assertEqual(item["data"]["clip"], "ident.wav")
            self.assertNotIn("clip_voice_at", item["data"])
            with self.assertRaisesRegex(rundown.RundownError, "track, a voice line, or a clip"):
                rundown.append(state)
            with self.assertRaisesRegex(rundown.RundownError, "both a clip and a voice line"):
                rundown.append(state, clip="ident.wav", clip_voice_at=1.0)
            with self.assertRaisesRegex(rundown.RundownError, "both a clip and a voice line"):
                rundown.append(state, say="Line", clip_voice_at=1.0)
            for bad in (-0.1, 600.5, True):
                with self.assertRaisesRegex(rundown.RundownError, "0 to 600"):
                    rundown.append(state, say="Line", clip="echo.wav", clip_voice_at=bad)

    def test_follows_names_an_existing_item_or_the_one_right_before(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            with self.assertRaisesRegex(rundown.RundownError, "no earlier programme item"):
                rundown.append(state, say="Line", follows="previous")
            first = rundown.append(state, track="Song")
            Journal(state).set_state(first["filename"], "played")
            rundown.archive_played(state, first["filename"])
            self.assertEqual(
                rundown.append(state, say="After it.", follows="previous")["data"]["follows"],
                first["id"],
            )
            second = rundown.append(state, track="Another")
            self.assertEqual(
                rundown.append(state, say="Then.", follows="previous")["data"]["follows"],
                second["id"],
            )
            by_id = rundown.append(state, say="By id.", follows=first["id"])
            self.assertEqual(by_id["data"]["follows"], first["id"])
            shelved = rundown.append(state, track="Unavailable version")
            Journal(state).set_state(shelved["filename"], "failed", "account cannot stream")
            self.assertEqual(
                rundown.append(state, say="Skips the shelved item.", follows="previous")
                ["data"]["follows"],
                by_id["id"],
            )
            with self.assertRaisesRegex(rundown.RundownError, "invalid item id"):
                rundown.append(state, say="Line", follows="../etc")
            with self.assertRaisesRegex(rundown.RundownError, "not found"):
                rundown.append(state, say="Line", follows="000000000000001")

    def test_follows_previous_with_an_empty_queue_uses_the_latest_play_not_the_latest_id(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            journal = Journal(state)
            older = rundown.append(state, track="Queued first, played last")
            newer = rundown.append(state, track="Queued second, played first")
            with mock.patch("audience_of_one.transactions.time.time", return_value=100.0):
                journal.set_state(newer["filename"], "played")
            with mock.patch("audience_of_one.transactions.time.time", return_value=200.0):
                journal.set_state(older["filename"], "played")
            rundown.archive_played(state, newer["filename"])
            rundown.archive_played(state, older["filename"])
            self.assertEqual(
                rundown.append(state, say="After it.", follows="previous")["data"]["follows"],
                older["id"],
            )

    def test_clips_table_needs_a_root(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["clips"] = {"root": "~/Music/AudienceOfOne/clips"}
        station_config.validate(data)
        data["clips"] = {"root": " "}
        with self.assertRaisesRegex(station_config.ConfigError, "clips.root"):
            station_config.validate(data)
        data["clips"] = "~/clips"
        with self.assertRaisesRegex(station_config.ConfigError, r"\[clips\] must be a table"):
            station_config.validate(data)

    def args(self, state: Path, **values) -> argparse.Namespace:
        return argparse.Namespace(
            config=ROOT / "examples" / "config.macos-smoke.toml",
            state_dir=state, as_json=True, **values,
        )

    def test_queue_and_parser_carry_the_clip_fields(self):
        parsed = cli.build_parser().parse_args([
            "queue", "Next Song", "A line.", "--clip", "echo.wav",
            "--clip-voice-at", "5.74", "--transition", "clean", "--follows", "previous",
        ])
        self.assertEqual((parsed.clip, parsed.clip_voice_at), ("echo.wav", 5.74))
        self.assertEqual(parsed.follows, "previous")
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.command_queue(argparse.Namespace(
                    state_dir=state, config=None, as_json=True, track=None, say=None,
                    lang=None, transition="clean", after="autoplay", phone=False,
                    device=None, clip="ident.wav", clip_voice_at=None,
                ))
            self.assertEqual(code, 0)
            queued = rundown.items(state)[0]["data"]
            self.assertEqual(queued["clip"], "ident.wav")
            self.assertNotIn("say", queued)

    def test_say_intercom_can_play_an_ident_without_a_line(self):
        parsed = cli.build_parser().parse_args(["say", "--clip", "ident.wav", "--phone"])
        self.assertIsNone(parsed.text)
        seen = {}

        class InspectingEngine(SuccessfulEngine):
            def execute(self, item):
                seen.update(item["data"])
                return super().execute(item)

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            args = self.args(Path(raw), text=None, phone=True, clip="ident.wav",
                             clip_voice_at=None)
            with mock.patch("audience_of_one.cli.DesktopEngine", InspectingEngine), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.command_say(args)
            self.assertEqual(code, 0)
            self.assertEqual(seen["clip"], "ident.wav")
            self.assertNotIn("say", seen)
            self.assertEqual(seen["transition"], "clean")
            self.assertTrue(seen["duck"])


if __name__ == "__main__":
    unittest.main()
