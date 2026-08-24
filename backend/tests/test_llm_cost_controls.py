import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

import app as app_module
from models.schemas import (
    DISCLOSURE_VERSION,
    TRANSPARENCY_VERSION,
    RecommendRequest,
)
from services import llm, rule_manager
from services.llm_provider import LLMProviderError, LLMResult
from services.recommend import (
    RERANK_MAX_TOKENS,
    get_recommend_rerank_mode,
    rank_tags_by_vector,
    rerank_tags_with_llm,
)


def _llm_result(content: str = "{}") -> LLMResult:
    return LLMResult(
        content=content,
        model="test-model",
        provider="test-provider",
        usage={},
        latency_ms=1,
    )


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/tag/recommend",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "app": app_module.app,
    })


class VectorRecommendationTests(unittest.IsolatedAsyncioTestCase):
    def test_vector_rank_is_deterministic_deduplicated_and_non_ai(self):
        candidates = [
            {"word": "lower", "similarity": 0.4},
            {"word": "Higher", "similarity": 0.9},
            {"word": " higher ", "similarity": 0.8},
        ]
        with (
            patch("services.recommend.get_country_hot_words") as trends,
            patch("services.recommend.pseudonymize_async") as pseudonymize,
            patch("services.recommend._call_llm_with_retry") as provider_call,
        ):
            result = rank_tags_by_vector(candidates, max_output=2)

        self.assertEqual([item["word"] for item in result], ["Higher", "lower"])
        self.assertTrue(all(not item["ai_generated"] for item in result))
        self.assertTrue(all(
            item["reason"] == "基于多语言向量相似度排序推荐"
            for item in result
        ))
        trends.assert_not_called()
        pseudonymize.assert_not_called()
        provider_call.assert_not_called()

    def test_invalid_rerank_mode_falls_back_to_vector(self):
        with (
            patch.dict(os.environ, {"RECOMMEND_RERANK_MODE": "unexpected"}),
            self.assertLogs("services.recommend", level="WARNING"),
        ):
            self.assertEqual(get_recommend_rerank_mode(), "vector")

    async def test_default_route_uses_vector_ranker_without_llm(self):
        candidates = [
            {"word": "winter coat", "similarity": 0.91, "source": "amazon"},
            {"word": "wool coat", "similarity": 0.81, "source": "ebay"},
        ]
        vector_rank = MagicMock(
            return_value=rank_tags_by_vector(candidates, max_output=2)
        )
        llm_rank = AsyncMock()
        with (
            patch(
                "app.retrieve_candidate_tags",
                new=AsyncMock(return_value=candidates),
            ),
            patch("app.get_recommend_rerank_mode", return_value="vector"),
            patch("app.rank_tags_by_vector", new=vector_rank),
            patch("app.rerank_tags_with_llm", new=llm_rank),
        ):
            response = await app_module.recommend_tags.__wrapped__(
                _request(),
                RecommendRequest(
                    title="winter coat", target_country="DE", top_k=2
                ),
                _scope={"scope": "joytag:recommend"},
            )

        llm_rank.assert_not_awaited()
        vector_rank.assert_called_once_with(candidates, 2)
        self.assertFalse(response.ai_assisted)
        self.assertTrue(all(not item.ai_generated for item in response.recommendations))

    async def test_llm_mode_keeps_optional_rerank_path(self):
        candidates = [{"word": "winter coat", "similarity": 0.91}]
        llm_rank = AsyncMock(return_value=[{
            "word": "winter coat",
            "reason": "LLM selected",
            "ai_generated": True,
        }])
        with (
            patch(
                "app.retrieve_candidate_tags",
                new=AsyncMock(return_value=candidates),
            ),
            patch("app.get_recommend_rerank_mode", return_value="llm"),
            patch("app.rank_tags_by_vector") as vector_rank,
            patch("app.rerank_tags_with_llm", new=llm_rank),
        ):
            response = await app_module.recommend_tags.__wrapped__(
                _request(),
                RecommendRequest(title="winter coat", target_country="DE"),
                _scope={"scope": "joytag:recommend"},
            )

        vector_rank.assert_not_called()
        llm_rank.assert_awaited_once()
        self.assertTrue(response.ai_assisted)


class LLMRetryAndLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_timeout_retries_only_once(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock(side_effect=[
            httpx.ReadTimeout("temporary timeout"),
            _llm_result("ok"),
        ])
        with (
            patch("services.llm.get_llm_provider", return_value=provider),
            patch("services.llm.asyncio.sleep", new=AsyncMock()) as sleep,
            patch("services.llm._record_trace") as record_trace,
        ):
            result = await llm._call_llm_with_retry(
                [{"role": "user", "content": "test"}],
                temperature=0,
                max_tokens=17,
                call_type="rerank",
            )

        self.assertEqual(provider.chat_completion.await_count, 2)
        sleep.assert_awaited_once_with(1)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(
            provider.chat_completion.await_args_list[1].kwargs["max_tokens"], 17
        )
        self.assertEqual(record_trace.call_args.kwargs["retry_count"], 1)

    async def test_non_retryable_provider_error_is_attempted_once(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock(
            side_effect=LLMProviderError("invalid configuration")
        )
        with (
            patch("services.llm.get_llm_provider", return_value=provider),
            patch("services.llm.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(LLMProviderError):
                await llm._call_llm_with_retry(
                    [{"role": "user", "content": "test"}], temperature=0
                )

        self.assertEqual(provider.chat_completion.await_count, 1)
        sleep.assert_not_awaited()

    async def test_http_429_and_5xx_retry_at_most_once(self):
        request = httpx.Request("POST", "https://example.test")
        for status in (429, 503):
            response = httpx.Response(status, request=request)
            error = httpx.HTTPStatusError(
                "status", request=request, response=response
            )
            provider = MagicMock()
            provider.chat_completion = AsyncMock(
                side_effect=[error, _llm_result("ok")]
            )
            with (
                self.subTest(status=status),
                patch("services.llm.get_llm_provider", return_value=provider),
                patch("services.llm.asyncio.sleep", new=AsyncMock()),
            ):
                result = await llm._call_llm_with_retry(
                    [{"role": "user", "content": "test"}], temperature=0
                )

            self.assertEqual(provider.chat_completion.await_count, 2)
            self.assertEqual(result.retry_count, 1)

    async def test_http_400_is_not_retried(self):
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(400, request=request)
        error = httpx.HTTPStatusError("status", request=request, response=response)
        provider = MagicMock()
        provider.chat_completion = AsyncMock(side_effect=error)
        with (
            patch("services.llm.get_llm_provider", return_value=provider),
            patch("services.llm.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                await llm._call_llm_with_retry(
                    [{"role": "user", "content": "test"}], temperature=0
                )

        self.assertEqual(provider.chat_completion.await_count, 1)
        sleep.assert_not_awaited()

    async def test_assessment_json_parse_failure_is_not_retried(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock(return_value=_llm_result("not json"))
        with (
            patch("services.llm.get_llm_provider", return_value=provider),
            patch(
                "services.llm.check_word_against_rules",
                return_value=(None, "", None),
            ),
            patch(
                "services.llm.pseudonymize_async",
                new=AsyncMock(return_value=("winter coat", {})),
            ),
            patch("services.llm._record_trace", return_value=1),
        ):
            assessment, _, _, _ = await llm.assess_single("winter coat", "DE")

        self.assertEqual(assessment, "存疑")
        self.assertEqual(provider.chat_completion.await_count, 1)

    def test_only_408_429_and_5xx_http_errors_are_retryable(self):
        request = httpx.Request("POST", "https://example.test")
        for status, expected in (
            (400, False),
            (401, False),
            (408, True),
            (429, True),
            (500, True),
            (600, False),
        ):
            response = httpx.Response(status, request=request)
            error = httpx.HTTPStatusError(
                "status", request=request, response=response
            )
            with self.subTest(status=status):
                self.assertEqual(llm._is_retryable_llm_error(error), expected)

    async def test_assess_and_translate_pass_fixed_token_limits(self):
        assess_call = AsyncMock(return_value=_llm_result(
            '{"result":"可复用","reason":"ok"}'
        ))
        with (
            patch("services.llm.check_word_against_rules", return_value=(None, "", None)),
            patch(
                "services.llm.pseudonymize_async",
                new=AsyncMock(return_value=("winter coat", {})),
            ),
            patch("services.llm._call_llm_with_retry", new=assess_call),
            patch("services.llm._record_trace", return_value=1),
        ):
            await llm.assess_single("winter coat", "DE")

        self.assertEqual(
            assess_call.await_args.kwargs["max_tokens"], llm.ASSESS_MAX_TOKENS
        )
        self.assertEqual(
            assess_call.await_args.kwargs["max_retries"], llm.LLM_MAX_RETRIES
        )
        self.assertEqual(assess_call.await_args.kwargs["call_type"], "assess")
        self.assertFalse(assess_call.await_args.kwargs["trace_success"])

        translate_call = AsyncMock(return_value=_llm_result('{"冬季外套":"winter coat"}'))
        with (
            patch(
                "services.llm.pseudonymize_async",
                new=AsyncMock(return_value=("冬季外套", {})),
            ),
            patch("services.llm._call_llm_with_retry", new=translate_call),
        ):
            await llm.translate_chinese_to_foreign_batch(["冬季外套"], "英语")

        self.assertEqual(
            translate_call.await_args.kwargs["max_tokens"],
            llm.TRANSLATE_MAX_TOKENS,
        )
        self.assertEqual(
            translate_call.await_args.kwargs["max_retries"], llm.LLM_MAX_RETRIES
        )

    async def test_optional_rerank_passes_fixed_token_limit(self):
        provider_call = AsyncMock(return_value=_llm_result(
            '{"recommendations":[{"word":"winter coat","reason":"ok"}]}'
        ))
        with (
            patch("services.recommend.get_country_hot_words", return_value=[]),
            patch(
                "services.recommend.pseudonymize_async",
                new=AsyncMock(return_value=("winter coat", {})),
            ),
            patch(
                "services.recommend._call_llm_with_retry",
                new=provider_call,
            ),
        ):
            await rerank_tags_with_llm(
                [{"word": "winter coat", "similarity": 0.9}],
                product_title="winter coat",
                product_category=None,
                target_country="DE",
            )

        self.assertEqual(provider_call.await_args.kwargs["max_tokens"], RERANK_MAX_TOKENS)
        self.assertEqual(
            provider_call.await_args.kwargs["max_retries"], llm.LLM_MAX_RETRIES
        )


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    def _dependency_patches(self, provider):
        qdrant = MagicMock()
        qdrant.get_collections.return_value = []
        return (
            patch("services.qdrant_store.get_qdrant_client", return_value=qdrant),
            patch("services.db.is_db_available", return_value=True),
            patch("services.embedding._get_model", return_value=object()),
            patch("services.llm_provider.get_llm_provider", return_value=provider),
        )

    async def test_plain_health_does_not_construct_or_call_provider(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock()
        patches = self._dependency_patches(provider)
        with patches[0], patches[1], patches[2], patches[3] as provider_factory:
            response = await app_module.health(
                deep=False, llm_probe=False, health_probe_token=None
            )

        self.assertEqual(response.status_code, 200)
        provider_factory.assert_not_called()
        provider.chat_completion.assert_not_awaited()

    async def test_deep_health_only_checks_provider_configuration(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock()
        patches = self._dependency_patches(provider)
        with patches[0], patches[1], patches[2], patches[3] as provider_factory:
            response = await app_module.health(
                deep=True, llm_probe=False, health_probe_token=None
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["checks"]["llm"], "configured")
        provider_factory.assert_called_once_with()
        provider.chat_completion.assert_not_awaited()

    async def test_probe_without_configured_secret_is_disabled(self):
        with (
            patch.dict(os.environ, {"HEALTH_PROBE_TOKEN": ""}),
            patch("services.llm_provider.get_llm_provider") as provider_factory,
        ):
            response = await app_module.health(
                deep=True, llm_probe=True, health_probe_token="anything"
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["checks"]["llm"], "probe_disabled")
        provider_factory.assert_not_called()

    async def test_probe_rejects_wrong_secret_before_provider_call(self):
        with (
            patch.dict(os.environ, {"HEALTH_PROBE_TOKEN": "correct"}),
            patch("services.llm_provider.get_llm_provider") as provider_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await app_module.health(
                    deep=True, llm_probe=True, health_probe_token="wrong"
                )
        self.assertEqual(raised.exception.status_code, 403)
        provider_factory.assert_not_called()

    async def test_probe_rejects_missing_request_secret_before_provider_call(self):
        with (
            patch.dict(os.environ, {"HEALTH_PROBE_TOKEN": "correct"}),
            patch("services.llm_provider.get_llm_provider") as provider_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await app_module.health(
                    deep=False, llm_probe=True, health_probe_token=None
                )
        self.assertEqual(raised.exception.status_code, 403)
        provider_factory.assert_not_called()

    async def test_probe_with_correct_secret_calls_provider_once(self):
        provider = MagicMock()
        provider.chat_completion = AsyncMock(return_value=_llm_result("ok"))
        patches = self._dependency_patches(provider)
        with (
            patch.dict(os.environ, {"HEALTH_PROBE_TOKEN": "correct"}),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
        ):
            response = await app_module.health(
                deep=False,
                llm_probe=True,
                health_probe_token="correct",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["checks"]["llm"], "ok")
        provider.chat_completion.assert_awaited_once()


class RuleNormalizationTests(unittest.TestCase):
    def test_conservative_normalization_handles_width_case_and_spaces(self):
        self.assertEqual(
            rule_manager._normalize_rule_key("  ＥＣＯ\t Friendly  "),
            "eco friendly",
        )
        self.assertNotEqual(
            rule_manager._normalize_rule_key("C++"),
            rule_manager._normalize_rule_key("C"),
        )
        self.assertNotEqual(
            rule_manager._normalize_rule_key("café"),
            rule_manager._normalize_rule_key("cafe"),
        )

    def test_safe_rule_match_uses_normalized_comparison(self):
        with (
            patch("services.rule_manager.get_safe_words", return_value=["Wool   Coat"]),
            patch("services.rule_manager.get_banned_entries", return_value=[]),
        ):
            result, reason, _ = rule_manager.check_word_against_rules(
                "  ｗＯＯＬ coat ", "DE"
            )
        self.assertTrue(result)
        self.assertEqual(reason, "通过安全词库")

    def test_banned_rule_match_uses_normalized_comparison(self):
        with (
            patch("services.rule_manager.get_safe_words", return_value=[]),
            patch(
                "services.rule_manager.get_banned_entries",
                return_value=[{"word": "Eco   Friendly", "rule_id": "manual-1"}],
            ),
            patch("services.rule_manager._match_ucpd", return_value=None),
        ):
            result, _, rule_id = rule_manager.check_word_against_rules(
                "  ＥＣＯ friendly shoes ", "DE"
            )
        self.assertFalse(result)
        self.assertEqual(rule_id, "manual-1")

    def test_no_anchor_mode_prioritizes_banned_rule_over_safe_rule(self):
        with (
            patch(
                "services.rule_manager.get_safe_words",
                return_value=["eco-friendly"],
            ),
            patch("services.rule_manager.get_banned_entries", return_value=[]),
            patch(
                "services.rule_manager._match_ucpd",
                return_value=("ucpd_env_generic", "eco-friendly"),
            ),
        ):
            default_result, _, _ = rule_manager.check_word_against_rules(
                "eco-friendly", "DE"
            )
            gated_result, _, rule_id = rule_manager.check_word_against_rules(
                "eco-friendly", "DE", banned_first=True
            )

        self.assertTrue(default_result)
        self.assertFalse(gated_result)
        self.assertEqual(rule_id, "ucpd_env_generic")

    def test_rule_add_and_remove_use_normalized_keys_without_rewriting_original(self):
        existing = {"safe": [{"word": "Wool   Coat"}]}
        with (
            patch("services.rule_manager._read_json", return_value=existing),
            patch("services.rule_manager._write_json") as write,
        ):
            result = rule_manager.add_safe_word("DE", "  ｗＯＯＬ coat ")
        self.assertEqual(result["action"], "exists")
        write.assert_not_called()

        with (
            patch("services.rule_manager._read_json", return_value=existing),
            patch("services.rule_manager._write_json") as write,
        ):
            result = rule_manager.remove_safe_word("DE", "  ｗＯＯＬ coat ")
        self.assertEqual(result["action"], "removed")
        self.assertEqual(write.call_args.args[1]["safe"], [])

        existing_banned = {"banned": [{"word": "C++   Course"}]}
        with (
            patch("services.rule_manager._read_json", return_value=existing_banned),
            patch("services.rule_manager._write_json") as write,
        ):
            result = rule_manager.add_banned_word("DE", "  c++ course ")
        self.assertEqual(result["action"], "exists")
        write.assert_not_called()

        with (
            patch("services.rule_manager._read_json", return_value=existing_banned),
            patch("services.rule_manager._write_json") as write,
        ):
            result = rule_manager.remove_banned_word("DE", "  c++ course ")
        self.assertEqual(result["action"], "removed")
        self.assertEqual(write.call_args.args[1]["banned"], [])


class DisclosureVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disclosure_versions_and_dates_are_synchronized(self):
        self.assertEqual(DISCLOSURE_VERSION, "2026-08-24")
        self.assertEqual(TRANSPARENCY_VERSION, "2026-08-24")

        with patch("app.get_recommend_rerank_mode", return_value="vector"):
            disclosure = await app_module.disclosure_parameters()
        self.assertEqual(disclosure.version, "2026-08-24")
        self.assertEqual(disclosure.last_updated, "2026-08-24")

        with patch(
            "services.transparency.get_recommend_rerank_mode",
            return_value="vector",
        ):
            from services.transparency import transparency_payload

            payload = transparency_payload()
        self.assertEqual(payload["version"], "2026-08-24")
        self.assertEqual(payload["last_updated"], "2026-08-24")


if __name__ == "__main__":
    unittest.main()
