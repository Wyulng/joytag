import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import alignment
from services.collectors import amazon_suggest, cn_longtail, overseas_trends, seed_builder
from services.embedding import EMBEDDING_DIM, get_embeddings
from services.qdrant_store import (
    ANCHOR_COLLECTION,
    BLOCKED_COLLECTION,
    LOCAL_COLLECTION,
    PENDING_COLLECTION,
    _iter_scroll,
    _scroll_all,
    batch_count_linked_local_tags,
    get_dashboard_stats,
    get_existing_word_decision,
)


class _Encoded:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


def _point(point_id, **payload):
    return SimpleNamespace(id=point_id, payload=payload)


class BatchEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_65_texts_use_three_batches_in_order(self):
        model = MagicMock()

        def encode(batch, **_kwargs):
            return _Encoded([[1.0] * EMBEDDING_DIM for _ in batch])

        model.encode.side_effect = encode
        texts = [f"word-{index}" for index in range(65)]

        with patch("services.embedding._get_model", return_value=model):
            vectors = await get_embeddings(texts)

        self.assertEqual(len(vectors), 65)
        self.assertEqual([len(vector) for vector in vectors], [EMBEDDING_DIM] * 65)
        self.assertEqual(model.encode.call_count, 3)
        self.assertEqual([len(call.args[0]) for call in model.encode.call_args_list], [32, 32, 1])
        self.assertTrue(all(abs(sum(value * value for value in vector) ** 0.5 - 1.0) < 1e-6
                            for vector in vectors))

    async def test_empty_input_and_empty_text_are_rejected_as_specified(self):
        self.assertEqual(await get_embeddings([]), [])
        with self.assertRaises(ValueError):
            await get_embeddings(["  "])


class StreamingQdrantTests(unittest.TestCase):
    def test_iter_scroll_reads_each_page_and_scroll_all_keeps_compatibility(self):
        client = MagicMock()
        first_page = [_point("1", word="one")]
        second_page = [_point("2", word="two")]
        client.scroll.side_effect = [
            (first_page, "next"),
            (second_page, None),
        ]

        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            streamed = list(_iter_scroll("local_tags", batch_size=1))

        self.assertEqual([point.id for point in streamed], ["1", "2"])
        self.assertEqual(client.scroll.call_count, 2)

        client.scroll.reset_mock()
        client.scroll.side_effect = [
            (first_page, "next"),
            (second_page, None),
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            materialized = _scroll_all("local_tags", batch_size=1)
        self.assertEqual([point.id for point in materialized], ["1", "2"])

    def test_recent_anchor_keeps_only_bounded_sorted_candidates(self):
        records = [
            _point(str(index), cn_word=f"word-{index}", category="服装",
                   created_at=f"2026-01-{index % 28 + 1:02d}T{index // 28:02d}:00:00+00:00")
            for index in range(60)
        ]
        records.extend([
            _point("tie-b", cn_word="b-word", category="服装", created_at="2026-02-01T00:00:00+00:00"),
            _point("tie-a", cn_word="a-word", category="服装", created_at="2026-02-01T00:00:00+00:00"),
        ])
        with patch.object(seed_builder, "_iter_scroll", return_value=iter(records)) as stream:
            recent = seed_builder.get_recent_anchor_words(limit=50)

        self.assertEqual(len(recent), 50)
        self.assertEqual(recent[:2], [("a-word", "服装"), ("b-word", "服装")])
        self.assertEqual(stream.call_count, 1)

    def test_dashboard_and_linked_tag_counts_consume_stream(self):
        client = MagicMock()
        count_by_collection = {
            LOCAL_COLLECTION: 4,
            PENDING_COLLECTION: 2,
            ANCHOR_COLLECTION: 3,
            BLOCKED_COLLECTION: 1,
        }
        client.count.side_effect = lambda collection_name, **_kwargs: SimpleNamespace(
            count=count_by_collection[collection_name]
        )
        countries = iter([_point("1", country="DE"), _point("2", country="FR"), _point("3", country="DE")])
        with (
            patch("services.qdrant_store.get_qdrant_client", return_value=client),
            patch("services.qdrant_store._iter_scroll", return_value=countries) as stream,
        ):
            stats = get_dashboard_stats()
        self.assertEqual(stats["distinct_countries"], 2)
        self.assertEqual(stats["compliance_rate"], 0.667)
        stream.assert_called_once()

        linked = iter([
            _point("1", anchor_cn_id="a"),
            _point("2", anchor_cn_id="a"),
            _point("3", anchor_cn_id="b"),
        ])
        with patch("services.qdrant_store._iter_scroll", return_value=linked):
            self.assertEqual(batch_count_linked_local_tags(["a", "b", "missing"]),
                             {"a": 2, "b": 1, "missing": 0})


class DecisionCacheTests(unittest.TestCase):
    def test_blocked_decision_has_priority_over_other_collections(self):
        point_id = "blocked-id"
        client = MagicMock()
        client.retrieve.return_value = [
            _point(point_id, word="eco claim", country="DE", reason="blocked")
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            decision = get_existing_word_decision("eco claim", "DE")

        self.assertEqual(decision["action"], "blocked")
        self.assertEqual(decision["collection"], BLOCKED_COLLECTION)
        client.retrieve.assert_called_once()
        self.assertEqual(client.retrieve.call_args.kwargs["collection_name"], BLOCKED_COLLECTION)

    def test_direct_alignment_cache_hit_skips_all_processing(self):
        decision = {
            "collection": PENDING_COLLECTION,
            "id": "pending-1",
            "payload": {"assessment_reason": "already reviewed"},
            "action": "pending",
            "status": "存疑",
            "stored": False,
        }
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=decision),
            patch.object(alignment, "get_embedding", new=AsyncMock()) as embed,
            patch.object(alignment, "search_cn_anchor_by_word") as search,
            patch.object(alignment, "check_word_against_rules") as rules,
            patch.object(alignment, "assess_single", new=AsyncMock()) as assess,
        ):
            result = asyncio.run(alignment.process_overseas_word("winter coat", "DE"))

        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["action"], "pending")
        embed.assert_not_awaited()
        search.assert_not_called()
        rules.assert_not_called()
        assess.assert_not_awaited()


class SharedFetchExecutorTests(unittest.TestCase):
    def tearDown(self):
        amazon_suggest.close_fetch_executor()

    def test_all_fetch_jobs_are_capped_at_16_workers(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def fetch(_country, seed, category):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return [(seed, 0, category)]

        seeds = {"DE": [(f"seed-{index}", "服装") for index in range(64)]}
        result = amazon_suggest.fanout_fetch(
            ["DE"], seeds, fetch, "test_source", max_workers=1
        )

        self.assertLessEqual(maximum, amazon_suggest.OVERSEAS_FETCH_WORKERS)
        self.assertEqual(amazon_suggest._get_fetch_executor()._max_workers, 16)
        self.assertEqual(len(result), 50)

    def test_executor_can_be_recreated_after_shutdown(self):
        first = amazon_suggest._get_fetch_executor()
        amazon_suggest.close_fetch_executor()
        second = amazon_suggest._get_fetch_executor()
        self.assertIsNot(first, second)


class CollectorBatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_collector_batches_only_new_words(self):
        words = [("a", 1, "服装"), ("b", 2, "服装"), ("a", 3, "服装")]
        vectors = [[float(index)] * EMBEDDING_DIM for index in range(1, 3)]
        with (
            patch.object(cn_longtail, "_load_progress", return_value={"processed_words": []}),
            patch.object(cn_longtail, "_save_progress"),
            patch.object(cn_longtail, "fetch_cn_longtail_words", new=AsyncMock(return_value=words)),
            patch.object(cn_longtail, "cn_anchors_exist", return_value=set()) as qdrant_lookup,
            patch.object(cn_longtail, "get_embeddings", new=AsyncMock(return_value=vectors)) as batch,
            patch.object(cn_longtail, "process_cn_longtail_word", new=AsyncMock()) as process,
            patch.object(cn_longtail, "record_event"),
        ):
            results = [item async for item in cn_longtail._collect_cn_generator()]

        self.assertEqual(batch.await_args.args[0], ["a", "b"])
        qdrant_lookup.assert_called_once_with(["a", "b"])
        self.assertEqual(process.await_count, 2)
        self.assertEqual(process.call_args_list[0].kwargs["vector"], vectors[0])
        self.assertEqual(results[-1]["new"], 2)
        self.assertEqual(results[-1]["duplicates"], 1)

    async def test_overseas_collector_skips_cached_words_before_batch_embedding(self):
        trends = [
            ("cached", "DE", "服装", 1.0, "amazon_suggest"),
            ("new-a", "DE", "服装", 0.9, "amazon_suggest"),
            ("new-b", "FR", "服装", 0.8, "ebay_suggest"),
        ]
        cached = {"action": "approved", "status": "可复用", "stored": True}
        vector_a = [1.0] * EMBEDDING_DIM
        vector_b = [2.0] * EMBEDDING_DIM

        def lookup(word, _country):
            return cached if word == "cached" else None

        async def process(**kwargs):
            return {"action": "approved", "stored": True, "status": "可复用"}

        with (
            patch.object(overseas_trends, "_load_progress", return_value={"processed_keys": []}),
            patch.object(overseas_trends, "_save_progress"),
            patch.object(overseas_trends, "fetch_all_trends", new=AsyncMock(return_value=trends)),
            patch.object(overseas_trends, "get_existing_word_decision", side_effect=lookup) as lookup_mock,
            patch.object(overseas_trends, "get_embeddings", new=AsyncMock(return_value=[vector_a, vector_b])) as batch,
            patch.object(overseas_trends, "process_overseas_word", side_effect=process) as process_mock,
            patch.object(overseas_trends, "record_event"),
        ):
            results = [item async for item in overseas_trends._collect_overseas_generator()]

        self.assertEqual(lookup_mock.call_count, 3)
        self.assertEqual(batch.await_args.args[0], ["new-a", "new-b"])
        self.assertEqual(process_mock.await_count, 2)
        self.assertEqual(results[0]["duplicate"], True)
        self.assertEqual(results[-1]["duplicates"], 1)
        self.assertEqual(results[-1]["approved"], 2)


if __name__ == "__main__":
    unittest.main()
