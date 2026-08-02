from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from audience_of_one import rundown
from audience_of_one.scheduler import StationScheduler
from audience_of_one.transactions import Journal


class FakeMusic:
    def __init__(self, snapshot=None):
        self.current = snapshot
        self.events: list[str] = []

    def snapshot(self):
        return self.current

    def set_repeat_on_device(self, mode, device):
        self.events.append(f"repeat:{mode}")
        return {"accepted": True, "mode": mode, "device": device}

    def pause(self, device):
        self.events.append("pause")
        return {"accepted": True, "device": device}


class FakeEngine:
    def __init__(self, state: Path, events: list[str], *, duration=5.0):
        self.state = state
        self.events = events
        self.duration = duration
        self.journal = Journal(state)

    def prepare_boundary(self, item):
        self.journal.begin(item["filename"])
        self.journal.set_state(item["filename"], "ready")
        self.events.append(f"prepare:{item['id']}")
        return dict(item["data"]), {"duration": self.duration}

    def fire_boundary(self, item, programme, prepared):
        self.journal.set_state(item["filename"], "firing")
        if programme.get("track"):
            self.journal.receipt(item["filename"], "track", {
                "confirmed": True,
                "uri": programme["track"],
                "playback": {
                    "uri": programme["track"],
                    "device": {"id": "device", "name": "Listener"},
                },
            })
        result = self.journal.set_state(item["filename"], "played")
        rundown.archive_played(self.state, item["filename"])
        self.events.append(f"fire:{item['id']}")
        return result


def playing(uri: str, *, progress=0, duration=100_000):
    return {
        "uri": uri,
        "is_playing": True,
        "progress_ms": progress,
        "duration_ms": duration,
        "device": {"id": "device", "name": "Listener"},
    }


class SchedulerTests(unittest.TestCase):
    def scheduler(self, state, music, events, *, duration=5.0):
        return StationScheduler(
            {"scheduler": {
                "poll_seconds": 1,
                "prepare_window_seconds": 75,
                "boundary_lead_seconds": 1,
            }},
            state,
            music=music,
            engine=FakeEngine(state, events, duration=duration),
            sleep=lambda _seconds: None,
        )

    def test_cold_start_consumes_first_programme(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, track="spotify:track:first")
            events: list[str] = []
            scheduler = self.scheduler(state, FakeMusic(), events)
            self.assertEqual(scheduler.tick(), f"played:{item['id']}")
            self.assertEqual(events, [f"prepare:{item['id']}", f"fire:{item['id']}"])
            self.assertFalse((state / "queue" / item["filename"]).exists())

    def test_tail_prepares_early_and_fires_inside_voice_window(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(
                state, track="spotify:track:next", say="Coming up.", transition="tail"
            )
            music = FakeMusic(playing("spotify:track:old", progress=40_000))
            events: list[str] = []
            scheduler = self.scheduler(state, music, events, duration=5.0)
            self.assertEqual(scheduler.tick(), f"waiting:{item['id']}")
            self.assertEqual(events, [f"prepare:{item['id']}"])
            music.current = playing("spotify:track:old", progress=94_000)
            self.assertEqual(scheduler.tick(), f"played:{item['id']}")
            self.assertEqual(events[-1], f"fire:{item['id']}")

    def test_autoplay_change_yields_immediately_to_queued_programme(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, track="spotify:track:planned")
            music = FakeMusic(playing("spotify:track:owned", progress=1_000))
            events: list[str] = []
            scheduler = self.scheduler(state, music, events)
            self.assertEqual(scheduler.tick(), f"waiting:{item['id']}")
            music.current = playing("spotify:track:autoplay", progress=500)
            self.assertEqual(scheduler.tick(), f"played:{item['id']}")

    def test_failed_head_does_not_block_the_next_item(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            failed = rundown.append(state, track="spotify:track:failed")
            Journal(state).set_state(failed["filename"], "failed", "injected")
            following = rundown.append(state, track="spotify:track:following")
            events: list[str] = []
            scheduler = self.scheduler(state, FakeMusic(), events)
            self.assertEqual(scheduler.tick(), f"played:{following['id']}")

    def test_new_queue_clears_a_repeat_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            first = rundown.append(
                state, track="spotify:track:first", after="repeat"
            )
            music = FakeMusic()
            events: list[str] = []
            scheduler = self.scheduler(state, music, events)
            scheduler.tick()
            self.assertIn("repeat:track", music.events)
            rundown.append(state, track="spotify:track:second")
            music.current = playing(first["data"]["track"], progress=20_000)
            scheduler.tick()
            self.assertIn("repeat:off", music.events)

    def test_stop_policy_pauses_the_backend_without_killing_scheduler(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            rundown.append(
                state, track="spotify:track:last", after="stop"
            )
            music = FakeMusic()
            events: list[str] = []
            scheduler = self.scheduler(state, music, events)
            scheduler.tick()
            music.current = playing("spotify:track:last", progress=99_500)
            self.assertEqual(scheduler.tick(), "after:stopped")
            self.assertIn("pause", music.events)

    def test_stop_policy_records_a_natural_local_player_end(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            rundown.append(state, track="spotify:track:last", after="stop")
            music = FakeMusic()
            events: list[str] = []
            scheduler = self.scheduler(state, music, events)
            scheduler.tick()
            music.current = playing("spotify:track:last", progress=98_000)
            scheduler.tick()
            music.current = None
            self.assertEqual(scheduler.tick(), "after:stopped-natural")
            self.assertEqual(scheduler.tick(), "after:stopped-natural")


if __name__ == "__main__":
    unittest.main()
