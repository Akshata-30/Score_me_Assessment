"""Command line interface: python3 -m decision --help."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .engine import Conflict, Engine, Missing


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


def main():
    parser = argparse.ArgumentParser(description="Run and explain configurable decisions.")
    parser.add_argument("--db", default="decision.db", help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("publish", help="Validate, register and activate a policy").add_argument("file")
    submit = commands.add_parser("submit", help="Validate and queue a request")
    submit.add_argument("workflow")
    submit.add_argument("--key", required=True)
    submit.add_argument("--input", required=True, help="Path to request JSON")
    for name, help_text in [("run", "Advance one request"), ("show", "Show request and audit"),
                            ("review", "Resolve manual review")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id")
        if name == "review":
            command.add_argument("choice", choices=["approve", "reject"])
            command.add_argument("--reviewer", required=True)
            command.add_argument("--reason", required=True)
    commands.add_parser("work", help="Advance queued and due requests").add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    engine = None
    try:
        engine = Engine(args.db)
        if args.command == "publish":
            result = engine.publish(read_json(args.file))
        elif args.command == "submit":
            result = engine.submit(args.workflow, args.key, read_json(args.input))
        elif args.command == "review":
            result = engine.review(args.id, args.choice, args.reviewer, args.reason)
        elif args.command == "work":
            result = engine.work(args.limit)
        else:
            result = getattr(engine, args.command)(args.id)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error), "kind": type(error).__name__}), file=sys.stderr)
        return 3 if isinstance(error, Conflict) else 4 if isinstance(error, Missing) else 2
    finally:
        if engine:
            engine.close()


if __name__ == "__main__":
    sys.exit(main())
