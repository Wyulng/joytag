import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import alignment
from services.embedding import EMBEDDING_DIM, _repair_position_ids, get_embedding
from services.qdrant_store import VECTOR_SIZE, insert_pending_review, upsert_cn_anchor


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
        with (
            patch.object(alignment, "get_embedding", new=AsyncMock(return_value=vector)) as embed,
            patch.object(
                alignment,
                "search_cn_anchor_by_word",
                return_value={"id": "anchor-1", "cn_word": "冬季外套", "score": 0.91},
            ),
            patch.object(alignment, "assess_single", new=AsyncMock(return_value=("可复用", "通过", None, None))),
            patch.object(alignment, "upsert_local_tag", return_value="tag-1") as upsert,
            patch.object(alignment, "_record_word_lineage"),
        ):
            result = await alignment.process_overseas_word(
                word="winter coat",
                country="DE",
                source="amazon_suggest",
            )

        embed.assert_awaited_once_with("winter coat")
        self.assertEqual(upsert.call_args.kwargs["vector"], vector)
        self.assertEqual(result["anchor_cn_word"], "冬季外套")
        self.assertNotIn("translate_foreign_to_chinese", alignment.__dict__)


class QdrantEmbeddingMetadataTests(unittest.TestCase):
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
