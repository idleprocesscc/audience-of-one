"""Station ears: measure the record that will actually play.

A host who cannot hear the room still needs to know what it is walking into: the
key an echo must answer, the chord a record leaves hanging, whether it fades or
stops dead, and when its last sound really ends. Catalog metadata and lookups
describe *a* version of a song; this module decodes the exact local file or
account-authorized stream the station would play and measures it.

Loudness and the shape of the ending need only ffmpeg. Key and chord estimates
need numpy for the spectrum (``pip install 'audience-of-one[ears]'``); they are
estimates, and relative major/minor keys often score almost the same.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Any

NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
# Krumhansl-Kessler key profiles, tonic first.
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
STEP_SECONDS = 0.25
ENDING_WINDOW_SECONDS = 5.0
NUMPY_HINT = "install numpy for key and chord: pip install 'audience-of-one[ears]'"


class EarsError(RuntimeError):
    pass


def _numpy():
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def _correlation(a: list[float], b: list[float]) -> float:
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    da = [value - mean_a for value in a]
    db = [value - mean_b for value in b]
    denominator = math.sqrt(sum(v * v for v in da) * sum(v * v for v in db))
    return sum(x * y for x, y in zip(da, db)) / denominator if denominator else 0.0


def _rotate(profile: list[float], tonic: int) -> list[float]:
    return [profile[(index - tonic) % 12] for index in range(12)]


def estimate_key(chroma: list[float]) -> dict[str, Any]:
    scores = []
    for tonic in range(12):
        scores.append((_correlation(chroma, _rotate(MAJOR, tonic)), tonic, "major"))
        scores.append((_correlation(chroma, _rotate(MINOR, tonic)), tonic, "minor"))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    relative = {
        best[2], second[2],
    } == {"major", "minor"} and (
        (second[1] - best[1]) % 12 == (9 if best[2] == "major" else 3)
    )
    return {
        "estimate": f"{NAMES[best[1]]} {best[2]}",
        "correlation": round(best[0], 3),
        "candidates": [
            {"key": f"{NAMES[tonic]} {mode}", "correlation": round(score, 3)}
            for score, tonic, mode in scores[:3]
        ],
        "relative_tie": bool(relative and best[0] - second[0] < 0.05),
    }


def estimate_chord(chroma: list[float]) -> dict[str, Any]:
    best = None
    for root in range(12):
        for quality, third in (("", 4), ("m", 3)):
            template = [0.0] * 12
            for interval in (0, third, 7):
                template[(root + interval) % 12] = 1.0
            score = _correlation(chroma, template)
            if best is None or score > best[0]:
                best = (score, f"{NAMES[root]}{quality}")
    loudest = sorted(range(12), key=lambda index: chroma[index], reverse=True)[:4]
    return {
        "chord": best[1],
        "correlation": round(best[0], 3),
        "pitch_classes": [NAMES[index] for index in loudest],
    }


def envelope(samples: Any, rate: int, numpy_module: Any = None) -> list[float]:
    """RMS level in dBFS for each quarter second."""
    size = int(rate * STEP_SECONDS)
    count = len(samples) // size
    if count == 0:
        return []
    if numpy_module is not None:
        np = numpy_module
        blocks = np.asarray(samples[:count * size], dtype=np.float64).reshape(count, size)
        rms = np.sqrt((blocks ** 2).mean(axis=1))
        return [float(value) for value in 20 * np.log10(rms + 1e-9)]
    levels = []
    for start in range(0, count * size, size):
        mean = math.fsum(value * value for value in samples[start:start + size]) / size
        levels.append(20 * math.log10(math.sqrt(mean) + 1e-9))
    return levels


def ending(levels: list[float]) -> dict[str, Any]:
    """Describe how a record ends: cut, ring, or fade, and when sound really stops."""
    if not levels:
        return {"shape": "silent"}
    middle = levels[len(levels) // 5: len(levels) * 4 // 5] or levels
    body = statistics.median(middle)
    floor = max(body - 40.0, -70.0)
    audible = [index for index, level in enumerate(levels) if level > floor]
    if not audible:
        return {"shape": "silent", "body_rms_dbfs": round(body, 1)}
    last = audible[-1]
    loud = [index for index in range(last + 1) if levels[index] >= body - 6.0]
    winding = loud[-1] if loud else last
    tail = (last - winding) * STEP_SECONDS
    # A linear fade spends half its length in the first 6 dB, so a 12-15 s
    # fade-out measures only 6-7 s from there; a struck final chord rings shorter.
    shape = "cut" if tail < 1.5 else "fade" if tail > 6.0 else "ring"
    return {
        "shape": shape,
        "body_rms_dbfs": round(body, 1),
        "winds_down_at": round((winding + 1) * STEP_SECONDS, 2),
        "last_sound_at": round((last + 1) * STEP_SECONDS, 2),
        "silence_after_seconds": round((len(levels) - last - 1) * STEP_SECONDS, 2),
    }


def chroma(np: Any, samples: Any, rate: int, start: float = 0.0,
           end: float | None = None) -> list[float] | None:
    """Pitch-class profile of a span, from 80 Hz to 2 kHz."""
    first = int(max(0.0, start) * rate)
    last = len(samples) if end is None else int(end * rate)
    x = np.asarray(samples[first:last], dtype=np.float64)
    frame, hop = 8192, 4096
    if len(x) < frame:
        return None
    frequencies = np.fft.rfftfreq(frame, 1 / rate)
    band = (frequencies >= 80) & (frequencies <= 2000)
    classes = np.rint(12 * np.log2(frequencies[band] / 440.0) + 69).astype(int) % 12
    window = np.hanning(frame)
    starts = np.arange(0, len(x) - frame + 1, hop)
    total = np.zeros(12)
    for offset in range(0, len(starts), 128):
        frames = np.stack([x[index:index + frame] for index in starts[offset:offset + 128]])
        magnitudes = np.abs(np.fft.rfft(frames * window, axis=1))[:, band]
        total += np.bincount(classes, weights=magnitudes.sum(axis=0), minlength=12)
    energy = float(total.sum())
    return [float(value) / energy for value in total] if energy > 0 else None


def loudness(stderr: str) -> dict[str, float] | None:
    marker = stderr.rfind("Summary:")
    if marker < 0:
        return None
    summary = stderr[marker:]
    fields = {
        "integrated_lufs": r"I:\s+(-?[\d.]+) LUFS",
        "range_lu": r"LRA:\s+(-?[\d.]+) LU",
        "true_peak_dbfs": r"Peak:\s+(-?[\d.]+) dBFS",
    }
    result = {}
    for key, pattern in fields.items():
        match = re.search(pattern, summary)
        if match:
            result[key] = float(match.group(1))
    return result or None


def decode(source: str, rate: int, *,
           run: Callable[..., Any] = subprocess.run) -> tuple[bytes, str]:
    """Decode to mono float PCM while ffmpeg measures loudness on the way through."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EarsError("ffmpeg is required for station ears")
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", source, "-vn",
        "-af", f"ebur128=peak=true,aresample={rate},"
               "aformat=sample_fmts=flt:channel_layouts=mono",
        "-f", "f32le", "-",
    ]
    try:
        result = run(command, capture_output=True, timeout=300, check=False)
    except subprocess.TimeoutExpired as error:
        raise EarsError("decoding the record timed out") from error
    # A stream URL is an expiring credential: never echo it back in an error.
    stderr = result.stderr.decode("utf-8", "replace").replace(source, "<record>")
    if result.returncode != 0 or not result.stdout:
        lines = stderr.strip().splitlines()
        raise EarsError(f"cannot decode the record: {lines[-1] if lines else 'no audio'}")
    return result.stdout, stderr


def _samples(data: bytes, np: Any) -> Any:
    if np is not None:
        return np.frombuffer(data, dtype="<f4")
    samples = array("f")
    samples.frombytes(data[: len(data) - len(data) % 4])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


_AUTO = object()


def analyze(source: str, *, numpy_module: Any = _AUTO,
            decoder: Callable[[str, int], tuple[bytes, str]] = decode) -> dict[str, Any]:
    np = _numpy() if numpy_module is _AUTO else numpy_module
    rate = 22050 if np is not None else 8000
    data, stderr = decoder(source, rate)
    samples = _samples(data, np)
    end = ending(envelope(samples, rate, np))
    result: dict[str, Any] = {
        "duration_seconds": round(len(samples) / rate, 2),
        "loudness": loudness(stderr),
        "ending": end,
    }
    if np is None:
        result["key"] = None
        result["key_unavailable"] = NUMPY_HINT
        return result
    whole = chroma(np, samples, rate)
    result["key"] = estimate_key(whole) if whole else None
    if end.get("last_sound_at"):
        finish = end["last_sound_at"]
        tail = chroma(np, samples, rate, finish - ENDING_WINDOW_SECONDS, finish)
        if tail:
            end.update(estimate_chord(tail))
    return result


def _cache_path(state_path: Path, uri: str) -> Path:
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:24]
    return state_path / "ears" / f"{digest}.json"


def _load_cached(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _store(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(source: str, uri: str) -> dict[str, int] | None:
    """A local file can be replaced at the same path; a remaster is a different record."""
    if not uri.startswith("local:"):
        return None
    stat = Path(source).stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _usable(cached: dict | None, fingerprint: dict | None) -> bool:
    if not cached:
        return False
    if cached.get("key") is None and cached.get("key_unavailable") and _numpy() is not None:
        return False
    return fingerprint is None or cached.get("fingerprint") == fingerprint


def listen(state_path: Path, value: str, client: Any, *, refresh: bool = False,
           analyzer: Callable[[str], dict[str, Any]] = analyze) -> dict[str, Any]:
    """Measure one record from the configured shelf, reusing an earlier measurement."""
    source_of = getattr(client, "analysis_source", None)
    if not callable(source_of):
        raise EarsError(
            "station ears needs the local or QQ Music record box; "
            "Spotify audio cannot be decoded"
        )
    raw = value.strip()
    # An exact QQ Music id can be answered without asking the service for a new stream.
    if not refresh and raw.startswith("qqmusic:"):
        cached = _load_cached(_cache_path(state_path, raw))
        if _usable(cached, None):
            return {**cached, "cached": True}
    uri, evidence = client.resolve(raw)
    source = source_of(uri)
    fingerprint = _fingerprint(source, uri)
    path = _cache_path(state_path, uri)
    if not refresh:
        cached = _load_cached(path)
        if _usable(cached, fingerprint):
            return {**cached, "cached": True}
    analysis = analyzer(source)
    artists = evidence.get("artists") or []
    name = evidence.get("name") or evidence.get("relative_path") or uri
    record = {
        "version": 1,
        "uri": uri,
        "name": f"{name} — {', '.join(artists)}" if artists else name,
        "analyzed_at": time.time(),
        **analysis,
    }
    if fingerprint is not None:
        record["fingerprint"] = fingerprint
    _store(path, record)
    return {**record, "cached": False}


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def describe(record: dict[str, Any]) -> str:
    lines = [f"{record.get('name') or record.get('uri')} · {record.get('uri')}"
             + (" (cached)" if record.get("cached") else "")]
    key = record.get("key")
    if key:
        others = ", ".join(
            f"{candidate['key']} {candidate['correlation']:.2f}"
            for candidate in key["candidates"][1:]
        )
        tie = " · relative major/minor nearly tied" if key.get("relative_tie") else ""
        lines.append(f"  key       {key['estimate']}  r={key['correlation']:.2f}  "
                     f"(also {others}){tie}")
    else:
        lines.append(f"  key       — ({record.get('key_unavailable') or 'not measured'})")
    end = record.get("ending") or {}
    if end.get("shape") == "silent":
        lines.append("  ends      silent")
    else:
        chord = f" on {end['chord']}" if end.get("chord") else ""
        lines.append(
            f"  ends      {end.get('shape')}{chord} · winds down {_clock(end.get('winds_down_at'))}"
            f" · last sound {_clock(end.get('last_sound_at'))}"
            f" · {end.get('silence_after_seconds', 0):.1f} s silence after"
        )
    level = record.get("loudness") or {}
    if level:
        lines.append(
            f"  loudness  {level.get('integrated_lufs', float('nan')):.1f} LUFS"
            f" · true peak {level.get('true_peak_dbfs', float('nan')):.1f} dBFS"
        )
    return "\n".join(lines)
