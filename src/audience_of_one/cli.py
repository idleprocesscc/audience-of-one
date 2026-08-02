"""The public station control desk."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, catalog, covers, rundown, scheduler, shortcuts, wildcards
from . import config as station_config
from .adapters import music_client, spotify_client
from .adapters.phone import PhoneError, phone_voice_player
from .adapters.qqmusic import QQMusicClient, QQMusicError, select_search_result
from .adapters.spotify import SpotifyError
from .adapters.spotify_auth import authorize_interactive
from .desktop import DesktopEngine, PlayoutError
from .fader import LiveFader
from .paths import config_file, state_dir
from .transactions import Journal, TransactionError
from .tts import TTSError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="station",
        description="The control desk for an audience-of-one radio station.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="override config.toml path")
    parser.add_argument("--state-dir", type=Path, help="override runtime state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="create public config and state directories")
    init_parser.add_argument("--force", action="store_true", help="replace existing config")

    config_parser = sub.add_parser("config", help="inspect or validate configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="print the active config path")
    config_sub.add_parser("validate", help="validate schema and list missing values")

    doctor = sub.add_parser("doctor", help="read-only readiness report")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument(
        "--phone", action="store_true",
        help="also authenticate to the configured phone and inspect its heartbeat",
    )

    spotify = sub.add_parser("spotify", help="authorize or inspect Spotify")
    spotify_sub = spotify.add_subparsers(dest="spotify_command", required=True)
    spotify_auth = spotify_sub.add_parser("auth", help="authorize with local PKCE callback")
    spotify_auth.add_argument("--no-browser", action="store_true")
    spotify_auth.add_argument("--timeout", type=float, default=240)
    spotify_devices = spotify_sub.add_parser("devices", help="list Connect devices")
    spotify_devices.add_argument("--json", action="store_true", dest="as_json")
    spotify_now = spotify_sub.add_parser("now", help="show current playback receipt")
    spotify_now.add_argument("--json", action="store_true", dest="as_json")
    spotify_list = spotify_sub.add_parser("list", help="show the configured Spotify shelf")
    spotify_list.add_argument("--playlist", help="override spotify.primary_playlist")
    spotify_list.add_argument("--json", action="store_true", dest="as_json")

    list_parser = sub.add_parser(
        "list", help="show the configured music shelf without mixing backends"
    )
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    status = sub.add_parser("status", help="show engine and rundown state")
    status.add_argument("--json", action="store_true", dest="as_json")

    start = sub.add_parser("start", help="start the always-on rundown scheduler")
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--json", action="store_true", dest="as_json")

    stop = sub.add_parser("stop", help="stop the scheduler without stopping current music")
    stop.add_argument("--json", action="store_true", dest="as_json")

    queue = sub.add_parser("queue", help="append a programme item to the rundown")
    queue.add_argument("track", nargs="?", help="Spotify URI or resolver query")
    queue.add_argument("say", nargs="?", help="optional voice line")
    queue.add_argument("--lang", choices=["zh", "en"])
    queue.add_argument("--transition", choices=sorted(rundown.TRANSITIONS), default="overlap")
    queue.add_argument("--after", choices=sorted(rundown.AFTER_MODES), default="autoplay")
    queue.add_argument("--phone", action="store_true")
    queue.add_argument("--device")
    queue.add_argument("--json", action="store_true", dest="as_json")

    rundown_parser = sub.add_parser("rundown", help="show the mutable programme rundown")
    rundown_parser.add_argument("--json", action="store_true", dest="as_json")

    covers = sub.add_parser("covers", help="manage pre-generated recovery lines")
    covers.add_argument("covers_command", choices=["build"], help="render configured lines")

    liners = sub.add_parser("liners", help="manage pre-generated improv liners")
    liners.add_argument("liners_command", choices=["build"], help="render configured lines")

    open_parser = sub.add_parser("open", help="play one receipted desktop programme now")
    open_parser.add_argument("track", nargs="?", help="Spotify URI or resolver query")
    open_parser.add_argument("--say", help="optional voice line")
    open_parser.add_argument(
        "--transition", choices=sorted(rundown.TRANSITIONS),
        default="overlap",
    )
    open_parser.add_argument("--device", help="exact or partial Spotify Connect device")
    open_parser.add_argument(
        "--phone", action="store_true",
        help="send speech to the configured Android receiver",
    )
    open_parser.add_argument("--json", action="store_true", dest="as_json")

    say_parser = sub.add_parser(
        "say", help="speak one receipted line now, without starting or changing music"
    )
    say_parser.add_argument("text", help="the line to synthesize and play")
    say_parser.add_argument(
        "--phone", action="store_true",
        help="speak through the configured Android receiver instead of this Mac",
    )
    say_parser.add_argument("--json", action="store_true", dest="as_json")

    retry_parser = sub.add_parser(
        "retry", help="run one queued, failed, or interrupted desktop item"
    )
    retry_parser.add_argument("item", help="retryable rundown item id")
    retry_parser.add_argument("--json", action="store_true", dest="as_json")

    history_parser = sub.add_parser("history", help="show completed desktop items")
    history_parser.add_argument("--json", action="store_true", dest="as_json")

    off_parser = sub.add_parser("off", help="pause the active music backend with a receipt")
    off_parser.add_argument("--device", help="exact or unambiguous Connect device")
    off_parser.add_argument("--json", action="store_true", dest="as_json")

    toggle_parser = sub.add_parser(
        "toggle", help="pause or resume the active music backend with a receipt"
    )
    toggle_parser.add_argument("--device", help="music device; defaults to the configured one")
    toggle_parser.add_argument("--json", action="store_true", dest="as_json")

    mpv_parser = sub.add_parser("mpv", help="inspect or stop the local mpv player")
    mpv_sub = mpv_parser.add_subparsers(dest="mpv_command", required=True)
    mpv_list = mpv_sub.add_parser("list", help="scan the configured local record box")
    mpv_list.add_argument("--json", action="store_true", dest="as_json")
    mpv_quit = mpv_sub.add_parser("quit", help="fully exit the background mpv process")
    mpv_quit.add_argument("--json", action="store_true", dest="as_json")

    qqmusic_parser = sub.add_parser(
        "qqmusic", help="search or play through a local QQMusicApi companion"
    )
    qqmusic_sub = qqmusic_parser.add_subparsers(dest="qqmusic_command", required=True)
    qqmusic_doctor = qqmusic_sub.add_parser("doctor", help="verify the companion routes")
    qqmusic_doctor.add_argument("--json", action="store_true", dest="as_json")
    qqmusic_login = qqmusic_sub.add_parser("login", help="authorize private QQ Music shelves")
    qqmusic_login.add_argument("--type", choices=["qq", "wx"], default="qq")
    qqmusic_login.add_argument("--timeout", type=float, default=180.0)
    qqmusic_login.add_argument("--no-open", action="store_true")
    qqmusic_list = qqmusic_sub.add_parser("list", help="show private QQ Music shelves")
    qqmusic_list.add_argument("--playlist", help="also expand one exact name or numeric id")
    qqmusic_list.add_argument("--json", action="store_true", dest="as_json")
    qqmusic_search = qqmusic_sub.add_parser("search", help="search QQ Music without playing")
    qqmusic_search.add_argument("query")
    qqmusic_search.add_argument("--limit", type=int, default=10)
    qqmusic_search.add_argument("--json", action="store_true", dest="as_json")
    qqmusic_open = qqmusic_sub.add_parser("open", help="play one QQ Music result through mpv")
    qqmusic_open.add_argument("query", help="title/artist query or qqmusic:MID")
    qqmusic_open.add_argument("--say", help="optional voice line")
    qqmusic_open.add_argument(
        "--transition", choices=sorted(rundown.TRANSITIONS), default="overlap"
    )
    qqmusic_open.add_argument("--device")
    qqmusic_open.add_argument(
        "--phone", action="store_true",
        help="stream through the configured Android Termux mpv receiver",
    )
    qqmusic_open.add_argument("--json", action="store_true", dest="as_json")

    shortcut_parser = sub.add_parser("shortcut", help="install or inspect the macOS toggle")
    shortcut_sub = shortcut_parser.add_subparsers(dest="shortcut_command", required=True)
    shortcut_install = shortcut_sub.add_parser("install", help="install launcher and import workflow")
    shortcut_install.add_argument("--device", default="local_mac")
    shortcut_install.add_argument("--no-open", action="store_true")
    shortcut_install.add_argument("--json", action="store_true", dest="as_json")
    shortcut_doctor = shortcut_sub.add_parser("doctor", help="inspect launcher and shortcut")
    shortcut_doctor.add_argument("--json", action="store_true", dest="as_json")
    shortcut_uninstall = shortcut_sub.add_parser("uninstall", help="remove station-owned files")
    shortcut_uninstall.add_argument("--json", action="store_true", dest="as_json")

    fader_parser = sub.add_parser(
        "fader", help="move the active local music fader and preserve a restore point"
    )
    fader_parser.add_argument("target", help="integer 0-100, or restore")
    fader_parser.add_argument("--seconds", type=float, help="fader travel time")
    fader_parser.add_argument("--json", action="store_true", dest="as_json")

    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return (args.config.expanduser() if args.config else config_file(),
            args.state_dir.expanduser() if args.state_dir else state_dir())


def _validate(path: Path) -> tuple[dict, list[str]]:
    data = station_config.load(path)
    return data, station_config.validate(data)


def command_init(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        station_config.initialize(path, state_path, force=args.force)
    except station_config.ConfigError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"created {path}")
    print(f"created {state_path}")
    print("next: edit config, set secrets in the named environment variables, then run station doctor")
    return 0


def command_config(args: argparse.Namespace) -> int:
    path, _ = _paths(args)
    if args.config_command == "path":
        print(path)
        return 0
    try:
        _, actions = _validate(path)
    except station_config.ConfigError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if actions:
        print("configuration schema: valid")
        for action in actions:
            print(f"ACTION REQUIRED: {action}")
        return 1
    print("configuration: ready")
    return 0


def doctor_payload(path: Path, state_path: Path, *, phone: bool = False) -> dict:
    checks = []
    data = None
    try:
        data, actions = _validate(path)
        checks.append({"component": "config", "status": "pass", "detail": str(path)})
        config_secure = path.is_file() and not (path.stat().st_mode & 0o077)
        checks.append({
            "component": "config_permissions",
            "status": "pass" if config_secure else "action_required",
            "detail": "0600 or stricter" if config_secure else f"run chmod 600 {path}",
        })
        checks.extend({"component": "configuration", "status": "action_required", "detail": action}
                      for action in actions)
    except station_config.ConfigError as error:
        checks.append({"component": "config", "status": "action_required", "detail": str(error)})
    state_secure = state_path.is_dir() and not (state_path.stat().st_mode & 0o077)
    checks.append({
        "component": "state",
        "status": "pass" if state_secure else "action_required",
        "detail": str(state_path) if state_secure else f"create/chmod 700 {state_path}",
    })
    checks.append({
        "component": "platform",
        "status": "pass" if platform.system() == "Darwin" else "action_required",
        "detail": platform.system(),
    })
    backend = ((data or {}).get("music") or {}).get("backend", "spotify")
    adapter = (data or {}).get("spotify", {}).get("adapter")
    executables = {"afplay", "ffprobe", "scutil"}
    if backend == "local":
        executables.add("mpv")
    elif adapter == "macos_applescript":
        executables.add("osascript")
    if data:
        tts_config = data.get("tts") or {}
        providers = tts_config.get("providers") or {}
        selected = {
            tts_config.get("chinese"), tts_config.get("english"),
        }
        if any(
            isinstance(providers.get(name), dict)
            and providers[name].get("type") == "macos"
            for name in selected
        ):
            executables.update({"say", "ffmpeg"})
    for executable in sorted(executables):
        found = shutil.which(executable)
        checks.append({
            "component": executable,
            "status": "pass" if found else "action_required",
            "detail": found or f"{executable} is not on PATH",
        })
    if backend == "local":
        library = Path(str((data.get("music") or {}).get("library_root") or "")).expanduser()
        checks.append({
            "component": "local_library",
            "status": "pass" if library.is_dir() else "action_required",
            "detail": str(library) if library.is_dir() else f"create local library {library}",
        })
    qqmusic_config = (data or {}).get("qqmusic") or {}
    if qqmusic_config.get("enabled"):
        try:
            receipt = QQMusicClient(
                qqmusic_config,
                credential_path=state_path / "secrets" / "qqmusic-credential.json",
            ).doctor()
            checks.append({
                "component": "qqmusic",
                "status": "pass",
                "detail": f"required routes confirmed at {receipt['base_url']}",
            })
        except QQMusicError as error:
            checks.append({
                "component": "qqmusic",
                "status": "action_required",
                "detail": str(error),
            })
    token_path = state_path / "secrets" / "spotify-tokens.json"
    secrets_path = token_path.parent
    if secrets_path.exists():
        secrets_secure = secrets_path.is_dir() and not (secrets_path.stat().st_mode & 0o077)
        checks.append({
            "component": "secrets_permissions",
            "status": "pass" if secrets_secure else "action_required",
            "detail": "0700 or stricter" if secrets_secure
            else f"run chmod 700 {secrets_path}",
        })
    token_status = "pass" if backend == "local" or adapter == "macos_applescript" \
        else "action_required"
    token_detail = (
        f"not required for {backend if backend == 'local' else adapter}"
        if token_status == "pass"
        else "run station spotify auth after configuring SPOTIFY_CLIENT_ID"
    )
    if backend == "spotify" and adapter == "web_api" and token_path.is_file():
        try:
            token = json.loads(token_path.read_text(encoding="utf-8"))
            required = {"user-modify-playback-state", "user-read-playback-state"}
            scopes = set(str(token.get("scope") or "").split())
            missing = sorted(required - scopes)
            if not token.get("access_token") or not token.get("refresh_token"):
                raise ValueError("required token fields are missing")
            if missing:
                raise ValueError("missing scopes: " + ", ".join(missing))
            if token_path.stat().st_mode & 0o077:
                raise ValueError("token file permissions must be 0600")
            expiry = float(token.get("expires_at", 0))
            state = "refresh due on next live request" if expiry <= time.time() else "unexpired"
            token_status = "pass"
            token_detail = f"structurally valid ({state}); run station spotify devices for live proof"
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            token_detail = f"invalid Spotify token cache: {error}; run station spotify auth"
    checks.append({
        "component": "spotify_auth",
        "status": token_status,
        "detail": token_detail,
    })
    adapter_ready = backend == "local" or adapter in {"web_api", "macos_applescript"}
    checks.append({
        "component": "broadcast_engine",
        "status": "pass" if adapter_ready else "action_required",
        "detail": f"desktop direct playout is installed with "
        f"{backend if backend == 'local' else adapter}; "
        "always-on scheduling is not available in v0.1"
        if adapter_ready else "choose a supported spotify.adapter",
    })
    if phone:
        android = (data or {}).get("android") or {}
        if not android.get("enabled"):
            checks.append({
                "component": "phone_transport",
                "status": "action_required",
                "detail": "set android.enabled = true before a phone probe",
            })
        else:
            try:
                player = phone_voice_player(data)
                health_path = str(android.get("health_path") or "phone-health.json")
                raw = player.transport.read(health_path)
                health = json.loads(raw)
                age = time.time() - float(health.get("updated_at", 0))
                if health.get("state") != "running" or not 0 <= age <= 90:
                    raise ValueError("player heartbeat is stopped or stale")
                checks.append({
                    "component": "phone_transport",
                    "status": "pass",
                    "detail": "authenticated MCP session and fresh player heartbeat",
                })
            except (PhoneError, ValueError, TypeError, json.JSONDecodeError) as error:
                checks.append({
                    "component": "phone_transport",
                    "status": "action_required",
                    "detail": f"phone probe failed: {error}",
                })
    ready = all(check["status"] == "pass" for check in checks)
    return {"version": 1, "ready": ready, "checks": checks}


def command_doctor(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    payload = doctor_payload(path, state_path, phone=bool(getattr(args, "phone", False)))
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        for check in payload["checks"]:
            label = check["status"].upper().replace("_", " ")
            print(f"{label}: {check['component']} — {check['detail']}")
    return 0 if payload["ready"] else 1


def _rundown_payload(state_path: Path) -> dict:
    journal = Journal(state_path)
    entries = []
    for item in rundown.items(state_path):
        transaction = journal.load(item["filename"])
        entries.append({
            "id": item["id"],
            "order": item["order"],
            "readable": item["data"] is not None,
            "programme": item["data"],
            "transaction_state": (transaction or {}).get("state"),
        })
    return {"version": 1, "count": len(entries), "up_next": entries}


def command_status(args: argparse.Namespace) -> int:
    _, state_path = _paths(args)
    payload = _rundown_payload(state_path)
    engine = scheduler.read_status(state_path)
    payload.update({
        "engine_running": bool(engine.get("running")),
        "engine_status": engine.get("state", "stopped"),
        "scheduler": engine,
    })
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        detail = engine.get("last_action") or engine.get("state", "stopped")
        print(f"engine: {'running' if engine.get('running') else 'stopped'} ({detail})")
        print(f"rundown: {payload['count']} item(s)")
    return 0


def command_start(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        current = scheduler.read_status(state_path)
        if current.get("running"):
            payload = {"version": 1, "started": True, "already_running": True, **current}
        elif args.foreground:
            scheduler.StationScheduler(data, state_path).run_forever()
            payload = {"version": 1, "started": True, "foreground": True}
        else:
            log_path = state_path / "scheduler" / "process.log"
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable, "-m", "audience_of_one.scheduler",
                        "--config", str(path.resolve()),
                        "--state-dir", str(state_path.resolve()),
                    ],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 5.0
            observed = {}
            while time.monotonic() < deadline:
                observed = scheduler.read_status(state_path)
                if observed.get("running") and observed.get("pid") == process.pid:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            if not observed.get("running") or observed.get("pid") != process.pid:
                raise PlayoutError(f"scheduler did not start; inspect {log_path}")
            payload = {
                "version": 1, "started": True, "already_running": False, **observed,
            }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"ON AIR: scheduler pid {payload.get('pid', os.getpid())}")
        return 0
    except (station_config.ConfigError, PlayoutError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def _scheduler_process_matches(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, timeout=3, check=False,
    )
    return result.returncode == 0 and "audience_of_one.scheduler" in result.stdout


def command_stop(args: argparse.Namespace) -> int:
    _, state_path = _paths(args)
    current = scheduler.read_status(state_path)
    pid = current.get("pid")
    if not current.get("running") or not isinstance(pid, int):
        payload = {"version": 1, "stopped": True, "already_stopped": True}
    elif not _scheduler_process_matches(pid):
        print("ERROR: refusing to signal a pid that is not the station scheduler", file=sys.stderr)
        return 2
    else:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and scheduler.read_status(state_path).get("running"):
            time.sleep(0.1)
        if scheduler.read_status(state_path).get("running"):
            print("ERROR: scheduler did not stop within 5 seconds", file=sys.stderr)
            return 2
        payload = {"version": 1, "stopped": True, "already_stopped": False, "pid": pid}
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("OFF AIR: scheduler stopped")
    return 0


def command_queue(args: argparse.Namespace) -> int:
    _, state_path = _paths(args)
    try:
        item = rundown.append(
            state_path,
            track=args.track,
            say=args.say,
            lang=args.lang,
            transition=args.transition,
            after=args.after,
            phone=bool(getattr(args, "phone", False)),
            device=args.device,
            duck=bool(getattr(args, "duck", False)),
        )
    except (rundown.RundownError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    payload = {
        "version": 1,
        "queued": True,
        "id": item["id"],
        "transaction_state": "queued",
        "programme": item["data"],
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"queued {item['id']} (transaction=queued)")
    return 0


def command_rundown(args: argparse.Namespace) -> int:
    _, state_path = _paths(args)
    payload = _rundown_payload(state_path)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"UP NEXT — {payload['count']} item(s)")
    for index, entry in enumerate(payload["up_next"], 1):
        programme = entry["programme"] or {}
        parts = []
        if programme.get("say"):
            parts.append(f"voice: {programme['say']}")
        if programme.get("track"):
            parts.append(f"track: {programme['track']}")
        parts.append(f"transition: {programme.get('transition', 'overlap')}")
        print(f"{index:>2}. {entry['id']} [{entry['transaction_state']}] " + " | ".join(parts))
    return 0


def command_covers(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data, _ = _validate(path)
        receipts = covers.build(data, state_path)
    except (station_config.ConfigError, TTSError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    for index, receipt in enumerate(receipts, 1):
        print(
            f"cover-{index}.mp3: provider={receipt['provider']} "
            f"duration={receipt['duration_seconds']}s bytes={receipt['bytes']}"
        )
    return 0


def command_liners(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data, _ = _validate(path)
        receipts = wildcards.build(data, state_path)
    except (station_config.ConfigError, wildcards.WildcardError, TTSError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    for receipt in receipts:
        print(
            f"{receipt['id']}.mp3: provider={receipt['provider']} "
            f"duration={receipt['duration_seconds']}s bytes={receipt['bytes']}"
        )
    return 0


def command_open(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    item = None
    try:
        data = _desktop_config(path)
        item = rundown.append(
            state_path,
            track=args.track,
            say=args.say,
            transition=args.transition,
            phone=bool(getattr(args, "phone", False)),
            device=args.device,
            duck=bool(getattr(args, "duck", False)),
        )
        transaction = DesktopEngine(data, state_path).execute(item)
        payload = {
            "version": 1,
            "opened": True,
            "id": item["id"],
            "transaction": transaction,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            voice = transaction.get("voice") or {}
            track = transaction.get("track") or {}
            details = []
            if voice.get("played"):
                details.append("voice=played")
            if track.get("confirmed"):
                details.append("track=confirmed")
            print(f"PLAYOUT CONFIRMED: {item['id']} ({', '.join(details)})")
        return 0
    except (
        station_config.ConfigError, rundown.RundownError, PlayoutError,
        TransactionError, OSError,
    ) as error:
        if getattr(args, "as_json", False):
            payload = {"version": 1, "opened": False, "error": str(error)}
            if item:
                payload["id"] = item["id"]
                payload["transaction"] = Journal(state_path).load(item["filename"])
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            if item:
                print(
                    f"failed item: {item['id']} (retry with: station retry {item['id']})",
                    file=sys.stderr,
                )
        return 2


def command_say(args: argparse.Namespace) -> int:
    """Use the receipted playout path as an immediate voice intercom."""
    values = vars(args).copy()
    values.update({
        "track": None,
        "say": args.text,
        "transition": "clean",
        "device": None,
        "duck": bool(args.phone),
    })
    return command_open(argparse.Namespace(**values))


def _desktop_config(path: Path) -> dict:
    data, _ = _validate(path)
    if data["station"].get("mode") != "desktop":
        raise station_config.ConfigError("desktop playout requires station.mode = desktop")
    backend = (data.get("music") or {}).get("backend", "spotify")
    if backend == "spotify" \
            and data["spotify"].get("adapter") not in {"web_api", "macos_applescript"}:
        raise station_config.ConfigError("desktop playout needs a supported Spotify adapter")
    if backend not in {"spotify", "local"}:
        raise station_config.ConfigError("desktop playout needs a supported music backend")
    return data


def command_retry(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    item = None
    try:
        data = _desktop_config(path)
        item = rundown.get(state_path, args.item)
        transaction = DesktopEngine(data, state_path).retry(item)
        payload = {
            "version": 1,
            "retried": True,
            "id": item["id"],
            "transaction": transaction,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"PLAYOUT CONFIRMED AFTER RETRY: {item['id']}")
        return 0
    except (
        station_config.ConfigError, rundown.RundownError, PlayoutError,
        TransactionError, OSError,
    ) as error:
        if args.as_json:
            payload = {"version": 1, "retried": False, "error": str(error)}
            if item:
                payload["id"] = item["id"]
                payload["transaction"] = Journal(state_path).load(item["filename"])
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_history(args: argparse.Namespace) -> int:
    _, state_path = _paths(args)
    journal = Journal(state_path)
    entries = []
    for path in sorted((state_path / "played").glob("*.json"), reverse=True):
        try:
            programme = json.loads(path.read_text(encoding="utf-8"))
            transaction = journal.load(path.name)
        except (OSError, json.JSONDecodeError, TransactionError):
            continue
        entries.append({
            "id": path.stem,
            "programme": programme,
            "transaction": transaction,
        })
    payload = {"version": 1, "count": len(entries), "played": entries}
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"PLAYED — {len(entries)} item(s)")
        for entry in entries:
            programme = entry["programme"]
            label = programme.get("track") or programme.get("say") or "unknown"
            print(f"{entry['id']} {label}")
    return 0


def command_off(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        backend = (data.get("music") or {}).get("backend", "spotify")
        client = (
            spotify_client(data["spotify"], state_path)
            if backend == "spotify" else music_client(data, state_path)
        )
        device_spec = args.device or (
            data["spotify"].get("device") if backend == "spotify"
            else (data.get("music") or {}).get("device")
        ) or None
        accepted = client.pause(device_spec)
        device = accepted["device"]
        paused = client.wait_for_paused(device["id"], timeout=5)
        payload = {
            "version": 1,
            "off": True,
            "device": device,
            "playback": paused,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"OFF: pause confirmed on {device.get('name') or device['id']}")
        return 0
    except (station_config.ConfigError, SpotifyError, OSError) as error:
        if args.as_json:
            print(json.dumps({"version": 1, "off": False, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_toggle(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        backend = (data.get("music") or {}).get("backend", "spotify")
        client = (
            spotify_client(data["spotify"], state_path)
            if backend == "spotify" else music_client(data, state_path)
        )
        device_spec = args.device or (
            data["spotify"].get("device") if backend == "spotify"
            else (data.get("music") or {}).get("device")
        ) or None
        before = client.snapshot()
        if before and before.get("is_playing"):
            accepted = client.pause(device_spec)
            action = "paused"
            playback = client.wait_for_paused(accepted["device"]["id"], timeout=5)
        else:
            accepted = client.resume(device_spec)
            action = "resumed"
            playback = client.wait_for_resumed(accepted["device"]["id"], timeout=5)
        payload = {
            "version": 1,
            "action": action,
            "device": accepted["device"],
            "playback": playback,
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"{action.upper()}: confirmed on {accepted['device'].get('name')}")
        return 0
    except (station_config.ConfigError, SpotifyError, OSError) as error:
        if args.as_json:
            print(json.dumps({"version": 1, "action": None, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def _minutes(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "?:??"
    total = max(0, round(duration_ms / 1000))
    return f"{total // 60}:{total % 60:02d}"


def _recent_mark(track: dict) -> str:
    positions = track.get("recent_positions") or []
    if not positions:
        return ""
    mark = f"recent #{min(positions)}"
    return mark + (f" ×{len(positions)}" if len(positions) > 1 else "")


def _print_spotify_shelf(payload: dict) -> None:
    playlist = payload["playlist"]
    sections = payload["sections"]
    total = len(sections["long_term_preference"]) + len(
        sections["unranked_playlist_order"]
    )
    print(f"SPOTIFY LIST — {playlist['name']} — {total} tracks")

    def show(tracks: list[dict], *, weekly: bool = False) -> None:
        for index, track in enumerate(tracks, 1):
            marks = []
            if track.get("long_term_rank") is not None:
                marks.append(f"long #{track['long_term_rank']}")
            recent = _recent_mark(track)
            if recent:
                marks.append(recent)
            if weekly and track.get("also_in_primary"):
                marks.append(f"also in {playlist['name']}")
            evidence = f"  [{' · '.join(marks)}]" if marks else ""
            artists = ", ".join(track.get("artists") or []) or "unknown artist"
            print(f"{index:>3}. {track['name']} — {artists}{evidence}")

    long_term = sections["long_term_preference"]
    print(f"\nLONG-TERM PREFERENCE — {len(long_term)} matches from Spotify Top 50")
    show(long_term)
    unranked = sections["unranked_playlist_order"]
    print(f"\n---\n\nUNRANKED — {len(unranked)} tracks in playlist order")
    show(unranked)
    weekly = sections["spotify_weekly"]
    label = f"{len(weekly)} recommendations" if weekly else "unavailable"
    if payload.get("weekly_error"):
        label += " · playlist not exposed to this app"
    print(f"\n---\n\nSPOTIFY WEEKLY — {label}")
    show(weekly, weekly=True)


def _print_mpv_shelf(payload: dict) -> None:
    print(f"MPV LIST — {payload['count']} tracks — {payload['library_root']}")
    for track in payload["tracks"]:
        artists = ", ".join(track.get("artists") or [])
        byline = f" — {artists}" if artists else ""
        print(
            f"{track['position']:>3}. {track['name']}{byline} "
            f"[{_minutes(track.get('duration_ms'))} · {track['format']}]"
        )
        print(f"     {track['uri']}")


def _catalog_payload(data: dict, state_path: Path, source: str, *, playlist: str = "") -> dict:
    if source == "mpv":
        music = data.get("music") or {}
        if music.get("backend") != "local":
            raise station_config.ConfigError("station mpv list needs music.backend = local")
        return catalog.local_shelf(music)
    spotify_config = data.get("spotify")
    if not isinstance(spotify_config, dict):
        raise station_config.ConfigError("missing [spotify] table")
    client = spotify_client(spotify_config, state_path)
    return catalog.spotify_shelf(client, spotify_config, playlist=playlist)


def _emit_catalog(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["source"] == "spotify":
        _print_spotify_shelf(payload)
    else:
        _print_mpv_shelf(payload)


def command_list(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        backend = (data.get("music") or {}).get("backend", "spotify")
        source = "mpv" if backend == "local" else "spotify"
        payload = _catalog_payload(data, state_path, source)
        _emit_catalog(payload, as_json=args.as_json)
        return 0
    except (station_config.ConfigError, SpotifyError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_mpv(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        if (data.get("music") or {}).get("backend") != "local":
            raise station_config.ConfigError("station mpv needs music.backend = local")
        if args.mpv_command == "list":
            payload = _catalog_payload(data, state_path, "mpv")
            _emit_catalog(payload, as_json=args.as_json)
            return 0
        client = music_client(data, state_path)
        receipt = client.quit()
        payload = {"version": 1, "mpv": receipt}
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            suffix = " (already stopped)" if receipt.get("already_stopped") else ""
            print(f"MPV STOPPED: confirmed{suffix}")
        return 0
    except (station_config.ConfigError, SpotifyError, OSError) as error:
        if args.as_json:
            print(json.dumps({"version": 1, "mpv": None, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_fader(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        desktop = data.get("desktop") or {}
        backend = (data.get("music") or {}).get("backend", "spotify")
        if backend == "local":
            client = music_client(data, state_path)
            factory = getattr(client, "fader", None)
            desk = LiveFader(state_path, backend=factory() if callable(factory) else None)
        else:
            desk = LiveFader(state_path)
        if args.target == "restore":
            seconds = args.seconds
            if seconds is None:
                seconds = float(desktop.get("fade_up_seconds", 1.0))
            receipt = desk.restore(seconds=seconds)
        else:
            try:
                target = int(args.target)
            except ValueError as error:
                raise PlayoutError("fader target must be 0-100 or restore") from error
            seconds = args.seconds
            if seconds is None:
                seconds = float(desktop.get("fade_down_seconds", 0.8))
            receipt = desk.move(target, seconds=seconds)
        payload = {"version": 1, "fader": receipt}
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif receipt.get("restored"):
            print(
                f"FADER RESTORED: {receipt['from_percent']} → "
                f"{receipt['to_percent']} in {receipt['seconds']}s"
            )
        else:
            print(
                f"FADER: {receipt['from_percent']} → {receipt['to_percent']} "
                f"in {receipt['seconds']}s "
                f"(restore={receipt['restore_percent']})"
            )
        return 0
    except (station_config.ConfigError, SpotifyError, PlayoutError, OSError) as error:
        if args.as_json:
            print(json.dumps({"version": 1, "fader": None, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_spotify(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = station_config.load(path)
        spotify_config = data.get("spotify")
        if not isinstance(spotify_config, dict):
            raise station_config.ConfigError("missing [spotify] table")
        client = spotify_client(spotify_config, state_path)
        if args.spotify_command == "auth":
            if spotify_config.get("adapter") == "macos_applescript":
                print(json.dumps({
                    "authorized": True,
                    "required": False,
                    "adapter": "macos_applescript",
                }, indent=2))
                return 0
            receipt = authorize_interactive(
                client, open_browser=not args.no_browser, timeout=args.timeout
            )
            print(json.dumps(receipt, indent=2))
            return 0
        if args.spotify_command == "devices":
            devices = client.devices()
            if args.as_json:
                print(json.dumps({"version": 1, "devices": devices}, ensure_ascii=False, indent=2))
            else:
                for device in devices:
                    marker = "*" if device.get("is_active") else " "
                    print(f"{marker} {device.get('name') or 'unknown'} [{device.get('id') or '?'}]")
            return 0
        if args.spotify_command == "list":
            payload = catalog.spotify_shelf(
                client, spotify_config, playlist=args.playlist or ""
            )
            _emit_catalog(payload, as_json=args.as_json)
            return 0
        receipt = client.snapshot()
        if args.as_json:
            print(json.dumps({"version": 1, "playback": receipt}, ensure_ascii=False, indent=2))
        elif receipt:
            print(
                f"{receipt['name']} — {', '.join(receipt['artists'])} "
                f"on {receipt['device']['name']}"
            )
        else:
            print("nothing playing")
        return 0
    except (station_config.ConfigError, SpotifyError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_qqmusic(args: argparse.Namespace) -> int:
    path, state_path = _paths(args)
    try:
        data = _desktop_config(path)
        qqmusic = data.get("qqmusic") or {}
        if not qqmusic.get("enabled"):
            raise station_config.ConfigError("set qqmusic.enabled = true first")
        client = QQMusicClient(
            qqmusic,
            credential_path=state_path / "secrets" / "qqmusic-credential.json",
        )
        if args.qqmusic_command == "doctor":
            payload = {"version": 1, "qqmusic": client.doctor()}
        elif args.qqmusic_command == "login":
            if args.timeout <= 0:
                raise QQMusicError("QQ Music login timeout must be positive")
            started = client.begin_login(args.type)
            extension = ".jpg" if started["mimetype"] == "image/jpeg" else ".png"
            qr_path = state_path / "secrets" / f"qqmusic-login{extension}"
            qr_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            qr_path.write_bytes(started["image"])
            qr_path.chmod(0o600)
            opened = False
            if not args.no_open and platform.system() == "Darwin":
                opened = subprocess.run(
                    ["open", str(qr_path)], capture_output=True, text=True,
                    timeout=10, check=False,
                ).returncode == 0
            print(f"QQ MUSIC LOGIN QR: {qr_path}")
            print("Scan with QQ or WeChat, then confirm on the phone.")
            deadline = time.monotonic() + args.timeout
            last_event = None
            credential = None
            while time.monotonic() < deadline:
                status = client.login_status(started["identifier"], args.type)
                event = int(status.get("event", -1))
                if event != last_event:
                    labels = {0: "authorized", 1: "waiting for scan", 2: "waiting for confirmation"}
                    print(f"QQ MUSIC LOGIN: {labels.get(event, 'stopped')}")
                    last_event = event
                if event == 0 and isinstance(status.get("credential"), dict):
                    credential = status["credential"]
                    break
                if event in {3, 4, -1}:
                    raise QQMusicError(
                        {3: "QQ Music login QR expired", 4: "QQ Music login was refused"}
                        .get(event, "QQ Music login returned an unknown state")
                    )
                time.sleep(1.5)
            if credential is None:
                raise QQMusicError("QQ Music login timed out")
            receipt = client.save_credential(credential)
            qr_path.unlink(missing_ok=True)
            payload = {
                "version": 1, "qqmusic": {
                    "signed_in": True,
                    "account": receipt["account"],
                    "credential_path": receipt["credential_path"],
                    "qr_opened": opened,
                },
            }
            print(f"QQ MUSIC AUTHORIZED: account {receipt['account']}")
            return 0
        elif args.qqmusic_command == "list":
            payload = client.private_shelf(playlist=args.playlist or "")
        elif args.qqmusic_command == "search":
            if not 1 <= args.limit <= 50:
                raise QQMusicError("QQ Music search limit must be from 1 to 50")
            tracks = client.search(args.query, limit=args.limit)
            payload = {
                "version": 1, "source": "qqmusic_api", "query": args.query,
                "count": len(tracks), "tracks": tracks,
                "playback_touched": False, "rundown_touched": False,
            }
        else:
            if (data.get("music") or {}).get("backend") != "local":
                raise station_config.ConfigError(
                    "station qqmusic open needs music.backend = local so mpv owns playout"
                )
            if args.query.startswith("qqmusic:"):
                track = args.query
            else:
                track = select_search_result(
                    args.query, client.search(args.query, limit=5)
                )["uri"]
            forwarded = argparse.Namespace(
                config=path,
                state_dir=state_path,
                track=track,
                say=args.say,
                transition=args.transition,
                device=args.device,
                phone=bool(getattr(args, "phone", False)),
                as_json=args.as_json,
            )
            return command_open(forwarded)
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.qqmusic_command == "doctor":
            print(f"QQ MUSIC READY: {client.base_url}")
            print("PRIVATE LIST: signed in" if payload["qqmusic"]["signed_in"]
                  else "PRIVATE LIST: run station qqmusic login")
        elif args.qqmusic_command == "list":
            sections = payload["sections"]
            print(f"QQ MUSIC LIST — account {payload['account']}")
            print(f"\nMY FAVORITES — {len(sections['my_favorites'])} tracks")
            for track in sections["my_favorites"]:
                artists = ", ".join(track.get("artists") or []) or "unknown artist"
                print(f"{track['position']:>3}. {track['name']} — {artists}  [{track['uri']}]")
            for key, title in (
                ("created_playlists", "CREATED PLAYLISTS"),
                ("collected_playlists", "COLLECTED PLAYLISTS"),
            ):
                print(f"\n---\n\n{title} — {len(sections[key])}")
                for playlist in sections[key]:
                    print(
                        f"{playlist['name']} — {playlist['track_count']} tracks "
                        f"[id={playlist['id']}]"
                    )
            selected = payload.get("selected_playlist")
            if selected:
                print(f"\n---\n\nPLAYLIST — {selected['name']} — {len(selected['tracks'])} tracks")
                for track in selected["tracks"]:
                    artists = ", ".join(track.get("artists") or []) or "unknown artist"
                    print(f"{track['position']:>3}. {track['name']} — {artists}  [{track['uri']}]")
        else:
            print(f"QQ MUSIC SEARCH — {len(payload['tracks'])} result(s)")
            for track in payload["tracks"]:
                artists = ", ".join(track.get("artists") or []) or "unknown artist"
                print(f"{track['position']:>3}. {track['name']} — {artists}  [{track['uri']}]")
        return 0
    except (station_config.ConfigError, QQMusicError, OSError) as error:
        if getattr(args, "as_json", False):
            print(json.dumps({"version": 1, "qqmusic": None, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2


def command_shortcut(args: argparse.Namespace) -> int:
    path, _ = _paths(args)
    try:
        if args.shortcut_command == "install":
            payload = shortcuts.install(path, args.device, open_import=not args.no_open)
        elif args.shortcut_command == "doctor":
            payload = shortcuts.doctor()
        else:
            payload = shortcuts.uninstall()
        if args.as_json:
            print(json.dumps({"version": 1, "shortcut": payload}, ensure_ascii=False, indent=2))
        elif args.shortcut_command == "install":
            print(f"SHORTCUT LAUNCHER: {payload['launcher']}")
            print(f"IMPORT: {payload['workflow']}")
            print("After import, assign Control-Command-P in Shortcuts.")
        elif args.shortcut_command == "doctor":
            print("SHORTCUT READY" if payload["ready"] else "SHORTCUT NEEDS ATTENTION")
            for component, passed in payload["checks"].items():
                print(f"{'PASS' if passed else 'ACTION'} {component}")
        else:
            print("SHORTCUT FILES REMOVED")
            print(payload["next"])
        return 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        code = command_init(args)
    elif args.command == "config":
        code = command_config(args)
    elif args.command == "doctor":
        code = command_doctor(args)
    elif args.command == "status":
        code = command_status(args)
    elif args.command == "start":
        code = command_start(args)
    elif args.command == "stop":
        code = command_stop(args)
    elif args.command == "list":
        code = command_list(args)
    elif args.command == "queue":
        code = command_queue(args)
    elif args.command == "rundown":
        code = command_rundown(args)
    elif args.command == "covers":
        code = command_covers(args)
    elif args.command == "liners":
        code = command_liners(args)
    elif args.command == "open":
        code = command_open(args)
    elif args.command == "say":
        code = command_say(args)
    elif args.command == "retry":
        code = command_retry(args)
    elif args.command == "history":
        code = command_history(args)
    elif args.command == "off":
        code = command_off(args)
    elif args.command == "toggle":
        code = command_toggle(args)
    elif args.command == "mpv":
        code = command_mpv(args)
    elif args.command == "fader":
        code = command_fader(args)
    elif args.command == "spotify":
        code = command_spotify(args)
    elif args.command == "qqmusic":
        code = command_qqmusic(args)
    elif args.command == "shortcut":
        code = command_shortcut(args)
    else:
        raise AssertionError(f"unhandled station command: {args.command}")
    raise SystemExit(code)
