from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from audience_of_one.adapters.phone import (
    MCPPhoneTransport,
    PhoneError,
    PhoneMusicPlayer,
    PhoneUncertainError,
    PhoneVoicePlayer,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeTransport:
    def __init__(self, reads):
        self.files = {}
        self.reads = list(reads)
        self.deletes = []

    def delete(self, path, *, missing_ok=True):
        self.deletes.append(path)
        self.files.pop(path, None)

    def write(self, path, content, *, append=False):
        if append:
            self.files[path] = self.files.get(path, "") + content
        else:
            self.files[path] = content

    def replace(self, path, content):
        self.files[path] = content

    def read(self, path):
        if not self.reads:
            return ""
        return self.reads.pop(0)


def ack(*events):
    return "\n".join(
        f'{{"event":"{name}","at":{at},"detail":"test"}}'
        for name, at in events
    )


class PhoneVoicePlayerTests(unittest.TestCase):
    def test_integrity_stage_and_four_receipts_confirm_ducked_playout(self):
        start = ack(("duck_started", 1), ("voice_started", 2))
        finish = ack(
            ("duck_started", 1),
            ("voice_started", 2),
            ("duck_finished", 3),
            ("voice_finished", 4),
        )
        transport = FakeTransport(["", start, finish])
        clock = FakeClock()
        player = PhoneVoicePlayer(
            transport,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            chunk_size=1024,
        )
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "liner.mp3"
            audio.write_bytes(b"an audio payload")
            receipt = player.play(audio, 2.0, remote_id="wildcard-attempt-1")

        encoded = transport.files["station-inbox/wildcard-attempt-1.b64"]
        self.assertTrue(encoded.endswith("\nEND.\n"))
        self.assertEqual(base64.b64decode(encoded.removesuffix("\nEND.\n")), b"an audio payload")
        integrity = transport.files["station-inbox/wildcard-attempt-1.integrity"]
        self.assertIn("size=16", integrity)
        self.assertIn("duck=1", integrity)
        self.assertIn("ready=1", integrity)
        self.assertTrue(receipt["voice_confirmed"])
        self.assertTrue(receipt["duck_confirmed"])
        self.assertTrue(receipt["ack_order_valid"])

    def test_voice_failure_is_never_reported_as_played(self):
        transport = FakeTransport(["", ack(("voice_failed", 1))])
        clock = FakeClock()
        player = PhoneVoicePlayer(
            transport,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "liner.mp3"
            audio.write_bytes(b"audio")
            with self.assertRaisesRegex(PhoneError, "voice failed"):
                player.play(audio, 1.0, remote_id="wildcard-attempt-2")

    def test_voice_can_be_confirmed_without_inventing_a_duck_receipt(self):
        start = ack(("duck_failed", 1), ("voice_started", 2))
        finish = ack(("duck_failed", 1), ("voice_started", 2), ("voice_finished", 3))
        transport = FakeTransport(["", start, finish])
        clock = FakeClock()
        player = PhoneVoicePlayer(
            transport,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "liner.mp3"
            audio.write_bytes(b"audio")
            receipt = player.play(audio, 1.0, remote_id="wildcard-attempt-3")
        self.assertTrue(receipt["voice_confirmed"])
        self.assertFalse(receipt["duck_confirmed"])
        self.assertFalse(receipt["ack_order_valid"])

    def test_no_duck_request_is_explicit_in_integrity_manifest(self):
        start = ack(("voice_started", 1))
        finish = ack(("voice_started", 1), ("voice_finished", 2))
        transport = FakeTransport(["", start, finish])
        clock = FakeClock()
        player = PhoneVoicePlayer(
            transport, monotonic=clock.monotonic, sleep=clock.sleep,
        )
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "liner.mp3"
            audio.write_bytes(b"audio")
            receipt = player.play(
                audio, 1.0, remote_id="hard-transition", duck=False
            )
        self.assertIn(
            "duck=0", transport.files["station-inbox/hard-transition.integrity"]
        )
        self.assertFalse(receipt["duck_confirmed"])
        self.assertTrue(receipt["ack_order_valid"])

    def test_remote_id_rejects_path_traversal(self):
        player = PhoneVoicePlayer(FakeTransport([]))
        with self.assertRaisesRegex(PhoneError, "unsafe"):
            player.play(Path("missing.mp3"), 1.0, remote_id="../escape")

    def test_custom_runtime_directories_can_be_selected(self):
        start = ack(("voice_started", 1))
        finish = ack(("voice_started", 1), ("voice_finished", 2))
        transport = FakeTransport(["", start, finish])
        clock = FakeClock()
        player = PhoneVoicePlayer(
            transport,
            inbox_dir="custom-voice-inbox",
            ack_dir="custom-acks",
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with tempfile.TemporaryDirectory() as raw:
            audio = Path(raw) / "liner.mp3"
            audio.write_bytes(b"audio")
            player.play(audio, 1.0, remote_id="custom-voice", duck=False)
        self.assertIn("custom-voice-inbox/custom-voice.b64", transport.files)
        self.assertIn("custom-voice-inbox/custom-voice.integrity", transport.files)

    def test_completed_receipt_is_reused_without_replaying_audio(self):
        complete = ack(
            ("duck_started", 1),
            ("voice_started", 2),
            ("duck_finished", 3),
            ("voice_finished", 4),
        )
        transport = FakeTransport([complete])
        player = PhoneVoicePlayer(transport)
        receipt = player.play(Path("does-not-need-to-exist.mp3"), 1.0, remote_id="same-attempt")
        self.assertTrue(receipt["reused_completed_receipt"])
        self.assertTrue(receipt["played"])
        self.assertEqual(transport.files, {})

    def test_incomplete_started_receipt_refuses_automatic_replay(self):
        transport = FakeTransport([ack(("duck_started", 1), ("voice_started", 2))])
        player = PhoneVoicePlayer(transport)
        with self.assertRaisesRegex(PhoneUncertainError, "refusing automatic replay"):
            player.play(Path("missing.mp3"), 1.0, remote_id="uncertain-attempt")
        self.assertEqual(transport.files, {})


class PhoneMusicPlayerTests(unittest.TestCase):
    @staticmethod
    def receipts(url_hash: str) -> str:
        return "\n".join([
            json.dumps({
                "event": "track_started", "at": 1,
                "detail": f"url-{url_hash}",
            }),
            json.dumps({
                "event": "track_position", "at": 2,
                "detail": (
                    f"url-{url_hash} progress-1250 duration-240000 "
                    "volume-100 repeat-off"
                ),
            }),
        ])

    def test_expiring_stream_is_staged_and_requires_matching_position_receipt(self):
        url = "https://cdn.example/record.m4a?ticket=private"
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        transport = FakeTransport(["", self.receipts(url_hash)])
        clock = FakeClock()
        player = PhoneMusicPlayer(
            transport, monotonic=clock.monotonic, sleep=clock.sleep,
        )
        receipt = player.play({
            "uri": "qqmusic:MID123",
            "url": url,
            "url_expiration_seconds": 7200,
            "name": "Record",
            "artists": ["Artist"],
            "duration_ms": 240_000,
        }, remote_id="phone-track-1")

        encoded = transport.files["station-music-inbox/phone-track-1.b64"]
        payload = json.loads(base64.b64decode(
            encoded.removesuffix("\nEND.\n")
        ))
        self.assertEqual(payload["url"], url)
        self.assertTrue(receipt["confirmed"])
        self.assertTrue(receipt["music_bed"]["confirmed"])
        self.assertEqual(receipt["playback"]["repeat_state"], "off")
        self.assertNotIn(url, json.dumps(receipt))

    def test_position_receipt_for_another_stream_fails_closed(self):
        url = "https://cdn.example/record.m4a?ticket=one"
        wrong = "f" * 64
        transport = FakeTransport(["", self.receipts(wrong)])
        clock = FakeClock()
        player = PhoneMusicPlayer(
            transport, monotonic=clock.monotonic, sleep=clock.sleep,
        )
        with self.assertRaisesRegex(PhoneError, "different stream"):
            player.play({
                "uri": "qqmusic:MID123", "url": url,
                "url_expiration_seconds": 7200,
            }, remote_id="phone-track-2")

    def test_started_without_position_refuses_to_replay_stream(self):
        transport = FakeTransport([json.dumps({
            "event": "track_started", "at": 1, "detail": "url-abc",
        })])
        player = PhoneMusicPlayer(transport)
        with self.assertRaisesRegex(PhoneError, "refusing automatic replay"):
            player.play({
                "uri": "qqmusic:MID123",
                "url": "https://cdn.example/record.m4a?ticket=one",
            }, remote_id="phone-track-3")
        self.assertEqual(transport.files, {})

    def test_custom_music_directory_can_be_selected(self):
        url = "https://cdn.example/record.m4a?ticket=private"
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        transport = FakeTransport(["", self.receipts(url_hash)])
        clock = FakeClock()
        player = PhoneMusicPlayer(
            transport,
            inbox_dir="custom-music-inbox",
            ack_dir="custom-acks",
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        player.play({
            "uri": "qqmusic:MID123",
            "url": url,
            "url_expiration_seconds": 7200,
        }, remote_id="custom-track")
        self.assertIn("custom-music-inbox/custom-track.b64", transport.files)
        self.assertIn("custom-music-inbox/custom-track.integrity", transport.files)

class MCPPhoneTransportTests(unittest.TestCase):
    def test_session_and_tool_call_use_bearer_auth_without_returning_the_token(self):
        class Response:
            def __init__(self, body, headers=None):
                self.body = body.encode()
                self.headers = headers or {}

            def read(self):
                return self.body

        responses = [
            Response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), {
                "mcp-session-id": "session-1",
            }),
            Response("null"),
            Response(json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "written"}]},
            })),
        ]
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return responses.pop(0)

        transport = MCPPhoneTransport(
            "https://phone.invalid/mcp",
            "secret-test-token",
            "public-location",
            opener=opener,
        )
        output = transport.call("android_write_file", {
            "location_id": "public-location",
            "path": "station-inbox/test",
            "content": "hello",
        })
        self.assertEqual(output, "written")
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0][0].get_header("Authorization"), "Bearer secret-test-token")
        self.assertEqual(requests[0][0].get_header("User-agent"), "Audience-of-One/0.1")
        self.assertIsNone(requests[0][0].get_header("Mcp-session-id"))
        self.assertEqual(requests[1][0].get_header("Mcp-session-id"), "session-1")
        self.assertNotIn("secret-test-token", output)

    def test_optional_storage_prefix_stays_below_the_authorized_location(self):
        transport = MCPPhoneTransport(
            "https://phone.invalid/mcp", "token", "builtin:music",
            path_prefix="AudienceOfOne",
        )
        self.assertEqual(
            transport._path("station-acks/item.jsonl"),
            "AudienceOfOne/station-acks/item.jsonl",
        )
        with self.assertRaisesRegex(PhoneError, "stay below"):
            transport._path("../outside")
        transport.call = lambda *_args, **_kwargs: (
            "CAUTION: transport annotation\n1| first\n2| second"
        )
        self.assertEqual(transport.read("receipt.jsonl"), "first\nsecond")

    def test_explicitly_expired_session_is_reinitialized_once(self):
        class Response:
            def __init__(self, body, session=None):
                self.body = json.dumps(body).encode() if body else b""
                self.headers = {"mcp-session-id": session} if session else {}

            def read(self):
                return self.body

        responses = [
            Response({"result": {}}, "session-old"),
            Response({}),
            Response({"error": {"message": "session expired"}}),
            Response({"result": {}}, "session-new"),
            Response({}),
            Response({"result": {"content": [{"type": "text", "text": "ok"}]}}),
        ]

        def opener(request, timeout):
            return responses.pop(0)

        transport = MCPPhoneTransport(
            "https://phone.invalid/mcp", "token", "location", opener=opener
        )
        self.assertEqual(transport.call("android_read_file", {}), "ok")
        self.assertEqual(transport.session_id, "session-new")
        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main()
