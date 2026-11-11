"""Validate declarative policies and evaluate rules without executing user code."""
import json
import math


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def matches_type(value, kind):
    return {
        "string": lambda: type(value) is str,
        "boolean": lambda: type(value) is bool,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) is int or (type(value) is float and math.isfinite(value)),
    }[kind]()


TYPES = {"string", "boolean", "integer", "number"}
OUTCOMES = {"approved", "rejected"}
COMPARATORS = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
               "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
               "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}


def shape(obj, required, optional=()):
    require(type(obj) is dict, "Expected a JSON object")
    require(set(required) <= obj.keys() <= set(required) | set(optional),
            f"Expected keys {', '.join(required)}; optional: {', '.join(optional) or 'none'}")


def validate(policy):
    shape(policy, ("name", "version", "fields", "start", "stages"))
    for key in ("name", "version", "start"):
        require(type(policy[key]) is str and policy[key].strip(), f"Invalid {key}")
    fields, stages = policy["fields"], policy["stages"]
    require(type(fields) is dict and fields, "Define at least one input field")
    for name, spec in fields.items():
        require(name and "." not in name, f"Invalid field name: {name}")
        shape(spec, ("type", "required"), ("min", "enum"))
        kind = spec["type"]
        require(type(kind) is str and kind in TYPES, f"Invalid type for {name}")
        require(type(spec["required"]) is bool, f"Invalid required flag for {name}")
        if "min" in spec:
            require(kind in {"integer", "number"} and matches_type(spec["min"], kind),
                    f"Invalid minimum for {name}")
        if "enum" in spec:
            require(type(spec["enum"]) is list and spec["enum"] and
                    all(matches_type(x, kind) for x in spec["enum"]), f"Invalid enum for {name}")
    require(type(stages) is dict and policy["start"] in stages, "Start stage does not exist")

    def field_kind(field):
        require(type(field) is str and field in fields, f"Unknown input field: {field}")
        return fields[field]["type"]

    edges = {}
    for name, stage in stages.items():
        require(type(stage) is dict, f"Invalid stage: {name}")
        kind = stage.get("type")
        if kind == "rule":
            shape(stage, ("type", "field", "op", "pass", "fail", "reason"), ("value",))
            field_type = field_kind(stage["field"])
            op = stage["op"]
            require(type(op) is str and (op == "present" or op in COMPARATORS), f"Invalid operator: {op}")
            require(type(stage["reason"]) is str and stage["reason"].strip(), "Rule needs a reason")
            if op == "present":
                require("value" not in stage, "present does not accept a value")
            else:
                require("value" in stage and matches_type(stage["value"], field_type), "Invalid rule value")
                require(op in {"eq", "ne"} or field_type in {"integer", "number"},
                        "Ordered comparisons need a numeric field")
            edges[name] = [stage["pass"], stage["fail"]]
        elif kind == "verify":
            shape(stage, ("type", "field", "pass", "fail", "exhausted", "max_attempts", "delay_seconds"))
            require(field_kind(stage["field"]) == "string" and fields[stage["field"]]["required"],
                    "Verifier needs a required string field")
            require(type(stage["max_attempts"]) is int and 1 <= stage["max_attempts"] <= 10,
                    "max_attempts must be 1–10")
            require(type(stage["delay_seconds"]) is int and 1 <= stage["delay_seconds"] <= 3600,
                    "delay_seconds must be 1–3600")
            edges[name] = [stage["pass"], stage["fail"], stage["exhausted"]]
        elif kind == "review":
            shape(stage, ("type", "approve", "reject"))
            edges[name] = [stage["approve"], stage["reject"]]
        elif kind == "end":
            shape(stage, ("type", "outcome"))
            require(type(stage["outcome"]) is str and stage["outcome"] in OUTCOMES, "Invalid outcome")
            edges[name] = []
        else:
            raise Invalid(f"Unknown stage type: {kind}")
        require(all(type(target) is str and target in stages for target in edges[name]),
                f"Invalid transition from {name}")
    # Review decisions become final atomically with the review record.
    for name, stage in stages.items():
        targets = {"approve": "approved", "reject": "rejected"} if stage["type"] == "review" else {}
        for key, outcome in targets.items():
            require(stages[stage[key]].get("outcome") == outcome, f"{name}: {key} must end as {outcome}")
        if stage["type"] == "verify":
            require(stages[stage["exhausted"]]["type"] == "review", "Retry exhaustion must route to review")
    visiting, visited = set(), set()

    def visit(name):
        require(name not in visiting, f"Workflow contains a cycle at {name}")
        if name in visited:
            return
        visiting.add(name)
        for target in edges[name]:
            visit(target)
        visiting.remove(name)
        visited.add(name)

    require(len(stages) <= 100, "Limit workflows to 100 stages")
    visit(policy["start"])
    require(visited == stages.keys(), "All stages must be reachable from start")


def validate_input(fields, payload):
    require(type(payload) is dict, "Request must be a JSON object")
    unknown = sorted(payload.keys() - fields.keys())
    require(not unknown, f"Unexpected fields: {', '.join(unknown)}")
    for name, spec in fields.items():
        if name not in payload:
            require(not spec["required"], f"Missing required field: {name}")
            continue
        value = payload[name]
        require(matches_type(value, spec["type"]), f"{name} must be {spec['type']}")
        if "min" in spec:
            require(value >= spec["min"], f"{name} must be at least {spec['min']}")
        if "enum" in spec:
            require(value in spec["enum"], f"{name} must be one of {spec['enum']}")


def evaluate(stage, payload):
    field, op = stage["field"], stage["op"]
    value = payload.get(field)
    present = field in payload and (not isinstance(value, str) or bool(value.strip()))
    passed = present and (op == "present" or COMPARATORS[op](value, stage["value"]))
    details = {"input_ref": [field], "operator": op, "present": present,
               "matched": passed, "reason": stage["reason"]}
    if op != "present":
        details["expected"] = stage["value"]
    return stage["pass" if passed else "fail"], details
