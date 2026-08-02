from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audience_of_one import rundown
from audience_of_one.provenance import PROVENANCE
from audience_of_one.transactions import Journal, TransactionError


class RundownTests(unittest.TestCase):
    def test_append_creates_queue_item_and_queued_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(
                state,
                track="spotify:track:example",
                say="Next up.",
                transition="tail",
                phone=True,
            )
            transaction = Journal(state).load(item["filename"])
            self.assertEqual(transaction["state"], "queued")
            self.assertEqual(transaction["provenance"], PROVENANCE)
            self.assertEqual(item["data"]["provenance"], PROVENANCE)
            self.assertEqual(item["data"]["transition"], "tail")
            self.assertEqual(item["data"]["output"], "phone")
            self.assertEqual((state / "queue" / item["filename"]).stat().st_mode & 0o777, 0o600)

    def test_items_keep_mutable_order(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            first = rundown.append(state, track="spotify:track:first")
            second = rundown.append(state, track="spotify:track:second")
            result = rundown.items(state)
            self.assertEqual([item["id"] for item in result], [first["id"], second["id"]])
            self.assertEqual([item["order"] for item in result], [1000, 2000])

    def test_voice_only_cannot_request_repeat(self):
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(rundown.RundownError):
            rundown.append(Path(raw), say="Stay with me.", after="repeat")

    def test_legacy_dark_normalizes_to_blackout(self):
        with tempfile.TemporaryDirectory() as raw:
            item = rundown.append(Path(raw), say="A line", transition="dark")
            self.assertEqual(item["data"]["transition"], "blackout")

    def test_played_item_moves_out_of_live_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, say="Good morning.")
            destination = rundown.archive_played(state, item["filename"])
            self.assertFalse((state / "queue" / item["filename"]).exists())
            self.assertEqual(destination.read_text(), (state / "played" / item["filename"]).read_text())

    def test_get_accepts_id_or_filename_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, say="A line")
            self.assertEqual(rundown.get(state, item["id"])["data"]["say"], "A line")
            self.assertEqual(
                rundown.get(state, item["filename"])["filename"], item["filename"]
            )
            with self.assertRaises(rundown.RundownError):
                rundown.get(state, "../item")


class TransactionTests(unittest.TestCase):
    def test_failed_transaction_requires_station_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            journal = Journal(Path(raw))
            journal.set_state("000000000000001.json", "failed", "injected")
            with self.assertRaisesRegex(TransactionError, "station retry"):
                journal.begin("000000000000001.json")

    def test_reset_keeps_previous_attempt_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            journal = Journal(Path(raw))
            old = journal.ensure("000000000000001.json")
            new = journal.reset("000000000000001.json")
            self.assertEqual(new["previous_attempt_id"], old["attempt_id"])
            self.assertNotEqual(new["attempt_id"], old["attempt_id"])

    def test_event_receipts_carry_project_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            item = rundown.append(state, say="A line")
            Journal(state).begin(item["filename"])
            events = [
                json.loads(line)
                for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(events)
            self.assertTrue(all(event["provenance"] == PROVENANCE for event in events))

    def test_begin_rejects_duplicate_owner_state(self):
        with tempfile.TemporaryDirectory() as raw:
            journal = Journal(Path(raw))
            item = "000000000000001.json"
            journal.begin(item)
            with self.assertRaisesRegex(TransactionError, "already preparing"):
                journal.begin(item)

    def test_component_receipts_merge_without_erasing_preparation(self):
        with tempfile.TemporaryDirectory() as raw:
            journal = Journal(Path(raw))
            item = "000000000000001.json"
            journal.receipt(item, "voice", {"prepared": True})
            data = journal.receipt(item, "voice", {"played": True})
            self.assertEqual(data["voice"], {"prepared": True, "played": True})

    def test_improv_receipt_is_separate_from_dead_air_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            journal = Journal(Path(raw))
            item = "000000000000001.json"
            data = journal.receipt(item, "improv", {
                "wildcard_liner": {"selected": True, "played": False}
            })
            self.assertTrue(data["improv"]["wildcard_liner"]["selected"])
            self.assertEqual(data["recovery"], {})


if __name__ == "__main__":
    unittest.main()
