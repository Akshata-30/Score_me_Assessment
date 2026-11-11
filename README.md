# Resilient Decision System

A configurable workflow engine that validates incoming requests, runs decision rules, retries a simulated dependency, and explains each outcome. Employee onboarding is the main workflow. The application approval and claim examples also run on the same engine using their own JSON policies.

## Run it

From this directory, with Python 3.9 or newer. No packages, credentials, or network connection are required.

```sh
python3 examples/demo.py
python3 -m unittest discover -s tests -v
```

The demo takes a few seconds and uses a temporary database. A Finance hire waits for an HR directory retry while a new policy takes effect. The demo replays the original submission, compares the two policy outcomes, and records an HR decision.

## Use the CLI

```sh
python3 -m decision publish configs/onboarding-v1.json
python3 -m decision submit onboarding --key hire-001 --input examples/onboarding.json
```

Copy the returned ID into the following commands. The example input uses the `EMP-FLAKY` employee ID, so the first run schedules a retry after two seconds.

```sh
python3 -m decision run REQUEST_ID
# After the retry is due:
python3 -m decision work
python3 -m decision show REQUEST_ID
```

| Command | Result |
|---|---|
| `publish FILE` | Validate and activate a version of a workflow. Publishing the same version with different contents fails. |
| `submit WORKFLOW --key KEY --input FILE` | Validate and queue a request. A replay returns its original ID and current status. |
| `run ID` | Advance one request to a terminal result, manual review, or a future retry. Safe to run again. |
| `work --limit 100` | Advance a batch of queued and due requests once. Invoke periodically if automatic pickup is needed. |
| `show ID` | Return the input snapshot, policy version, current state, and ordered audit trail. |
| `review ID approve\|reject --reviewer NAME --reason TEXT` | Resolve a request waiting for review and record who decided and why. |

Use `--db PATH` before the command to select a database; the default is `decision.db`. Results are JSON on stdout. Errors are JSON on stderr: exit 2 for invalid input or a local error, 3 for conflicts, and 4 for missing workflows or requests.

For onboarding, the CLI status `approved` means **Ready for onboarding**, and `manual_review` means **Needs HR review**. The workflow checks paperwork presence, verifies the employee ID, and applies department training rules. HR may approve or reject a paused request with a reason.

The directory uses synthetic fixtures. `EMP-FLAKY` fails temporarily on the first attempt, `EMP-OUTAGE` always fails temporarily, and `EMP-MISMATCH` returns a definitive mismatch. Other nonblank employee IDs pass. In the application example, the verification simulator treats `flaky` as temporarily unavailable on the first attempt, `outage` as always unavailable, and `invalid` as a definitive failed check. Other document references pass. These are synthetic examples; the system does not contact an HR directory or document service.

## Change a workflow

1. Copy an existing policy file and give it a new `version` for the same workflow, or a new `name` to define a separate workflow.
2. Change field definitions, rules, branch targets, or retry settings. For example, [onboarding v2](configs/onboarding-v2.json) extends the security training requirement from Engineering to Finance.
3. Publish the file. New submissions use the active version; existing requests, retries, and reviews keep their original version. Publish a previous, unchanged definition to reactivate it for future submissions.

| Configuration | Meaning |
|---|---|
| `fields` | Named fields with a scalar `type` and `required` flag; numeric fields can specify `min`, and any field can specify `enum`. Unknown input keys are rejected. |
| `start`, `stages` | The entry stage and a named, acyclic graph of stages. Every stage must be reachable. |
| `rule` | Test `field` with `present`, `eq`, `ne`, `lt`, `lte`, `gt`, or `gte`; follow `pass` or `fail`. A missing or blank string field fails a rule. Ordered comparisons are numeric. |
| `verify` | Verify a required string field, follow `pass` or `fail`, or send the request to the `exhausted` review stage. Configure `max_attempts` and the initial `delay_seconds`. |
| `review` | Pause until a reviewer chooses `approve` or `reject`. Each choice points to the matching terminal stage. |
| `end` | Finish with `outcome` set to `approved` or `rejected`. |

See [Architecture](ARCHITECTURE.md) for design choices and scaling considerations, and [Decision examples](examples/DECISIONS.md) for inputs and the resulting audit reasoning.
