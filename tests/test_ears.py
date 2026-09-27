from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import ears
from audience_of_one.adapters.spotify import SpotifyError
from tests.test_local_mpv import FakeMPV

try:
    import numpy
except ImportError:
    numpy = None

SUMMARY = """
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -16.3 LUFS
    Threshold: -26.5 LUFS

  Loudness range:
    LRA:         5.1 LU

  True peak:
    Peak:       -1.2 dBFS
"""


def pcm(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


class KeyAndChordTests(unittest.TestCase):
    def test_a_key_profile_is_recognised_with_its_runner_ups(self):
        e_minor = ears._rotate(ears.MINOR, 4)
        key = ears.estimate_key(e_minor)
        self.assertEqual(key["estimate"], "E minor")
        self.assertAlmostEqual(key["correlation"], 1.0, places=3)
        self.assertEqual(len(key["candidates"]), 3)

    def test_relative_major_and_minor_are_flagged_when_nearly_tied(self):
        blend = [
            major + minor for major, minor in
            zip(ears._rotate(ears.MAJOR, 7), ears._rotate(ears.MINOR, 4))
        ]
        key = ears.estimate_key(blend)
        self.assertIn(key["estimate"], {"G major", "E minor"})
        self.assertTrue(key["relative_tie"])
        self.assertFalse(ears.estimate_key(ears._rotate(ears.MAJOR, 0))["relative_tie"])

    def test_the_final_chord_is_named_from_its_pitch_classes(self):
        chroma = [0.02] * 12
        for index, weight in ((4, 0.3), (7, 0.25), (11, 0.25)):
            chroma[index] = weight
        chord = ears.estimate_chord(chroma)
        self.assertEqual(chord["chord"], "Em")
        self.assertEqual(set(chord["pitch_classes"][:3]), {"E", "G", "B"})
        chroma = [0.02] * 12
        for index in (0, 4, 7):
            chroma[index] = 0.3
        self.assertEqual(ears.estimate_chord(chroma)["chord"], "C")


class EndingTests(unittest.TestCase):
    def test_a_record_that_stops_dead_is_a_cut(self):
        end = ears.ending([-20.0] * 100 + [-120.0] * 8)
        self.assertEqual(end["shape"], "cut")
        self.assertEqual(end["last_sound_at"], 25.0)
        self.assertEqual(end["silence_after_seconds"], 2.0)

    def test_a_short_decay_rings_and_a_long_one_fades(self):
        ring = [-20.0] * 80 + [-20.0 - 3 * step for step in range(1, 17)] + [-120.0] * 4
        fade = [-20.0] * 80 + [-20.0 - 0.7 * step for step in range(1, 61)] + [-120.0] * 4
        self.assertEqual(ears.ending(ring)["shape"], "ring")
        faded = ears.ending(fade)
        self.assertEqual(faded["shape"], "fade")
        self.assertLess(faded["winds_down_at"], faded["last_sound_at"] - 6)

    def test_silence_has_no_ending_to_describe(self):
        self.assertEqual(ears.ending([-120.0] * 20)["shape"], "silent")
        self.assertEqual(ears.ending([])["shape"], "silent")

    def test_pure_python_envelope_reports_rms_in_dbfs(self):
        levels = ears.envelope([0.5] * 8000, 8000)
        self.assertEqual(len(levels), 4)
        self.assertAlmostEqual(levels[0], 20 * math.log10(0.5), places=3)


class DecodeTests(unittest.TestCase):
    def test_loudness_is_read_from_the_ebur128_summary(self):
        self.assertEqual(ears.loudness("noise\n" + SUMMARY), {
            "integrated_lufs": -16.3, "range_lu": 5.1, "true_peak_dbfs": -1.2,
        })
        self.assertIsNone(ears.loudness("no summary"))

    def test_decode_measures_while_decoding_and_never_echoes_a_stream_url(self):
        url = "https://cdn.example/song.m4a?vkey=secret"
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, 1, b"", f"{url}: Server returned 403 Forbidden".encode()
            )

        with mock.patch("audience_of_one.ears.shutil.which", return_value="/bin/ffmpeg"), \
             self.assertRaises(ears.EarsError) as raised:
            ears.decode(url, 22050, run=run)
        self.assertNotIn("secret", str(raised.exception))
        self.assertIn("<record>", str(raised.exception))
        command = calls[0]
        self.assertIn("ebur128=peak=true", command[command.index("-af") + 1])
        self.assertEqual(command[command.index("-f") + 1], "f32le")

    def test_decode_requires_ffmpeg(self):
        with mock.patch("audience_of_one.ears.shutil.which", return_value=None), \
             self.assertRaisesRegex(ears.EarsError, "ffmpeg is required"):
            ears.decode("song.flac", 8000)


class AnalyzeAndListenTests(unittest.TestCase):
    @staticmethod
    def decoder(seconds_of_sound: float, rate_seen: list[int]):
        def decode(source, rate):
            rate_seen.append(rate)
            sound = [0.1 if index % 2 else -0.1 for index in range(int(rate * seconds_of_sound))]
            return pcm(sound + [0.0] * (rate * 2)), SUMMARY
        return decode

    def test_without_numpy_the_ending_and_loudness_are_still_measured(self):
        rates: list[int] = []
        result = ears.analyze(
            "song.flac", numpy_module=None, decoder=self.decoder(20.0, rates)
        )
        self.assertEqual(rates, [8000])
        self.assertEqual(result["duration_seconds"], 22.0)
        self.assertEqual(result["ending"]["shape"], "cut")
        self.assertEqual(result["ending"]["last_sound_at"], 20.0)
        self.assertEqual(result["loudness"]["integrated_lufs"], -16.3)
        self.assertIsNone(result["key"])
        self.assertIn("audience-of-one[ears]", result["key_unavailable"])

    def test_listen_measures_a_local_file_once_until_the_file_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root, state = Path(raw) / "records", Path(raw) / "state"
            (root / "Nujabes").mkdir(parents=True)
            record = root / "Nujabes" / "Counting Stars.mp3"
            record.write_bytes(b"ID3 first pressing")
            client = FakeMPV(root, state)
            sources: list[str] = []

            def analyzer(source):
                sources.append(source)
                return {"duration_seconds": 248.0, "key": {"estimate": "B minor"},
                        "ending": {"shape": "fade"}}

            first = ears.listen(state, "Nujabes/Counting Stars.mp3", client, analyzer=analyzer)
            self.assertFalse(first["cached"])
            self.assertEqual(first["uri"], "local:Nujabes/Counting Stars.mp3")
            self.assertEqual(sources, [str(record.resolve())])
            again = ears.listen(state, "local:Nujabes/Counting Stars.mp3", client,
                                analyzer=analyzer)
            self.assertTrue(again["cached"])
            self.assertEqual(len(sources), 1)
            cache = next((state / "ears").glob("*.json"))
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

            record.write_bytes(b"ID3 a remaster at the same path")
            remastered = ears.listen(state, "local:Nujabes/Counting Stars.mp3", client,
                                     analyzer=analyzer)
            self.assertFalse(remastered["cached"])
            self.assertEqual(len(sources), 2)
            record.unlink()
            with self.assertRaisesRegex(SpotifyError, "does not exist"):
                ears.listen(state, "local:Nujabes/Counting Stars.mp3", client,
                            analyzer=analyzer)

    def test_a_keyless_measurement_is_repeated_once_numpy_can_hear_keys(self):
        with tempfile.TemporaryDirectory() as raw:
            root, state = Path(raw) / "records", Path(raw) / "state"
            root.mkdir()
            (root / "Song.flac").write_bytes(b"fLaC")
            client = FakeMPV(root, state)
            results = [
                {"key": None, "key_unavailable": ears.NUMPY_HINT, "ending": {}},
                {"key": {"estimate": "E major"}, "ending": {}},
            ]
            analyzer = mock.Mock(side_effect=results)
            with mock.patch("audience_of_one.ears._numpy", return_value=None):
                ears.listen(state, "local:Song.flac", client, analyzer=analyzer)
                self.assertTrue(ears.listen(state, "local:Song.flac", client,
                                            analyzer=analyzer)["cached"])
            with mock.patch("audience_of_one.ears._numpy", return_value=object()):
                heard = ears.listen(state, "local:Song.flac", client, analyzer=analyzer)
            self.assertFalse(heard["cached"])
            self.assertEqual(heard["key"]["estimate"], "E major")
            self.assertEqual(analyzer.call_count, 2)

    def test_refresh_measures_again_and_qq_streams_are_decoded_but_not_stored(self):
        class Resolver:
            def resolve(self, value):
                return ("qqmusic:002nWUQ438fyh1",
                        {"name": "City Of Stars (Humming)", "artists": ["Emma Stone"]},
                        "https://cdn.example/humming.m4a?vkey=secret")

        with tempfile.TemporaryDirectory() as raw:
            root, state = Path(raw) / "records", Path(raw) / "state"
            root.mkdir()
            client = FakeMPV(root, state)
            client.stream_resolver = Resolver()
            sources: list[str] = []

            def analyzer(source):
                sources.append(source)
                return {"duration_seconds": 159.8, "key": None, "ending": {"shape": "ring"}}

            ears.listen(state, "qqmusic:002nWUQ438fyh1", client, analyzer=analyzer)
            result = ears.listen(state, "qqmusic:002nWUQ438fyh1", client,
                                 refresh=True, analyzer=analyzer)
            self.assertEqual(len(sources), 2)
            self.assertIn("vkey=secret", sources[0])
            self.assertEqual(result["name"], "City Of Stars (Humming) — Emma Stone")
            stored = next((state / "ears").glob("*.json")).read_text()
            self.assertNotIn("secret", stored)

    def test_spotify_audio_cannot_be_measured(self):
        class Spotify:
            def resolve(self, value):
                raise AssertionError("should refuse before resolving")

        with tempfile.TemporaryDirectory() as raw, \
             self.assertRaisesRegex(ears.EarsError, "Spotify audio cannot be decoded"):
            ears.listen(Path(raw), "spotify:track:one", Spotify())

    def test_an_expired_qq_stream_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "records"
            root.mkdir()
            with self.assertRaisesRegex(SpotifyError, "expired"):
                FakeMPV(root, Path(raw) / "state").analysis_source("qqmusic:MID")

    def test_describe_reads_like_a_console_note(self):
        text = ears.describe({
            "uri": "qqmusic:MID", "name": "City Of Stars (Humming) — Emma Stone",
            "cached": True,
            "key": {"estimate": "E minor", "correlation": 0.81, "relative_tie": False,
                    "candidates": [{"key": "E minor", "correlation": 0.81},
                                   {"key": "G major", "correlation": 0.65},
                                   {"key": "B minor", "correlation": 0.5}]},
            "ending": {"shape": "ring", "chord": "Em", "winds_down_at": 153.0,
                       "last_sound_at": 158.5, "silence_after_seconds": 1.25},
            "loudness": {"integrated_lufs": -16.3, "true_peak_dbfs": -1.2},
        })
        self.assertIn("E minor  r=0.81  (also G major 0.65, B minor 0.50)", text)
        self.assertIn("ring on Em · winds down 2:33 · last sound 2:38", text)
        self.assertIn("(cached)", text)


@unittest.skipUnless(numpy is not None, "needs numpy")
class SpectrumTests(unittest.TestCase):
    def test_chroma_of_an_a_minor_triad_names_am(self):
        rate = 22050
        t = numpy.arange(rate * 4) / rate
        chord = sum(numpy.sin(2 * numpy.pi * f * t) for f in (220.0, 261.63, 329.63)) / 3
        self.assertEqual(ears.estimate_chord(ears.chroma(numpy, chord, rate))["chord"], "Am")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_real_decode_of_a_fading_c_major_chord(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "chord.wav"
            tones = "+".join(f"sin(2*PI*{f}*t)" for f in (261.63, 329.63, 392.0))
            subprocess.run([
                "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                f"aevalsrc=0.2*({tones}):s=44100:d=30",
                "-af", "afade=t=out:st=14:d=14", str(path),
            ], check=True)
            result = ears.analyze(str(path))
            self.assertEqual(result["ending"]["chord"], "C")
            self.assertEqual(result["ending"]["shape"], "fade")
            self.assertIsNotNone(result["loudness"]["integrated_lufs"])


if __name__ == "__main__":
    unittest.main()
