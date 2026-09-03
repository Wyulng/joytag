import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import collector_state
from services import db
from services.collectors import cn_ecommerce
from services.collectors import amazon_suggest
from services.qdrant_store import ANCHOR_COLLECTION, _generate_deterministic_id, cn_anchors_exist


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {
            "result": [
                ["夏季连衣裙", "100"],
                ["纯棉连衣裙", "1"],
                ["无热度字段", "not-a-number"],
                ["2026", "99"],
            ]
        }


class DatabaseBatchExecutionTests(unittest.TestCase):
    def test_execute_many_uses_cursor_executemany(self):
        pool = MagicMock()
        connection = pool.connection.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value

        with (
            patch.object(db, "_db_available", True),
            patch.object(db, "get_pool", return_value=pool),
        ):
            db.execute_many("INSERT INTO test_table VALUES (%s)", [(1,)])

        cursor.executemany.assert_called_once_with(
            "INSERT INTO test_table VALUES (%s)", [(1,)]
        )
        connection.executemany.assert_not_called()


class ChineseSourceTests(unittest.TestCase):
    def test_taobao_request_is_parameterized_and_keeps_relative_heat(self):
        with patch.object(cn_ecommerce.requests, "get", return_value=_Response()) as request:
            result = cn_ecommerce._fetch_taobao_suggest("连衣裙", "服装")

        self.assertEqual(request.call_args.kwargs["params"], {"code": "utf-8", "q": "连衣裙"})
        self.assertEqual([item["word"] for item in result], ["夏季连衣裙", "纯棉连衣裙", "无热度字段"])
        self.assertEqual(result[0]["raw_heat"], 100.0)
        self.assertEqual(result[2]["raw_heat"], None)

    def test_heat_normalization_uses_value_and_position_fallback(self):
        items = [
            {"word": "高热度", "rank": 0, "raw_heat": "100"},
            {"word": "低热度", "rank": 1, "raw_heat": "1"},
            {"word": "缺失热度", "rank": 2, "raw_heat": None},
        ]
        result = cn_ecommerce._normalise_response_heat(items)

        self.assertGreater(result[0]["item_heat"], result[1]["item_heat"])
        self.assertGreater(result[1]["item_heat"], result[2]["item_heat"])

    def test_same_word_from_multiple_seeds_is_merged_with_multi_seed_bonus(self):
        seed_a = {
            "seed_word": "女装",
            "normalized_seed": "女装",
            "seed_depth": 0,
            "category": "服装",
        }
        seed_b = {
            "seed_word": "连衣裙",
            "normalized_seed": "连衣裙",
            "seed_depth": 0,
            "category": "服装",
        }
        aggregate, changed = cn_ecommerce._aggregate_suggestions([
            (seed_a, [{"word": "夏季连衣裙", "rank": 0, "raw_heat": 10}], True),
            (seed_b, [{"word": "夏季连衣裙", "rank": 1, "raw_heat": 8}], True),
        ])

        self.assertEqual(len(aggregate), 1)
        self.assertEqual(aggregate["夏季连衣裙"]["parent_count"], 2)
        self.assertEqual(len(changed), 1)
        self.assertGreater(aggregate["夏季连衣裙"]["source_heat_score"], 0.4)

    def test_seed_selection_enforces_dynamic_fixed_quotas(self):
        now = datetime.now(timezone.utc)
        frontier = [
            {
                "seed_word": f"动态{index}",
                "normalized_seed": f"动态{index}",
                "seed_kind": "suggestion",
                "category": "服装" if index % 2 else "鞋类",
                "source_heat_score": 0.9,
                "last_seen_at": now,
                "last_queried_at": None,
                "next_query_at": None,
            }
            for index in range(70)
        ] + [
            {
                "seed_word": f"固定{index}",
                "normalized_seed": f"固定{index}",
                "seed_kind": "fixed",
                "category": "服装" if index % 2 else "鞋类",
                "source_heat_score": 0.5,
                "last_seen_at": now,
                "last_queried_at": None,
                "next_query_at": None,
            }
            for index in range(30)
        ]
        with patch.object(cn_ecommerce, "_ensure_seed_frontier", return_value=frontier):
            selected = cn_ecommerce._select_seed_records()

        self.assertEqual(len(selected), 80)
        self.assertEqual(sum(item["seed_kind"] != "fixed" for item in selected), 64)
        self.assertEqual(sum(item["seed_kind"] == "fixed" for item in selected), 16)

    def test_dynamic_seed_is_not_due_during_cooldown(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertFalse(cn_ecommerce._seed_is_due({"next_query_at": future}, datetime.now(timezone.utc)))


class ChineseCandidateSelectionTests(unittest.TestCase):
    def _seed(self, *, depth=0):
        return {
            "seed_word": "女装",
            "normalized_seed": "女装",
            "seed_kind": "fixed",
            "category": "服装",
            "seed_depth": depth,
        }

    def test_observes_all_candidates_and_filters_existing_before_limit(self):
        seed = self._seed(depth=2)
        items = [
            {"word": "已有锚点", "rank": 0, "raw_heat": 10},
            {"word": "新词甲", "rank": 1, "raw_heat": 9},
            {"word": "新词乙", "rank": 2, "raw_heat": 8},
        ]
        observation_result = {
            "attempted": 3,
            "written": 3,
            "write_failed": False,
            "error_type": None,
        }
        with (
            patch.object(cn_ecommerce, "_select_seed_records", return_value=[seed]),
            patch.object(cn_ecommerce, "_fetch_one_seed", return_value=(items, "request", True)),
            patch.object(cn_ecommerce.collector_state, "mark_seed_queried"),
            patch.object(cn_ecommerce.collector_state, "get_candidate_observations", return_value={}),
            patch.object(
                cn_ecommerce.collector_state,
                "observe_candidates",
                return_value=observation_result,
            ) as observe,
            patch.object(cn_ecommerce.collector_state, "upsert_seed_frontier"),
            patch.object(cn_ecommerce.collector_state, "list_seed_frontier", return_value=[seed]),
            patch.object(cn_ecommerce.collector_state, "trim_seed_frontier") as trim,
            patch.object(cn_ecommerce, "cn_anchors_exist", return_value={"已有锚点"}) as existing,
        ):
            selected = cn_ecommerce.get_cn_trending_words("run-1")

        self.assertEqual([word for word, _heat, _category in selected], ["新词甲", "新词乙"])
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(len(observe.call_args.args[2]), 3)
        self.assertEqual(observe.call_args.kwargs["run_id"], "run-1")
        existing.assert_called_once_with(["已有锚点", "新词甲", "新词乙"])
        trim.assert_called_once_with(cn_ecommerce.CN_MAX_FRONTIER)
        stats = cn_ecommerce.get_last_collection_stats()
        self.assertEqual(stats["candidate_observations"], 3)
        self.assertEqual(stats["candidate_observations_backfilled"], 3)
        self.assertEqual(stats["eligible_before_qdrant"], 3)
        self.assertEqual(stats["qdrant_existing_filtered"], 1)
        self.assertEqual(stats["selected_candidates"], 2)

    def test_cached_snapshot_missing_observations_is_backfilled_without_new_seed(self):
        seed = self._seed()
        items = [{"word": "快照新词", "rank": 0, "raw_heat": 3}]
        with (
            patch.object(cn_ecommerce, "_select_seed_records", return_value=[seed]),
            patch.object(cn_ecommerce, "_fetch_one_seed", return_value=(items, "cache", False)),
            patch.object(cn_ecommerce.collector_state, "get_candidate_observations", return_value={}),
            patch.object(
                cn_ecommerce.collector_state,
                "observe_candidates",
                return_value={"attempted": 1, "written": 1, "write_failed": False},
            ) as observe,
            patch.object(cn_ecommerce.collector_state, "list_seed_frontier", return_value=[seed]),
            patch.object(cn_ecommerce.collector_state, "trim_seed_frontier"),
            patch.object(cn_ecommerce, "cn_anchors_exist", return_value=set()),
        ):
            selected = cn_ecommerce.get_cn_trending_words("run-cache")

        self.assertEqual([word for word, _heat, _category in selected], ["快照新词"])
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["run_id"], "run-cache")

    def test_existing_observations_are_not_rewritten_for_unchanged_snapshot(self):
        seed = self._seed()
        items = [{"word": "已观察词", "rank": 0, "raw_heat": 3}]
        observation = {
            "normalized_word": "已观察词",
            "decision_status": None,
            "next_eligible_at": None,
        }
        with (
            patch.object(cn_ecommerce, "_select_seed_records", return_value=[seed]),
            patch.object(cn_ecommerce, "_fetch_one_seed", return_value=(items, "cache", False)),
            patch.object(
                cn_ecommerce.collector_state,
                "get_candidate_observations",
                return_value={"已观察词": observation},
            ),
            patch.object(cn_ecommerce.collector_state, "observe_candidates") as observe,
            patch.object(cn_ecommerce.collector_state, "list_seed_frontier", return_value=[seed]),
            patch.object(cn_ecommerce.collector_state, "trim_seed_frontier"),
            patch.object(cn_ecommerce, "cn_anchors_exist", return_value=set()),
        ):
            cn_ecommerce.get_cn_trending_words("run-unchanged")

        observe.assert_not_called()
        self.assertEqual(cn_ecommerce.get_last_collection_stats()["candidate_observations"], 0)

    def test_frontier_is_trimmed_after_dynamic_expansion(self):
        seed = self._seed()
        before = [
            {
                "seed_kind": "suggestion",
                "seed_word": f"动态{index}",
            }
            for index in range(cn_ecommerce.CN_MAX_FRONTIER + 3)
        ]
        after = before[:cn_ecommerce.CN_MAX_FRONTIER]
        with (
            patch.object(cn_ecommerce, "_select_seed_records", return_value=[seed]),
            patch.object(cn_ecommerce, "_fetch_one_seed", return_value=([], "cache", False)),
            patch.object(cn_ecommerce.collector_state, "list_seed_frontier", side_effect=[before, after]),
            patch.object(cn_ecommerce.collector_state, "trim_seed_frontier") as trim,
        ):
            selected = cn_ecommerce.get_cn_trending_words("run-frontier")

        self.assertEqual(selected, [])
        trim.assert_called_once_with(cn_ecommerce.CN_MAX_FRONTIER)
        stats = cn_ecommerce.get_last_collection_stats()
        self.assertEqual(stats["active_dynamic_seeds"], cn_ecommerce.CN_MAX_FRONTIER)
        self.assertEqual(stats["frontier_trimmed"], 3)


class QdrantBatchLookupTests(unittest.TestCase):
    def test_cn_anchors_exist_retrieves_ids_in_chunks(self):
        client = MagicMock()
        first_id = _generate_deterministic_id("词甲")
        second_id = _generate_deterministic_id("词乙")
        client.retrieve.side_effect = [
            [SimpleNamespace(id=first_id)],
            [SimpleNamespace(id=second_id)],
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            existing = cn_anchors_exist(["词甲", "词乙"], batch_size=1)

        self.assertEqual(existing, {"词甲", "词乙"})
        self.assertEqual(client.retrieve.call_count, 2)
        self.assertEqual(
            client.retrieve.call_args_list[0].kwargs["ids"],
            [first_id],
        )
        self.assertEqual(
            client.retrieve.call_args_list[1].kwargs["ids"],
            [second_id],
        )


class SourceSnapshotTests(unittest.TestCase):
    def tearDown(self):
        amazon_suggest.close_fetch_executor()

    def test_fresh_snapshot_skips_network_fetch(self):
        fetch = MagicMock(return_value=[("network", 0, "服装")])
        snapshot = {"response": [["cached", 0, "服装"]]}
        with (
            patch.object(amazon_suggest.collector_state, "get_source_snapshot", return_value=snapshot),
            patch.object(amazon_suggest.collector_state, "source_snapshot_is_fresh", return_value=True),
        ):
            result = amazon_suggest.fanout_fetch(
                ["DE"], {"DE": [("dress", "服装")]}, fetch, "amazon_suggest"
            )

        fetch.assert_not_called()
        self.assertEqual(result[0]["query"], "cached")


class CollectorStateTests(unittest.TestCase):
    def test_key_normalization_is_conservative(self):
        self.assertEqual(collector_state.normalize_collector_key("  Ｃ＋＋   "), "c++")
        self.assertNotEqual(
            collector_state.normalize_collector_key("C++"),
            collector_state.normalize_collector_key("C"),
        )

    def test_observe_candidates_writes_batch_with_collection_run_id(self):
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(db, "execute_many") as execute_many,
        ):
            result = collector_state.observe_candidates(
                "cn",
                "CN",
                [{"word": "夏季连衣裙", "source_heat_score": 0.8}],
                source="taobao_suggest",
                run_id="run-42",
            )

        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["written"], 1)
        self.assertFalse(result["write_failed"])
        rows = execute_many.call_args.args[1]
        self.assertEqual(rows[0][8], "run-42")

    def test_observe_candidates_reports_write_failure_without_raising(self):
        with (
            patch.object(db, "is_db_available", return_value=True),
            patch.object(db, "execute_many", side_effect=RuntimeError("db down")),
            self.assertLogs("services.collector_state", level="WARNING") as logs,
        ):
            result = collector_state.observe_candidates(
                "cn",
                "CN",
                [{"word": "失败候选", "source_heat_score": 0.5}],
                source="taobao_suggest",
            )

        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["written"], 0)
        self.assertTrue(result["write_failed"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertTrue(any("attempted=1" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
