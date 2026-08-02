from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import cli, rundown
from audience_of_one.desktop import PlayoutError
from audience_of_one.transactions import Journal

ROOT = Path(__file__).resolve().parents[1]


class SuccessfulEngine:
    def __init__(self, _config, state):
        self.state = state

    def execute(self, item):
        journal = Journal(self.state)
        journal.begin(item["filename"])
        result = journal.set_state(item["filename"], "played")
        rundown.archive_played(self.state, item["filename"])
        return result

    def retry(self, item):
        Journal(self.state).reset(item["filename"])
        return self.execute(item)


class CLICommandTests(unittest.TestCase):
    def args(self, state: Path, **values):
        return argparse.Namespace(
            config=ROOT / "examples" / "config.macos-smoke.toml",
            state_dir=state,
            as_json=True,
            **values,
        )

    def test_retry_resets_failed_attempt_and_executes_same_item(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            state = Path(raw)
            item = rundown.append(state, say="Try again.")
            old = Journal(state).set_state(item["filename"], "failed", "injected")
            output = io.StringIO()
            with mock.patch("audience_of_one.cli.DesktopEngine", SuccessfulEngine), \
                 contextlib.redirect_stdout(output):
                code = cli.command_retry(self.args(state, item=item["id"]))
            self.assertEqual(code, 0, output.getvalue())
            transaction = Journal(state).load(item["filename"])
            self.assertEqual(transaction["state"], "played")
            self.assertEqual(transaction["previous_attempt_id"], old["attempt_id"])
            self.assertTrue((state / "played" / item["filename"]).exists())

    def test_open_failure_json_returns_retryable_item_id(self):
        class FailingEngine:
            def __init__(self, _config, state):
                self.state = state

            def execute(self, item):
                Journal(self.state).set_state(item["filename"], "failed", "injected")
                raise PlayoutError("injected")

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            state = Path(raw)
            args = self.args(
                state, track=None, say="A line", transition="overlap", device=None,
            )
            output = io.StringIO()
            with mock.patch("audience_of_one.cli.DesktopEngine", FailingEngine), \
                 contextlib.redirect_stdout(output):
                code = cli.command_open(args)
            self.assertEqual(code, 2)
            self.assertIn('"id":', output.getvalue())
            self.assertIn('"state": "failed"', output.getvalue())

    def test_open_phone_marks_the_direct_item_for_android_delivery(self):
        seen = {}

        class InspectingEngine(SuccessfulEngine):
            def execute(self, item):
                seen.update(item["data"])
                return super().execute(item)

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            state = Path(raw)
            args = self.args(
                state, track=None, say="A phone line", transition="overlap",
                device=None, phone=True,
            )
            with mock.patch("audience_of_one.cli.DesktopEngine", InspectingEngine), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.command_open(args)
            self.assertEqual(code, 0)
            self.assertEqual(seen["output"], "phone")

    def test_say_is_a_clean_voice_only_intercom_not_a_music_command(self):
        seen = {}

        class InspectingEngine(SuccessfulEngine):
            def execute(self, item):
                seen.update(item["data"])
                return super().execute(item)

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            state = Path(raw)
            args = self.args(state, text="Lunch is ready.", phone=True)
            with mock.patch("audience_of_one.cli.DesktopEngine", InspectingEngine), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.command_say(args)
            self.assertEqual(code, 0)
            self.assertEqual(seen["say"], "Lunch is ready.")
            self.assertEqual(seen["output"], "phone")
            self.assertEqual(seen["transition"], "clean")
            self.assertTrue(seen["duck"])
            self.assertNotIn("track", seen)

    def test_retry_can_run_a_queued_item(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ):
            state = Path(raw)
            item = rundown.append(state, say="Not yet.")
            with mock.patch("audience_of_one.cli.DesktopEngine", SuccessfulEngine), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.command_retry(self.args(state, item=item["id"]))
            self.assertEqual(code, 0)
            self.assertTrue((state / "played" / item["filename"]).exists())

    def test_history_reads_only_archived_played_items(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            played = rundown.append(state, say="Finished.")
            Journal(state).set_state(played["filename"], "played")
            rundown.archive_played(state, played["filename"])
            rundown.append(state, say="Still queued.")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_history(self.args(state))
            self.assertEqual(code, 0)
            self.assertIn('"count": 1', output.getvalue())
            self.assertIn("Finished.", output.getvalue())
            self.assertNotIn("Still queued.", output.getvalue())

    def test_off_requires_pause_receipt(self):
        class FakeClient:
            def __init__(self, _config, _state):
                pass

            def pause(self, spec):
                self.spec = spec
                return {"accepted": True, "device": {"id": "desk", "name": "Desktop"}}

            def wait_for_paused(self, device_id, timeout):
                return {"is_playing": False, "device": {"id": device_id}}

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ), mock.patch("audience_of_one.cli.spotify_client", side_effect=FakeClient):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_off(self.args(Path(raw), device="Desktop"))
            self.assertEqual(code, 0)
            self.assertIn('"off": true', output.getvalue())

    def test_toggle_pauses_a_playing_backend_with_a_receipt(self):
        class FakeClient:
            def __init__(self, _config, _state):
                pass

            def snapshot(self):
                return {"is_playing": True, "device": {"id": "desk"}}

            def pause(self, spec):
                return {"accepted": True, "device": {"id": "desk", "name": spec}}

            def wait_for_paused(self, device_id, timeout):
                return {"is_playing": False, "device": {"id": device_id}}

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ), mock.patch("audience_of_one.cli.spotify_client", side_effect=FakeClient):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_toggle(self.args(Path(raw), device="Desktop"))
            self.assertEqual(code, 0)
            self.assertIn('"action": "paused"', output.getvalue())

    def test_toggle_resumes_a_paused_backend_with_a_receipt(self):
        class FakeClient:
            def __init__(self, _config, _state):
                pass

            def snapshot(self):
                return {"is_playing": False, "device": {"id": "desk"}}

            def resume(self, spec):
                return {"accepted": True, "device": {"id": "desk", "name": spec}}

            def wait_for_resumed(self, device_id, timeout):
                return {"is_playing": True, "device": {"id": device_id}}

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ), mock.patch("audience_of_one.cli.spotify_client", side_effect=FakeClient):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_toggle(self.args(Path(raw), device="Desktop"))
            self.assertEqual(code, 0)
            self.assertIn('"action": "resumed"', output.getvalue())

    def test_mpv_quit_reports_confirmed_process_exit(self):
        class FakeClient:
            def quit(self):
                return {"accepted": True, "stopped": True, "already_stopped": False}

        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.cli._desktop_config", return_value={
                 "station": {"mode": "desktop"}, "music": {"backend": "local"},
             }), mock.patch("audience_of_one.cli.music_client", return_value=FakeClient()):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_mpv(self.args(Path(raw), mpv_command="quit"))
            self.assertEqual(code, 0)
            self.assertIn('"stopped": true', output.getvalue())

    def test_list_auto_selects_the_configured_backend_without_mixing(self):
        payload = {
            "version": 1, "source": "mpv", "library_root": "/records",
            "count": 0, "tracks": [], "playback_touched": False,
            "rundown_touched": False,
        }
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.cli._desktop_config", return_value={
                 "station": {"mode": "desktop"}, "music": {"backend": "local"},
             }), mock.patch("audience_of_one.cli.catalog.local_shelf", return_value=payload):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_list(self.args(Path(raw)))
            self.assertEqual(code, 0)
            self.assertIn('"source": "mpv"', output.getvalue())

    def test_explicit_mpv_list_uses_the_same_shelf_payload(self):
        payload = {
            "version": 1, "source": "mpv", "library_root": "/records",
            "count": 0, "tracks": [], "playback_touched": False,
            "rundown_touched": False,
        }
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.cli._desktop_config", return_value={
                 "station": {"mode": "desktop"}, "music": {"backend": "local"},
             }), mock.patch("audience_of_one.cli.catalog.local_shelf", return_value=payload):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_mpv(self.args(Path(raw), mpv_command="list"))
            self.assertEqual(code, 0)
            self.assertIn('"source": "mpv"', output.getvalue())

    def test_fader_command_returns_a_durable_restore_receipt(self):
        class FakeLiveFader:
            def __init__(self, state):
                self.state = state

            def move(self, target, seconds):
                return {
                    "moved": True, "from_percent": 70, "to_percent": target,
                    "restore_percent": 70, "seconds": seconds, "steps": [target],
                }

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ), mock.patch("audience_of_one.cli.LiveFader", FakeLiveFader):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_fader(self.args(
                    Path(raw), target="40", seconds=0.8,
                ))
            self.assertEqual(code, 0)
            self.assertIn('"restore_percent": 70', output.getvalue())

    def test_liners_build_reports_each_verified_cached_file(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            "os.environ", {"SPOTIFY_CLIENT_ID": "fake-public-id"}
        ), mock.patch("audience_of_one.cli.wildcards.build", return_value=[
            {
                "id": "liner-1",
                "provider": "fake",
                "duration_seconds": 1.25,
                "bytes": 123,
            },
            {
                "id": "liner-2",
                "provider": "fake",
                "duration_seconds": 1.5,
                "bytes": 145,
            },
        ]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.command_liners(self.args(Path(raw), liners_command="build"))
            self.assertEqual(code, 0)
            self.assertIn("liner-1.mp3", output.getvalue())
            self.assertIn("liner-2.mp3", output.getvalue())


if __name__ == "__main__":
    unittest.main()
