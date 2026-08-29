from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from audience_of_one.adapters.phone import MCPPhoneTransport, PhoneError
from audience_of_one.cli import doctor_payload

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "android" / "termux" / "station_phone_mcp.py"
SPEC = importlib.util.spec_from_file_location("station_phone_mcp", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER_MODULE)


class PhoneRuntimeTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = SERVER_MODULE.StationMCPServer(
            ("127.0.0.1", 0), root=self.root,
            token="test-token-with-at-least-thirty-two-characters",
            location_id="station",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.transport = MCPPhoneTransport(
            f"http://{host}:{port}/mcp",
            "test-token-with-at-least-thirty-two-characters",
            "station",
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_public_transport_writes_appends_reads_and_deletes_below_root(self):
        self.transport.write("station-inbox/test.b64", "chunk-one")
        self.transport.write("station-inbox/test.b64", "-two", append=True)
        self.assertEqual(
            self.transport.read("station-inbox/test.b64"), "chunk-one-two"
        )
        self.transport.delete("station-inbox/test.b64")
        self.assertEqual(self.transport.read("station-inbox/test.b64"), "")

    def test_wrong_location_and_path_escape_fail_closed(self):
        wrong = MCPPhoneTransport(
            self.transport.url, self.transport.token, "somewhere-else"
        )
        with self.assertRaises(PhoneError):
            wrong.write("station-inbox/test", "no")
        with self.assertRaises(PhoneError):
            self.transport.write("../outside", "no")
        self.assertFalse((self.root.parent / "outside").exists())

    def test_wrong_bearer_token_is_rejected(self):
        wrong = MCPPhoneTransport(self.transport.url, "wrong-token", "station")
        with self.assertRaises(PhoneError):
            wrong.read("station-acks/test.jsonl")

    def test_transport_reinitializes_after_phone_runtime_restart(self):
        self.transport.write("station-inbox/test", "still here")
        self.server.sessions.clear()
        self.assertEqual(self.transport.read("station-inbox/test"), "still here")

    def test_phone_doctor_authenticates_and_requires_a_fresh_player_heartbeat(self):
        (self.root / "phone-health.json").write_text(json.dumps({
            "state": "running",
            "pid": 123,
            "updated_at": time.time(),
            "detail": "",
        }))
        config = ROOT / "examples" / "config.android-premium.toml"
        state = self.root / "mac-state"
        state.mkdir(mode=0o700)
        env = {
            "SPOTIFY_CLIENT_ID": "public-test-id",
            "MINIMAX_API_KEY": "test",
            "ELEVENLABS_API_KEY": "test",
            "STATION_PHONE_MCP_URL": self.transport.url,
            "STATION_PHONE_MCP_TOKEN": self.transport.token,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            payload = doctor_payload(config, state, phone=True)
        phone = next(
            check for check in payload["checks"]
            if check["component"] == "phone_transport"
        )
        self.assertEqual(phone["status"], "pass")
        self.assertIn("authenticated MCP", phone["detail"])

    def test_phone_doctor_can_read_an_existing_runtime_health_path(self):
        nested = self.root / "existing-runtime"
        nested.mkdir()
        (nested / "health.json").write_text(json.dumps({
            "state": "running",
            "pid": 456,
            "updated_at": time.time(),
            "detail": "",
        }))
        source = (ROOT / "examples" / "config.android-premium.toml").read_text()
        source = source.replace(
            'path_prefix = ""',
            'path_prefix = ""\nhealth_path = "existing-runtime/health.json"',
        )
        config = self.root / "custom-health.toml"
        config.write_text(source)
        config.chmod(0o600)
        state = self.root / "custom-state"
        state.mkdir(mode=0o700)
        env = {
            "SPOTIFY_CLIENT_ID": "public-test-id",
            "MINIMAX_API_KEY": "test",
            "ELEVENLABS_API_KEY": "test",
            "STATION_PHONE_MCP_URL": self.transport.url,
            "STATION_PHONE_MCP_TOKEN": self.transport.token,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            payload = doctor_payload(config, state, phone=True)
        phone = next(
            check for check in payload["checks"]
            if check["component"] == "phone_transport"
        )
        self.assertEqual(phone["status"], "pass")

    def test_runtime_sources_keep_host_transport_and_device_values_external(self):
        sources = [
            ROOT / "android" / "termux" / "station-phone-player.sh",
            ROOT / "android" / "termux" / "station-phone",
            ROOT / "android" / "termux" / "install.sh",
            ROOT / "android" / "termux" / "station_phone_mcp.py",
            ROOT / "android" / "termux" / "station_phone_mpv.py",
            ROOT / "android" / "termux" / "station-call-in-record.sh",
            ROOT / "android" / "focus-helper" / "app" / "src" / "main"
            / "java" / "io" / "github" / "audienceofone" / "djcontrol"
            / "FocusActivity.java",
            ROOT / "android" / "focus-helper" / "app" / "src" / "main"
            / "java" / "io" / "github" / "audienceofone" / "djcontrol"
            / "RecordActivity.java",
            ROOT / "android" / "focus-helper" / "app" / "src" / "main"
            / "java" / "io" / "github" / "audienceofone" / "djcontrol"
            / "RecorderService.java",
        ]
        forbidden_runtime_defaults = (
            "/Users/",
            "/data/data/com.termux/files/home/",
            ".claude/",
            "trycloudflare.com",
            "argotunnel.com",
            "cloudflared tunnel run",
            "Mac mini",
        )
        for source in sources:
            text = source.read_text(encoding="utf-8")
            for marker in forbidden_runtime_defaults:
                self.assertNotIn(marker, text, f"{marker!r} leaked through {source}")

    def test_call_in_recorder_is_explicit_only_not_a_browser_handler(self):
        manifest = (
            ROOT / "android" / "focus-helper" / "app" / "src" / "main"
            / "AndroidManifest.xml"
        )
        root = ET.parse(manifest).getroot()
        android = "{http://schemas.android.com/apk/res/android}"
        activity = next(
            node for node in root.findall("./application/activity")
            if node.get(f"{android}name") == ".RecordActivity"
        )
        self.assertEqual(activity.get(f"{android}exported"), "true")
        self.assertEqual(activity.findall("intent-filter"), [])

    def test_call_in_view_intent_gets_a_unique_default_clip(self):
        activity = (
            ROOT / "android" / "focus-helper" / "app" / "src" / "main"
            / "java" / "io" / "github" / "audienceofone" / "djcontrol"
            / "RecordActivity.java"
        ).read_text(encoding="utf-8")
        receipt = (
            ROOT / "android" / "termux" / "station_focus_receipt.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"call-in-work/" + timestamp + ".m4a"', activity)
        self.assertIn('f"{clip_id}.m4a"', receipt)
        self.assertIn('notify(f"call-in:{clip_id}")', receipt)

    def test_idle_runtime_blocks_on_private_events_without_a_permanent_wakelock(self):
        player = (ROOT / "android" / "termux" / "station-phone-player.sh").read_text()
        installer = (ROOT / "android" / "termux" / "install.sh").read_text()
        self.assertIn('read -r -t "$HEARTBEAT_SECONDS"', player)
        self.assertIn("$HOME/.local/share/audience-of-one-phone/transport", player)
        boot = installer.split("BOOT_FILE", 1)[-1]
        self.assertNotIn("printf 'termux-wake-lock", boot)

    def test_transport_event_wait_wakes_after_a_complete_queue_item(self):
        initial = self.transport.wait_event(0, 0)
        sequence = initial["sequence"]
        self.transport.write("station-inbox/event.b64", "chunk")
        quiet = self.transport.wait_event(sequence, 0.01)
        self.assertEqual(quiet["sequence"], sequence)
        self.transport.write("station-inbox/event.b64", "\nEND.\n", append=True)
        woke = self.transport.wait_event(sequence, 0.1)
        self.assertGreater(woke["sequence"], sequence)
        self.assertEqual(woke["event"], "queue:event")

    def test_phone_power_lease_is_named_and_expiring(self):
        acquired = self.transport.lease("test:voice", "acquire", 5)
        self.assertIn("test:voice", acquired["active_leases"])
        released = self.transport.lease("test:voice", "release")
        self.assertNotIn("test:voice", released["active_leases"])

    def test_phone_runtime_and_install_handoff_expose_short_human_controls(self):
        control = (ROOT / "android" / "termux" / "station-phone").read_text()
        installer = (ROOT / "android" / "termux" / "install.sh").read_text()
        self.assertIn('basename "$0")" = "fm"', control)
        self.assertIn('action="${1:-toggle}"', control)
        self.assertIn('ln -sf "$BIN/station-phone" "$BIN/fm"', installer)
        self.assertIn("fm (pause/resume), fm off (stop), fm s (status)", installer)


if __name__ == "__main__":
    unittest.main()
