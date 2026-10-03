"""Release safety regressions. Only synthetic temporary inputs are permitted."""
import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import cli
import demo
import usage_store


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName == "test_mcp_import_listing_and_all_six_tools_respect_gate":
            # Windows constructs an internal socketpair when creating a loop.
            # Create it before guards, then keep all application work guarded.
            self.loop = asyncio.new_event_loop()
            self.addCleanup(self.close_loop)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch("usage_store.discover_sources", side_effect=AssertionError("Discovery forbidden in release tests")))
        self.guards.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.guards.enter_context(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")))
        self.guards.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        self.guards.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")))

    def close_loop(self):
        # Cleanups run in reverse order: guards have already been removed.
        try:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        finally:
            self.loop.close()

    def test_demo_uses_only_temporary_exports_and_verifies_results(self):
        original_reader = usage_store.read_source
        read_paths = []

        def synthetic_only(kind, path, diagnostics):
            resolved = Path(path).resolve()
            self.assertEqual(kind, "export")
            self.assertTrue(resolved.parent.name.startswith("synthetic-usage-demo-"))
            self.assertEqual(resolved.parent.parent, Path(tempfile.gettempdir()).resolve())
            read_paths.append(resolved)
            return original_reader(kind, path, diagnostics)

        with patch("usage_store.read_source", side_effect=synthetic_only), patch("pathlib.Path.home", side_effect=AssertionError("User home forbidden")):
            report = demo.run_demo()
        self.assertEqual(report["unique_events"], 4)
        self.assertEqual(report["second_refresh_files_read"], 0)
        self.assertEqual(report["recorded_cost_usd"], 0.06)
        self.assertEqual(report["api_equivalent_estimate_usd"], 0.0056)
        self.assertEqual(report["projects"]["project-aurora"]["input_tokens"], 1500)
        self.assertEqual(report["projects"]["project-beacon"]["input_tokens"], 2500)
        self.assertEqual(report["projects"]["project-aurora"]["recorded_cost_events"], 1)
        self.assertEqual(report["projects"]["project-beacon"]["sources"], ["claude-code", "opencode"])
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(len(read_paths), 3)
        self.assertTrue(all(not path.exists() for path in read_paths))

    def test_no_arguments_and_rejected_refresh_never_open_store(self):
        with patch("usage_store.UsageStore", side_effect=AssertionError("Store must not open")), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 0)
            for argv in (["refresh"], ["refresh", "--local"], ["refresh", "--db", "unused.sqlite3"]):
                with self.assertRaises(SystemExit) as result:
                    cli.main(argv)
                self.assertEqual(result.exception.code, 2)

    def test_explicit_bad_export_returns_nonzero_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "invalid.jsonl"
            fixture.write_text("{invalid synthetic JSON}\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["refresh", "--db", str(root / "ledger.sqlite3"), "--export", str(fixture), "--json"])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(result["status"]["complete"])
            self.assertTrue(result["status"]["source_failures"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["status", "--db", str(root / "ledger.sqlite3")]), 1)

    def test_legacy_entry_point_uses_the_same_permission_gate(self):
        with patch("usage_store.UsageStore", side_effect=AssertionError("Store must not open")), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with patch("sys.argv", ["usage_store.py"]):
                self.assertEqual(usage_store.main(), 0)
            for argv in (["refresh"], ["refresh", "--local"], ["refresh", "--db", "unused.sqlite3"]):
                with patch("sys.argv", ["usage_store.py", *argv]):
                    with self.assertRaises(SystemExit) as result:
                        usage_store.main()
                self.assertEqual(result.exception.code, 2)

    def test_existing_ledger_query_does_not_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "ledger.sqlite3"
            with usage_store.UsageStore(db) as store:
                store.refresh(sources=[])
            with patch.object(usage_store.UsageStore, "refresh", side_effect=AssertionError("Query must not refresh")), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["totals", "--db", str(db)]), 0)

    def test_project_filter_separates_assistants_and_keeps_empty_costs_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, fixture = root / "ledger.sqlite3", root / "projects.jsonl"
            base = {
                "machine_id": "fictional-machine", "provider": "fictional-provider",
                "model": "fictional/project-model", "time": 1767355200000,
            }
            rows = [
                dict(base, msg_id="a1", session_id="a1", source="opencode", billing_source="api", session_dir="/fictional/project-alpha", input=10, cost=0.04),
                dict(base, msg_id="a2", session_id="a2", source="codex", billing_source="codex", session_dir="/fictional/project-alpha", input=20),
                dict(base, msg_id="b1", session_id="b1", source="claude-code", billing_source="subscription", session_dir="/fictional/project-beta", input=90),
                dict(base, msg_id="b2", session_id="b2", source="opencode", billing_source="api", session_dir="/fictional/project-beta", input=80, cost=0.02),
            ]
            fixture.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with usage_store.UsageStore(db) as store:
                store.add_price("fictional/project-model", {key: 0.000001 for key in ("input", "output", "cache_read", "cache_write")}, effective_from=base["time"] - 1, provenance="Invented regression tariff")

            def query(*args):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main([*args, "--db", str(db)]), 0)
                return json.loads(output.getvalue())

            alpha = query("refresh", "--export", str(fixture), "--project", "PROJECT-ALPHA")["totals"]
            self.assertEqual(alpha["messages"], 2)
            self.assertEqual(alpha["tokens"]["input"], 30)
            self.assertEqual(alpha["recorded_cost_messages"], 1)
            self.assertAlmostEqual(alpha["recorded_cost"], 0.04)
            self.assertAlmostEqual(alpha["api_equivalent_cost"], 0.00003)
            beta = query("totals", "--project", "project-beta")["totals"]
            self.assertEqual(beta["messages"], 2)
            self.assertEqual(beta["tokens"]["input"], 170)
            self.assertAlmostEqual(beta["recorded_cost"], 0.02)
            self.assertAlmostEqual(beta["api_equivalent_cost"], 0.00017)
            self.assertEqual(query("totals")["totals"]["messages"], 4)
            absent = query("totals", "--project", "nonexistent-project")
            self.assertTrue(absent["status"]["complete"])
            self.assertEqual(absent["totals"]["messages"], 0)
            self.assertTrue(all(value == 0 for value in absent["totals"]["tokens"].values()))
            self.assertIsNone(absent["totals"]["recorded_cost"])
            self.assertIsNone(absent["totals"]["api_equivalent_cost"])
            with usage_store.UsageStore(db) as store:
                session = next(row for row in store.aggregate(group="session") if row["session_id"] == "a2")
                store.assign_session(session["session_key"], project_id="fictional-assigned-project", reason="Synthetic assignment regression")
            assigned = query("totals", "--project", "ASSIGNED-PROJECT")["totals"]
            self.assertEqual(assigned["messages"], 1)
            self.assertEqual(assigned["tokens"]["input"], 20)
            self.assertIsNone(assigned["recorded_cost"])
            self.assertAlmostEqual(assigned["api_equivalent_cost"], 0.00002)

    def test_status_rejects_project_filter_before_opening_store(self):
        with patch("usage_store.UsageStore", side_effect=AssertionError("Store must not open")), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.main(["status", "--db", "unused.sqlite3", "--project", "project-alpha"])
            self.assertEqual(result.exception.code, 2)

    @unittest.skipUnless(importlib.util.find_spec("mcp") and importlib.util.find_spec("pydantic"), "Optional MCP dependencies are not installed")
    def test_mcp_import_listing_and_all_six_tools_respect_gate(self):
        with patch.dict(os.environ, {}, clear=True), patch("usage_store.UsageStore", side_effect=AssertionError("Store must not open")):
            server = importlib.import_module("server")
            with patch.object(server, "UsageStore", side_effect=AssertionError("Store must not open")):
                listed = self.loop.run_until_complete(server.mcp.list_tools())
                self.assertEqual({tool.name for tool in listed}, {
                    "usage_summary", "usage_by_model", "usage_sessions", "usage_by_source", "usage_query", "usage_status",
                })
                calls = [
                    (server.usage_summary, server.UsageSummaryInput()),
                    (server.usage_by_model, server.UsageByModelInput()),
                    (server.usage_sessions, server.UsageSessionsInput()),
                    (server.usage_by_source_tool, server.UsageBySourceInput()),
                    (server.usage_query_tool, server.UsageQueryInput()),
                    (server.usage_status_tool, None),
                ]
                for tool, params in calls:
                    with self.assertRaisesRegex(RuntimeError, "Local discovery disabled"):
                        self.loop.run_until_complete(tool(params) if params is not None else tool())
                with patch.dict(os.environ, {"OPENCODE_USAGE_ALLOW_DISCOVERY": "1"}):
                    with self.assertRaisesRegex(RuntimeError, "explicit private ledger"):
                        server._authorized_db()
                    with patch.dict(os.environ, {"OPENCODE_USAGE_DB": "chosen-private-ledger.sqlite3"}):
                        self.assertEqual(server._authorized_db(), "chosen-private-ledger.sqlite3")


class DiscoveryTests(unittest.TestCase):
    """Exercise actual discovery with every app root redirected to a temp tree."""

    def setUp(self):
        import claude_code_reader
        import codex_reader
        import cursor_reader

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="discovery-fixture-")))
        self.stack.enter_context(patch.dict(os.environ, {
            "USERPROFILE": str(self.root), "CURSOR_USAGE_EVENTS_CSV": "", "CURSOR_USAGE_CSV": "",
        }))
        for target, value in (
            ("usage.DATA_DIR", self.root / "opencode"),
            ("usage.EXPORTS_DIR", self.root / "exports"),
            ("claude_code_reader.CLAUDE_HOME", self.root / "claude"),
            ("codex_reader.CODEX_HOME", self.root / "codex"),
            ("cursor_reader.STATE_VSCDB", self.root / "cursor" / "state.vscdb"),
        ):
            self.stack.enter_context(patch(target, str(value)))
        self.stack.enter_context(patch("usage.machine_id", return_value="fixture-local-machine"))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")))
        original_scandir = os.scandir
        original_reader = usage_store.read_source
        self.denied_root = None

        def temp_scandir(path):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root))
            if resolved == self.denied_root:
                raise PermissionError("Synthetic permission denial")
            return original_scandir(path)

        def temp_reader(kind, path, diagnostics):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))
            return original_reader(kind, path, diagnostics)

        self.stack.enter_context(patch("os.scandir", side_effect=temp_scandir))
        self.stack.enter_context(patch("usage_store.read_source", side_effect=temp_reader))
        self.store = self.stack.enter_context(usage_store.UsageStore(self.root / "ledger.sqlite3"))

    def write_export(self):
        directory = self.root / "exports"
        directory.mkdir(exist_ok=True)
        path = directory / "synthetic.jsonl"
        path.write_text(json.dumps({
            "machine_id": "fictional-machine", "msg_id": "example-1",
            "session_id": "example-session", "source": "opencode",
            "provider": "fictional-provider", "model": "fictional/demo-model",
            "billing_source": "api", "time": 1767355200000,
            "input": 100, "output": 20, "cost": 0.01,
        }) + "\n", encoding="utf-8")
        return path

    def test_healthy_source_is_complete_when_other_apps_are_absent(self):
        self.write_export()
        status = self.store.refresh()
        self.assertTrue(status["complete"])
        self.assertEqual(status["issues"], [])
        self.assertTrue(status["unavailable_sources"])
        self.assertTrue(all(item["status"] == "unavailable" for item in status["unavailable_sources"]))
        self.assertEqual(self.store.aggregate()["messages"], 1)

    def test_no_discovered_inputs_is_explicitly_incomplete(self):
        status = self.store.refresh()
        self.assertFalse(status["complete"])
        self.assertEqual(status["files_seen"], 0)
        self.assertTrue(any(item["status"] == "no_sources" for item in status["issues"]))

    def test_explicit_missing_cursor_csv_is_a_failure(self):
        self.write_export()
        with patch.dict(os.environ, {"CURSOR_USAGE_EVENTS_CSV": str(self.root / "missing.csv")}):
            status = self.store.refresh()
        self.assertFalse(status["complete"])
        self.assertTrue(any(item["kind"] == "cursor-csv" and item["status"] == "error" for item in status["source_failures"]))

    def test_explicit_missing_snapshot_is_a_failure(self):
        status = self.store.refresh(sources=[("export", str(self.root / "missing.jsonl"))])
        self.assertFalse(status["complete"])
        self.assertEqual(status["unavailable_sources"], [])
        self.assertTrue(any(item["kind"] == "export" and item["status"] == "error" for item in status["source_failures"]))

    def test_previously_ingested_disappearance_retains_stale_evidence(self):
        path = self.write_export()
        self.assertTrue(self.store.refresh()["complete"])
        path.unlink()
        status = self.store.refresh()
        self.assertFalse(status["complete"])
        self.assertTrue(any(item["status"] == "missing" for item in status["source_failures"]))
        self.assertEqual(self.store.aggregate()["messages"], 1)

    def test_permission_errors_block_even_with_one_healthy_source(self):
        self.write_export()
        for path in (self.root / "claude" / "projects", self.root / "Downloads"):
            with self.subTest(path=path.name):
                self.denied_root = path
                status = self.store.refresh()
                self.assertFalse(status["complete"])
                self.assertTrue(any(item["status"] == "error" and "permission denial" in item["error"] for item in status["issues"]))
                self.assertEqual(self.store.aggregate()["messages"], 1)


if __name__ == "__main__":
    unittest.main()
