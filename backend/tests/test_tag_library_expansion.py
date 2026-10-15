import unittest
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
import os
from types import SimpleNamespace
from unittest.mock import patch

from services import collector_state, daily_report, db, llm_trace, task_scheduler
from services.collectors import cn_ecommerce, cn_longtail, cn_sources, overseas_trends, seed_builder


def _seed(word: str, *, depth: int = 0, kind: str = "suggestion") -> dict:
    return {
        "seed_word": word,
        "normalized_seed": collector_state.normalize_collector_key(word),
        "seed_kind": kind,
        "seed_depth": depth,
        "category": "服装",
    }


class SourceAdmissionTests(unittest.TestCase):
    def test_production_source_selection_fails_closed_to_approved_sources(self):
        self.assertEqual(
            cn_sources.production_sources("alibaba_keyword_recommend,taobao_suggest"),
            ("taobao_suggest",),
        )
        self.assertEqual(
            cn_sources.production_sources("alibaba_keyword_recommend"),
            ("taobao_suggest",),
        )

    def test_unapproved_official_adapter_does_not_send_a_request(self):
        adapter = cn_sources.get_cn_source_adapter("alibaba_keyword_recommend")
        with self.assertRaisesRegex(RuntimeError, "requires signed authorized access"):
            adapter.fetch_with_status("连衣裙", "服装")

    def test_invalid_taobao_response_is_counted_as_an_outbound_failure(self):
        response = SimpleNamespace(status_code=200, json=lambda: [])
        with (
            patch.object(cn_ecommerce, "_respect_source_min_interval"),
            patch.object(cn_ecommerce.requests, "get", return_value=response),
        ):
            result = cn_ecommerce._fetch_taobao_suggest_result("连衣裙", "服装")

        self.assertIsNone(result.candidates)
        self.assertTrue(result.outbound)
        self.assertEqual(result.status_code, 200)

    def test_429_preserves_retry_after(self):
        response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "7200"},
        )
        with (
            patch.object(cn_ecommerce, "_respect_source_min_interval"),
            patch.object(cn_ecommerce.requests, "get", return_value=response),
        ):
            result = cn_ecommerce._fetch_taobao_suggest_result("连衣裙", "服装")

        self.assertTrue(result.outbound)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after_seconds, 7200)

    def test_timeout_is_an_outbound_failure(self):
        import requests

        with (
            patch.object(cn_ecommerce, "_respect_source_min_interval"),
            patch.object(cn_ecommerce.requests, "get", side_effect=requests.Timeout()),
        ):
            result = cn_ecommerce._fetch_taobao_suggest_result("连衣裙", "服装")

        self.assertTrue(result.outbound)
        self.assertIsNone(result.candidates)

    def test_retry_after_backoff_is_at_least_the_source_requested_delay(self):
        before = datetime.now(timezone.utc)
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(collector_state, "get_source_snapshot", return_value={"error_streak": 0}),
            patch.object(db, "execute") as execute,
        ):
            collector_state.record_source_error(
                "taobao_suggest", "CN", "女装", retry_after_seconds=7200
            )

        next_fetch_at = execute.call_args.args[1][1]
        self.assertGreaterEqual(next_fetch_at, before + timedelta(seconds=7199))


class ChineseBudgetAndProvenanceTests(unittest.TestCase):
    def _patch_non_network_state(self):
        return (
            patch.object(collector_state, "get_healthy_collection_run_streak", return_value=0),
            patch.object(collector_state, "get_candidate_observations", return_value={}),
            patch.object(collector_state, "observe_candidates", return_value={
                "attempted": 0,
                "written": 0,
                "write_failed": False,
            }),
            patch.object(collector_state, "mark_seed_queried"),
            patch.object(collector_state, "upsert_seed_frontier"),
            patch.object(collector_state, "list_seed_frontier", return_value=[]),
            patch.object(collector_state, "trim_seed_frontier"),
            patch.object(cn_ecommerce, "cn_anchors_exist", return_value=set()),
        )

    def test_cache_hits_do_not_consume_daily_outbound_request_budget(self):
        seeds = [_seed(f"高质量种子{i}") for i in range(5)]
        lanes = {
            "high_quality": seeds,
            "fixed": [],
            "depth2": [],
            "exploration": [],
        }
        responses = [
            ([{"word": "缓存候选", "rank": 0}], "cache", False),
            ([{"word": "请求候选一", "rank": 0}], "request", True),
            ([{"word": "请求候选二", "rank": 0}], "request", True),
        ]
        with ExitStack() as stack:
            stack.enter_context(patch.object(cn_ecommerce, "CN_SOURCE_LIST", ("taobao_suggest",)))
            stack.enter_context(patch.object(cn_ecommerce, "get_daily_request_budget", return_value=(2, 0)))
            stack.enter_context(patch.object(cn_ecommerce, "_select_seed_lanes", return_value=lanes))
            fetch = stack.enter_context(patch.object(cn_ecommerce, "_fetch_one_seed", side_effect=responses))
            for patcher in self._patch_non_network_state():
                stack.enter_context(patcher)
            cn_ecommerce.get_cn_trending_words("budget-run")

        self.assertEqual(fetch.call_count, 3)
        stats = cn_ecommerce.get_last_collection_stats()
        self.assertEqual(stats["request_budget"], 2)
        self.assertEqual(stats["source_requests"], 2)
        self.assertEqual(stats["source_cache_hits"], 1)
        self.assertEqual(stats["source_metrics"]["taobao_suggest"]["requests"], 2)
        self.assertEqual(stats["source_metrics"]["taobao_suggest"]["unique_candidates"], 3)
        self.assertEqual(stats["source_metrics"]["taobao_suggest"]["selected_candidates"], 3)

    def test_candidate_observation_failure_does_not_block_qdrant_filtering(self):
        seed = _seed("女装", kind="fixed")
        lanes = {
            "high_quality": [seed],
            "fixed": [],
            "depth2": [],
            "exploration": [],
        }
        state_patches = self._patch_non_network_state()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cn_ecommerce, "get_daily_request_budget", return_value=(1, 0)))
            stack.enter_context(patch.object(cn_ecommerce, "_select_seed_lanes", return_value=lanes))
            stack.enter_context(patch.object(cn_ecommerce, "_fetch_one_seed", return_value=(
                [{"word": "新锚点候选", "rank": 0, "raw_heat": 10}], "request", True
            )))
            stack.enter_context(patch.object(
                collector_state,
                "observe_candidates",
                return_value={"attempted": 1, "written": 0, "write_failed": True},
            ))
            exists = stack.enter_context(patch.object(cn_ecommerce, "cn_anchors_exist", return_value=set()))
            for patcher in [state_patches[1], *state_patches[3:7]]:
                stack.enter_context(patcher)
            selected = cn_ecommerce.get_cn_trending_words("observation-failure")

        self.assertEqual([row[0] for row in selected], ["新锚点候选"])
        exists.assert_called_once()
        self.assertEqual(
            cn_ecommerce.get_last_collection_stats()["candidate_observation_write_failed"],
            1,
        )

    def test_429_stops_the_source_for_the_rest_of_the_run(self):
        lanes = {
            "high_quality": [_seed(f"高质量种子{i}") for i in range(5)],
            "fixed": [],
            "depth2": [],
            "exploration": [],
        }
        state_patches = self._patch_non_network_state()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cn_ecommerce, "CN_SOURCE_LIST", ("taobao_suggest",)))
            stack.enter_context(patch.object(cn_ecommerce, "get_daily_request_budget", return_value=(3, 0)))
            stack.enter_context(patch.object(cn_ecommerce, "_select_seed_lanes", return_value=lanes))
            fetch = stack.enter_context(patch.object(cn_ecommerce, "_fetch_one_seed", return_value=(
                [], "error", False, 429, 7200
            )))
            for patcher in state_patches:
                stack.enter_context(patcher)
            cn_ecommerce.get_cn_trending_words("rate-limit-run")

        fetch.assert_called_once()
        stats = cn_ecommerce.get_last_collection_stats()
        self.assertEqual(stats["source_requests"], 1)
        self.assertEqual(stats["source_429"], 1)
        self.assertEqual(stats["source_metrics"]["taobao_suggest"]["errors"], 1)

    def test_cross_source_duplicates_use_nfkc_key_and_keep_support(self):
        seed_a = _seed("种子甲")
        seed_b = _seed("种子乙")
        aggregate, _ = cn_ecommerce._aggregate_suggestions([
            (
                seed_a,
                [{"word": "ＡＢＣ  dress", "rank": 0, "raw_heat": 100}],
                True,
                "taobao_suggest",
            ),
            (
                seed_b,
                [{"word": "abc dress", "rank": 0, "raw_heat": 2}],
                True,
                "approved_public_source",
            ),
        ])

        self.assertEqual(list(aggregate), ["abc dress"])
        self.assertEqual(aggregate["abc dress"]["source_support_count"], 2)
        self.assertEqual(aggregate["abc dress"]["seed_support_count"], 2)
        self.assertEqual(
            aggregate["abc dress"]["source_set"],
            ["approved_public_source", "taobao_suggest"],
        )

    def test_budget_rises_only_after_seven_healthy_runs(self):
        with patch.object(collector_state, "get_healthy_collection_run_streak", return_value=6):
            self.assertEqual(cn_ecommerce.get_daily_request_budget(), (200, 6))
        with patch.object(collector_state, "get_healthy_collection_run_streak", return_value=7):
            self.assertEqual(cn_ecommerce.get_daily_request_budget(), (500, 7))

    def test_health_streak_requires_quality_and_candidate_observation_gates(self):
        healthy_result = {
            "expansion_metrics_version": 1,
            "source_requests": 200,
            "source_errors": 0,
            "source_429": 0,
            "source_success_rate": 1.0,
            "source_429_rate": 0.0,
            "candidate_observation_write_failed": 0,
            "qdrant_existing_filter_failed": False,
        }
        cn_rows = [({"status": "success", "result": healthy_result},)] * 7
        overseas_row = ({
            "status": "success",
            "result": {
                "expansion_metrics_version": 1,
                "candidate_observation_write_failed": 0,
            },
        },)
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(db, "fetchall", return_value=cn_rows),
            patch.object(db, "fetchone", return_value=overseas_row),
        ):
            self.assertEqual(
                collector_state.get_healthy_collection_run_streak("collect.cn.auto", limit=7),
                7,
            )

        unhealthy = dict(healthy_result, candidate_observation_write_failed=1)
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(db, "fetchall", return_value=[({"status": "success", "result": unhealthy},)] * 7),
            patch.object(db, "fetchone", return_value=overseas_row),
        ):
            self.assertEqual(
                collector_state.get_healthy_collection_run_streak("collect.cn.auto", limit=7),
                0,
            )


class OverseasExpansionTests(unittest.TestCase):
    def test_anchor_selection_keeps_newest_and_rotates_across_full_collection(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = [
            SimpleNamespace(payload={
                "cn_word": f"锚点{index}",
                "category": ("服装", "鞋类", "家居")[index % 3],
                "first_seen_at": (base + timedelta(days=index)).isoformat(),
            })
            for index in range(15)
        ]
        with patch.object(seed_builder, "_iter_scroll", return_value=iter(records)):
            first = seed_builder.get_rotating_anchor_words(
                limit=5, now=base + timedelta(days=20)
            )
        with patch.object(seed_builder, "_iter_scroll", return_value=iter(records)):
            second = seed_builder.get_rotating_anchor_words(
                limit=5, now=base + timedelta(days=20, hours=8)
            )

        self.assertEqual(first[0][0], "锚点14")
        self.assertEqual(second[0][0], "锚点14")
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertNotEqual(first[1:], second[1:])

    def test_country_cap_advances_20_to_35_to_50_after_healthy_stages(self):
        for streak, expected in ((0, 20), (6, 20), (7, 35), (13, 35), (14, 50)):
            with self.subTest(streak=streak), patch.object(
                collector_state, "get_healthy_collection_run_streak", return_value=streak
            ):
                self.assertEqual(overseas_trends.get_per_country_cap(), (expected, streak))

    def test_per_country_merge_respects_cap_and_records_source(self):
        amazon = [
            {"query": f"amazon {index}", "country": "DE", "category": "服装", "trend_score": 1.0, "source": "amazon_suggest"}
            for index in range(40)
        ]
        ebay = [
            {"query": f"ebay {index}", "country": "DE", "category": "服装", "trend_score": 0.8, "source": "ebay_suggest"}
            for index in range(40)
        ]

        merged = overseas_trends._merge_sources(amazon, ebay, ["DE"], per_country=35)

        self.assertEqual(len(merged), 35)
        self.assertTrue(all(row[1] == "DE" for row in merged))
        self.assertEqual(sum(row[4] == "amazon_suggest" for row in merged), 26)
        self.assertEqual(sum(row[4] == "ebay_suggest" for row in merged), 9)


class ExpansionReportingTests(unittest.TestCase):
    def test_successful_anchor_insert_counts_are_attributed_to_each_source(self):
        result = cn_longtail._with_source_insert_metrics(
            {"source_metrics": {"taobao_suggest": {"requests": 3}}},
            {"taobao_suggest": 2},
        )

        self.assertEqual(result["source_metrics"]["taobao_suggest"]["new_anchors"], 2)
        self.assertEqual(result["new_anchors_by_source"], {"taobao_suggest": 2})

    def test_source_aggregate_is_safe_and_keeps_expansion_counters(self):
        safe = task_scheduler._safe_result({
            "source_metrics": {
                "taobao_suggest": {
                    "requests": 200,
                    "qdrant_existing": 15,
                    "new_anchors": 50,
                }
            },
            "new_anchors_by_source": {"taobao_suggest": 50},
            "words": ["must-not-be-retained"],
        })

        self.assertEqual(safe["source_metrics"]["taobao_suggest"]["new_anchors"], 50)
        self.assertEqual(safe["new_anchors_by_source"]["taobao_suggest"], 50)
        self.assertNotIn("words", safe)

    def test_llm_usage_and_optional_price_are_aggregated_by_country_and_category(self):
        rows = [
            [],
            [],
            [("assess", 3, 1, 0, 1000, 500)],
            [("DE", "服装", "assess", 3, 1000, 500)],
            [("candidate_observation_total", 3)],
        ]
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(db, "fetchall", side_effect=rows),
            patch.dict(os.environ, {
                "LLM_INPUT_COST_PER_1M_USD": "2",
                "LLM_OUTPUT_COST_PER_1M_USD": "4",
            }),
        ):
            summary = daily_report._db_summary_sync()

        context = summary["llm"]["by_country_category"]["DE"]["服装"]
        self.assertEqual(context["calls"], 3)
        self.assertEqual(context["input_tokens"], 1000)
        self.assertEqual(context["output_tokens"], 500)
        self.assertEqual(context["estimated_cost_usd"], 0.004)
        self.assertEqual(summary["llm"]["estimated_cost_usd"], 0.004)

    def test_provider_token_usage_keys_are_normalized(self):
        self.assertEqual(llm_trace._usage_count({"prompt_tokens": "12"}, "input"), 12)
        self.assertEqual(llm_trace._usage_count({"completion_tokens": 7}, "output"), 7)
        self.assertEqual(llm_trace._usage_count({"inputTokens": 8}, "input"), 8)
        self.assertEqual(llm_trace._usage_count({"outputTokens": 4}, "output"), 4)
        self.assertEqual(
            llm_trace._usage_count({"prompt_tokens": "bad", "input_tokens": 9}, "input"),
            9,
        )


if __name__ == "__main__":
    unittest.main()
