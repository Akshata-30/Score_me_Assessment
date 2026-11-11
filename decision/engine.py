"""Orchestrate durable requests, independent of the CLI."""
import json
import time
from uuid import uuid4

from .policy import canonical, evaluate, require, validate, validate_input
from .store import Store


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


class DependencyUnavailable(Exception):
    pass


def simulated_verifier(reference, attempt, operation_id):
    """Read-only simulator. A real adapter must honor a timeout and operation ID."""
    if reference in {"outage", "EMP-OUTAGE"} or (
        reference in {"flaky", "EMP-FLAKY"} and attempt == 1
    ):
        raise DependencyUnavailable("Verifier unavailable")
    return reference not in {"invalid", "EMP-MISMATCH"}


class Engine:
    def __init__(self, path, clock=time.time, verifier=simulated_verifier):
        self.store = Store(path)
        self.clock = clock
        self.verifier = verifier

    def close(self):
        self.store.close()

    def publish(self, policy):
        validate(policy)
        name, version, definition = policy["name"], policy["version"], canonical(policy)
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT definition FROM policy_versions WHERE name=? AND version=?", (name, version)
            ).fetchone()
            if previous and previous["definition"] != definition:
                raise Conflict(f"{name}@{version} already exists with different content")
            db.execute("INSERT OR IGNORE INTO policy_versions VALUES (?, ?, ?)", (name, version, definition))
            db.execute("INSERT INTO active_policies VALUES (?, ?) ON CONFLICT(name) "
                       "DO UPDATE SET version=excluded.version", (name, version))
        return {"workflow": name, "active_version": version}

    def event(self, db, request_id, kind, details):
        db.execute("INSERT INTO events(request_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                   (request_id, self.clock(), kind, canonical(details)))

    def submit(self, workflow, key, payload):
        require(type(key) is str and 1 <= len(key.strip()) <= 200, "Provide an idempotency key (1–200 characters)")
        encoded = canonical(payload)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE workflow=? AND idem_key=?", (workflow, key)).fetchone()
            if row:
                if row["payload"] != encoded:
                    raise Conflict("Idempotency key already belongs to a different payload")
                return {"id": row["id"], "duplicate": True, "status": row["status"]}
            definition = db.execute(
                "SELECT definition FROM policy_versions JOIN active_policies USING(name, version) WHERE name=?",
                (workflow,)
            ).fetchone()
            if not definition:
                raise Missing(f"No active workflow named {workflow}")
            policy = json.loads(definition["definition"])
            validate_input(policy["fields"], payload)
            request_id = str(uuid4())
            db.execute("INSERT INTO requests(id, workflow, version, idem_key, payload, stage, status) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                       (request_id, workflow, policy["version"], key, encoded, policy["start"]))
            self.event(db, request_id, "submitted", {"policy_version": policy["version"],
                       "input_ref": "request.payload", "to": {"status": "queued", "stage": policy["start"]}})
            return {"id": request_id, "duplicate": False, "status": "queued"}

    def load(self, db, request_id):
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise Missing(f"Unknown request: {request_id}")
        return row

    def policy_for(self, db, row):
        definition = db.execute("SELECT definition FROM policy_versions WHERE name=? AND version=?",
                                (row["workflow"], row["version"])).fetchone()["definition"]
        return json.loads(definition)

    def change(self, db, row, stage, status, kind, details, attempt=0, due_at=None):
        before = {key: row[key] for key in ("stage", "status", "attempt", "due_at")}
        after = dict(stage=stage, status=status, attempt=attempt, due_at=due_at)
        db.execute("UPDATE requests SET stage=?, status=?, attempt=?, due_at=? WHERE id=?",
                   (stage, status, attempt, due_at, row["id"]))
        self.event(db, row["id"], kind, {**details, "from": before, "to": after})

    def advance(self, db, row, policy, target, kind, details):
        stage = policy["stages"][target]
        status = {"review": "manual_review", "end": stage.get("outcome")}.get(stage["type"], "queued")
        self.change(db, row, target, status, kind, details)

    def step(self, request_id):
        with self.store.transaction() as db:
            row = self.load(db, request_id)
            if row["status"] == "retry_wait" and row["due_at"] > self.clock():
                return False
            if row["status"] not in {"queued", "retry_wait"}:
                return False
            policy = self.policy_for(db, row)
            stage = policy["stages"][row["stage"]]
            payload = json.loads(row["payload"])
            kind = stage["type"]
            if kind == "rule":
                target, trace = evaluate(stage, payload)
                self.advance(db, row, policy, target, "rule_evaluated", trace)
            elif kind == "verify":
                attempt = row["attempt"] + 1
                operation = f"{request_id}:{row['stage']}"
                trace = {"input_ref": [stage["field"]], "attempt": attempt,
                         "max_attempts": stage["max_attempts"], "operation_id": operation}
                try:
                    passed = self.verifier(payload[stage["field"]], attempt, operation)
                except DependencyUnavailable as error:
                    trace["reason"] = str(error)
                    if attempt == stage["max_attempts"]:
                        self.advance(db, row, policy, stage["exhausted"], "retry_exhausted", trace)
                    else:
                        delay = min(3600, stage["delay_seconds"] * 2 ** (attempt - 1))
                        self.change(db, row, row["stage"], "retry_wait", "retry_scheduled",
                                    trace, attempt, self.clock() + delay)
                else:
                    self.advance(db, row, policy, stage["pass" if passed else "fail"],
                                 "dependency_checked", {**trace, "verified": passed})
            else:
                self.advance(db, row, policy, row["stage"], "stage_entered", {})
            return True

    def run(self, request_id):
        while self.step(request_id):
            pass
        return self.show(request_id)

    def work(self, limit=100):
        require(type(limit) is int and 1 <= limit <= 1000, "Limit must be 1–1000")
        ids = [row["id"] for row in self.store.db.execute(
            "SELECT id FROM requests WHERE status='queued' OR (status='retry_wait' AND due_at<=?) "
            "ORDER BY rowid LIMIT ?", (self.clock(), limit)
        )]
        return [self.run(request_id)["request"] for request_id in ids]

    def review(self, request_id, choice, reviewer, reason):
        require(choice in {"approve", "reject"} and reviewer.strip() and reason.strip(),
                "Review requires a choice, reviewer, and reason")
        with self.store.transaction() as db:
            row = self.load(db, request_id)
            if row["status"] != "manual_review":
                raise Conflict("Request is not awaiting manual review")
            policy = self.policy_for(db, row)
            stage = policy["stages"][row["stage"]]
            self.advance(db, row, policy, stage[choice], "review_decided",
                         {"choice": choice, "reviewer": reviewer, "reason": reason})
        return self.show(request_id)

    def show(self, request_id):
        with self.store.transaction(write=False) as db:
            row = dict(self.load(db, request_id))
            row["payload"] = json.loads(row["payload"])
            events = [{"seq": event["seq"], "at": event["at"], "kind": event["kind"],
                       **json.loads(event["detail"])} for event in db.execute(
                "SELECT * FROM events WHERE request_id=? ORDER BY seq", (request_id,)
            )]
            return {"request": row, "audit": events}
