"""DSAR 主体权利工具（EU 合规改造新增，2026-08）。

GDPR Art.15（访问权）/ Art.17（删除权，30 天内）/ Art.21（反对权）：
- create_request：公开受理端点写工单（ticket_id 返回给请求人）
- search_subject_data：跨 Qdrant 4 集合 + llm_trace + audit 检索词条相关数据
- execute_erasure：Qdrant 硬删除 + llm_trace 删除 + audit 脱敏替换（保 hash-chain）+ lineage ERASE 事件 + 证据写 erasure_proof

注意：本项目词条数据为公开来源聚合词，不含直接个人标识符；
DSAR 以"词条"为检索单位（word_sha256 / 精确词匹配）。
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from services.db import execute, fetchall, fetchone
from services.lineage import record_event, EVENT_ERASE
from models.schemas import DSAR_REQUEST_TYPES, DSAR_NOTE_MAX_LENGTH

logger = logging.getLogger(__name__)

VALID_TYPES = DSAR_REQUEST_TYPES   # 唯一权威源在 models.schemas（披露/限流/模型共用）
VALID_STATUS = ("received", "in_progress", "completed", "rejected")


def create_request(request_type: str, contact: str, subject_note: str = "",
                   created_by: str = "public") -> dict:
    """创建 DSAR 工单。返回 {ticket_id, status, message}。"""
    if request_type not in VALID_TYPES:
        raise ValueError(f"request_type 须为 {', '.join(VALID_TYPES)}")
    if not contact or not contact.strip():
        raise ValueError("contact 必填（用于 30 天响应时限内联系请求人）")
    ticket_id = str(uuid.uuid4())
    execute(
        "INSERT INTO dsar_request (ticket_id, request_type, contact, subject_note, created_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (ticket_id, request_type, contact.strip(), subject_note.strip()[:DSAR_NOTE_MAX_LENGTH], created_by),
    )
    logger.info(f"[dsar] 新请求: type={request_type}, ticket={ticket_id[:8]}…, by={created_by}")
    return {
        "ticket_id": ticket_id,
        "status": "received",
        "message": "请求已受理，将在 30 天内（GDPR Art.12(3)）通过您提供的联系方式回复",
    }


def list_requests(status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = fetchone(f"SELECT count(*) FROM dsar_request {where_sql}", tuple(params))[0]
    rows = fetchall(
        f"SELECT ticket_id, ts, request_type, status, contact, subject_note, "
        f"findings, erasure_proof, completed_at, created_by "
        f"FROM dsar_request {where_sql} ORDER BY ts DESC LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    )
    items = [
        {
            "ticket_id": r[0],
            "ts": r[1].isoformat(),
            "request_type": r[2],
            "status": r[3],
            "contact": r[4],
            "subject_note": r[5],
            "findings": r[6],
            "erasure_proof": r[7],
            "completed_at": r[8].isoformat() if r[8] else None,
            "created_by": r[9],
        }
        for r in rows
    ]
    return items, total


def get_request(ticket_id: str) -> dict | None:
    row = fetchone(
        "SELECT ticket_id, ts, request_type, status, contact, subject_note, "
        "findings, erasure_proof, completed_at, created_by "
        "FROM dsar_request WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not row:
        return None
    return {
        "ticket_id": row[0],
        "ts": row[1].isoformat(),
        "request_type": row[2],
        "status": row[3],
        "contact": row[4],
        "subject_note": row[5],
        "findings": row[6],
        "erasure_proof": row[7],
        "completed_at": row[8].isoformat() if row[8] else None,
        "created_by": row[9],
    }


def update_status(ticket_id: str, status: str, findings: dict | None = None) -> dict | None:
    """更新工单状态/检索结果。返回更新后的工单。"""
    if status not in VALID_STATUS:
        raise ValueError(f"status 须为 {', '.join(VALID_STATUS)}")
    execute(
        "UPDATE dsar_request SET status = %s, "
        "findings = COALESCE(%s, findings), "
        "completed_at = CASE WHEN %s = 'completed' THEN now() ELSE completed_at END "
        "WHERE ticket_id = %s",
        (status, json.dumps(findings, ensure_ascii=False) if findings is not None else None,
         status, ticket_id),
    )
    return get_request(ticket_id)


def search_subject_data(term: str, country: str | None = None) -> dict:
    """跨库检索与词条相关的数据（访问权证据）。term 为词条文本。"""
    # 1. Qdrant 4 集合精确词匹配
    from services.qdrant_store import search_words_exact
    qdrant_hits = search_words_exact(term, country=country)

    # 2. LLM 调用留痕（word_sha256 / prompt_hash）
    from services.llm_trace import find_by_word
    traces = find_by_word(term, limit=100)

    # 3. 审计日志（snapshot/detail 含该词的行）
    term_hash = hashlib.sha256(term.encode("utf-8")).hexdigest()
    audit_rows = fetchall(
        "SELECT id, ts, actor_username, action, resource_type, resource_id "
        "FROM audit_log WHERE resource_snapshot::text ILIKE %s OR detail::text ILIKE %s "
        "ORDER BY ts DESC LIMIT 100",
        (f"%{term}%", f"%{term}%"),
    )
    audit_hits = [
        {"id": r[0], "ts": r[1].isoformat(), "actor_username": r[2], "action": r[3],
         "resource_type": r[4], "resource_id": r[5]}
        for r in audit_rows
    ]

    return {
        "term": term,
        "term_sha256": term_hash,
        "qdrant": qdrant_hits,
        "llm_trace": traces,
        "audit": audit_hits,
    }


def execute_erasure(term: str, country: str | None = None) -> dict:
    """执行删除权（Art.17）：全链路硬删除 + 证据。返回 erasure_proof。"""
    # 1. Qdrant 4 集合硬删除（软删除的向量仍可被相似度召回，必须硬删除）
    from services.qdrant_store import delete_points_by_word
    qdrant_deleted = delete_points_by_word(term, country=country)

    # 2. LLM 留痕删除
    from services.llm_trace import delete_by_word
    trace_deleted = delete_by_word(term)

    # 3. 审计脱敏替换（保 hash-chain：hash 不覆盖 snapshot/detail）
    from services.audit import redact_audit_for_erasure
    audit_redacted = redact_audit_for_erasure([term])

    # 4. 血缘 ERASE 事件（擦除本身留痕）
    record_event(
        run_id=str(uuid.uuid4()),
        job_name="dsar_erasure",
        event_type=EVENT_ERASE,
        outputs=[{"term_sha256": hashlib.sha256(term.encode("utf-8")).hexdigest(),
                  "collections": ["cn_anchors", "local_tags", "pending_review", "blocked_decisions"]}],
    )

    proof = {
        "term_sha256": hashlib.sha256(term.encode("utf-8")).hexdigest(),
        "qdrant_points_deleted": qdrant_deleted,
        "llm_trace_rows_deleted": trace_deleted,
        "audit_rows_redacted": audit_redacted,
        "erased_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"[dsar] 擦除完成: {proof}")
    return proof


def complete_erasure_request(ticket_id: str, term: str, country: str | None = None) -> dict:
    """执行擦除并闭环工单（写 erasure_proof + completed）。"""
    proof = execute_erasure(term, country=country)
    execute(
        "UPDATE dsar_request SET status = 'completed', erasure_proof = %s, completed_at = now() "
        "WHERE ticket_id = %s",
        (json.dumps(proof, ensure_ascii=False), ticket_id),
    )
    return proof
