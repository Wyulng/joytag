import os
import uuid
from uuid import UUID
import hashlib
import logging
from datetime import datetime, timezone
from typing import Iterator
from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, MatchText, PointIdsList, FilterSelector
from dotenv import load_dotenv

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

# 四个集合名称
ANCHOR_COLLECTION = "cn_anchors"          # 中文长尾词底座（不对外输出）
LOCAL_COLLECTION = "local_tags"           # 本地化 Tag（唯一输出源）
PENDING_COLLECTION = "pending_review"     # 待人工审核队列
BLOCKED_COLLECTION = "blocked_decisions"  # 被拦截词（UCPD/GDPR 合规决策留痕，2026-08 新增）

VECTOR_SIZE = 768  # Alibaba-NLP/gte-multilingual-base (768 dimensions)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Alibaba-NLP/gte-multilingual-base")
EMBEDDING_MODEL_REVISION = os.getenv(
    "EMBEDDING_MODEL_REVISION",
    "9bbca17d9273fd0d03d5725c7a4b0f6b45142062",
)
EMBEDDING_NORMALIZED = os.getenv("EMBEDDING_NORMALIZE", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
ANCHOR_MATCH_THRESHOLD = float(os.getenv("ANCHOR_MATCH_THRESHOLD", "0.60"))
ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD = float(
    os.getenv("ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD", "0.75")
)
for threshold_name, threshold_value in (
    ("ANCHOR_MATCH_THRESHOLD", ANCHOR_MATCH_THRESHOLD),
    ("ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD", ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD),
):
    if not 0.0 <= threshold_value <= 1.0:
        raise ValueError(f"{threshold_name} must be between 0.0 and 1.0")

_client = None


def _add_embedding_metadata(payload: dict) -> dict:
    """标记所有 Qdrant 点使用的向量模型，避免不同空间的数据混入。"""
    payload.update({
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": VECTOR_SIZE,
        "embedding_revision": EMBEDDING_MODEL_REVISION,
        "embedding_normalized": EMBEDDING_NORMALIZED,
    })
    return payload

def _generate_deterministic_id(*parts: str) -> str:
    """基于自然键生成确定性 UUID，确保同一词条生成相同 ID"""
    # 使用长度前缀编码避免 ":" 分隔符与词内容冲突
    # 例如 _generate_deterministic_id('a:b', 'c') 和 _generate_deterministic_id('a', 'b:c') 生成不同 ID
    key = "".join(f"{len(p)}:{p}" for p in parts)
    return str(uuid.UUID(bytes=hashlib.md5(key.encode()).digest()[:16]))

def get_qdrant_client():
    global _client
    if _client is None:
        logger.info(f"[qdrant] 初始化客户端: {QDRANT_URL}")
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        # 确保四个集合都存在
        for name, vector_required in [
            (ANCHOR_COLLECTION, True), (LOCAL_COLLECTION, True),
            (PENDING_COLLECTION, False), (BLOCKED_COLLECTION, False),
        ]:
            if not _client.collection_exists(name):
                logger.info(f"[qdrant] 创建集合: {name}")
                if vector_required:
                    _client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
                    )
                else:
                    # pending_review 不需要向量检索，但仍需占位向量（可为零向量）
                    _client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
                    )
            else:
                logger.debug(f"[qdrant] 集合已存在: {name}")
                collection = _client.get_collection(name)
                vector_config = collection.config.params.vectors
                configured_size = getattr(vector_config, "size", None)
                if configured_size is not None and configured_size != VECTOR_SIZE:
                    raise RuntimeError(
                        f"Qdrant collection {name} uses {configured_size} dimensions; "
                        f"the current embedding model requires {VECTOR_SIZE}. "
                        "Back up and recreate qdrant_storage for a cold start."
                    )
    return _client


def _iter_scroll(
    collection_name: str,
    filter_condition: Filter = None,
    payload_keys: list[str] = None,
    batch_size: int = 1000,
) -> Iterator:
    """分页流式读取 Qdrant 记录，每次只保留当前页。"""
    client = get_qdrant_client()
    next_offset = None
    while True:
        records, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=filter_condition,
            limit=batch_size,
            offset=next_offset,
            with_payload=True if payload_keys is None else payload_keys,
            with_vectors=False,
        )
        yield from records
        if next_offset is None:
            break


def _scroll_all(
    collection_name: str,
    filter_condition: Filter = None,
    payload_keys: list[str] = None,
    batch_size: int = 1000,
) -> list:
    """兼容接口：物化完整结果；需要流式处理时使用 _iter_scroll。"""
    return list(_iter_scroll(
        collection_name=collection_name,
        filter_condition=filter_condition,
        payload_keys=payload_keys,
        batch_size=batch_size,
    ))

# ==================== 中文锚点操作 ====================
def upsert_cn_anchor(cn_word: str, vector: list[float], category: str = None,
                     provenance: dict = None, updated_at: str | None = None,
                     trend_score: float = 0.0,
                     trend_score_source: str | None = None) -> str:
    """存储中文锚点词，返回锚点 ID（仅含中文词+向量，不含六国翻译）
    同一 cn_word 多次调用会更新而非重复创建。
    provenance: 溯源元数据（source_type/collection_run_id/collected_at），EU 合规改造新增。
    """
    client = get_qdrant_client()
    point_id = _generate_deterministic_id(cn_word)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "cn_word": cn_word,
        "created_at": now,
        "first_seen_at": (provenance or {}).get("collected_at") or now,
        "updated_at": updated_at or now,
        "trend_score": float(trend_score or 0.0),
        "trend_score_is_absolute": False,
    }
    if trend_score_source:
        payload["trend_score_source"] = trend_score_source
    if category:
        payload["category"] = category
    if provenance:
        payload["provenance"] = provenance
    _add_embedding_metadata(payload)
    client.upsert(
        collection_name=ANCHOR_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=vector,
            payload=payload
        )]
    )
    logger.info(f"[qdrant] 存储中文锚点: {cn_word} (id={point_id})")
    return point_id


def list_cn_anchors(
    limit: int = 20,
    cursor: str = None,
    search: str = None,
    category: str = None
) -> tuple[list, str | None]:
    """分页列出中文锚点，支持按关键词和类目筛选，返回 (列表, next_cursor)"""
    client = get_qdrant_client()
    conditions = []
    if search:
        conditions.append(FieldCondition(key="cn_word", match=MatchText(text=search)))
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    filter_condition = Filter(must=conditions) if conditions else None
    points, next_cursor = client.scroll(
        collection_name=ANCHOR_COLLECTION,
        scroll_filter=filter_condition,
        limit=limit,
        offset=cursor,  # Qdrant API interprets string offset as point_id cursor
        with_payload=True,
        with_vectors=False
    )
    results = [{
        "id": p.id,
        "cn_word": p.payload.get("cn_word"),
        "category": p.payload.get("category"),
        "created_at": p.payload.get("created_at"),
    } for p in points]
    return results, next_cursor


def count_cn_anchors(search: str = None, category: str = None) -> int:
    """返回中文锚点总数，支持按关键词和类目筛选"""
    client = get_qdrant_client()
    conditions = []
    if search:
        conditions.append(FieldCondition(key="cn_word", match=MatchText(text=search)))
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    filter_condition = Filter(must=conditions) if conditions else None
    result = client.count(
        collection_name=ANCHOR_COLLECTION,
        count_filter=filter_condition,
        exact=True
    )
    return result.count

def delete_cn_anchor(anchor_id: str) -> bool:
    """删除指定锚点"""
    client = get_qdrant_client()
    client.delete(collection_name=ANCHOR_COLLECTION, points_selector=PointIdsList(points=[anchor_id]))
    logger.info(f"[qdrant] 删除中文锚点: {anchor_id}")
    return True

def cn_anchor_exists(cn_word: str) -> bool:
    """检查中文锚点是否已存在"""
    client = get_qdrant_client()
    # 使用确定性ID检查（和 upsert_cn_anchor 相同的生成逻辑）
    point_id = _generate_deterministic_id(cn_word)
    points = client.retrieve(
        collection_name=ANCHOR_COLLECTION,
        ids=[point_id],
        with_payload=False,
        with_vectors=False
    )
    return len(points) > 0


# ==================== 本地化 Tag 操作 ====================
def upsert_local_tag(
    word: str,
    vector: list[float],
    country: str,
    compliance_status: str,
    reason: str,
    anchor_cn_id: str = None,
    anchor_cn_word: str = None,
    source: str = "overseas",
    category: str = None,
    trend_score: float = 0.0,
    provenance: dict = None,
    llm_trace_id: int = None,
    rule_ids: list[str] = None,
    assessed_by: str = None
) -> str:
    """存储本地化变体，关联到中文锚点（如有）
    同一 (word, country) 多次调用会更新而非重复创建。
    provenance/llm_trace_id/rule_ids/assessed_by：EU 合规改造新增的溯源与决策留痕字段。
    """
    client = get_qdrant_client()
    point_id = _generate_deterministic_id(word, country)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "word": word,
        "country": country,
        "compliance_status": compliance_status,
        "reason": reason,
        "source": source,
        "trend_score": trend_score,
        "created_at": now,
        "updated_at": now
    }
    if anchor_cn_id:
        payload["anchor_cn_id"] = anchor_cn_id
    if anchor_cn_word:
        payload["anchor_cn_word"] = anchor_cn_word
    if category:
        payload["category"] = category
    if provenance:
        payload["provenance"] = provenance
    if llm_trace_id:
        payload["llm_trace_id"] = llm_trace_id
    if rule_ids:
        payload["rule_ids"] = rule_ids
    if assessed_by:
        payload["assessed_by"] = assessed_by
    _add_embedding_metadata(payload)

    client.upsert(
        collection_name=LOCAL_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=vector,
            payload=payload
        )]
    )
    logger.info(f"[qdrant] 存储本地标签: {word} ({country}, status={compliance_status})")
    return point_id


def list_local_tags(
    country: str = None,
    category: str = None,
    search: str = None,
    limit: int = 20,
    offset: int | str = 0
) -> tuple[list, int | str | None]:
    """
    分页列出本地词库中的标签。
    可按国家、类目筛选，支持关键词搜索。
    返回 (列表, next_offset)，next_offset 为 None 时表示已无更多数据。
    """
    client = get_qdrant_client()
    conditions = []
    if country:
        conditions.append(FieldCondition(key="country", match=MatchValue(value=country)))
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if search:
        conditions.append(FieldCondition(key="word", match=MatchText(text=search)))

    filter_condition = Filter(must=conditions) if conditions else None

    points, next_offset = client.scroll(
        collection_name=LOCAL_COLLECTION,
        scroll_filter=filter_condition,
        limit=limit,
        offset=offset,
        with_payload=True,
        with_vectors=False
    )

    results = []
    for point in points:
        p = point.payload
        results.append({
            "id": point.id,
            "word": p.get("word", ""),
            "country": p.get("country", ""),
            "category": p.get("category"),
            "compliance_status": p.get("compliance_status", ""),
            "reason": p.get("reason", ""),
            "source": p.get("source", ""),
            "trend_score": p.get("trend_score", 0.0),
            "anchor_cn_id": p.get("anchor_cn_id"),
            "anchor_cn_word": p.get("anchor_cn_word"),
            "created_at": p.get("created_at", ""),
        })
    return results, next_offset


def count_local_tags(country: str = None, category: str = None, search: str = None) -> int:
    """获取与列表接口相同筛选条件下的本地词库词条总数。"""
    client = get_qdrant_client()
    conditions = []
    if country:
        conditions.append(FieldCondition(key="country", match=MatchValue(value=country)))
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if search:
        conditions.append(FieldCondition(key="word", match=MatchText(text=search)))

    filter_condition = Filter(must=conditions) if conditions else None

    result = client.count(
        collection_name=LOCAL_COLLECTION,
        count_filter=filter_condition,
        exact=True
    )
    return result.count


def get_dashboard_stats() -> dict:
    """聚合后台概览统计数据"""
    client = get_qdrant_client()

    total_tags = client.count(
        collection_name=LOCAL_COLLECTION, exact=True
    ).count

    total_pending = client.count(
        collection_name=PENDING_COLLECTION, exact=True
    ).count

    total_anchors = client.count(
        collection_name=ANCHOR_COLLECTION, exact=True
    ).count

    # 扫描 local_tags 获取有数据的国家数（只读 country 字段，不读向量）
    distinct_countries = 0
    try:
        countries = set()
        for record in _iter_scroll(
            collection_name=LOCAL_COLLECTION,
            payload_keys=["country"],
        ):
            country = (record.payload or {}).get("country")
            if country:
                countries.add(country)
        distinct_countries = len(countries)
    except Exception as exc:
        logger.warning("[qdrant] 统计国家数失败: %s", exc)

    # 合规率 = 已入库 / (已入库 + 待审核)
    total = total_tags + total_pending
    compliance_rate = round(total_tags / total, 3) if total > 0 else 0.0

    return {
        "total_tags": total_tags,
        "total_pending": total_pending,
        "total_anchors": total_anchors,
        "distinct_countries": distinct_countries,
        "compliance_rate": compliance_rate,
        "total_blocked": count_blocked_decisions(),
    }

def delete_local_tag(tag_id: str) -> bool:
    """从本地词库中删除指定词条"""
    client = get_qdrant_client()
    client.delete(
        collection_name=LOCAL_COLLECTION,
        points_selector=PointIdsList(points=[tag_id])
    )
    logger.info(f"[qdrant] 删除本地标签: {tag_id}")
    return True

def local_tag_exists(word: str, country: str) -> bool:
    """检查本地标签是否已存在（按 word + country 唯一键）"""
    client = get_qdrant_client()
    point_id = _generate_deterministic_id(word, country)
    points = client.retrieve(
        collection_name=LOCAL_COLLECTION,
        ids=[point_id],
        with_payload=False,
        with_vectors=False
    )
    return len(points) > 0


def batch_count_linked_local_tags(anchor_ids: list[str]) -> dict[str, int]:
    """批量统计多个锚点关联的已验证本地标签数量，返回 {anchor_id: count}"""
    if not anchor_ids:
        return {}
    try:
        records = _iter_scroll(
            collection_name=LOCAL_COLLECTION,
            filter_condition=Filter(should=[
                FieldCondition(key="anchor_cn_id", match=MatchValue(value=aid))
                for aid in anchor_ids
            ]),
            payload_keys=["anchor_cn_id"],
        )
    except Exception as e:
        logger.error(f"[qdrant] 批量统计关联标签失败: {e}")
        raise

    counts = {aid: 0 for aid in anchor_ids}
    for r in records:
        aid = (r.payload or {}).get("anchor_cn_id")
        if aid in counts:
            counts[aid] += 1
    return counts

def insert_pending_review(
    word: str,
    country: str,
    assessment_reason: str,
    source: str = "overseas",
    category: str = None,
    cn_original: str = None,
    provenance: dict = None,
    llm_trace_id: int = None,
    rule_ids: list[str] = None,
    assessed_by: str = None
) -> str:
    """
    将存疑词条写入待审核队列。
    pending_review 集合不需要语义检索，因此向量部分存零向量占位。
    同一 (word, country) 多次调用会更新 assessment_reason 和 created_at。
    provenance/llm_trace_id/rule_ids/assessed_by：EU 合规改造新增的溯源与决策留痕字段。
    """
    client = get_qdrant_client()
    point_id = _generate_deterministic_id(word, country)
    # 生成零向量占位
    zero_vector = [0.0] * VECTOR_SIZE
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "word": word,
        "country": country,
        "assessment_reason": assessment_reason,
        "source": source,
        "status": "pending",
        "created_at": now,
        "updated_at": now
    }
    if category:
        payload["category"] = category
    if cn_original:
        payload["cn_original"] = cn_original
    if provenance:
        payload["provenance"] = provenance
    if llm_trace_id:
        payload["llm_trace_id"] = llm_trace_id
    if rule_ids:
        payload["rule_ids"] = rule_ids
    if assessed_by:
        payload["assessed_by"] = assessed_by
    _add_embedding_metadata(payload)

    client.upsert(
        collection_name=PENDING_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=zero_vector,
            payload=payload
        )]
    )
    logger.info(f"[qdrant] 插入待审核: {word} ({country}, source={source})")
    return point_id

def list_pending_reviews(country: str = None, limit: int = 50, offset: int = 0) -> tuple[list, int | None]:
    """
    分页列出待审核词条，可按国家筛选。
    返回 (列表, next_offset)，列表每个元素包含 id 和 payload。
    next_offset 为 None 时表示已无更多数据。
    """
    client = get_qdrant_client()
    filter_condition = None
    if country:
        filter_condition = Filter(
            must=[FieldCondition(key="country", match=MatchValue(value=country))]
        )

    # 使用 scroll API 获取所有点（或配合 limit）
    points, next_offset = client.scroll(
        collection_name=PENDING_COLLECTION,
        scroll_filter=filter_condition,
        limit=limit,
        offset=offset,
        with_payload=True,
        with_vectors=False
    )
    results = []
    for point in points:
        results.append({
            "id": point.id,
            "payload": point.payload
        })
    return results, next_offset

def get_pending_by_id(point_id: str) -> dict:
    """根据 ID 获取单条待审核记录（ID 格式非法视为不存在，404 而非 500）"""
    try:
        _ = UUID(point_id)
    except ValueError:
        return None
    client = get_qdrant_client()
    points = client.retrieve(
        collection_name=PENDING_COLLECTION,
        ids=[point_id],
        with_payload=True,
        with_vectors=False
    )
    if not points:
        return None
    return {"id": points[0].id, "payload": points[0].payload}

def get_pending_review_count(country: str = None) -> int:
    """获取待审核词条总数"""
    client = get_qdrant_client()
    filter_condition = None
    if country:
        filter_condition = Filter(
            must=[FieldCondition(key="country", match=MatchValue(value=country))]
        )
    result = client.count(
        collection_name=PENDING_COLLECTION,
        count_filter=filter_condition,
        exact=True
    )
    return result.count

def delete_pending_review(point_id: str) -> bool:
    """从待审核队列中删除指定记录"""
    client = get_qdrant_client()
    client.delete(
        collection_name=PENDING_COLLECTION,
        points_selector=PointIdsList(points=[point_id])
    )
    logger.info(f"[qdrant] 删除待审核记录: {point_id}")
    return True
def search_cn_anchor_by_word(
    query_text: str,
    vector: list[float],
    score_threshold: float | None = None,
    category: str | None = None,
) -> dict | None:
    """
    通过多语言向量相似度在 cn_anchors 中查找中文锚点。有类目时仅在同类目
    锚点中检索并使用跨语言阈值；无类目时使用更严格阈值，降低短词误配风险。
    返回找到的锚点记录（含 id, cn_word），未找到或相似度低于阈值时返回 None。
    """
    effective_threshold = score_threshold
    if effective_threshold is None:
        effective_threshold = (
            ANCHOR_MATCH_THRESHOLD
            if category
            else ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD
        )
    category_filter = None
    if category:
        category_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    client = get_qdrant_client()
    results = client.search(
        collection_name=ANCHOR_COLLECTION,
        query_vector=vector,
        query_filter=category_filter,
        limit=1,
        with_payload=True,
        with_vectors=False
    )
    if not results:
        logger.debug(f"[qdrant] 未找到中文锚点: {query_text}")
        return None
    best = results[0]
    if best.score < effective_threshold:
        logger.debug(
            f"[qdrant] 中文锚点相似度不足: {query_text} "
            f"(score={best.score:.3f} < {effective_threshold}, category={category})"
        )
        return None
    logger.info(f"[qdrant] 找到中文锚点: {best.payload.get('cn_word')} (score={best.score:.3f})")
    return {
        "id": best.id,
        "cn_word": best.payload.get("cn_word"),
        "score": best.score
    }


# ==================== 拦截决策留痕（EU 合规改造新增，2026-08） ====================
def insert_blocked_decision(
    word: str,
    country: str,
    reason: str,
    source: str = "overseas",
    category: str = None,
    cn_word: str = None,
    rule_id: str = None,
    llm_trace_id: int = None,
    trend_score: float = 0.0,
    provenance: dict = None
) -> str:
    """
    持久化"需拦截"决策（UCPD/GDPR 合规证据：决策可复现、可举证）。
    同一 (word, country) 多次拦截会更新 reason（保留最近决策）。
    """
    client = get_qdrant_client()
    point_id = _generate_deterministic_id(word, country)
    zero_vector = [0.0] * VECTOR_SIZE
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "word": word,
        "country": country,
        "reason": reason,
        "source": source,
        "trend_score": trend_score,
        "created_at": now,
        "updated_at": now
    }
    if category:
        payload["category"] = category
    if cn_word:
        payload["cn_word"] = cn_word
    if rule_id:
        payload["rule_id"] = rule_id
    if llm_trace_id:
        payload["llm_trace_id"] = llm_trace_id
    if provenance:
        payload["provenance"] = provenance
    _add_embedding_metadata(payload)

    client.upsert(
        collection_name=BLOCKED_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=zero_vector,
            payload=payload
        )]
    )
    logger.info(f"[qdrant] 记录拦截决策: {word} ({country}, rule_id={rule_id})")
    return point_id


def list_blocked_decisions(country: str = None, limit: int = 50, offset: int = 0) -> tuple[list, int | None]:
    """分页列出拦截决策记录，可按国家筛选。"""
    client = get_qdrant_client()
    filter_condition = None
    if country:
        filter_condition = Filter(
            must=[FieldCondition(key="country", match=MatchValue(value=country))]
        )
    points, next_offset = client.scroll(
        collection_name=BLOCKED_COLLECTION,
        scroll_filter=filter_condition,
        limit=limit,
        offset=offset,
        with_payload=True,
        with_vectors=False
    )
    results = []
    for point in points:
        results.append({
            "id": point.id,
            "payload": point.payload
        })
    return results, next_offset


def delete_blocked_decision(point_id: str) -> bool:
    """删除指定拦截决策记录。"""
    client = get_qdrant_client()
    client.delete(
        collection_name=BLOCKED_COLLECTION,
        points_selector=PointIdsList(points=[point_id])
    )
    logger.info(f"[qdrant] 删除拦截决策: {point_id}")
    return True


def count_blocked_decisions(country: str = None) -> int:
    """获取拦截决策记录总数。"""
    client = get_qdrant_client()
    filter_condition = None
    if country:
        filter_condition = Filter(
            must=[FieldCondition(key="country", match=MatchValue(value=country))]
        )
    result = client.count(
        collection_name=BLOCKED_COLLECTION,
        count_filter=filter_condition,
        exact=True
    )
    return result.count


# ==================== DSAR 检索/删除辅助（GDPR Art.17 删除权，2026-08） ====================
_ALL_COLLECTIONS = (ANCHOR_COLLECTION, LOCAL_COLLECTION, PENDING_COLLECTION, BLOCKED_COLLECTION)


def search_words_exact(word: str, country: str = None) -> list[dict]:
    """
    DSAR：按词条精确匹配跨 4 个集合检索（硬匹配，非全文搜索）。
    返回 [{"collection", "id", "payload"}]，供访问权导出与删除权定位。
    """
    client = get_qdrant_client()
    results = []
    for collection in _ALL_COLLECTIONS:
        word_field = "cn_word" if collection == ANCHOR_COLLECTION else "word"
        conditions = [FieldCondition(key=word_field, match=MatchValue(value=word))]
        if country and collection != ANCHOR_COLLECTION:
            conditions.append(FieldCondition(key="country", match=MatchValue(value=country)))
        records = _scroll_all(
            collection_name=collection,
            filter_condition=Filter(must=conditions),
        )
        for r in records:
            results.append({"collection": collection, "id": r.id, "payload": r.payload})
    return results


def delete_points_by_word(word: str, country: str = None) -> int:
    """
    DSAR：按词条元数据过滤硬删除（GDPR Art.17：软删除的向量仍可被相似度召回，必须硬删除）。
    跨 4 个集合执行，返回删除总数。
    """
    client = get_qdrant_client()
    deleted = 0
    for collection in _ALL_COLLECTIONS:
        word_field = "cn_word" if collection == ANCHOR_COLLECTION else "word"
        conditions = [FieldCondition(key=word_field, match=MatchValue(value=word))]
        if country and collection != ANCHOR_COLLECTION:
            conditions.append(FieldCondition(key="country", match=MatchValue(value=country)))
        filter_condition = Filter(must=conditions)
        matching_count = sum(
            1 for _ in _iter_scroll(collection, filter_condition, payload_keys=[word_field])
        )
        result = client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=filter_condition)
        )
        # Qdrant returns UpdateResult, not the deleted points. Count the
        # matching records before issuing the hard-delete operation.
        deleted += matching_count
    logger.info(f"[qdrant] DSAR 硬删除: word={word}, country={country}, deleted={deleted}")
    return deleted


def get_point(collection: str, point_id: str) -> dict | None:
    """按 ID 获取任意集合的单条记录（含 payload）。"""
    client = get_qdrant_client()
    points = client.retrieve(
        collection_name=collection,
        ids=[point_id],
        with_payload=True,
        with_vectors=False
    )
    if not points:
        return None
    return {"id": points[0].id, "payload": points[0].payload}


def get_existing_word_decision(word: str, country: str) -> dict | None:
    """按确定性自然键读取已有海外词决策，避免重复向量化和评估。

    决策优先级固定为 blocked → local_tags → pending_review。该查询只做
    三次 point retrieve，不进行向量搜索；管理员删除对应记录即可使缓存失效。
    """
    point_id = _generate_deterministic_id(word, country)
    candidates = (
        (BLOCKED_COLLECTION, "blocked", "需拦截", False),
        (LOCAL_COLLECTION, "approved", "可复用", True),
        (PENDING_COLLECTION, "pending", "存疑", False),
    )
    for collection, action, default_status, stored in candidates:
        point = get_point(collection, point_id)
        if point is None:
            continue
        payload = point.get("payload") or {}
        return {
            "collection": collection,
            "id": point.get("id", point_id),
            "payload": payload,
            "action": action,
            "status": payload.get("compliance_status", default_status),
            "stored": stored,
        }
    return None
