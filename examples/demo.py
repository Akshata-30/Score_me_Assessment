"""End-to-end CLI demo using a temporary database and synthetic inputs."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def show(label, output):
    print(f"\n{label}\n{json.dumps(output, indent=2)}")


def main():
    with tempfile.TemporaryDirectory() as directory:
        def cli(*args):
            return json.loads(subprocess.check_output(
                [sys.executable, "-m", "decision", "--db", str(Path(directory) / "demo.db"), *args],
                cwd=ROOT, text=True,
            ))

        cli("publish", "configs/onboarding-v1.json")
        submit = ("submit", "onboarding", "--input", "examples/onboarding.json", "--key")
        first = cli(*submit, "hire-001")
        pending = cli("run", first["id"])
        assert pending["request"]["status"] == "retry_wait"
        show("1. The HR directory is temporarily unavailable. The retry is saved.", pending)

        cli("publish", "configs/onboarding-v2.json")
        replay = cli(*submit, "hire-001")
        assert replay["duplicate"] and replay["id"] == first["id"]
        show("2. A duplicate submission returns the existing request.", replay)

        second = cli(*submit, "hire-002")
        waiting = cli("run", second["id"])
        due_at = max(pending["request"]["due_at"], waiting["request"]["due_at"])
        time.sleep(max(0, due_at - time.time()) + 0.05)
        cli("work")

        first_result = cli("show", first["id"])
        second_result = cli("show", second["id"])
        assert (first_result["request"]["version"], first_result["request"]["status"]) == ("1", "approved")
        assert (second_result["request"]["version"], second_result["request"]["status"]) == ("2", "manual_review")
        show("3. The original Finance hire is ready for onboarding under v1.", first_result)
        show("4. The same input needs HR review under v2 because training is incomplete.", second_result)

        result = cli("review", second["id"], "reject", "--reviewer", "Demo HR reviewer",
                     "--reason", "Security training must be completed before this hire can proceed.")
        assert result["request"]["status"] == "rejected"
        show("5. HR's decision and reason appear in the audit trail.", result["audit"][-1])


if __name__ == "__main__":
    main()
