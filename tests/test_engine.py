import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from decision.engine import Conflict, Engine
from decision.policy import Invalid


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = {"consent": True, "amount": 40000, "document_ref": "valid"}


def policy(name):
    return json.loads((ROOT / "configs" / f"{name}.json").read_text())


class EngineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.db"
        self.now = 100
        self.open()
        self.engine.publish(policy("application-v1"))
        self.engine.publish(policy("claim-v1"))

    def open(self):
        self.engine = Engine(self.path, clock=lambda: self.now)
        self.addCleanup(self.engine.close)

    def submit(self, key="a", **overrides):
        return self.engine.submit("application", key, {**APPLICATION, **overrides})["id"]

    def test_happy_path_has_ordered_explanation(self):
        result = self.engine.run(self.submit(amount=50000))
        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual([e["kind"] for e in result["audit"]],
                         ["submitted", "rule_evaluated", "rule_evaluated",
                          "dependency_checked", "rule_evaluated"])
        self.assertEqual(result["audit"][-1]["input_ref"], ["amount"])
        self.assertEqual(result["audit"][-1]["expected"], 50000)
        for previous, current in zip(result["audit"][1:], result["audit"][2:]):
            self.assertEqual(previous["to"], current["from"])

    def test_rejection_short_circuits_dependency(self):
        result = self.engine.run(self.submit(consent=False, document_ref="outage"))
        self.assertEqual(result["request"]["status"], "rejected")
        self.assertEqual(len(result["audit"]), 2)
        self.assertFalse(result["audit"][-1]["matched"])

    def test_negative_verification_does_not_retry(self):
        result = self.engine.run(self.submit(document_ref="invalid"))
        self.assertEqual(result["request"]["status"], "rejected")
        self.assertEqual(result["audit"][-1]["kind"], "dependency_checked")
        self.assertFalse(result["audit"][-1]["verified"])

    def test_invalid_intake_does_not_consume_key(self):
        for payload in ({"amount": 3}, {**APPLICATION, "amount": True},
                        {**APPLICATION, "amount": 0}, {**APPLICATION, "typo": 1}):
            with self.subTest(payload=payload), self.assertRaises(Invalid):
                self.engine.submit("application", "a", payload)
        self.assertEqual(self.engine.work(), [])
        self.assertIsNotNone(self.submit())

    def test_duplicate_returns_original_and_conflict_preserves_it(self):
        first = self.submit()
        self.engine.run(first)
        replay = self.engine.submit("application", "a", dict(reversed(list(APPLICATION.items()))))
        self.assertEqual(replay, {"id": first, "duplicate": True, "status": "approved"})
        with self.assertRaises(Conflict):
            self.submit(amount=99999)
        self.assertEqual(len(self.engine.show(first)["audit"]), 5)

    def test_concurrent_duplicate_submissions(self):
        def submit():
            engine = Engine(self.path)
            try:
                return engine.submit("application", "concurrent", APPLICATION)
            finally:
                engine.close()

        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: submit(), range(8)))
        self.assertEqual(len({row["id"] for row in results}), 1)
        self.assertEqual(sum(not row["duplicate"] for row in results), 1)
        self.assertEqual(len(self.engine.show(results[0]["id"])["audit"]), 1)

    def test_retry_survives_restart_and_respects_due_time(self):
        request = self.submit(document_ref="flaky")
        pending = self.engine.run(request)
        self.assertEqual(pending["request"]["status"], "retry_wait")
        self.assertEqual(pending["request"]["due_at"], 102)
        self.engine.close()
        self.open()
        self.now = 101
        self.assertEqual(self.engine.work(), [])
        self.assertEqual(self.engine.run(request), pending)
        self.now = 102
        self.assertEqual(self.engine.work()[0]["status"], "approved")
        trace = self.engine.show(request)["audit"]
        attempts = [event for event in trace if "operation_id" in event]
        self.assertEqual([event["attempt"] for event in attempts], [1, 2])
        self.assertEqual(attempts[0]["operation_id"], attempts[1]["operation_id"])

    def test_outage_exhaustion_and_manual_rejection(self):
        request = self.submit(document_ref="outage")
        self.assertEqual(self.engine.run(request)["request"]["due_at"], 102)
        self.now = 102
        self.assertEqual(self.engine.run(request)["request"]["due_at"], 106)
        self.now = 106
        exhausted = self.engine.run(request)
        self.assertEqual(exhausted["audit"][-1]["kind"], "retry_exhausted")
        self.assertEqual(exhausted["audit"][-1]["attempt"], 3)
        self.assertEqual(exhausted["request"]["status"], "manual_review")
        result = self.engine.review(request, "reject", "analyst", "Could not verify the document.")
        self.assertEqual(result["request"]["status"], "rejected")
        self.assertEqual(result["audit"][-1]["reviewer"], "analyst")
        with self.assertRaises(Conflict):
            self.engine.review(request, "approve", "analyst", "Duplicate review")

    def test_config_change_only_applies_to_new_requests(self):
        old = self.submit(document_ref="flaky")
        self.assertEqual(self.engine.run(old)["request"]["status"], "retry_wait")
        self.engine.publish(policy("application-v2"))
        self.assertEqual(self.submit(document_ref="flaky"), old)
        self.now = 102
        approved = self.engine.run(old)
        newer = self.engine.run(self.submit(key="b"))
        self.assertEqual((approved["request"]["version"], approved["request"]["status"]), ("1", "approved"))
        self.assertEqual((newer["request"]["version"], newer["request"]["status"]), ("2", "manual_review"))
        self.assertEqual(newer["audit"][-1]["expected"], 25000)

    def test_bad_policy_cannot_replace_live_policy(self):
        bad = policy("application-v2")
        bad["stages"]["threshold"]["pass"] = "consent"
        with self.assertRaises(Invalid):
            self.engine.publish(bad)
        replacement = policy("application-v1")
        replacement["stages"]["threshold"]["value"] = 1
        with self.assertRaises(Conflict):
            self.engine.publish(replacement)
        self.assertEqual(self.engine.run(self.submit())["request"]["status"], "approved")

    def test_second_workflow_runs_without_engine_changes(self):
        request = self.engine.submit("claim", "a", {"amount": 7000, "policy_active": True})["id"]
        result = self.engine.run(request)
        self.assertEqual(result["request"]["status"], "manual_review")
        self.assertFalse(result["audit"][-1]["present"])
        result = self.engine.review(request, "approve", "analyst", "Verified alternate evidence.")
        self.assertEqual(result["request"]["status"], "approved")

    def test_audit_write_failure_rolls_back_state(self):
        request = self.submit()
        db = self.engine.store.db
        db.execute("""
            CREATE TRIGGER break_audit BEFORE INSERT ON events
            BEGIN SELECT RAISE(ABORT, 'simulated audit storage failure'); END
        """)
        before = self.engine.show(request)
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.run(request)
        self.assertEqual(self.engine.show(request), before)
        db.execute("DROP TRIGGER break_audit")
        self.assertEqual(self.engine.run(request)["request"]["status"], "approved")
