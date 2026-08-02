"""Receipt-driven desktop playout primitives for macOS."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import rundown, tts, wildcards
from .adapters import music_client
from .adapters.phone import (
    PhoneError,
    PhoneUncertainError,
    phone_music_player,
    phone_voice_player,
)
from .adapters.spotify import SpotifyError
from .transactions import Journal, TransactionError


class PlayoutError(RuntimeError):
    pass


class MacSpotifyFader:
    """Move only Spotify's local app volume, leaving system volume untouched."""

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep):
        self.sleep = sleep
        self.executable = shutil.which("osascript")
        if not self.executable:
            raise PlayoutError("desktop playout needs osascript on PATH")

    def _run(self, body: str) -> str:
        script = f"with timeout of 3 seconds\n{body}\nend timeout"
        last = ""
        for attempt in range(2):
            try:
                result = subprocess.run(
                    [self.executable, "-e", script], capture_output=True, text=True,
                    timeout=5, check=False,
                )
            except subprocess.TimeoutExpired:
                last = "timed out after 5 seconds"
                if attempt == 0:
                    self.sleep(0.5)
                    continue
                raise PlayoutError(f"Spotify AppleScript failed: {last}") from None
            if result.returncode == 0:
                return result.stdout.strip()
            last = result.stderr.strip()
            if attempt == 0:
                self.sleep(0.5)
        raise PlayoutError(f"Spotify AppleScript failed: {last or 'unknown error'}")

    def volume(self) -> int:
        raw = self._run('tell application "Spotify" to get sound volume')
        try:
            value = int(raw)
        except ValueError as error:
            raise PlayoutError(f"Spotify returned invalid volume: {raw!r}") from error
        if not 0 <= value <= 100:
            raise PlayoutError(f"Spotify returned invalid volume: {value}")
        return value

    @staticmethod
    def local_computer_name() -> str:
        executable = shutil.which("scutil") or "/usr/sbin/scutil"
        try:
            result = subprocess.run(
                [executable, "--get", "ComputerName"], capture_output=True,
                text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PlayoutError(f"cannot read the local macOS ComputerName: {error}") from error
        name = result.stdout.strip()
        if result.returncode != 0 or not name:
            raise PlayoutError("cannot read the local macOS ComputerName with scutil")
        return name

    def fade(self, target: int, seconds: float, *, hz: float = 6.0) -> dict:
        if not 0 <= target <= 100:
            raise PlayoutError("fader target must be from 0 to 100")
        start = self.volume()
        seconds = max(0.0, float(seconds))
        steps = max(1, min(24, round(seconds * hz)))
        delay = seconds / steps
        values = []
        for step in range(1, steps + 1):
            value = round(start + (target - start) * step / steps)
            self._run(f'tell application "Spotify" to set sound volume to {value}')
            values.append(value)
            if step < steps and delay:
                self.sleep(delay)
        self.sleep(0.1)
        actual = self.volume()
        if actual != target:
            raise PlayoutError(
                f"Spotify fader target did not stick: wanted {target}, got {actual}; "
                "the current Connect target may not expose volume to AppleScript"
            )
        return {
            "from_percent": start,
            "to_percent": actual,
            "steps": values,
            "verified": True,
        }


class MacVoicePlayer:
    def __init__(self, desktop: dict[str, Any], *, sleep: Callable[[float], None] = time.sleep):
        self.desktop = desktop
        self.sleep = sleep
        self.executable = shutil.which("afplay")
        if not self.executable:
            raise PlayoutError("desktop playout needs afplay on PATH")

    def play(self, path: Path, duration: float) -> dict:
        started_request_at = time.time()
        process = subprocess.Popen(
            [self.executable, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        settle = float(self.desktop.get("voice_start_settle_seconds", 0.12))
        self.sleep(settle)
        status = process.poll()
        if status is not None:
            detail = (process.stderr.read() if process.stderr else "").strip()
            raise PlayoutError(
                f"voice player exited before audible start (exit={status}): {detail}"
            )
        started_at = time.time()
        timeout = max(10.0, duration + float(
            self.desktop.get("voice_timeout_margin_seconds", 10.0)
        ))
        try:
            _, detail = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            raise PlayoutError(f"voice playback timed out after {timeout:.1f}s") from error
        if process.returncode != 0:
            raise PlayoutError(
                f"voice playback failed (exit={process.returncode}): {(detail or '').strip()}"
            )
        return {
            "player": "afplay",
            "pid": process.pid,
            "requested_at": started_request_at,
            "started_at": started_at,
            "finished_at": time.time(),
            "duration_seconds": duration,
        }


class DesktopEngine:
    """Execute one direct desktop item and publish only evidenced success."""

    def __init__(
        self,
        config: dict[str, Any],
        state_path: Path,
        *,
        spotify: Any | None = None,
        synthesize: Callable[[dict[str, Any], str, Path], str] = tts.synthesize,
        verify_audio: Callable[[Path], float] = tts.verify_audio,
        voice_player: Any | None = None,
        phone_player: Any | None = None,
        phone_music: Any | None = None,
        fader: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.state_path = state_path
        self.desktop = config.get("desktop") or {}
        self.music = spotify or music_client(config, state_path)
        self.synthesize = synthesize
        self.verify_audio = verify_audio
        self.voice_player = voice_player
        self.phone_player = phone_player
        self.phone_music = phone_music
        self.fader = fader
        self.sleep = sleep
        self.journal = Journal(state_path)

    @contextmanager
    def _claim(self):
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_path / "desktop-playout.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PlayoutError("another desktop playout owns the station lock") from error
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()}\n")
                handle.flush()
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _prepare(self, filename: str, programme: dict) -> dict:
        prepared: dict[str, Any] = {}
        phone_output = programme.get("output") == "phone"
        if phone_output:
            android = self.config.get("android") or {}
            if not android.get("enabled"):
                raise PlayoutError("phone output requires android.enabled = true")
            prepared["phone"] = True
        backend = (self.config.get("music") or {}).get("backend", "spotify")
        configured_device = (
            self.config["spotify"].get("device") if backend == "spotify"
            else (self.config.get("music") or {}).get("device")
        )
        device_spec = programme.get("device") or configured_device or None
        if programme.get("track"):
            phone_local_music = phone_output and backend == "local"
            if phone_local_music:
                android = self.config.get("android") or {}
                receiver = str(android.get("device") or "Android Phone").strip()
                requested = str(programme.get("device") or receiver).strip()
                if requested.casefold() in {"local_mac", "current"}:
                    raise PlayoutError(
                        "phone QQ Music needs the Android receiver, not local_mac"
                    )
                if android.get("device") and requested.casefold() != receiver.casefold():
                    raise PlayoutError(
                        f"phone music target {requested!r} does not match "
                        f"android.device {receiver!r}"
                    )
                device = {
                    "id": receiver,
                    "name": receiver,
                    "type": "Smartphone",
                    "is_active": True,
                    "selectable": False,
                    "routing_proof": "authenticated_phone_receiver",
                }
            else:
                device = self.music.ready_device(device_spec)
            if programme.get("say"):
                if not phone_local_music and (not device_spec or (
                    device_spec != device.get("id")
                    and str(device_spec).casefold()
                    != str(device.get("name") or "").casefold()
                )):
                    raise PlayoutError(
                        "combined desktop playout requires the exact target device name or id"
                    )
                if phone_output:
                    if not phone_local_music:
                        receiver = str(
                            (self.config.get("android") or {}).get("device") or ""
                        ).strip()
                        if not receiver:
                            raise PlayoutError(
                                "combined phone playout requires android.device to name "
                                "the same Spotify Connect target"
                            )
                        if receiver != device.get("id") and receiver.casefold() \
                                != str(device.get("name") or "").casefold():
                            raise PlayoutError(
                                f"phone receiver {receiver!r} does not match Spotify target "
                                f"{device.get('name')!r}"
                            )
                else:
                    if str(device.get("type") or "").casefold() != "computer":
                        raise PlayoutError(
                            "combined local voice playout requires a Spotify Computer device"
                        )
                    factory = getattr(self.music, "fader", None)
                    fader = self.fader or (
                        factory() if callable(factory) else MacSpotifyFader(sleep=self.sleep)
                    )
                    local_name = str(self.desktop.get("local_device_name") or "").strip() \
                        or fader.local_computer_name()
                    if str(device.get("name") or "").casefold() != local_name.casefold():
                        raise PlayoutError(
                            f"combined target {device.get('name')!r} is not this Mac "
                            f"({local_name!r})"
                        )
                    prepared["fader"] = fader
            uri, resolved = self.music.resolve(programme["track"])
            supports_uri = getattr(self.music, "supports_track_uri", None)
            valid_uri = (
                bool(supports_uri(uri)) if callable(supports_uri)
                else uri.startswith("spotify:track:")
            )
            if not valid_uri:
                raise PlayoutError(
                    "station open needs one exact track; playlists belong in a rundown"
                )
            prepared["device"] = device
            prepared["uri"] = uri
            if phone_local_music:
                export = getattr(self.music, "phone_stream", None)
                if not callable(export):
                    raise PlayoutError("local backend cannot export a phone stream")
                prepared["phone_stream"] = export(uri)
            self.journal.receipt(filename, "track", {
                "prepared": True,
                "requested": programme["track"],
                "uri": uri,
                "resolved": resolved,
                "device": device,
            })
        if programme.get("say"):
            audio = self.state_path / "prepared" / f"{Path(filename).stem}.mp3"
            provider = self.synthesize(self.config, programme["say"], audio)
            duration = self.verify_audio(audio)
            prepared["audio"] = audio
            prepared["duration"] = duration
            self.journal.receipt(filename, "voice", {
                "prepared": True,
                "provider": provider,
                "audio": str(audio),
                "duration_seconds": duration,
            })
        return prepared

    def _start_track(self, filename: str, prepared: dict) -> dict:
        device = prepared["device"]
        uri = prepared["uri"]
        if prepared.get("phone_stream"):
            transaction = self.journal.load(filename) or {}
            remote_id = str(transaction.get("remote_id") or Path(filename).stem)
            player = self.phone_music or phone_music_player(self.config)
            receipt = player.play(prepared["phone_stream"], remote_id=remote_id)
            playback = {**receipt.get("playback", {}), "device": device}
            bed = {**receipt.get("music_bed", {}), "device": device}
            result = {
                **receipt,
                "started": True,
                "confirmed": True,
                "confirmed_at": time.time(),
                "playback": playback,
                "music_bed": bed,
                "repeat_state": playback.get("repeat_state"),
                "device": device,
            }
            self.journal.receipt(filename, "track", result)
            return result
        accepted = self.music.play_uri(uri, device)
        try:
            playback = self.music.wait_for_playback(uri, device["id"], timeout=15)
        except SpotifyError:
            self._probe_failed_track_stage(filename, uri, device, "identity")
            raise
        self.journal.receipt(filename, "track", {
            **accepted,
            "started": True,
            "identity_confirmed_at": time.time(),
            "playback": playback,
        })
        self.music.set_repeat_on_device("off", device)
        try:
            repeat = self.music.wait_for_repeat(
                "off", device["id"], timeout=5, expected_uri=uri
            )
        except SpotifyError:
            self._probe_failed_track_stage(filename, uri, device, "repeat")
            raise
        bed = self.music.music_bed_probe(
            window=float(getattr(self.music, "stability_window_seconds", 1.2)),
            min_advance_ms=400,
            expected_uri=uri, expected_device_id=device["id"],
        )
        self.journal.receipt(filename, "track", {"music_bed": bed})
        if not bed.get("confirmed"):
            self._record_supersession(filename, uri, device, bed)
            raise PlayoutError(f"playback stability failed: {bed.get('reason')}")
        receipt = {
            **accepted,
            "confirmed": True,
            "confirmed_at": time.time(),
            "playback": playback,
            "repeat_state": repeat.get("repeat_state"),
            "music_bed": bed,
        }
        self.journal.receipt(filename, "track", receipt)
        return receipt

    def _probe_failed_track_stage(
        self,
        filename: str,
        requested_uri: str,
        device: dict,
        stage: str,
    ) -> None:
        bed = self.music.music_bed_probe(
            window=float(getattr(self.music, "stability_window_seconds", 1.2)),
            min_advance_ms=400,
            expected_uri=requested_uri,
            expected_device_id=device["id"],
        )
        self.journal.receipt(filename, "track", {
            "failed_stage": stage,
            "music_bed": bed,
        })
        if not bed.get("confirmed"):
            self._record_supersession(filename, requested_uri, device, bed)

    def _record_supersession(
        self,
        filename: str,
        requested_uri: str,
        device: dict,
        bed: dict,
    ) -> None:
        if bed.get("reason") not in {"unexpected_track", "track_changed"}:
            return
        actual = bed.get("actual_track")
        if not isinstance(actual, dict):
            return
        actual_uri = actual.get("uri")
        if not isinstance(actual_uri, str) or not actual_uri.startswith("spotify:track:") \
                or actual_uri == requested_uri:
            return
        superseded = {
            "requested_uri": requested_uri,
            "actual": actual,
            "detected_at": time.time(),
            "stable": False,
        }
        self.journal.receipt(filename, "track", {"superseded": superseded})
        actual_bed = self.music.music_bed_probe(
            window=float(getattr(self.music, "stability_window_seconds", 1.2)),
            min_advance_ms=400,
            expected_uri=actual_uri,
            expected_device_id=device["id"],
        )
        superseded.update({
            "stable": bool(actual_bed.get("confirmed")),
            "actual_music_bed": actual_bed,
        })
        self.journal.receipt(filename, "track", {"superseded": superseded})
        if not actual_bed.get("confirmed"):
            return
        improv = self.config.get("improv")
        if not isinstance(improv, dict) or not improv.get("enabled"):
            return
        transaction = self.journal.load(filename) or {}
        attempt_id = str(transaction.get("attempt_id") or Path(filename).stem)
        try:
            decision = wildcards.choose(
                self.config,
                self.state_path,
                attempt_id,
                previous_id=wildcards.last_played_id(self.state_path),
            )
        except wildcards.WildcardError as error:
            decision = {
                "selected": False,
                "played": False,
                "reason": "liner_bank_unavailable",
                "detail": str(error),
            }
        decision.update({
            "requested_uri": requested_uri,
            "actual_track": actual,
        })
        if decision.get("selected"):
            android = self.config.get("android") or {}
            if not android.get("enabled"):
                decision["delivery_status"] = "android_disabled"
            else:
                try:
                    player = self.phone_player or phone_voice_player(self.config)
                    remote_id = f"wildcard-{attempt_id}"
                    delivery = player.play(
                        Path(decision["audio"]),
                        float(decision.get("duration_seconds") or 0),
                        remote_id=remote_id,
                    )
                    if not isinstance(delivery, dict):
                        raise PhoneError("phone voice player returned an invalid receipt")
                    decision["delivery"] = delivery
                    decision["played"] = bool(delivery.get("voice_confirmed"))
                    if decision["played"] and delivery.get("duck_confirmed") \
                            and delivery.get("ack_order_valid"):
                        decision["delivery_status"] = "played_with_duck"
                    elif decision["played"]:
                        decision["delivery_status"] = "played_without_confirmed_duck"
                    else:
                        decision["delivery_status"] = "unconfirmed"
                    if decision["played"]:
                        try:
                            wildcards.mark_played(
                                self.state_path, decision["liner_id"], remote_id
                            )
                            decision["repeat_guard_saved"] = True
                        except OSError as error:
                            decision["repeat_guard_saved"] = False
                            decision["repeat_guard_error"] = str(error)
                except (PhoneError, TypeError, ValueError) as error:
                    decision["played"] = False
                    decision["delivery_status"] = "failed"
                    decision["delivery_error"] = str(error)
        self.journal.receipt(filename, "improv", {"wildcard_liner": decision})

    def _play_voice(self, filename: str, prepared: dict, *, duck: bool) -> dict:
        if prepared.get("phone"):
            player = self.phone_player or phone_voice_player(self.config)
            transaction = self.journal.load(filename) or {}
            remote_id = str(transaction.get("remote_id") or Path(filename).stem)
            try:
                receipt = player.play(
                    prepared["audio"], prepared["duration"],
                    remote_id=remote_id, duck=duck,
                )
            except PhoneUncertainError as error:
                self.journal.receipt(filename, "voice", {
                    "played": False,
                    "player": "android_receiver",
                    "remote_id": remote_id,
                    "delivery_uncertain": True,
                    "events": error.receipts,
                })
                raise
            if not isinstance(receipt, dict) or not receipt.get("voice_confirmed"):
                raise PhoneError("phone voice did not return a voice_finished receipt")
            self.journal.receipt(filename, "voice", {
                "played": True,
                "player": "android_receiver",
                "duration_seconds": prepared["duration"],
                "remote_id": remote_id,
                "duck_requested": bool(duck),
                "duck_confirmed": bool(receipt.get("duck_confirmed")),
                "ack_order_valid": bool(receipt.get("ack_order_valid")),
                "delivery": receipt,
            })
            return receipt
        player = self.voice_player or MacVoicePlayer(self.desktop, sleep=self.sleep)
        restore = None
        fader = None
        duck_receipt = None
        try:
            if duck:
                fader = prepared.get("fader") or self.fader or MacSpotifyFader(sleep=self.sleep)
                restore = fader.volume()
                target = min(restore, int(self.desktop.get("duck_percent", 35)))
                duck_receipt = fader.fade(
                    target, float(self.desktop.get("fade_down_seconds", 0.8))
                )
                self.journal.receipt(filename, "voice", {"duck": duck_receipt})
            receipt = player.play(prepared["audio"], prepared["duration"])
            self.journal.receipt(filename, "voice", {"played": True, **receipt})
            return receipt
        finally:
            if restore is not None and fader is not None:
                restored = fader.fade(
                    restore, float(self.desktop.get("fade_up_seconds", 1.0))
                )
                self.journal.receipt(filename, "voice", {"restore": restored})

    def _recovery_cover(self, filename: str, *, phone: bool = False) -> dict | None:
        cover_dir = self.state_path / "covers"
        cover = cover_dir / "current" / "cover-1.mp3"
        if not cover.is_file():
            cover = cover_dir / "cover-1.mp3"  # compatibility with early previews
        if not cover.is_file():
            self.journal.receipt(filename, "recovery", {
                "played": False, "reason": "cover-1.mp3 is not built",
            })
            return None
        try:
            duration = self.verify_audio(cover)
            if phone:
                player = self.phone_player or phone_voice_player(self.config)
                transaction = self.journal.load(filename) or {}
                remote_id = f"recovery-{transaction.get('attempt_id') or Path(filename).stem}"
                receipt = player.play(cover, duration, remote_id=remote_id, duck=False)
                if not receipt.get("voice_confirmed"):
                    raise PhoneError("phone recovery cover was not confirmed")
            else:
                player = self.voice_player or MacVoicePlayer(self.desktop, sleep=self.sleep)
                receipt = player.play(cover, duration)
            self.journal.receipt(filename, "recovery", {"played": True, **receipt})
            return receipt
        except (tts.TTSError, PhoneError, PlayoutError, OSError) as error:
            self.journal.receipt(filename, "recovery", {
                "played": False, "reason": str(error),
            })
            return None

    def execute(self, item: dict) -> dict:
        with self._claim():
            programme, prepared = self._prepare_claimed(item, allow_tail=False)
            return self._fire_claimed(item, programme, prepared)

    def prepare_boundary(self, item: dict) -> tuple[dict, dict]:
        """Prepare one queued item while the current record is still playing."""
        with self._claim():
            return self._prepare_claimed(item, allow_tail=True)

    def fire_boundary(self, item: dict, programme: dict, prepared: dict) -> dict:
        """Fire a previously prepared item at a scheduler-owned song boundary."""
        with self._claim():
            transaction = self.journal.load(item["filename"]) or {}
            if transaction.get("state") != "ready":
                raise PlayoutError(
                    f"prepared item {item['id']} is {transaction.get('state')}, not ready"
                )
            return self._fire_claimed(item, programme, prepared)

    def retry(self, item: dict) -> dict:
        with self._claim():
            transaction = self.journal.load(item["filename"])
            if not transaction:
                raise PlayoutError(f"item {item['id']} has no transaction")
            state = transaction.get("state")
            if state == "queued":
                programme, prepared = self._prepare_claimed(item, allow_tail=False)
                return self._fire_claimed(item, programme, prepared)
            if state not in {"failed", "preparing", "ready", "firing"}:
                raise PlayoutError(f"item {item['id']} is {state}, not retryable")
            if item["data"].get("output") == "phone" \
                    and (transaction.get("voice") or {}).get("delivery_uncertain"):
                raise PlayoutError(
                    "phone voice started without a finish receipt; refusing automatic replay"
                )
            resume_track_only = bool(
                item["data"].get("track")
                and (transaction.get("voice") or {}).get("played")
                and not (transaction.get("track") or {}).get("confirmed")
            )
            reset = self.journal.reset(item["filename"])
            if resume_track_only:
                reset["recovery"] = {
                    "resume_track_only": True,
                    "reason": "voice process completed in previous attempt",
                }
                self.journal.save(reset)
            programme, prepared = self._prepare_claimed(item, allow_tail=False)
            return self._fire_claimed(item, programme, prepared)

    def _prepare_claimed(
        self, item: dict, *, allow_tail: bool,
    ) -> tuple[dict, dict]:
        filename = item["filename"]
        programme = dict(item["data"])
        transition = rundown.normalize_transition(programme.get("transition", "overlap"))
        if transition == "tail" and not allow_tail:
            error = "tail needs a running rundown boundary; use overlap for a direct opener"
            self.journal.set_state(filename, "failed", error)
            raise PlayoutError(error)
        transaction = self.journal.begin(filename)
        if (transaction.get("recovery") or {}).get("resume_track_only") \
                and programme.get("track"):
            programme.pop("say", None)
            self.journal.receipt(filename, "recovery", {
                "resume_track_only": True,
                "applied": True,
            })
        try:
            prepared = self._prepare(filename, programme)
            self.journal.set_state(filename, "ready")
            return programme, prepared
        except (
            SpotifyError, PhoneError, tts.TTSError, PlayoutError, TransactionError,
            OSError, subprocess.SubprocessError,
        ) as error:
            self.journal.set_state(filename, "failed", str(error))
            raise PlayoutError(str(error)) from error

    def _fire_claimed(self, item: dict, programme: dict, prepared: dict) -> dict:
        filename = item["filename"]
        transition = rundown.normalize_transition(programme.get("transition", "overlap"))
        voice_played = False
        track_started = False
        try:
            self.journal.set_state(filename, "firing")
            if programme.get("say") and transition == "tail":
                self._play_voice(filename, prepared, duck=True)
                voice_played = True
            elif programme.get("say") and transition in {"clean", "blackout"}:
                self._play_voice(
                    filename, prepared, duck=bool(programme.get("duck")),
                )
                voice_played = True
                if transition == "blackout":
                    gap = self.desktop.get(
                        "blackout_gap_seconds",
                        self.desktop.get("dark_gap_seconds", 0.5),
                    )
                    self.sleep(float(gap))
            if programme.get("track"):
                self._start_track(filename, prepared)
                track_started = True
            if programme.get("say") and transition not in {"clean", "blackout", "tail"}:
                if transition == "intro":
                    self.sleep(float(self.desktop.get("intro_delay_seconds", 0.25)))
                self._play_voice(
                    filename, prepared,
                    duck=bool(
                        programme.get("duck")
                        or track_started and transition in {"overlap", "intro"}
                    ),
                )
                voice_played = True
            completed = self.journal.set_state(filename, "played")
            rundown.archive_played(self.state_path, filename)
            return completed
        except (
            SpotifyError, PhoneError, tts.TTSError, PlayoutError, TransactionError,
            OSError, subprocess.SubprocessError,
        ) as error:
            current = self.journal.load(filename) or {}
            voice_completed = voice_played or bool((current.get("voice") or {}).get("played"))
            track_was_confirmed = track_started or bool(
                (current.get("track") or {}).get("confirmed")
            )
            superseded_bed = bool(
                ((current.get("track") or {}).get("superseded") or {}).get("stable")
            )
            if voice_completed and programme.get("track") \
                    and not track_was_confirmed and not superseded_bed:
                self._recovery_cover(
                    filename, phone=programme.get("output") == "phone"
                )
            self.journal.set_state(filename, "failed", str(error))
            raise PlayoutError(str(error)) from error
