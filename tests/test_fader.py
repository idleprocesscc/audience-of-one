from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audience_of_one.desktop import PlayoutError
from audience_of_one.fader import LiveFader


class FakeBackend:
    def __init__(self, volume: int = 70, *, fail: bool = False):
        self.current = volume
        self.fail = fail
        self.events: list[tuple] = []

    def volume(self) -> int:
        self.events.append(("volume", self.current))
        return self.current

    def fade(self, target: int, seconds: float) -> dict:
        start = self.current
        self.events.append(("fade", start, target, seconds))
        if self.fail:
            raise PlayoutError("injected fader failure")
        self.current = target
        return {"from_percent": start, "to_percent": target, "steps": [target]}


class LiveFaderTests(unittest.TestCase):
    def test_first_move_is_the_restore_point_until_explicit_restore(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            backend = FakeBackend(72)
            desk = LiveFader(state, backend=backend)

            first = desk.move(40, seconds=0.8)
            second = desk.move(25, seconds=0.4)
            restored = desk.restore(seconds=1.1)

            self.assertEqual(first["restore_percent"], 72)
            self.assertEqual(second["restore_percent"], 72)
            self.assertEqual(restored["to_percent"], 72)
            self.assertEqual(backend.current, 72)
            self.assertFalse((state / "control" / "fader.json").exists())

    def test_failed_move_keeps_a_repairable_restore_point(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            backend = FakeBackend(63, fail=True)
            desk = LiveFader(state, backend=backend)
            with self.assertRaisesRegex(PlayoutError, "injected"):
                desk.move(30, seconds=0.8)
            saved = json.loads((state / "control" / "fader.json").read_text())
            self.assertEqual(saved["restore_percent"], 63)

    def test_restore_without_takeover_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw, self.assertRaisesRegex(
            PlayoutError, "no manual fader restore point"
        ):
            LiveFader(Path(raw), backend=FakeBackend()).restore(seconds=1)

    def test_negative_travel_is_rejected_before_moving(self):
        with tempfile.TemporaryDirectory() as raw:
            backend = FakeBackend()
            with self.assertRaisesRegex(PlayoutError, "non-negative"):
                LiveFader(Path(raw), backend=backend).move(40, seconds=-1)
            self.assertEqual(backend.events, [])


if __name__ == "__main__":
    unittest.main()
