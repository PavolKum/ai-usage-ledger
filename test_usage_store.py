"""Synthetic ledger regressions; never discover or read the user's usage logs.

Run with: python -m unittest -v test_usage_store
"""
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import usage_store
from usage_store import UsageStore


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="usage-store-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = UsageStore(self.root / "ledger.sqlite3")
        self.addCleanup(self.store.db.close)
        self.noon = datetime(2026, 1, 2, 12)
        self.when = int(self.noon.timestamp() * 1000)

    def event(self, msg_id="event-1", **changes):
        row = {
            "source": "codex", "machine_id": "fixture-machine",
            "msg_id": msg_id, "session_id": "fixture-session",
            "session_title": "Synthetic conversation", "session_dir": "",
            "model": "fixture/model", "provider": "fixture-provider",
            "billing_source": "codex", "time": self.when,
            "input": 100, "output": 20, "reasoning": 0,
            "cache_read": 0, "cache_write": 0,
        }
        row.update(changes)
        return row

    def write(self, name, rows, *, trailing=""):
        path = self.root / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows) + trailing, encoding="utf-8")
        # Filesystem timestamp resolution must not make same-size correction
        # fixtures accidentally look unchanged.
        stamp = path.stat().st_mtime_ns + 1_000_000
        os.utime(path, ns=(stamp, stamp))
        return path

    def refresh(self, *paths, **kwargs):
        return self.store.refresh(sources=[("export", str(path)) for path in paths], **kwargs)

    def price(self, *, rate=0.000001, **kwargs):
        return self.store.add_price(
            "fixture/model", {"input": rate, "output": rate, "cache_read": rate, "cache_write": rate},
            effective_from=kwargs.pop("effective_from", self.when - 86_400_000),
            provenance="Synthetic test tariff", **kwargs,
        )

    def cursor_csv(self, name, inputs, *, trailing=""):
        columns = ["Date", "Kind", "Model", "Max Mode", "Input (w/ Cache Write)",
                   "Input (w/o Cache Write)", "Cache Read", "Output Tokens", "Total Tokens", "Cost"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for value in inputs:
            writer.writerow({"Date": self.noon.isoformat(), "Kind": "Included", "Model": "fixture/model",
                             "Max Mode": "false", "Input (w/ Cache Write)": 0,
                             "Input (w/o Cache Write)": value, "Cache Read": 0,
                             "Output Tokens": 0, "Total Tokens": value, "Cost": "Included"})
        path = self.root / name
        path.write_text(buffer.getvalue() + trailing, encoding="utf-8")
        return path

    def test_unchanged_history_is_not_read_even_after_reopening(self):
        path = self.write("a.jsonl", [self.event()])
        first = self.refresh(path)
        self.assertEqual(first["files_read"], 1)
        self.assertEqual(self.store.aggregate()["messages"], 1)
        with UsageStore(self.root / "ledger.sqlite3") as reopened:
            with patch("usage_store.read_source", side_effect=AssertionError("Unchanged source read")):
                result = reopened.refresh(sources=[("export", str(path))])
            self.assertEqual(result["files_read"], 0)
            self.assertEqual(result["files_unchanged"], 1)
            self.assertEqual(reopened.aggregate()["tokens"]["input"], 100)

    def test_overlapping_snapshots_deduplicate_but_other_machines_do_not(self):
        one = self.write("a.jsonl", [self.event()])
        two = self.write("b.jsonl", [self.event(), self.event(machine_id="other-machine")])
        self.refresh(one, two)
        total = self.store.aggregate()
        self.assertEqual(total["messages"], 2)
        self.assertEqual(total["tokens"]["input"], 200)
        self.assertEqual(total["sessions"], 2)
        self.refresh(one, two, force=True)
        self.assertEqual(self.store.aggregate()["messages"], 2)

    def test_corrections_and_late_events_rebuild_historical_totals(self):
        yesterday = int((self.noon - timedelta(days=1)).timestamp() * 1000)
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        self.write("a.jsonl", [self.event(input=300), self.event("late", time=yesterday, input=50)])
        self.refresh(path)
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 350)
        self.assertEqual(self.store.aggregate(start=yesterday, end=self.when)["tokens"]["input"], 50)
        self.assertEqual(self.store.aggregate(start=self.when)["tokens"]["input"], 300)

    def test_truncation_replaces_active_snapshot_and_retains_provenance(self):
        path = self.write("a.jsonl", [self.event(), self.event("removed", input=200)])
        self.refresh(path)
        self.write("a.jsonl", [self.event()])
        self.refresh(path)
        self.assertEqual([row["msg_id"] for row in self.store.messages()], ["event-1"])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)
        retained = [json.loads(row[0])["msg_id"] for row in self.store.db.execute("SELECT payload FROM observations")]
        self.assertIn("removed", retained)

    def test_incomplete_tail_waits_for_completion_and_bad_snapshot_keeps_last_good(self):
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        pending = json.dumps(self.event("pending"))
        self.write("a.jsonl", [self.event()], trailing=pending[: len(pending) // 2])
        result = self.refresh(path)
        self.assertFalse(result["complete"])
        self.assertEqual(self.store.aggregate()["messages"], 1)
        self.write("a.jsonl", [self.event(), self.event("pending")])
        self.assertTrue(self.refresh(path)["complete"])
        self.write("a.jsonl", [self.event()], trailing="{broken JSON}\n")
        result = self.refresh(path)
        self.assertFalse(result["complete"])
        self.assertTrue(result["source_failures"])
        self.assertEqual(self.store.aggregate()["messages"], 2)

    def test_invalid_tokens_are_a_source_failure_not_a_zero(self):
        path = self.write("a.jsonl", [self.event(input=-1)])
        result = self.refresh(path)
        self.assertFalse(result["complete"])
        self.assertTrue(result["source_failures"])
        self.assertEqual(self.store.aggregate()["messages"], 0)

    def test_temporarily_missing_source_recovers_without_content_change(self):
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        self.assertFalse(self.refresh()["complete"])
        self.assertEqual(self.store.aggregate()["messages"], 1)
        result = self.refresh(path)
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_failures"], [])

    def test_transient_reader_failure_recovers_without_content_change(self):
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        with patch("usage_store.read_source", side_effect=OSError("Synthetic transient lock")):
            self.assertFalse(self.refresh(path, force=True)["complete"])
        self.assertEqual(self.store.aggregate()["messages"], 1)
        self.assertTrue(self.refresh(path)["complete"])

    def test_session_spanning_midnight_counts_once_and_partial_boundaries_are_exact(self):
        midnight = datetime(2026, 1, 3)
        boundary = int(midnight.timestamp() * 1000)
        path = self.write("a.jsonl", [
            self.event("before", time=boundary - 1, input=10),
            self.event("after", time=boundary, input=20),
            self.event("later", time=boundary + 3_600_000, input=30),
        ])
        self.refresh(path)
        self.assertEqual(self.store.aggregate()["sessions"], 1)
        self.assertEqual(self.store.aggregate(end=boundary)["tokens"]["input"], 10)
        self.assertEqual(self.store.aggregate(start=boundary, end=boundary + 3_600_000)["tokens"]["input"], 20)
        self.assertEqual(self.store.aggregate(start=boundary - 1, end=boundary + 1)["tokens"]["input"], 30)

    def test_session_identity_uses_machine_and_source_not_title(self):
        path = self.write("a.jsonl", [
            self.event(),
            self.event("other-id", session_id="other-session"),
            self.event("other-source", source="claude-code", billing_source="subscription"),
        ])
        self.refresh(path)
        sessions = self.store.aggregate(group="session")
        self.assertEqual(len(sessions), 3)
        self.assertEqual(len({row["session_key"] for row in sessions}), 3)
        self.assertEqual(set(self.store.aggregate(group="source")), {"codex", "claude-code"})
        self.assertEqual(set(self.store.aggregate(group="provider")), {"fixture-provider"})

    def test_recorded_api_cost_and_subscription_estimates_remain_distinct(self):
        self.price(rate=0.000002)
        path = self.write("a.jsonl", [
            self.event("metered", source="opencode", billing_source="api", cash_cost=7.0, recorded_cost=7.0, input=1_000_000, output=0),
            self.event("subscription", input=1_000_000, output=0),
        ])
        self.refresh(path)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertEqual(rows["metered"]["recorded_cost"], 7.0)
        self.assertEqual(rows["metered"]["cash_cost"], 7.0)
        self.assertAlmostEqual(rows["metered"]["api_equivalent_cost"], 2.0)
        self.assertIsNone(rows["subscription"]["recorded_cost"])
        self.assertEqual(rows["subscription"]["cash_cost"], 0)
        self.assertAlmostEqual(rows["subscription"]["api_equivalent_cost"], 2.0)
        total = self.store.aggregate()
        self.assertEqual(total["cash_cost"], 7.0)
        self.assertEqual(total["recorded_cost"], 7.0)
        self.assertAlmostEqual(total["api_equivalent_cost"], 4.0)
        self.assertAlmostEqual(total["implied_cost"], 9.0)
        self.assertAlmostEqual(total["total_cost"], 9.0)
        self.assertAlmostEqual(self.store.aggregate(start=self.when, end=self.when + 1)["total_cost"], 9.0)

    def test_missing_price_remains_unknown_in_events_and_aggregates(self):
        path = self.write("a.jsonl", [self.event()])
        result = self.refresh(path)
        row = self.store.messages()[0]
        self.assertIsNone(row["api_equivalent_cost"])
        self.assertEqual(row["pricing_status"], "missing")
        self.assertEqual(self.store.aggregate()["unpriced_messages"], 1)
        self.assertIsNone(self.store.aggregate()["api_equivalent_cost"])
        self.assertIsNone(self.store.aggregate()["recorded_cost"])
        self.assertEqual(result["unpriced"][0]["messages"], 1)

    def test_new_metered_model_fills_missing_estimate_once_with_audit(self):
        subscription = self.write("subscription.jsonl", [self.event("subscription", input=100, output=0)])
        self.refresh(subscription)
        self.assertIsNone(self.store.messages()[0]["api_equivalent_cost"])
        metered_row = self.event("metered", source="opencode", billing_source="api",
                                 input=100, output=0, cash_cost=2.0, recorded_cost=2.0)
        metered = self.write("metered.jsonl", [metered_row])
        result = self.refresh(subscription, metered)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        priced = rows["subscription"]
        self.assertAlmostEqual(priced["api_equivalent_cost"], 2.0)
        self.assertEqual(priced["cash_cost"], 0.0)
        self.assertEqual(priced["pricing_status"], "observed-derived")
        self.assertIn(priced["pricing_version"], result["new_price_versions"])
        self.assertGreaterEqual(result["newly_priced_events"], 1)
        history = list(self.store.db.execute(
            "SELECT pricing_version,reason FROM valuations WHERE event_key="
            "(SELECT event_key FROM events WHERE json_extract(payload,'$.msg_id')='subscription') ORDER BY id"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0][0], 0)
        self.assertEqual(history[1][0], priced["pricing_version"])
        self.assertIn("missing estimate", history[1][1])
        self.write("metered.jsonl", [{**metered_row, "cash_cost": 4.0, "recorded_cost": 4.0}])
        result = self.refresh(subscription, metered)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertEqual(rows["subscription"]["pricing_version"], priced["pricing_version"])
        self.assertAlmostEqual(rows["subscription"]["api_equivalent_cost"], 2.0)
        self.assertEqual(rows["metered"]["recorded_cost"], 4.0)
        self.assertEqual(result["new_price_versions"], [])
        self.assertEqual(result["newly_priced_events"], 0)

    def test_provider_tiers_and_half_open_effective_dates(self):
        cutoff = self.when + 1000
        self.price(rate=0.000001, effective_from=self.when)
        old = self.price(rate=0.000002, effective_from=self.when, effective_to=cutoff, provider="fixture-provider")
        new = self.price(rate=0.000003, effective_from=cutoff, provider="fixture-provider")
        long = self.price(rate=0.000005, effective_from=self.when, tier="long", provider="fixture-provider")
        path = self.write("a.jsonl", [
            self.event("too-early", time=self.when - 1, input=100, output=0),
            self.event("old", time=cutoff - 1, input=270_000, output=0),
            self.event("new", time=cutoff, input=100, output=0),
            self.event("long", input=270_001, output=0),
            self.event("other-provider", provider="elsewhere", input=100, output=0),
        ])
        self.refresh(path)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertIsNone(rows["too-early"]["api_equivalent_cost"])
        self.assertEqual(rows["old"]["pricing_version"], old)
        self.assertEqual(rows["new"]["pricing_version"], new)
        self.assertEqual(rows["long"]["pricing_version"], long)
        self.assertAlmostEqual(rows["old"]["api_equivalent_cost"], 0.54)
        self.assertAlmostEqual(rows["new"]["api_equivalent_cost"], 0.0003)
        self.assertAlmostEqual(rows["long"]["api_equivalent_cost"], 1.350005)
        self.assertAlmostEqual(rows["other-provider"]["api_equivalent_cost"], 0.0001)

    def test_all_token_categories_and_reasoning_are_valued_once(self):
        self.store.add_price("fixture/model", {"input": 1.0, "output": 2.0, "cache_read": 3.0, "cache_write": 4.0},
                             effective_from=self.when, provenance="Synthetic unit tariff")
        path = self.write("a.jsonl", [self.event(input=2, output=3, reasoning=5, cache_read=7, cache_write=11)])
        self.refresh(path)
        self.assertEqual(self.store.messages()[0]["api_equivalent_cost"], 83.0)

    def test_new_tariff_does_not_silently_reprice_unchanged_history(self):
        old = self.price(rate=0.000001)
        path = self.write("a.jsonl", [self.event(input=100, output=0)])
        self.refresh(path)
        new = self.price(rate=0.000002)
        self.refresh(path, force=True)
        self.assertEqual(self.store.messages()[0]["pricing_version"], old)
        self.write("a.jsonl", [self.event(input=100, output=0), self.event("new-event", input=100, output=0)])
        self.refresh(path)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertEqual(rows["event-1"]["pricing_version"], old)
        self.assertEqual(rows["new-event"]["pricing_version"], new)

    def test_explicit_reprice_is_scoped_audited_and_preserves_recorded_cost(self):
        old = self.price(rate=0.000001)
        path = self.write("a.jsonl", [
            self.event("first", input=100, output=0, recorded_cost=4.0),
            self.event("outside", time=self.when + 1, input=100, output=0),
        ])
        self.refresh(path)
        new = self.price(rate=0.000002)
        self.assertEqual(self.store.reprice(start=self.when, end=self.when + 1, reason="Synthetic tariff correction"), 1)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertEqual(rows["first"]["pricing_version"], new)
        self.assertEqual(rows["outside"]["pricing_version"], old)
        self.assertEqual(rows["first"]["recorded_cost"], 4.0)
        history = list(self.store.db.execute("SELECT pricing_version,reason FROM valuations ORDER BY id"))
        self.assertTrue(any(row[0] == old for row in history))
        self.assertEqual(tuple(history[-1]), (new, "Synthetic tariff correction"))
        with self.assertRaises(ValueError):
            self.store.reprice(reason=" ")

    def test_export_reordering_does_not_reprice_unchanged_usage(self):
        old = self.price(rate=0.000001)
        rows = [self.event("first", input=100, output=0), self.event("second", input=200, output=0)]
        path = self.write("a.jsonl", rows)
        self.refresh(path)
        self.price(rate=0.000003)
        self.write("a.jsonl", list(reversed(rows)))
        self.refresh(path)
        self.assertEqual({row["pricing_version"] for row in self.store.messages()}, {old})
        self.assertAlmostEqual(self.store.aggregate()["api_equivalent_cost"], 0.0003)

    def test_invalid_pricing_updates_fail_without_mutating_existing_valuation(self):
        self.price()
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        before = self.store.messages()[0]["api_equivalent_cost"]
        with self.assertRaises(ValueError):
            self.price(effective_from=None)
        with self.assertRaises(ValueError):
            self.price(effective_from=self.when, effective_to=self.when)
        with self.assertRaises(ValueError):
            self.price(rate=float("nan"))
        with self.assertRaises(ValueError):
            self.price(rate=-1)
        self.assertEqual(self.store.messages()[0]["api_equivalent_cost"], before)

    def test_session_assignment_filters_and_survives_metadata_updates(self):
        old_price = self.price()
        path = self.write("a.jsonl", [self.event(session_dir="C:/synthetic/project"), self.event("unassigned", session_id="unassigned")])
        self.refresh(path)
        sessions = {row["session_id"]: row for row in self.store.aggregate(group="session")}
        self.assertIsNotNone(sessions["fixture-session"]["project_id"])
        self.assertIsNone(sessions["unassigned"]["project_id"])
        self.assertIsNone(sessions["fixture-session"]["client_id"])
        key = sessions["fixture-session"]["session_key"]
        self.store.assign_session(key, project_id="fixture-project", client_id="fixture-client", reason="Synthetic assignment")
        self.assertEqual(self.store.aggregate(client="fixture-client")["messages"], 1)
        self.assertEqual(self.store.aggregate(project="fixture-project")["messages"], 1)
        self.assertEqual(self.store.aggregate(client="nobody")["messages"], 0)
        self.price(rate=0.000005)
        self.write("a.jsonl", [self.event(session_dir="C:/synthetic/project", session_title="Renamed"), self.event("unassigned", session_id="unassigned")])
        self.refresh(path)
        assigned = next(row for row in self.store.aggregate(group="session") if row["session_key"] == key)
        self.assertEqual(assigned["title"], "Renamed")
        self.assertEqual(assigned["project_id"], "fixture-project")
        self.assertEqual(assigned["client_id"], "fixture-client")
        self.assertEqual({row["pricing_version"] for row in self.store.messages()}, {old_price})
        with self.assertRaises(KeyError):
            self.store.assign_session("absent", reason="Synthetic invalid assignment")

    def test_older_cursor_export_becomes_active_when_newest_is_removed(self):
        older = self.cursor_csv("older.csv", [100])
        newer = self.cursor_csv("newer.csv", [100, 200])
        self.store.refresh(sources=[("cursor-csv", str(older))])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)
        self.store.refresh(sources=[("cursor-csv", str(newer))])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 300)
        newer.unlink()
        result = self.store.refresh(sources=[("cursor-csv", str(older))])
        self.assertTrue(result["complete"])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)
        self.assertEqual(self.store.aggregate()["messages"], 1)

    def test_incomplete_cursor_csv_does_not_claim_complete_new_usage(self):
        path = self.cursor_csv("usage.csv", [100])
        self.store.refresh(sources=[("cursor-csv", str(path))])
        self.cursor_csv("usage.csv", [100], trailing=f"{self.noon.isoformat()},Included,fixture/model,false,0,999")
        result = self.store.refresh(sources=[("cursor-csv", str(path))])
        self.assertFalse(result["complete"])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)

    def test_malformed_claude_shape_is_isolated_from_other_sources(self):
        good = self.write("good.jsonl", [self.event()])
        bad = self.write("bad-claude.jsonl", [["unexpected array"]])
        result = self.store.refresh(sources=[("claude-code", str(bad)), ("export", str(good))])
        self.assertFalse(result["complete"])
        self.assertTrue(result["source_failures"])
        self.assertEqual(self.store.aggregate()["messages"], 1)

    def test_invalid_claude_usage_is_not_silently_coerced_to_zero(self):
        path = self.write("claude.jsonl", [{
            "type": "assistant", "sessionId": "fixture-session", "requestId": "fixture-request",
            "timestamp": self.noon.isoformat(), "message": {
                "id": "fixture-call", "model": "fixture/model",
                "usage": {"input_tokens": "not-a-number", "output_tokens": 20},
            },
        }])
        result = self.store.refresh(sources=[("claude-code", str(path))])
        self.assertFalse(result["complete"])
        self.assertTrue(result["source_failures"])

    def test_invalid_claude_timestamp_is_not_silently_dated_1970(self):
        path = self.write("claude.jsonl", [{
            "type": "assistant", "sessionId": "fixture-session", "requestId": "fixture-request",
            "timestamp": "invalid-date", "message": {
                "id": "fixture-call", "model": "fixture/model",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        }])
        result = self.store.refresh(sources=[("claude-code", str(path))])
        self.assertFalse(result["complete"])
        self.assertTrue(result["source_failures"])

    def test_empty_claude_fragment_does_not_suppress_later_real_usage(self):
        empty = {
            "type": "assistant", "sessionId": "fixture-session", "requestId": "fixture-request",
            "timestamp": self.noon.isoformat(), "message": {
                "id": "fixture-call", "model": "fixture/model",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        populated = {**empty, "message": {**empty["message"], "usage": {"input_tokens": 100, "output_tokens": 20}}}
        path = self.write("claude.jsonl", [empty, populated, populated])
        result = self.store.refresh(sources=[("claude-code", str(path))])
        self.assertTrue(result["complete"])
        self.assertEqual(self.store.aggregate()["messages"], 1)
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)

    def test_legacy_codex_reader_still_loads_session_index_without_strict_flags(self):
        from codex_reader import read_codex_messages
        self.write("session_index.jsonl", [{"id": "fixture-session", "thread_name": "Synthetic title"}])
        self.assertEqual(read_codex_messages(codex_home=str(self.root), paths=[]), [])

    def test_absent_recorded_api_cost_is_distinct_from_explicit_zero(self):
        self.price()
        path = self.write("a.jsonl", [
            self.event("absent", source="opencode", billing_source="api"),
            self.event("zero", source="opencode", billing_source="api", cash_cost=0.0),
        ])
        self.refresh(path)
        rows = {row["msg_id"]: row for row in self.store.messages()}
        self.assertIsNone(rows["absent"]["recorded_cost"])
        self.assertEqual(rows["zero"]["recorded_cost"], 0.0)
        self.assertEqual(self.store.aggregate()["recorded_cost_messages"], 1)

    def test_full_day_facts_and_boundary_events_match_direct_interval_totals(self):
        midnight = int(datetime(2026, 1, 2).timestamp() * 1000)
        rows = [self.event(str(index), time=midnight + day * 86_400_000 + offset, input=index + 1)
                for index, (day, offset) in enumerate(
                    (day, offset) for day in range(4) for offset in (0, 1000, 43_200_000, 86_399_999))]
        path = self.write("a.jsonl", rows)
        self.refresh(path)
        for start, end in ((0, 2**62), (midnight, midnight + 86_400_000),
                           (midnight + 1, midnight + 3 * 86_400_000 + 1000),
                           (midnight + 86_400_000, midnight + 3 * 86_400_000),
                           (midnight + 1000, midnight + 1000)):
            with self.subTest(start=start, end=end):
                expected = [row for row in rows if start <= row["time"] < end]
                actual = self.store.aggregate(start=start, end=end)
                self.assertEqual(actual["messages"], len(expected))
                self.assertEqual(actual["tokens"]["input"], sum(row["input"] for row in expected))

    def test_week_window_begins_at_monday_midnight(self):
        sunday_afternoon = datetime(2026, 1, 4, 15, 45)
        monday_midnight = datetime(2025, 12, 29)
        with patch("usage.datetime") as clock:
            clock.now.return_value = sunday_afternoon
            windows = usage_store.usage.time_windows()
        self.assertEqual(windows["this_week"], int(monday_midnight.timestamp() * 1000))
        self.assertEqual(windows["today"], int(datetime(2026, 1, 4).timestamp() * 1000))

    def test_wal_changes_invalidate_db_snapshot_without_main_file_change(self):
        path = self.root / "synthetic.db"
        path.write_bytes(b"synthetic database placeholder")
        sources = [("opencode", str(path))]
        with patch("usage_store.read_source", return_value=[self.event()]) as reader:
            self.store.refresh(sources=sources)
            self.store.refresh(sources=sources)
            self.assertEqual(reader.call_count, 1)
        main_signature = path.stat().st_mtime_ns
        Path(str(path) + "-wal").write_bytes(b"synthetic WAL change")
        with patch("usage_store.read_source", return_value=[self.event(input=250)]) as reader:
            self.store.refresh(sources=sources)
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(path.stat().st_mtime_ns, main_signature)
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 250)

    def test_source_mutation_during_read_retains_previous_snapshot(self):
        path = self.write("a.jsonl", [self.event()])
        self.refresh(path)
        original_reader = usage_store.read_source

        def racing_reader(kind, source_path, diagnostics):
            rows = original_reader(kind, source_path, diagnostics)
            self.write("a.jsonl", [self.event(input=500)])
            return rows

        with patch("usage_store.read_source", side_effect=racing_reader):
            self.assertFalse(self.refresh(path, force=True)["complete"])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 100)
        self.assertTrue(self.refresh(path)["complete"])
        self.assertEqual(self.store.aggregate()["tokens"]["input"], 500)

    def test_codex_tenth_replayed_event_retracts_previously_counted_nine(self):
        session = "11111111-1111-1111-1111-111111111111"
        rows = [
            {"type": "session_meta", "payload": {"id": session, "model_provider": "fixture-provider", "cwd": "C:/synthetic/project"}},
            {"type": "turn_context", "payload": {"turn_id": "synthetic-turn", "model": "fixture/model"}},
        ]

        def token_event(offset):
            return {
                "type": "event_msg", "timestamp": datetime.fromtimestamp((self.when + offset) / 1000, timezone.utc).isoformat(),
                "payload": {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 120, "cached_input_tokens": 20,
                    "output_tokens": 30, "reasoning_output_tokens": 10,
                }}},
            }

        rows.extend(token_event(index * 50) for index in range(9))
        path = self.write("rollout-" + session + ".jsonl", rows)
        sources = [("codex", str(path))]
        self.store.refresh(sources=sources)
        self.assertEqual(self.store.aggregate()["messages"], 9)
        rows.append(token_event(450))
        self.write(path.name, rows)
        self.store.refresh(sources=sources)
        self.assertEqual(self.store.aggregate()["messages"], 0)
        rows.append(token_event(60_000))
        self.write(path.name, rows)
        self.store.refresh(sources=sources)
        self.assertEqual(self.store.aggregate()["messages"], 1)
        last = self.store.messages()[0]
        self.assertEqual((last["input"], last["output"], last["reasoning"], last["cache_read"]), (100, 20, 10, 20))


if __name__ == "__main__":
    unittest.main()
