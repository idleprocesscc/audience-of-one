from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import covers, tts


def config() -> dict:
    return {
        "tts": {
            "chinese": "minimax",
            "english": "elevenlabs",
            "providers": {
                "minimax": {"type": "minimax"},
                "elevenlabs": {"type": "elevenlabs"},
            },
        },
        "recovery": {"lines": ["Signal check.", "Still here."]},
    }


class TTSRoutingTests(unittest.TestCase):
    def test_language_router(self):
        self.assertTrue(tts.is_cjk("信号稍等，音乐马上回来。"))
        self.assertFalse(tts.is_cjk("The music will be right back."))

    @mock.patch("audience_of_one.tts._elevenlabs")
    def test_english_uses_elevenlabs(self, provider):
        with tempfile.TemporaryDirectory() as raw:
            name = tts.synthesize(config(), "Signal check.", Path(raw) / "voice.mp3")
        self.assertEqual(name, "elevenlabs")
        provider.assert_called_once()

    @mock.patch("audience_of_one.tts.urllib.request.urlopen")
    def test_minimax_adapter_uses_environment_secret_and_writes_audio(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "data": {"audio": b"ID3-minimax".hex()},
            "base_resp": {"status_code": 0},
        }).encode()
        urlopen.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"MINIMAX_API_KEY": "not-a-real-key"}
        ):
            output = Path(raw) / "voice.mp3"
            tts._minimax("Signal check.", output, {
                "api_key_env": "MINIMAX_API_KEY",
                "voice_id": "example-voice",
            })
            self.assertEqual(output.read_bytes(), b"ID3-minimax")

    @mock.patch("audience_of_one.tts.urllib.request.urlopen")
    def test_elevenlabs_adapter_uses_environment_secret_and_writes_audio(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = b"ID3-elevenlabs"
        urlopen.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "not-a-real-key"}
        ):
            output = Path(raw) / "voice.mp3"
            tts._elevenlabs("Signal check.", output, {
                "api_key_env": "ELEVENLABS_API_KEY",
                "voice_id": "example-voice",
            })
            self.assertEqual(output.read_bytes(), b"ID3-elevenlabs")

    def test_macos_adapter_creates_parent_and_publishes_encoded_file(self):
        def fake_run(command, **_kwargs):
            if command[0] == "/usr/bin/say":
                Path(command[2]).write_bytes(b"AIFF")
            else:
                Path(command[-1]).write_bytes(b"ID3-macos")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.tts.shutil.which", side_effect=[
                 "/usr/bin/say", "/opt/homebrew/bin/ffmpeg",
             ]), mock.patch("audience_of_one.tts.subprocess.run", side_effect=fake_run):
            output = Path(raw) / "missing" / "prepared" / "voice.mp3"
            tts._macos("Signal check.", output, {})
            self.assertEqual(output.read_bytes(), b"ID3-macos")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_macos_and_probe_timeouts_use_tts_error_contract(self):
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.tts.shutil.which", side_effect=[
                 "/usr/bin/say", "/opt/homebrew/bin/ffmpeg",
             ]), mock.patch(
                 "audience_of_one.tts.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("say", 30),
             ), self.assertRaisesRegex(tts.TTSError, "say timed out"):
            tts._macos("Signal check.", Path(raw) / "voice.mp3", {})

        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("audience_of_one.tts.shutil.which", return_value="/usr/bin/ffprobe"), \
             mock.patch(
                 "audience_of_one.tts.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("ffprobe", 10),
             ), self.assertRaisesRegex(tts.TTSError, "verification timed out"):
            tts.verify_audio(Path(raw) / "voice.mp3")


class CoverBuildTests(unittest.TestCase):
    def test_two_covers_publish_together(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)

            def fake_synthesize(_data, text, output):
                output.write_bytes((text * 20).encode())
                return "fake"

            with mock.patch("audience_of_one.covers.tts.synthesize", side_effect=fake_synthesize), \
                 mock.patch("audience_of_one.covers.tts.verify_audio", return_value=1.25):
                receipts = covers.build(config(), state)
            self.assertEqual(len(receipts), 2)
            self.assertTrue((state / "covers" / "current" / "cover-1.mp3").is_file())
            self.assertTrue((state / "covers" / "current" / "cover-2.mp3").is_file())
            self.assertFalse(list((state / "covers").glob("*.pending")))

    def test_failure_publishes_neither_pending_cover(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            calls = 0

            def fail_second(_data, text, output):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise tts.TTSError("injected failure")
                output.write_bytes((text * 20).encode())
                return "fake"

            with mock.patch("audience_of_one.covers.tts.synthesize", side_effect=fail_second), \
                 mock.patch("audience_of_one.covers.tts.verify_audio", return_value=1.25), \
                 self.assertRaises(tts.TTSError):
                covers.build(config(), state)
            self.assertFalse((state / "covers" / "current").exists())
            self.assertFalse(list((state / "covers").glob("*.pending")))

    def test_publish_failure_keeps_previous_cover_generation_as_a_pair(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)

            def fake_synthesize(_data, text, output):
                output.write_bytes((text * 20).encode())
                return "fake"

            patches = (
                mock.patch("audience_of_one.covers.tts.synthesize", side_effect=fake_synthesize),
                mock.patch("audience_of_one.covers.tts.verify_audio", return_value=1.25),
            )
            with patches[0], patches[1]:
                covers.build(config(), state)
            before = (state / "covers" / "current").resolve()
            real_replace = os.replace

            def fail_pointer(source, target):
                if Path(target).name == "current":
                    raise OSError("injected pointer failure")
                return real_replace(source, target)

            with mock.patch("audience_of_one.covers.tts.synthesize", side_effect=fake_synthesize), \
                 mock.patch("audience_of_one.covers.tts.verify_audio", return_value=1.25), \
                 mock.patch("audience_of_one.covers.os.replace", side_effect=fail_pointer), \
                 self.assertRaises(OSError):
                covers.build(config(), state)
            self.assertEqual((state / "covers" / "current").resolve(), before)
            self.assertTrue((before / "cover-1.mp3").is_file())
            self.assertTrue((before / "cover-2.mp3").is_file())


if __name__ == "__main__":
    unittest.main()
