from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from audience_of_one import config as station_config

ROOT = Path(__file__).resolve().parents[1]


def run_station(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "audience_of_one", *args]
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT / "src")
    if env:
        merged.update(env)
    return subprocess.run(
        command, cwd=ROOT, env=merged, text=True, capture_output=True, check=False
    )


class StationCLITests(unittest.TestCase):
    def test_help_exposes_station_vocabulary(self):
        result = run_station("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            "init", "config", "doctor", "open", "say", "rundown", "covers", "liners",
            "list", "spotify", "mpv", "toggle", "fader", "off",
        ):
            self.assertIn(command, result.stdout)
        self.assertNotIn("ch dj", result.stdout)

    def test_public_transition_surface_uses_blackout_not_dark(self):
        result = run_station("open", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blackout", result.stdout)
        self.assertIn("--phone", result.stdout)
        self.assertNotIn(",dark", result.stdout)

    def test_init_uses_isolated_paths_and_never_writes_private_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config" / "config.toml"
            state = root / "state"
            result = run_station("--config", str(config), "--state-dir", str(state), "init")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.is_file())
            self.assertTrue(state.is_dir())
            text = config.read_text()
            self.assertIn("MINIMAX_API_KEY", text)
            self.assertIn("ELEVENLABS_API_KEY", text)
            self.assertIn("SPOTIFY_CLIENT_ID", text)
            self.assertNotIn("SPOTIFY_CLIENT_SECRET", text)
            self.assertNotIn("~/.claude", text)
            self.assertNotIn("api_key =", text)
            self.assertIn("STATION_PHONE_MCP_URL", text)
            self.assertIn("liner_chance = 0.25", text)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_enabled_android_reports_its_explicit_setup_actions(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["android"]["enabled"] = True
        with mock.patch.dict(os.environ, {}, clear=True):
            actions = station_config.validate(data)
        self.assertIn("set android.location_id", actions)
        self.assertIn("set environment variable STATION_PHONE_MCP_URL", actions)
        self.assertIn("set environment variable STATION_PHONE_MCP_TOKEN", actions)

    def test_android_is_an_output_route_not_an_unshipped_host_mode(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["station"]["mode"] = "android"
        with self.assertRaisesRegex(station_config.ConfigError, "output route"):
            station_config.validate(data)

    def test_mobile_profile_requires_an_explicit_transport_choice(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["android"].update({"enabled": True, "transport": ""})
        with mock.patch.dict(os.environ, {}, clear=True):
            actions = station_config.validate(data)
        self.assertIn(
            "choose android.transport: tailscale when no other VPN must remain, "
            "or cloudflared when one must; lan is fallback",
            actions,
        )

    def test_improv_probability_outside_unit_interval_is_rejected(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["improv"]["liner_chance"] = 1.01
        with self.assertRaisesRegex(station_config.ConfigError, "from 0 to 1"):
            station_config.validate(data)

    def test_spotify_ad_wait_must_be_positive(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["spotify"]["ad_wait_seconds"] = 0
        with self.assertRaisesRegex(station_config.ConfigError, "positive number"):
            station_config.validate(data)

    def test_spotify_playlist_selectors_are_strings(self):
        data = tomllib.loads(station_config.DEFAULT_CONFIG)
        data["spotify"]["primary_playlist"] = ["not", "a", "selector"]
        with self.assertRaisesRegex(station_config.ConfigError, "primary_playlist"):
            station_config.validate(data)

    def test_macos_smoke_provider_does_not_require_voice_id(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            result = run_station(
                "--config", str(ROOT / "examples" / "config.macos-smoke.toml"),
                "--state-dir", str(state), "config", "validate",
                env={"SPOTIFY_CLIENT_ID": "fake-public-id"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("voice_id", result.stdout)

    def test_macos_free_profile_needs_no_spotify_oauth_or_client_id(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            state.mkdir(mode=0o700)
            result = run_station(
                "--config", str(ROOT / "examples" / "config.macos-free.toml"),
                "--state-dir", str(state), "doctor", "--json",
            )
            payload = json.loads(result.stdout)
            spotify = next(
                check for check in payload["checks"] if check["component"] == "spotify_auth"
            )
            engine = next(
                check for check in payload["checks"] if check["component"] == "broadcast_engine"
            )
            self.assertEqual(spotify["status"], "pass")
            self.assertIn("not required", spotify["detail"])
            self.assertEqual(engine["status"], "pass")
            self.assertIn("macos_applescript", engine["detail"])

    def test_local_profile_needs_no_spotify_account_and_requires_its_library(self):
        data = tomllib.loads((ROOT / "examples" / "config.local-mpv.toml").read_text())
        with mock.patch.dict(os.environ, {}, clear=True):
            actions = station_config.validate(data)
        self.assertNotIn("set environment variable SPOTIFY_CLIENT_ID", actions)
        data["music"]["library_root"] = ""
        with self.assertRaisesRegex(station_config.ConfigError, "library_root"):
            station_config.validate(data)

    def test_init_refuses_to_replace_config_without_force(self):
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.toml"
            state = Path(raw) / "state"
            first = run_station("--config", str(config), "--state-dir", str(state), "init")
            second = run_station("--config", str(config), "--state-dir", str(state), "init")
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 2)
            self.assertIn("already exists", second.stderr)

    def test_doctor_is_read_only_and_machine_readable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            state = root / "state"
            run_station("--config", str(config), "--state-dir", str(state), "init")
            before = config.read_bytes()
            result = run_station("--config", str(config), "--state-dir", str(state),
                                 "doctor", "--json")
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ready"])
            self.assertIn("broadcast_engine", [check["component"] for check in payload["checks"]])
            self.assertEqual(before, config.read_bytes())

    def test_doctor_rejects_garbage_token_file_instead_of_passing_on_existence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            secrets = state / "secrets"
            secrets.mkdir(parents=True)
            token = secrets / "spotify-tokens.json"
            token.write_text("not json")
            token.chmod(0o600)
            result = run_station(
                "--config", str(ROOT / "examples" / "config.macos-smoke.toml"),
                "--state-dir", str(state), "doctor", "--json",
                env={"SPOTIFY_CLIENT_ID": "fake-public-id"},
            )
            payload = json.loads(result.stdout)
            spotify = next(
                check for check in payload["checks"] if check["component"] == "spotify_auth"
            )
            self.assertEqual(spotify["status"], "action_required")
            self.assertIn("invalid Spotify token cache", spotify["detail"])

    def test_doctor_rejects_group_or_world_accessible_config_and_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_text((ROOT / "examples" / "config.macos-smoke.toml").read_text())
            config.chmod(0o644)
            state = root / "state"
            secrets = state / "secrets"
            secrets.mkdir(parents=True)
            state.chmod(0o777)
            secrets.chmod(0o777)
            token = secrets / "spotify-tokens.json"
            token.write_text(json.dumps({
                "access_token": "fake-access",
                "refresh_token": "fake-refresh",
                "expires_at": 9999999999,
                "scope": "user-modify-playback-state user-read-playback-state",
            }))
            token.chmod(0o600)
            result = run_station(
                "--config", str(config), "--state-dir", str(state),
                "doctor", "--json", env={"SPOTIFY_CLIENT_ID": "fake-public-id"},
            )
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ready"])
            statuses = {check["component"]: check["status"] for check in payload["checks"]}
            self.assertEqual(statuses["config_permissions"], "action_required")
            self.assertEqual(statuses["state"], "action_required")
            self.assertEqual(statuses["secrets_permissions"], "action_required")

    def test_unavailable_command_is_not_exposed(self):
        result = run_station("insert", "spotify:track:example", "hello")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
