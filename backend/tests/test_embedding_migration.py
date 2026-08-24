import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import alignment
from services.embedding import EMBEDDING_DIM, _repair_position_ids, get_embedding
from services.qdrant_store import (
    VECTOR_SIZE,
    insert_pending_review,
    search_cn_anchor_by_word,
    upsert_cn_anchor,
)


class EmbeddingMigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_gte_position_ids_are_restored_when_loader_leaves_garbage(self):
        import torch

        embeddings = MagicMock()
        embeddings.position_ids = torch.tensor([91, -7, 12, 44], dtype=torch.long)
        auto_model = MagicMock(embeddings=embeddings)
        transformer = MagicMock(auto_model=auto_model)
        model = MagicMock()
        model._first_module.return_value = transformer

        _repair_position_ids(model)

        self.assertEqual(embeddings.position_ids.tolist(), [0, 1, 2, 3])

    async def test_embedding_is_normalized_and_768_dimensions(self):
        encoded = MagicMock()
        encoded.tolist.return_value = [0.125] * EMBEDDING_DIM
        model = MagicMock()
        model.encode.return_value = encoded

        with patch("services.embedding._get_model", return_value=model):
            vector = await get_embedding("winter coat")

        self.assertEqual(len(vector), 768)
        self.assertAlmostEqual(sum(value * value for value in vector) ** 0.5, 1.0)
        model.encode.assert_called_once_with(
            "winter coat",
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

    async def test_overseas_alignment_uses_one_direct_multilingual_embedding(self):
        vector = [0.25] * VECTOR_SIZE
        assess = AsyncMock(return_value=("可复用", "通过", None, None))
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=None),
            patch.object(alignment, "get_embedding", new=AsyncMock(return_value=vector)) as embed,
            patch.object(
                alignment,
                "search_cn_anchor_by_word",
                return_value={"id": "anchor-1", "cn_word": "冬季外套", "score": 0.91},
            ) as search_anchor,
            patch.object(
                alignment, "check_word_against_rules", return_value=(None, "", None)
            ),
            patch.object(alignment, "assess_single", new=assess),
            patch.object(alignment, "upsert_local_tag", return_value="tag-1") as upsert,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word(
                word="winter coat",
                country="DE",
                category="服装",
                source="amazon_suggest",
            )

        embed.assert_awaited_once_with("winter coat")
        search_anchor.assert_called_once_with("winter coat", vector, category="服装")
        assess.assert_awaited_once_with("winter coat", "DE", category="服装")
        self.assertEqual(upsert.call_args.kwargs["vector"], vector)
        self.assertEqual(upsert.call_args.kwargs["assessed_by"], "llm")
        self.assertEqual(result["anchor_cn_word"], "冬季外套")
        self.assertNotIn("translate_foreign_to_chinese", alignment.__dict__)

    async def test_anchor_safe_rule_skips_assessment_and_records_rule_source(self):
        assess = AsyncMock()
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=None),
            patch.object(
                alignment,
                "get_embedding",
                new=AsyncMock(return_value=[0.25] * VECTOR_SIZE),
            ),
            patch.object(
                alignment,
                "search_cn_anchor_by_word",
                return_value={"id": "anchor-1", "cn_word": "冬季外套", "score": 0.91},
            ),
            patch.object(
                alignment,
                "check_word_against_rules",
                return_value=(True, "通过安全词库", None),
            ),
            patch.object(alignment, "assess_single", new=assess),
            patch.object(alignment, "upsert_local_tag", return_value="tag-1") as upsert,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word("winter coat", "DE")

        assess.assert_not_awaited()
        self.assertEqual(result["action"], "approved")
        self.assertEqual(upsert.call_args.kwargs["assessed_by"], "rule")

    async def test_no_anchor_skips_llm_and_enters_pending_review(self):
        vector = [0.25] * VECTOR_SIZE
        assess = AsyncMock()
        rule_check = MagicMock(return_value=(None, "", None))
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=None),
            patch.object(alignment, "get_embedding", new=AsyncMock(return_value=vector)),
            patch.object(alignment, "search_cn_anchor_by_word", return_value=None),
            patch.object(
                alignment,
                "check_word_against_rules",
                new=rule_check,
            ),
            patch.object(alignment, "assess_single", new=assess),
            patch.object(alignment, "insert_pending_review", return_value="pending-1") as pending,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word("unknown phrase", "DE")

        assess.assert_not_awaited()
        rule_check.assert_called_once_with(
            "unknown phrase", "DE", category=None, banned_first=True
        )
        self.assertEqual(result["action"], "pending_no_anchor")
        self.assertIn("未找到中文锚点，未调用 LLM", result["reason"])
        self.assertEqual(pending.call_args.kwargs["assessed_by"], "anchor_gate")
        self.assertNotIn("llm_trace_id", pending.call_args.kwargs)

    async def test_no_anchor_banned_rule_blocks_without_llm(self):
        assess = AsyncMock()
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=None),
            patch.object(
                alignment,
                "get_embedding",
                new=AsyncMock(return_value=[0.25] * VECTOR_SIZE),
            ),
            patch.object(alignment, "search_cn_anchor_by_word", return_value=None),
            patch.object(
                alignment,
                "check_word_against_rules",
                return_value=(False, "命中规则", "ucpd_env_generic"),
            ),
            patch.object(alignment, "assess_single", new=assess),
            patch.object(
                alignment, "insert_blocked_decision", return_value="blocked-1"
            ) as blocked,
            patch.object(alignment, "insert_pending_review") as pending,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word("eco-friendly", "DE")

        assess.assert_not_awaited()
        pending.assert_not_called()
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["rule_id"], "ucpd_env_generic")
        self.assertEqual(blocked.call_args.kwargs["rule_id"], "ucpd_env_generic")
        self.assertNotIn("llm_trace_id", blocked.call_args.kwargs)

    async def test_no_anchor_safe_rule_still_requires_manual_anchor(self):
        assess = AsyncMock()
        with (
            patch.object(alignment, "get_existing_word_decision", return_value=None),
            patch.object(
                alignment,
                "get_embedding",
                new=AsyncMock(return_value=[0.25] * VECTOR_SIZE),
            ),
            patch.object(alignment, "search_cn_anchor_by_word", return_value=None),
            patch.object(
                alignment,
                "check_word_against_rules",
                return_value=(True, "通过安全词库", None),
            ),
            patch.object(alignment, "assess_single", new=assess),
            patch.object(alignment, "insert_pending_review", return_value="pending-1") as pending,
            patch.object(alignment, "upsert_local_tag") as upsert,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word("winter coat", "DE")

        assess.assert_not_awaited()
        upsert.assert_not_called()
        self.assertEqual(result["action"], "pending_no_anchor")
        self.assertEqual(pending.call_args.kwargs["assessed_by"], "rule")
        self.assertIn("已通过安全规则", result["reason"])


class QdrantEmbeddingMetadataTests(unittest.TestCase):
    def test_multilingual_anchor_threshold_accepts_cross_language_match(self):
        client = MagicMock()
        client.search.return_value = [
            SimpleNamespace(
                id="anchor-1",
                score=0.641,
                payload={"cn_word": "冬季外套"},
            )
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            result = search_cn_anchor_by_word(
                "winter coat", [0.0] * VECTOR_SIZE, category="服装"
            )

        self.assertEqual(result["cn_word"], "冬季外套")
        query_filter = client.search.call_args.kwargs["query_filter"]
        self.assertEqual(
            query_filter.model_dump(exclude_none=True)["must"][0]["match"]["value"],
            "服装",
        )

    def test_uncategorized_match_keeps_stricter_threshold(self):
        client = MagicMock()
        client.search.return_value = [
            SimpleNamespace(
                id="anchor-1",
                score=0.641,
                payload={"cn_word": "冬季外套"},
            )
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            result = search_cn_anchor_by_word("winter coat", [0.0] * VECTOR_SIZE)

        self.assertIsNone(result)
        self.assertIsNone(client.search.call_args.kwargs["query_filter"])

    def test_anchor_payload_has_gte_metadata_and_768_vector_contract(self):
        client = MagicMock()
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            upsert_cn_anchor("冬季外套", [0.0] * VECTOR_SIZE)

        point = client.upsert.call_args.kwargs["points"][0]
        self.assertEqual(len(point.vector), 768)
        self.assertEqual(point.payload["embedding_model"], "Alibaba-NLP/gte-multilingual-base")
        self.assertEqual(point.payload["embedding_dim"], 768)
        self.assertTrue(point.payload["embedding_normalized"])

    def test_pending_payload_uses_768_dimension_metadata(self):
        client = MagicMock()
        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            insert_pending_review("winter coat", "DE", "needs review")

        point = client.upsert.call_args.kwargs["points"][0]
        self.assertEqual(len(point.vector), 768)
        self.assertEqual(point.payload["embedding_dim"], 768)


if __name__ == "__main__":
    unittest.main()
