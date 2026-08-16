"""LLM 调用留痕（EU 合规改造新增，2026-08）。

所有 DeepSeek/EU 模型调用（评估/翻译/重排）持久化：假名化后的提示词哈希、
token→实体类型映射（不含原文值）、假名化输出、模型/延迟/重试信息。
支撑：①出境数据的最小化举证 ②DSAR 按词检索 ③留存策略清理。

数据模型参考 Langfuse trace 概念，但为轻量自建（Langfuse 自托管需 ClickHouse+Redis，
对本项目过重）。写入为 best-effort：失败仅告警，不阻断业务调用。
"""
import hashlib
import json
import logging

from services.db import execute, fetchall, is_db_available

logger = logging.getLogger(__name__)


def record_trace(*, call_type: str, provider: str, model: str, request_id: str,
                 prompt_hash: str, prompt_pii: dict, response: str | None = None,
                 result: dict | None = None, latency_ms: int = 0, retry_count: int = 0,
                 error: str | None = None, word: str | None = None) -> int | None:
    """记录一次 LLM 调用。word 参数会计算 word_sha256 供 DSAR 检索。"""
    if not is_db_available():
        logger.warning(f"[llm_trace] 数据库不可用，跳过留痕: {call_type}")
        return None
    try:
        from services.db import get_pool
        with get_pool().connection() as conn:
            row = conn.execute(
                """INSERT INTO llm_trace
                   (call_type, provider, model, request_id, prompt_hash, prompt_pii,
                    word_sha256, response, result, latency_ms, retry_count, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    call_type,
                    provider,
                    model,
                    request_id,
                    prompt_hash,
                    json.dumps(prompt_pii, ensure_ascii=False),
                    hashlib.sha256(word.encode("utf-8")).hexdigest() if word else None,
                    response,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    latency_ms,
                    retry_count,
                    error,
                ),
            ).fetchone()
        return row[0]
    except Exception as e:
        logger.warning(f"[llm_trace] 留痕失败（best-effort）: {e}")
        return None


def find_by_word(word: str, limit: int = 100) -> list[dict]:
    """DSAR：按词检索 LLM 留痕（word_sha256 或 prompt_hash 匹配）。"""
    word_hash = hashlib.sha256(word.encode("utf-8")).hexdigest()
    rows = fetchall(
        "SELECT id, ts, call_type, provider, model, request_id, prompt_hash, "
        "prompt_pii, word_sha256, response, result, latency_ms, retry_count, error "
        "FROM llm_trace WHERE word_sha256 = %s OR prompt_hash = %s "
        "ORDER BY ts DESC LIMIT %s",
        (word_hash, word_hash, limit),
    )
    return [
        {
            "id": r[0],
            "ts": r[1].isoformat(),
            "call_type": r[2],
            "provider": r[3],
            "model": r[4],
            "request_id": r[5],
            "prompt_hash": r[6],
            "prompt_pii": r[7],
            "word_sha256": r[8],
            "response": r[9],
            "result": r[10],
            "latency_ms": r[11],
            "retry_count": r[12],
            "error": r[13],
        }
        for r in rows
    ]


def delete_by_word(word: str) -> int:
    """DSAR 擦除：按词删除留痕。返回删除行数。"""
    word_hash = hashlib.sha256(word.encode("utf-8")).hexdigest()
    before = len(fetchall(
        "SELECT id FROM llm_trace WHERE word_sha256 = %s", (word_hash,)
    ))
    execute("DELETE FROM llm_trace WHERE word_sha256 = %s", (word_hash,))
    return before


def purge_expired(days: int) -> int:
    """按留存期清理过期的留痕（默认 90 天）。返回删除行数。"""
    before = fetchall("SELECT count(*) FROM llm_trace WHERE ts < now() - (%s || ' days')::interval", (str(days),))
    count = before[0][0] if before else 0
    execute("DELETE FROM llm_trace WHERE ts < now() - (%s || ' days')::interval", (str(days),))
    return count
