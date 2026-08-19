from typing import List, Dict, Any, Optional
import asyncio
import json
import logging
import httpx
from fastapi import HTTPException
from services.embedding import get_embedding

logger = logging.getLogger(__name__)
from services.qdrant_store import get_qdrant_client, LOCAL_COLLECTION
from services.collectors.amazon_suggest import get_country_hot_words
from services.llm import _strip_json_fence, _call_llm_with_retry
from services.pii_guard import pseudonymize_async
from services.logging_config import log_safe_hash
from qdrant_client.models import Filter, FieldCondition, MatchValue

# 推荐调参常量（唯一权威源）：transparency 正文与 app.py 披露共用，改此处即可三处同步
TOP_K_RECALL = 16   # 向量召回候选数（top-16）
RERANK_DEPTH = 8    # LLM 精排输入深度（top-8）
MAX_OUTPUT_DEFAULT = 5  # 精排输出默认上限（实际以请求 top_k 为准）

RERANK_SYSTEM_PROMPT = """你是一个欧洲电商本土化运营专家。你的任务是根据商品信息，从候选标签列表中选出最合适的标签，并按推荐优先级排序，同时为每个标签生成简短的推荐理由（面向内部运营人员，语言为中文）。

评估标准：
1. 语义匹配度：标签与商品描述、类目的关联程度。
2. 文化合规性：标签在目标国家无负面联想或法律风险。
3. 趋势热度：优先选择当前电商搜索热词（Amazon 建议词快照，如有标注；2026-08 起替代原社交平台趋势）。
4. 避免重复含义的标签。

请严格按以下JSON格式输出，不要添加任何额外说明：
{
  "recommendations": [
    {"word": "标签词", "reason": "推荐理由"},
    ...
  ]
}
"""


async def retrieve_candidate_tags(
    query_text: str,
    target_country: str,
    category: Optional[str] = None,
    top_k: int = TOP_K_RECALL
) -> List[Dict[str, Any]]:
    # GDPR 最小化：不落完整查询文本，只记长度 + 哈希前缀（log_safe_hash 共享约定）
    logger.info(
        f"[recommend] 检索候选标签: query_len={len(query_text)}, "
        f"query_hash={log_safe_hash(query_text)}, "
        f"country={target_country}, top_k={top_k}"
    )
    query_vector = await get_embedding(query_text)

    filter_condition = Filter(
        must=[
            FieldCondition(key="country", match=MatchValue(value=target_country)),
            FieldCondition(key="compliance_status", match=MatchValue(value="可复用"))
        ]
    )

    client = get_qdrant_client()
    search_result = await asyncio.to_thread(
        client.search,
        collection_name=LOCAL_COLLECTION,
        query_vector=query_vector,
        query_filter=filter_condition,
        limit=top_k,
        with_payload=True,
        with_vectors=False
    )

    candidates = []
    for point in search_result:
        payload = point.payload
        candidates.append({
            "id": point.id,
            "word": payload.get("word"),
            "country": payload.get("country"),
            "category": payload.get("category"),
            "trend_score": payload.get("trend_score", 0.0),
            "reason": payload.get("reason"),
            "compliance_reason": payload.get("reason"),
            "source": payload.get("source"),
            "anchor_cn_word": payload.get("anchor_cn_word"),
            "similarity": point.score
        })
    logger.info(f"[recommend] 检索到 {len(candidates)} 个候选标签")
    return candidates


def filter_candidates_by_category(
    candidates: List[Dict[str, Any]],
    category: Optional[str]
) -> List[Dict[str, Any]]:
    if not category:
        return candidates
    filtered = []
    for c in candidates:
        tag_cat = c.get("category")
        if tag_cat is None or tag_cat == category:
            filtered.append(c)
    return filtered


async def rerank_tags_with_llm(
    candidates: List[Dict[str, Any]],
    product_title: str,
    product_category: Optional[str],
    target_country: str,
    max_output: int = 5
) -> List[Dict[str, str]]:
    if not candidates:
        logger.info("[recommend] 无候选标签，跳过 LLM 精排")
        return []

    candidates_sorted = sorted(candidates, key=lambda x: x.get("similarity", 0.0), reverse=True)
    top_candidates = candidates_sorted[:RERANK_DEPTH]

    candidate_descriptions = []
    for c in top_candidates:
        desc = f"- {c['word']}"
        if c.get("category"):
            desc += f" (类目: {c['category']})"
        if c.get("trend_score"):
            desc += f" (热度: {c['trend_score']})"
        desc += f" (相似度: {c.get('similarity', 0.0):.4f})"
        candidate_descriptions.append(desc)

    candidates_text = "\n".join(candidate_descriptions)

    # 注入实时电商热销上下文（Amazon 建议词，10 分钟 TTL 缓存；失败返回 [] 安全跳过）
    trend_context = ""
    trending = await asyncio.to_thread(get_country_hot_words, target_country, 10)
    if trending:
        top_trends = [t["query"] for t in trending[:10]]
        trend_context = f"\n当前{target_country}电商热销词：{', '.join(top_trends)}"

    # 发送前假名化（最小化 + 出境风险控制）：LLM 只看到 <EMAIL_ADDRESS_0> 等 token
    clean_title, pii_map = await pseudonymize_async(product_title)

    user_prompt = f"""商品信息：
- 标题：{clean_title}
- 类目：{product_category or '未指定'}
- 目标国家：{target_country}{trend_context}

候选标签列表（附带元数据）：
{candidates_text}

请从以上候选中选出最合适的 {max_output} 个标签，排序并给出理由。"""

    messages = [
        {"role": "system", "content": RERANK_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt}
    ]

    logger.info(f"[recommend] 请求 LLM 精排: {len(top_candidates)} 个候选")
    try:
        llm_result = await _call_llm_with_retry(
            messages, temperature=0.2, max_retries=2,
            call_type="rerank", prompt_pii=pii_map
        )
    except httpx.TimeoutException:
        logger.error("[rerank] LLM 请求超时")
        raise HTTPException(status_code=504, detail="LLM 服务超时，请稍后重试")
    except httpx.HTTPStatusError as e:
        logger.error(f"[rerank] LLM 返回非 2xx: {e.response.status_code}")
        raise HTTPException(status_code=502, detail="LLM 服务异常，请稍后重试")
    except (httpx.HTTPError, KeyError) as e:
        logger.error(f"[rerank] LLM 请求失败: {e}")
        raise HTTPException(status_code=502, detail="LLM 服务异常，请稍后重试")

    clean = _strip_json_fence(llm_result.content)

    try:
        result = json.loads(clean)
        raw_recommendations = result.get("recommendations", []) if isinstance(result, dict) else []
        candidate_words = {c["word"] for c in top_candidates}
        selected = []
        selected_words = set()
        for rec in raw_recommendations:
            if not isinstance(rec, dict):
                continue
            word = rec.get("word")
            if word not in candidate_words or word in selected_words:
                continue
            reason = rec.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = "基于候选标签与商品信息匹配"
            selected.append({"word": word, "reason": reason.strip(), "ai_generated": True})
            selected_words.add(word)
            if len(selected) >= max_output:
                break
        # Fill short or malformed model output only with already-vetted
        # candidates. A model must never introduce a new tag into the API.
        for c in candidates_sorted:
            if len(selected) >= max_output:
                break
            if c["word"] in selected_words:
                continue
            selected.append({"word": c["word"], "reason": "基于向量相似度推荐", "ai_generated": False})
            selected_words.add(c["word"])
        logger.info(f"[recommend] LLM 精排成功: 返回 {len(selected)} 个推荐")
        return selected
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.warning(f"[recommend] LLM 精排响应解析失败，回退到向量相似度排序")
        fallback = []
        for c in candidates_sorted[:max_output]:
            fallback.append({"word": c["word"], "reason": "基于向量相似度推荐", "ai_generated": False})
        return fallback
