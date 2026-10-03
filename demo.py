"""Offline, synthetic usage-ledger demonstration; standard library only."""
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from usage_store import UsageStore


def run_demo():
    """Return a verified report; all inputs and outputs live in a temporary folder."""
    with TemporaryDirectory(prefix="synthetic-usage-demo-") as temporary:
        root = Path(temporary)
        event = {
            "machine_id": "fictional-machine", "msg_id": "example-1",
            "session_id": "example-session", "session_title": "Synthetic demo",
            "source": "opencode", "provider": "fictional-provider",
            "model": "fictional/demo-model", "billing_source": "api",
            "session_dir": "/fictional/project-aurora",
            "time": 1767355200000, "input": 1000, "output": 200,
            "cache_read": 0, "cache_write": 0, "reasoning": 0, "cost": 0.02,
        }
        second_event = dict(event, msg_id="example-2", source="codex", billing_source="codex", input=500, output=100)
        second_event.pop("cost")  # Subscription usage has no recorded per-event bill.
        third_event = dict(event, msg_id="example-3", session_id="beacon-session", session_dir="/fictional/project-beacon", input=2000, output=400, cost=0.04)
        fourth_event = dict(second_event, msg_id="example-4", session_id="beacon-session", source="claude-code", billing_source="subscription", session_dir="/fictional/project-beacon")
        first, overlap = root / "first.jsonl", root / "overlap.jsonl"
        first.write_text(json.dumps(event) + "\n", encoding="utf-8")
        overlap.write_text("".join(json.dumps(row) + "\n" for row in [event, second_event, third_event, fourth_event]), encoding="utf-8")
        sources = [("export", str(first)), ("export", str(overlap))]
        with UsageStore(root / "demo.sqlite3") as store:
            store.add_price("fictional/demo-model", {
                "input": 0.000001, "output": 0.000002,
                "cache_read": 0.0000005, "cache_write": 0.000001,
            }, effective_from=event["time"] - 1, provenance="Invented demo tariff; not a vendor price")
            initial = store.refresh(sources=sources)
            totals = store.aggregate()
            projects = {}
            for project in ("project-aurora", "project-beacon"):
                bucket = store.aggregate(project=project)
                projects[project] = {
                    "sources": sorted(store.aggregate(project=project, group="source")),
                    "events": bucket["messages"],
                    "input_tokens": bucket["tokens"]["input"], "output_tokens": bucket["tokens"]["output"],
                    "recorded_cost_usd": round(bucket["recorded_cost"], 6),
                    "recorded_cost_events": bucket["recorded_cost_messages"],
                    "api_equivalent_estimate_usd": round(bucket["api_equivalent_cost"], 6),
                }
            unchanged = store.refresh(sources=sources)
            overlap.write_text("{malformed synthetic snapshot}\n", encoding="utf-8")
            stale = store.refresh(sources=sources)
            preserved = store.aggregate()
            checks = {
                "overlap_deduplicated": totals["messages"] == 4 and totals["tokens"]["input"] == 4000,
                "second_refresh_zero_rereads": unchanged["files_read"] == 0 and unchanged["files_unchanged"] == 2,
                "malformed_snapshot_reported_stale": not stale["complete"] and bool(stale["source_failures"]),
                "last_good_totals_preserved": preserved == totals,
                "separate_cost_measures": math.isclose(totals["recorded_cost"], 0.06) and math.isclose(totals["api_equivalent_cost"], 0.0056),
                "projects_separate_across_assistants": projects["project-aurora"]["input_tokens"] == 1500 and projects["project-beacon"]["input_tokens"] == 2500 and all(bucket["events"] == 2 and len(bucket["sources"]) == 2 for bucket in projects.values()),
            }
            if not initial["complete"] or not all(checks.values()):
                raise RuntimeError(f"Demo verification failed: {checks}; totals={totals}")
            return {
                "scope": "Synthetic data and invented USD tariff; no real usage or vendor prices",
                "cost_note": "Recorded cost sums only events containing a recorded amount. API-equivalent estimates are not invoices or subscription bills.",
                "submitted_rows": 5, "unique_events": totals["messages"],
                "input_tokens": totals["tokens"]["input"], "output_tokens": totals["tokens"]["output"],
                "recorded_cost_usd": round(totals["recorded_cost"], 6),
                "recorded_cost_events": totals["recorded_cost_messages"],
                "api_equivalent_estimate_usd": round(totals["api_equivalent_cost"], 6),
                "projects": projects,
                "initial_files_read": initial["files_read"],
                "second_refresh_files_read": unchanged["files_read"],
                "after_malformed_snapshot": "incomplete; last good totals retained",
                "checks": checks,
            }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
