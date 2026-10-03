"""Synthetic regressions for the checked Astra and Fable 5.1 tariff catalog."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import usage
import verified_pricing
from usage_store import UsageStore


FABLE = "anthropic/claude-fable-5.1"
ASTRA = "openai/gpt-6-astra"


class VerifiedPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="verified-pricing-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "ledger.sqlite3"
        self.when = int(datetime(2026, 9, 6, 12).timestamp() * 1000)

    def msg(self, model=ASTRA, **changes):
        row = {
            "model": model, "provider": model.split("/")[0],
            "input": 100, "output": 20, "reasoning": 0,
            "cache_read": 0, "cache_write": 0,
            "original_usage": {"service_tier": "standard", "inference_geo": "global", "processing_region": "global"},
            "source": "codex" if model == ASTRA else "claude-code",
            "billing_source": "codex" if model == ASTRA else "subscription",
            "machine_id": "fixture-machine", "session_id": "fixture-session",
            "session_title": "Synthetic pricing session", "session_dir": "",
            "msg_id": model, "time": self.when,
        }
        row.update(changes)
        return row

    def write(self, rows):
        path = self.root / "synthetic.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return [("export", str(path))]

    def test_official_ids_normalize_to_exact_verified_models(self):
        self.assertEqual(set(verified_pricing.MODELS), {FABLE, ASTRA})
        self.assertEqual(usage.normalize_model("claude-fable-5-1"), FABLE)
        self.assertEqual(usage.normalize_model("gpt-6-astra"), ASTRA)
        self.assertIsNotNone(verified_pricing.quote(self.msg(usage.normalize_model("claude-fable-5-1")))["cost"])

    def test_astra_short_context_prices_all_categories_once(self):
        row = self.msg(input=100, output=20, reasoning=5, cache_read=30, cache_write=40)
        quote = verified_pricing.quote(row)
        expected = (100 * 10 + 25 * 50 + 30 * 1 + 40 * 12.5) / 1_000_000
        self.assertAlmostEqual(quote["cost"], expected)
        self.assertEqual(quote["context_tier"], "short")
        self.assertIsNone(quote["issue"])

    def test_astra_threshold_is_strict_and_counts_cached_prompt_tokens(self):
        at_boundary = self.msg(input=270_000, cache_read=1000, cache_write=1000, output=500_000, reasoning=1000)
        short = verified_pricing.quote(at_boundary)
        long = verified_pricing.quote({**at_boundary, "cache_read": 1001})
        self.assertEqual(short["context_tier"], "short")
        self.assertEqual(long["context_tier"], "long")
        self.assertAlmostEqual(short["cost"], (270_000 * 10 + 1000 + 1000 * 12.5 + 501_000 * 50) / 1_000_000)

    def test_astra_long_rates_apply_to_the_entire_request(self):
        row = self.msg(input=100_000, cache_read=100_000, cache_write=72_001, output=100, reasoning=10)
        quote = verified_pricing.quote(row)
        expected = (100_000 * 20 + 100_000 * 2 + 72_001 * 25 + 110 * 75) / 1_000_000
        self.assertAlmostEqual(quote["cost"], expected)
        self.assertEqual(quote["context_tier"], "long")

    def test_astra_service_multipliers_apply_after_long_context_selection(self):
        row = self.msg(input=272_001, output=100, cache_read=10, cache_write=20)
        standard = (272_001 * 20 + 100 * 75 + 10 * 2 + 20 * 25) / 1_000_000
        for service, factor, canonical in (("batch", .5, "batch"), ("flex", .5, "flex"),
                                           ("fast", 2, "fast"), ("priority", 2, "fast")):
            with self.subTest(service=service):
                quote = verified_pricing.quote({**row, "original_usage": {"service_tier": service}})
                self.assertAlmostEqual(quote["cost"], standard * factor)
                self.assertEqual(quote["service_tier"], canonical)
                self.assertEqual(quote["context_tier"], "long")
        speed = verified_pricing.quote({**row, "original_usage": {"speed": "fast"}})
        self.assertAlmostEqual(speed["cost"], standard * 2)

    def test_fable_ttl_splits_use_distinct_write_rates_without_double_counting(self):
        row = self.msg(FABLE, input=100, output=10, cache_read=100, cache_write=150)
        expected = (100 * 10 + 10 * 50 + 100 * .25 + 100 * 12.5 + 50 * 20) / 1_000_000
        for split in (
            {"cache_write_5m": 100, "cache_write_1h": 50},
            {"original_usage": {"service_tier": "standard", "cache_creation": {
                "ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50}}},
        ):
            with self.subTest(split=split):
                self.assertAlmostEqual(verified_pricing.quote({**row, **split})["cost"], expected)

    def test_fable_batch_and_us_geography_stack_for_every_category(self):
        row = self.msg(FABLE, input=100, output=10, cache_read=100, cache_write=150,
                       cache_write_5m=100, cache_write_1h=50,
                       original_usage={"service_tier": "batch", "inference_geo": "us"})
        standard = (100 * 10 + 10 * 50 + 100 * .25 + 100 * 12.5 + 50 * 20) / 1_000_000
        quote = verified_pricing.quote(row)
        self.assertAlmostEqual(quote["cost"], standard * .5 * 1.1)
        self.assertEqual(quote["multiplier"], 1.1)

    def test_fable_long_context_has_no_premium(self):
        row = self.msg(FABLE, input=900_000, output=100)
        quote = verified_pricing.quote(row)
        self.assertAlmostEqual(quote["cost"], (900_000 * 10 + 100 * 50) / 1_000_000)
        self.assertEqual(quote["context_tier"], "short")

    def test_missing_metadata_uses_flagged_standard_global_assumptions(self):
        for model in (FABLE, ASTRA):
            with self.subTest(model=model):
                quote = verified_pricing.quote(self.msg(model, original_usage={}))
                self.assertIsNotNone(quote["cost"])
                self.assertEqual(quote["service_tier"], "standard")
                self.assertEqual(quote["multiplier"], 1)
                assumptions = " ".join(quote["assumptions"]).lower()
                self.assertIn("standard", assumptions)
                self.assertIn("global", assumptions)

    def test_unsupported_explicit_service_or_geography_has_no_quote(self):
        cases = [(FABLE, {"service_tier": tier}) for tier in ("fast", "priority", "flex", "unknown")]
        cases += [(ASTRA, {"service_tier": "unknown"}), (FABLE, {"speed": "fast"}),
                  (FABLE, {"inference_geo": "unknown"}), (ASTRA, {"service_tier": "batch", "speed": "fast"})]
        for model, metadata in cases:
            with self.subTest(model=model, metadata=metadata):
                quote = verified_pricing.quote(self.msg(model, original_usage=metadata))
                self.assertIsNone(quote["cost"])
                self.assertTrue(quote["issue"])

    def test_invalid_cache_ttl_breakdown_has_no_quote(self):
        for split in ({"cache_write_1h": -1}, {"cache_write_1h": 101},
                      {"cache_write_5m": 75, "cache_write_1h": 50}, {"cache_write_1h": 1.5},
                      {"original_usage": {"cache_creation": [100]}}):
            with self.subTest(split=split):
                quote = verified_pricing.quote(self.msg(FABLE, cache_write=100, **split))
                self.assertIsNone(quote["cost"])
                self.assertTrue(quote["issue"])

    def test_unknown_descendants_cannot_inherit_legacy_family_prices(self):
        for model in (FABLE + "0", FABLE + "-preview", ASTRA + "-mini", ASTRA + ".1"):
            with self.subTest(model=model):
                row = self.msg(model, cache_read=100)
                self.assertIsNone(verified_pricing.quote(row)["cost"])
                self.assertEqual(usage.impute_cost(row, usage.static_fallback_model_rates()), 0)

    def test_legacy_imputation_uses_the_same_verified_quote(self):
        rows = [self.msg(FABLE, cache_read=1000, cache_write=100, cache_write_1h=100),
                self.msg(ASTRA, input=300_000, original_usage={"service_tier": "priority"})]
        for row in rows:
            with self.subTest(model=row["model"]):
                self.assertAlmostEqual(usage.impute_cost(row, {}), verified_pricing.quote(row)["cost"])

    def test_catalog_installation_is_idempotent_and_does_not_invent_effective_dates(self):
        with UsageStore(self.db_path) as store:
            rows = list(store.db.execute("SELECT id,effective_from,effective_to FROM pricing_versions WHERE basis='verified-published'"))
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row[1] is None and row[2] is None for row in rows))
            first_ids = {row[0] for row in rows}
        with UsageStore(self.db_path) as store:
            later_ids = {row[0] for row in store.db.execute("SELECT id FROM pricing_versions WHERE basis='verified-published'")}
            self.assertEqual(later_ids, first_ids)

    def test_targeted_repricing_retains_recorded_cost_and_valuation_history(self):
        sources = self.write([self.msg(FABLE, source="opencode", billing_source="api", cash_cost=7.0, recorded_cost=7.0),
                              self.msg(ASTRA)])
        with UsageStore(self.db_path) as store:
            store.refresh(sources=sources)
            before = {row["model"]: row for row in store.messages()}
            history_count = store.db.execute("SELECT count(*) FROM valuations").fetchone()[0]
            self.assertEqual(store.reprice(models=[FABLE], reason="Synthetic Fable-only correction"), 1)
            after = {row["model"]: row for row in store.messages()}
            self.assertEqual(after[FABLE]["recorded_cost"], 7.0)
            self.assertEqual(after[FABLE]["cash_cost"], 7.0)
            self.assertEqual(after[ASTRA], before[ASTRA])
            self.assertEqual(store.db.execute("SELECT count(*) FROM valuations").fetchone()[0], history_count + 1)
            self.assertEqual(store.db.execute("SELECT reason FROM valuations ORDER BY id DESC LIMIT 1").fetchone()[0],
                             "Synthetic Fable-only correction")

    def test_repricing_locks_before_reading_target_observations(self):
        with UsageStore(self.db_path) as store:
            store.refresh(sources=self.write([self.msg()]))
            statements = []

            def trace(statement):
                statements.append((" ".join(statement.upper().split()), store.db.in_transaction))

            store.db.set_trace_callback(trace)
            try:
                self.assertEqual(store.reprice(models=[ASTRA], reason="Synthetic transaction check"), 1)
            finally:
                store.db.set_trace_callback(None)
            begin = next(index for index, (sql, _) in enumerate(statements) if sql == "BEGIN IMMEDIATE")
            selected = next(index for index, (sql, _) in enumerate(statements)
                            if sql.startswith("SELECT E.EVENT_KEY,E.OBSERVATION_ID,O.PAYLOAD FROM EVENTS"))
            committed = next(index for index, (sql, _) in enumerate(statements) if sql == "COMMIT")
            self.assertLess(begin, selected)
            self.assertTrue(statements[selected][1], "Target observations must be read inside the write transaction")
            self.assertLess(selected, committed)

    def test_nonbilling_metadata_preserves_history_but_fast_tier_corrects_it(self):
        row = self.msg()
        row.pop("original_usage")
        old_catalog = [{**entry, "rates": {category: rate / 2 for category, rate in entry["rates"].items()}}
                       for entry in verified_pricing.catalog()]
        with patch("verified_pricing.CATALOG_VERSION", "synthetic-historical-catalog"), patch("verified_pricing.catalog", return_value=old_catalog):
            with UsageStore(self.db_path) as store:
                store.refresh(sources=self.write([row]))
                historical = store.messages()[0]
                history_count = store.db.execute("SELECT count(*) FROM valuations").fetchone()[0]
        self.assertAlmostEqual(historical["api_equivalent_cost"], verified_pricing.quote(row)["cost"] / 2)
        with UsageStore(self.db_path) as store:
            for metadata in ({"pricing_metadata": {}},
                             {"pricing_metadata": {}, "original_usage": {"unrelated_counter": 42}}):
                with self.subTest(metadata=metadata):
                    store.refresh(sources=self.write([{**row, **metadata}]), force=True)
                    current = store.messages()[0]
                    self.assertEqual(current["api_equivalent_cost"], historical["api_equivalent_cost"])
                    self.assertEqual(current["pricing_version"], historical["pricing_version"])
                    self.assertEqual(store.db.execute("SELECT count(*) FROM valuations").fetchone()[0], history_count)
            corrected = {**row, "pricing_metadata": {"service_tier": "fast"},
                         "original_usage": {"unrelated_counter": 42}}
            store.refresh(sources=self.write([corrected]), force=True)
            current = store.messages()[0]
            self.assertAlmostEqual(current["api_equivalent_cost"], verified_pricing.quote(corrected)["cost"])
            self.assertNotEqual(current["pricing_version"], historical["pricing_version"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM valuations").fetchone()[0], history_count + 1)
            self.assertEqual(store.db.execute("SELECT reason FROM valuations ORDER BY id DESC LIMIT 1").fetchone()[0],
                             "source correction")

    def test_new_catalog_install_keeps_existing_nonnull_valuations_pinned(self):
        sources = self.write([self.msg(FABLE)])
        with UsageStore(self.db_path) as store:
            store.refresh(sources=sources)
            before = store.messages()[0]
            history_count = store.db.execute("SELECT count(*) FROM valuations").fetchone()[0]
        changed_catalog = [{**row, "rates": {category: rate * 2 for category, rate in row["rates"].items()}}
                           for row in verified_pricing.catalog()]
        with patch("verified_pricing.CATALOG_VERSION", "synthetic-catalog-v2"), patch("verified_pricing.catalog", return_value=changed_catalog):
            with UsageStore(self.db_path) as store:
                store.refresh(sources=sources)
                self.assertEqual(store.messages()[0]["api_equivalent_cost"], before["api_equivalent_cost"])
                self.assertEqual(store.messages()[0]["pricing_version"], before["pricing_version"])
                self.assertEqual(store.db.execute("SELECT count(*) FROM valuations").fetchone()[0], history_count)
                store.reprice(models=[FABLE], reason="Synthetic adoption of next catalog")
                self.assertAlmostEqual(store.messages()[0]["api_equivalent_cost"], before["api_equivalent_cost"] * 2)

    def test_store_resolves_service_and_context_using_verified_rates(self):
        rows = [self.msg(ASTRA, msg_id="astra-fast", input=300_000, original_usage={"service_tier": "priority"}),
                self.msg(FABLE, msg_id="fable-batch", input=100, cache_write=100, cache_write_1h=100,
                         original_usage={"service_tier": "batch", "inference_geo": "us"})]
        with UsageStore(self.db_path) as store:
            store.refresh(sources=self.write(rows))
            actual = {row["msg_id"]: row for row in store.messages()}
            for row in rows:
                self.assertAlmostEqual(actual[row["msg_id"]]["api_equivalent_cost"], verified_pricing.quote(row)["cost"])
                self.assertEqual(actual[row["msg_id"]]["pricing_status"], "verified-published")

    def test_store_does_not_fall_back_for_unquotable_verified_usage(self):
        rows = [self.msg(FABLE, original_usage={"service_tier": "priority"}),
                self.msg(ASTRA, original_usage={"service_tier": "unknown"}),
                self.msg(FABLE, msg_id="invalid-ttl", cache_write=100, cache_write_1h=101)]
        with UsageStore(self.db_path) as store:
            status = store.refresh(sources=self.write(rows))
            self.assertTrue(all(row["api_equivalent_cost"] is None for row in store.messages()))
            self.assertFalse(status["pricing_complete"])
            self.assertEqual(store.aggregate()["unpriced_messages"], 3)

    def test_malformed_metadata_leaves_unknown_costs_without_blocking_healthy_events(self):
        healthy = self.msg(msg_id="healthy")
        malformed = [
            self.msg(msg_id="original-list", original_usage=["invalid"]),
            self.msg(FABLE, msg_id="pricing-list", pricing_metadata=["invalid"]),
            self.msg(msg_id="service-list", original_usage={"service_tier": ["fast"]}),
            self.msg(FABLE, msg_id="geo-dict", original_usage={"inference_geo": {"region": "us"}}),
            self.msg(msg_id="speed-dict", pricing_metadata={"speed": {"name": "fast"}}),
            self.msg(msg_id="top-region-list", processing_region=["us"]),
        ]
        for row in malformed:
            with self.subTest(msg_id=row["msg_id"]):
                quote = verified_pricing.quote(row)
                self.assertIsNone(quote["cost"])
                self.assertTrue(quote["issue"])
        with UsageStore(self.db_path) as store:
            status = store.refresh(sources=self.write([healthy, *malformed]))
            self.assertTrue(status["complete"])
            self.assertFalse(status["pricing_complete"])
            actual = {row["msg_id"]: row for row in store.messages()}
            self.assertEqual(len(actual), len(malformed) + 1)
            self.assertAlmostEqual(actual["healthy"]["api_equivalent_cost"], verified_pricing.quote(healthy)["cost"])
            for row in malformed:
                with self.subTest(msg_id=row["msg_id"]):
                    self.assertIsNone(actual[row["msg_id"]]["api_equivalent_cost"])
                    self.assertTrue(actual[row["msg_id"]]["pricing_issue"])
            self.assertEqual(store.aggregate()["unpriced_messages"], len(malformed))


if __name__ == "__main__":
    unittest.main()
