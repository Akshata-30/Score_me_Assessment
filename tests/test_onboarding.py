import tempfile
import unittest
from pathlib import Path

from decision.__main__ import read_json
from decision.engine import Engine
from decision.policy import Invalid


ROOT = Path(__file__).resolve().parents[1]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = Engine(Path(directory.name) / "onboarding.db", clock=lambda: self.now)
        self.addCleanup(self.engine.close)
        self.publish(1)
        self.input = read_json(ROOT / "examples/onboarding.json")
        self.input["employee_id"] = "EMP-1042"

    def publish(self, version):
        self.engine.publish(read_json(ROOT / f"configs/onboarding-v{version}.json"))

    def run_hire(self, key, **changes):
        request = self.engine.submit("onboarding", key, {**self.input, **changes})
        return self.engine.run(request["id"])

    def test_training_policy_changes_for_finance(self):
        original = self.run_hire("v1", employee_id="EMP-FLAKY")
        self.assertEqual(original["request"]["status"], "retry_wait")
        self.publish(2)
        updated = self.run_hire("v2", employee_id="EMP-FLAKY")
        self.now = 102
        self.engine.work()
        self.assertEqual(self.engine.show(original["request"]["id"])["request"]["status"], "approved")
        result = self.engine.show(updated["request"]["id"])
        self.assertEqual(result["request"]["status"], "manual_review")
        self.assertEqual(result["audit"][-1]["input_ref"], ["security_training_complete"])

    def test_engineering_training_applies_to_v1(self):
        pending = self.run_hire("missing-training", department="engineering")
        self.assertEqual(pending["request"]["status"], "manual_review")
        ready = self.run_hire("trained", department="engineering", security_training_complete=True)
        self.assertEqual(ready["request"]["status"], "approved")

    def test_blank_document_and_declined_offer_skip_directory(self):
        for key, changes, status in (
            ("paperwork", {"document_ref": " "}, "manual_review"),
            ("offer", {"offer_accepted": False}, "rejected"),
        ):
            with self.subTest(key=key):
                result = self.run_hire(key, employee_id="EMP-OUTAGE", **changes)
                self.assertEqual(result["request"]["status"], status)
                self.assertFalse(any("operation_id" in event for event in result["audit"]))

    def test_directory_mismatch_can_be_resolved_by_hr(self):
        result = self.run_hire("mismatch", employee_id="EMP-MISMATCH")
        self.assertEqual(result["request"]["status"], "manual_review")
        self.assertFalse(result["audit"][-1]["verified"])
        approved = self.engine.review(result["request"]["id"], "approve", "HR", "Confirmed the employee record.")
        self.assertEqual(approved["request"]["status"], "approved")

    def test_outage_exhausts_three_attempts(self):
        result = self.run_hire("outage", employee_id="EMP-OUTAGE")
        self.assertEqual(result["request"]["status"], "retry_wait")
        self.now = 102
        self.engine.work()
        self.now = 106
        self.engine.work()
        result = self.engine.show(result["request"]["id"])
        self.assertEqual(result["request"]["status"], "manual_review")
        self.assertEqual(result["audit"][-1]["kind"], "retry_exhausted")
        self.assertEqual(result["audit"][-1]["attempt"], 3)

    def test_invalid_department_is_rejected_at_intake(self):
        with self.assertRaises(Invalid):
            self.run_hire("invalid", department="unknown")
