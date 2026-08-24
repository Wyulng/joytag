import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app import app, build_recommend_items, build_recommend_response, get_local_tags
from models.schemas import RecommendItem, RecommendRequest
from services.recommend import (
    rerank_tags_with_llm,
    validate_rerank_recommendations,
)


def _candidate(word: str, similarity: float, **metadata):
    return {"word": word, "similarity": similarity, **metadata}


class KeycloakRealmContractTests(unittest.TestCase):
    def test_service_token_includes_backend_audience(self):
        realm_path = Path(__file__).resolve().parents[2] / "keycloak" / "realm-export.json"
        realm = json.loads(realm_path.read_text(encoding="utf-8"))
        service_client = next(
            client
            for client in realm["clients"]
            if client.get("clientId") == "joytag-service"
        )
        audience_mapper = next(
            mapper
            for mapper in service_client.get("protocolMappers", [])
            if mapper.get("protocolMapper") == "oidc-audience-mapper"
        )

        self.assertEqual(audience_mapper.get("protocol"), "openid-connect")
        self.assertEqual(
            audience_mapper["config"].get("included.client.audience"),
            "joytag-service",
        )
        self.assertEqual(audience_mapper["config"].get("access.token.claim"), "true")
        self.assertEqual(audience_mapper["config"].get("id.token.claim"), "false")


class RecommendSchemaTests(unittest.TestCase):
    def test_openapi_uses_current_public_and_admin_paths(self):
        paths = set(app.openapi()["paths"])
        expected = {
            "/v1/tag/recommend",
            "/v1/disclosure/parameters",
            "/v1/transparency",
            "/v1/dsar/request",
            "/admin/api/collect/overseas",
            "/admin/api/collect/cn",
            "/admin/api/pending",
            "/admin/api/tags",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertNotIn("/admin/collect/overseas", paths)
        self.assertNotIn("/admin/pending", paths)

    def test_country_is_normalized_and_top_k_is_bounded(self):
        request = RecommendRequest(title="  wool coat  ", target_country="de", top_k=10)
        self.assertEqual(request.title, "wool coat")
        self.assertEqual(request.target_country, "DE")

        for values in (
            {"title": "coat", "target_country": "US", "top_k": 5},
            {"title": "coat", "target_country": "DE", "top_k": 0},
            {"title": "coat", "target_country": "DE", "top_k": 11},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                RecommendRequest(**values)

    def test_empty_result_keeps_complete_public_contract(self):
        response = build_recommend_response([], total_candidates=3, filtered_candidates=0)
        self.assertEqual(response.recommendations, [])
        self.assertFalse(response.ai_assisted)
        self.assertIsNotNone(response.parameters_version)
        self.assertEqual(response.disclosure_url, "/v1/disclosure/parameters")

    def test_response_ai_flag_reflects_actual_items(self):
        fallback = RecommendItem(word="winter coat", reason="vector", ai_generated=False)
        assisted = RecommendItem(word="wool coat", reason="reranked", ai_generated=True)
        self.assertFalse(build_recommend_response([fallback], 2, 2).ai_assisted)
        self.assertTrue(build_recommend_response([fallback, assisted], 2, 2).ai_assisted)


class RecommendRankingTests(unittest.TestCase):
    def test_illegal_llm_words_are_removed_and_vector_candidates_fill_gaps(self):
        candidates = [
            _candidate("winter coat", 0.92),
            _candidate("wool coat", 0.87),
        ]
        result = validate_rerank_recommendations(
            [
                {"word": "invented tag", "reason": "hallucinated"},
                {"word": "WOOL COAT", "reason": "specific material"},
            ],
            candidates,
            max_output=2,
        )
        self.assertEqual([item["word"] for item in result], ["wool coat", "winter coat"])
        self.assertTrue(result[0]["ai_generated"])
        self.assertFalse(result[1]["ai_generated"])

    def test_candidate_provenance_is_preserved(self):
        candidates = [_candidate(
            "winter coat",
            0.92,
            source="amazon_suggest",
            compliance_reason="rule and LLM passed",
            anchor_cn_word="冬季外套",
            trend_score=8.5,
        )]
        items = build_recommend_items(
            [{"word": "winter coat", "reason": "strong match", "ai_generated": True}],
            candidates,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "amazon_suggest")
        self.assertEqual(items[0].compliance_reason, "rule and LLM passed")
        self.assertEqual(items[0].anchor_cn_word, "冬季外套")
        self.assertEqual(items[0].trend_score, 8.5)


class RecommendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_unavailable_falls_back_to_vector_order(self):
        candidates = [
            _candidate("lower", 0.4),
            _candidate("higher", 0.9),
        ]
        with (
            patch("services.recommend.get_country_hot_words", return_value=[]),
            patch(
                "services.recommend.pseudonymize_async",
                new=AsyncMock(return_value=("title", {})),
            ),
            patch(
                "services.recommend._call_llm_with_retry",
                new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
            ),
        ):
            result = await rerank_tags_with_llm(
                candidates,
                product_title="title",
                product_category=None,
                target_country="DE",
                max_output=2,
            )

        self.assertEqual([item["word"] for item in result], ["higher", "lower"])
        self.assertTrue(all(not item["ai_generated"] for item in result))

    async def test_tag_list_passes_search_to_total_count(self):
        with (
            patch("app.list_local_tags", return_value=([], None)),
            patch("app.count_local_tags", return_value=0) as count_mock,
        ):
            response = await get_local_tags(
                country="DE",
                category="apparel",
                search="coat",
                limit=20,
                cursor=None,
                _auth={"sub": "test"},
            )

        self.assertEqual(response.total_count, 0)
        count_mock.assert_called_once_with(
            country="DE", category="apparel", search="coat"
        )


if __name__ == "__main__":
    unittest.main()
