from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import tts, wildcards


def config(*, enabled: bool = True, chance: float = 1.0) -> dict:
    return {
        "tts": {
            "chinese": "fake",
            "english": "fake",
            "providers": {"fake": {}},
        },
        "improv": {
            "enabled": enabled,
            "liner_chance": chance,
            "lines": ["One.", "Two.", "Three."],
        },
    }


def build_bank(data: dict, state: Path) -> None:
    def synthesize(_data, text, output):
        output.write_bytes((text * 20).encode())
        return "fake"

    with mock.patch(
        "audience_of_one.wildcards.tts.synthesize", side_effect=synthesize
    ), mock.patch("audience_of_one.wildcards.tts.verify_audio", return_value=1.25):
        wildcards.build(data, state)


class WildcardBuildTests(unittest.TestCase):
    def test_complete_liner_bank_publishes_atomically(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)

            def synthesize(_data, text, output):
                output.write_bytes((text * 20).encode())
                return "fake"

            with mock.patch(
                "audience_of_one.wildcards.tts.synthesize", side_effect=synthesize
            ), mock.patch("audience_of_one.wildcards.tts.verify_audio", return_value=1.25):
                receipts = wildcards.build(config(), state)
            self.assertEqual([row["id"] for row in receipts], [
                "liner-1", "liner-2", "liner-3",
            ])
            current = state / "wildcards" / "current"
            self.assertTrue((current / "manifest.json").is_file())
            self.assertEqual(len(list(current.glob("liner-*.mp3"))), 3)
            self.assertFalse(list((state / "wildcards").glob("*.pending")))

    def test_failed_generation_keeps_the_previous_bank(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            build_bank(config(), state)
            before = (state / "wildcards" / "current").resolve()
            calls = 0

            def fail_second(_data, text, output):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise tts.TTSError("injected failure")
                output.write_bytes((text * 20).encode())
                return "fake"

            with mock.patch(
                "audience_of_one.wildcards.tts.synthesize", side_effect=fail_second
            ), mock.patch(
                "audience_of_one.wildcards.tts.verify_audio", return_value=1.25
            ), self.assertRaises(tts.TTSError):
                wildcards.build(config(), state)
            self.assertEqual((state / "wildcards" / "current").resolve(), before)


class WildcardChoiceTests(unittest.TestCase):
    def test_same_attempt_makes_the_same_probability_and_liner_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            data = config()
            build_bank(data, state)
            first = wildcards.choose(data, state, "attempt-one")
            second = wildcards.choose(data, state, "attempt-one")
            self.assertEqual(first, second)
            self.assertTrue(first["selected"])
            self.assertFalse(first["played"])

    def test_disabled_or_zero_chance_never_selects(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            disabled = wildcards.choose(config(enabled=False), state, "attempt")
            zero = wildcards.choose(config(chance=0), state, "attempt")
            self.assertEqual(disabled["reason"], "disabled")
            self.assertEqual(zero["reason"], "probability")

    def test_previous_liner_is_avoided_when_an_alternative_exists(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            data = config()
            build_bank(data, state)
            first = wildcards.choose(data, state, "attempt-two")
            second = wildcards.choose(
                data, state, "attempt-two", previous_id=first["liner_id"]
            )
            self.assertNotEqual(second["liner_id"], first["liner_id"])


if __name__ == "__main__":
    unittest.main()
