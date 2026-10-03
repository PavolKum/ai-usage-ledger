"""Explicit local ingestion and ledger queries. No arguments only displays help."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["refresh", "totals", "status"])
    parser.add_argument("--db", help="Explicit ledger destination (required for every command)")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--local", action="store_true", help="Allow discovery and reading of local usage logs during refresh")
    sources.add_argument("--export", action="append", metavar="JSONL", help="Import only these normalized export snapshots; repeat for overlapping files")
    parser.add_argument("--json", action="store_true", help="Pretty-print JSON (otherwise compact JSON)")
    parser.add_argument("--project", help="Filter displayed totals by case-insensitive substring of session directory, assigned project, or upstream project ID; refresh still ingests all selected sources")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if not args.db:
        parser.error("--db is required; choose a private local ledger destination")
    if args.project is not None:
        if args.command == "status":
            parser.error("--project applies to totals or refresh, not status")
        if not args.project.strip():
            parser.error("--project must be a nonempty match string")
    if args.command == "refresh" and not (args.local or args.export):
        parser.error("refresh requires --local permission or explicit --export paths")
    if args.command != "refresh" and (args.local or args.export):
        parser.error("--local and --export are only valid with refresh")
    if args.command != "refresh" and not Path(args.db).is_file():
        parser.error("ledger does not exist; run an explicit refresh first")
    from usage_store import UsageStore
    try:
        with UsageStore(args.db) as store:
            if args.command == "refresh":
                sources = [("export", path) for path in args.export] if args.export else None
                status = store.refresh(sources=sources)
                result = {"totals": store.aggregate(project=args.project), "status": status}
            else:
                status = store.status()
                result = status if args.command == "status" else {"totals": store.aggregate(project=args.project), "status": status}
        print(json.dumps(result, indent=2 if args.json else None, default=str))
        return 0 if status["complete"] else 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
