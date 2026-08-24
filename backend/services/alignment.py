import logging
from datetime import datetime, timezone
from services.llm import assess_single
from services.embedding import get_embedding
from services.rule_manager import check_word_against_rules
from services.qdrant_store import (
    upsert_cn_anchor,
    upsert_local_tag,
    insert_pending_review,
    insert_blocked_decision,
    search_cn_anchor_by_word
)
from services.lineage import record_event, EVENT_OUTPUT

logger = logging.getLogger(__name__)


def _record_word_lineage(run_id: str, job_name: str, collection: str, word: str, country: str | None,
                         action: str, detail: dict | None = None):
    """词级血缘事件（best-effort）：单条词的入库/拦截决策，挂采集 run_id 供溯源检索。"""
    facets = {"action": action}
    if country:
        facets["country"] = country
    if detail:
        facets["detail"] = detail
    record_event(
        run_id=run_id,
        job_name=job_name,
        event_type=EVENT_OUTPUT,
        outputs=[{"namespace": "joytag.qdrant", "name": f"{collection}.{word}", "facets": facets}],
    )


# ==================== 处理中文长尾词（底座建设） ====================
async def process_cn_longtail_word(cn_word: str, category: str = None,
                                   provenance: dict = None, collection_run_id: str = None):
    """
    处理中文长尾词（纯概念底座，不做翻译）：
    1. 生成中文原文向量
    2. 存入 cn_anchors 集合（仅含中文词+向量，不含六国翻译）
    provenance: 溯源元数据（source_type/collection_run_id/collected_at），EU 合规改造新增。
    """
    logger.info(f"[alignment] 处理中文长尾词: {cn_word}")
    # 生成中文原文向量
    vector = await get_embedding(cn_word)

    # 存入中文锚点库
    anchor_id = upsert_cn_anchor(cn_word, vector, category=category, provenance=provenance)
    logger.info(f"[alignment] 中文锚点入库成功: {cn_word} (id={anchor_id})")
    _record_word_lineage(collection_run_id or "manual", "cn_collection", "cn_anchors",
                         cn_word, None, "inserted")
    # 返回空 assessments（MVP 阶段中文锚点不预翻译）
    return anchor_id, {}


# ==================== 处理海外趋势词（最终输出源） ====================
async def process_overseas_word(word: str, country: str, anchor_cn_id: str = None,
                                category: str = None, trend_score: float = 0.0,
                                source: str = "overseas", collection_run_id: str = None):
    """
    处理海外趋势词（本地化词汇）：
    1. 使用 GTE 多语言向量直接查找对应的中文锚点
    2. 无锚点时仅执行本地规则并进入待审核，不调用 LLM
    3. 有锚点时执行三级漏斗：规则库硬拦截 → LLM软判定
    4. 可复用 → 生成向量 → 存入 local_tags（关联到中文锚点）
    5. 存疑 → 写入 pending_review 队列
    6. 需拦截 → 持久化到 blocked_decisions（UCPD/GDPR 决策留痕，不再丢弃）
    """
    logger.info(f"[alignment] 处理海外词: {word} ({country}, source={source})")
    provenance = {
        "source_type": source,
        "collection_run_id": collection_run_id,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    # 第0步：直接使用多语言向量查找中文锚点，避免每个海外词额外调用翻译 LLM
    query_vector = await get_embedding(word)
    anchor_info = search_cn_anchor_by_word(word, query_vector, category=category)

    resolved_anchor_cn_id = anchor_info["id"] if anchor_info else None
    resolved_cn_word = anchor_info["cn_word"] if anchor_info else None
    if anchor_info:
        provenance["anchor_match_score"] = anchor_info["score"]

    if resolved_anchor_cn_id is None:
        rule_result, rule_reason, rule_id = check_word_against_rules(
            word, country, category=category, banned_first=True
        )
        if rule_result is False:
            logger.info(
                f"[alignment] 无锚点海外词命中本地禁用规则: "
                f"{word} ({country}, rule_id={rule_id})"
            )
            blocked_id = insert_blocked_decision(
                word=word,
                country=country,
                reason=rule_reason,
                source=source,
                category=category,
                rule_id=rule_id,
                trend_score=trend_score,
                provenance=provenance,
            )
            _record_word_lineage(
                collection_run_id or "manual",
                "overseas_collection",
                "blocked_decisions",
                word,
                country,
                "blocked",
                {"rule_id": rule_id, "anchor_match": "none"},
            )
            return {
                "stored": False,
                "action": "blocked",
                "blocked_id": blocked_id,
                "status": "需拦截",
                "reason": rule_reason,
                "rule_id": rule_id,
                "anchor_cn_word": None,
            }

        if rule_result is True:
            pending_reason = f"{rule_reason}；已通过安全规则，但缺少中文锚点，需人工补充锚点"
            assessed_by = "rule"
        else:
            pending_reason = "未找到中文锚点，未调用 LLM；可能未达到匹配阈值，需人工补充锚点并复核"
            assessed_by = "anchor_gate"

        logger.info(
            f"[alignment] 未找到多语言中文锚点，跳过 LLM 并进入待审核: "
            f"{word} ({country})"
        )
        pending_id = insert_pending_review(
            word=word,
            country=country,
            assessment_reason=pending_reason,
            source=source,
            category=category,
            provenance=provenance,
            rule_ids=[rule_id] if rule_id else None,
            assessed_by=assessed_by,
        )
        _record_word_lineage(
            collection_run_id or "manual",
            "overseas_collection",
            "pending_review",
            word,
            country,
            "pending_no_anchor",
            {"query_language": "multilingual", "assessed_by": assessed_by},
        )
        return {
            "stored": False,
            "action": "pending_no_anchor",
            "pending_id": pending_id,
            "status": "存疑",
            "reason": pending_reason,
            "anchor_cn_word": None,
        }

    rule_result, rule_reason, rule_id = check_word_against_rules(
        word, country, category=category
    )
    if rule_result is True:
        status, reason, trace_id = "可复用", rule_reason, None
        assessed_by = "rule"
    elif rule_result is False:
        status, reason, trace_id = "需拦截", rule_reason, None
        assessed_by = "rule"
    else:
        status, reason, rule_id, trace_id = await assess_single(
            word, country, category=category
        )
        assessed_by = "llm"
    logger.info(f"[alignment] 评估结果: {word} ({country}) -> {status}")

    if status == "可复用":
        tag_id = upsert_local_tag(
            word=word,
            vector=query_vector,
            country=country,
            compliance_status=status,
            reason=reason,
            anchor_cn_id=resolved_anchor_cn_id,
            anchor_cn_word=resolved_cn_word,
            source=source,
            category=category,
            trend_score=trend_score,
            provenance=provenance,
            llm_trace_id=trace_id,
            rule_ids=[rule_id] if rule_id else None,
            assessed_by=assessed_by
        )
        logger.info(f"[alignment] 海外词入库成功: {word} ({country}, id={tag_id})")
        _record_word_lineage(collection_run_id or "manual", "overseas_collection",
                             "local_tags", word, country, "approved",
                             {"anchor_cn_word": resolved_cn_word})
        return {
            "stored": True,
            "action": "approved",
            "id": tag_id,
            "status": status,
            "reason": reason,
            "anchor_cn_word": resolved_cn_word
        }

    elif status == "存疑":
        logger.info(f"[alignment] 海外词存疑，写入待审核: {word} ({country})")
        pending_id = insert_pending_review(
            word=word,
            country=country,
            assessment_reason=reason,
            source=source,
            category=category,
            provenance=provenance,
            llm_trace_id=trace_id,
            rule_ids=[rule_id] if rule_id else None,
            assessed_by=assessed_by
        )
        _record_word_lineage(collection_run_id or "manual", "overseas_collection",
                             "pending_review", word, country, "pending",
                             {"anchor_cn_word": resolved_cn_word} if resolved_cn_word else {})
        return {
            "stored": False,
            "action": "pending",
            "pending_id": pending_id,
            "status": status,
            "reason": reason,
            "anchor_cn_word": resolved_cn_word
        }

    else:  # "需拦截" —— 持久化决策留痕（UCPD/GDPR 举证，不再丢弃）
        logger.info(f"[alignment] 海外词被拦截，写入 blocked_decisions: {word} ({country}, rule_id={rule_id})")
        blocked_id = insert_blocked_decision(
            word=word,
            country=country,
            reason=reason,
            source=source,
            category=category,
            cn_word=resolved_cn_word,
            rule_id=rule_id,
            llm_trace_id=trace_id,
            trend_score=trend_score,
            provenance=provenance
        )
        _record_word_lineage(collection_run_id or "manual", "overseas_collection",
                             "blocked_decisions", word, country, "blocked",
                             {"anchor_cn_word": resolved_cn_word, "rule_id": rule_id})
        return {
            "stored": False,
            "action": "blocked",
            "blocked_id": blocked_id,
            "status": status,
            "reason": reason,
            "rule_id": rule_id,
            "anchor_cn_word": resolved_cn_word
        }
