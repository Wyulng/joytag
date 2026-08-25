import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services import collector_state
from services import db
from services.collectors import cn_ecommerce
from services.collectors import amazon_suggest


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


if __name__ == "__main__":
    unittest.main()
