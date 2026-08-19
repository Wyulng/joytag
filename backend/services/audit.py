"""hash-chain 审计日志（EU 合规改造新增，2026-08）。

满足 GDPR Art.5(2) 可责性：所有管理变更记录"谁在何时对什么资源做了什么"。
借鉴 AWS CloudTrail 的不可变审计思路，自实现 SHA-256 哈希链（无需 Trillian 等重型服务）。

设计要点：
- 写入经 audit_chain_head 单行 SELECT ... FOR UPDATE 串行化，链头在事务内更新。
- hash 仅覆盖不可变字段（prev_hash/id/ts/actor_sub/action/resource_type/resource_id），
  resource_snapshot/detail 不参与哈希——因为 DSAR 擦除需要对这些字段做 <REDACTED>
  脱敏替换（保链），可篡改性是"擦除权优先于快照防篡改"的有意取舍。
- verify_chain() 全链重算校验，供 /admin/api/audit/verify 做防篡改证据。
"""
import hashlib
import json
import logging

from services.db import execute, fetchone, fetchall, is_db_available, DBUnavailableError

logger = logging.getLogger(__name__)


def _row_hash(prev_hash: str, row_id: int, ts, actor_sub: str, action: str,
              resource_type: str, resource_id: str) -> str:
    payload = "|".join([
        prev_hash, str(row_id), ts.isoformat(), actor_sub,
        action, resource_type, resource_id,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_event(*, actor_sub: str, actor_username: str, actor_roles: list[str],
                 action: str, resource_type: str, resource_id: str,
                 resource_snapshot: dict | None = None, detail: dict | None = None,
                 request_id: str = "", ip: str = "") -> int:
    """追加一条审计记录并推进哈希链。同步函数，调用方用 asyncio.to_thread 包装。

    审计写入失败会抛 DBUnavailableError——管理变更端点应让其 500（变更不得无记录发生）。
    """
    if not is_db_available():
        raise DBUnavailableError("审计存储不可用，拒绝执行变更")

    from services.db import get_pool
    with get_pool().connection() as conn:
        with conn.transaction():
            head = conn.execute(
                "SELECT head_hash FROM audit_chain_head WHERE id = 1 FOR UPDATE"
            ).fetchone()
            if head is None:
                raise DBUnavailableError("审计链头缺失，schema 未初始化")
            prev_hash = head[0]

            row = conn.execute(
                """INSERT INTO audit_log
                   (actor_sub, actor_username, actor_roles, action, resource_type,
                    resource_id, resource_snapshot, detail, request_id, ip, prev_hash, hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, ts""",
                (
                    actor_sub,
                    actor_username,
                    list(actor_roles),
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(resource_snapshot, ensure_ascii=False) if resource_snapshot is not None else None,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else "{}",
                    request_id,
                    ip,
                    prev_hash,
                    "",  # hash 占位，随后计算回填
                ),
            ).fetchone()
            row_id, ts = row

            row_hash = _row_hash(prev_hash, row_id, ts, actor_sub, action, resource_type, resource_id)
            conn.execute("UPDATE audit_log SET hash = %s WHERE id = %s", (row_hash, row_id))
            conn.execute("UPDATE audit_chain_head SET head_hash = %s WHERE id = 1", (row_hash,))
    return row_id


def verify_chain() -> tuple[bool, str]:
    """全链重算校验。返回 (ok, 失败描述)。"""
    rows = fetchall(
        "SELECT id, ts, actor_sub, action, resource_type, resource_id, prev_hash, hash "
        "FROM audit_log ORDER BY id"
    )
    prev_expected = None
    for r in rows:
        row_id, ts, actor_sub, action, resource_type, resource_id, prev_hash, stored_hash = r
        if prev_expected is not None and prev_hash != prev_expected:
            return False, f"id={row_id}: prev_hash 断裂（期望 {prev_expected[:16]}…，实际 {prev_hash[:16]}…）"
        recomputed = _row_hash(prev_hash, row_id, ts, actor_sub, action, resource_type, resource_id)
        if recomputed != stored_hash:
            return False, f"id={row_id}: 行哈希不匹配"
        prev_expected = stored_hash
    return True, ""


def list_audit(*, limit: int = 50, offset: int = 0, action: str | None = None,
               actor_sub: str | None = None, resource_type: str | None = None) -> tuple[list[dict], int]:
    where, params = [], []
    if action:
        where.append("action = %s")
        params.append(action)
    if actor_sub:
        where.append("actor_sub = %s")
        params.append(actor_sub)
    if resource_type:
        where.append("resource_type = %s")
        params.append(resource_type)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = fetchone(f"SELECT count(*) FROM audit_log {where_sql}", tuple(params))[0]
    rows = fetchall(
        f"SELECT id, ts, actor_sub, actor_username, actor_roles, action, resource_type, "
        f"resource_id, resource_snapshot, detail, request_id, ip "
        f"FROM audit_log {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    )
    items = [
        {
            "id": r[0],
            "ts": r[1].isoformat(),
            "actor_sub": r[2],
            "actor_username": r[3],
            "actor_roles": r[4],
            "action": r[5],
            "resource_type": r[6],
            "resource_id": r[7],
            "resource_snapshot": r[8],
            "detail": r[9],
            "request_id": r[10],
            "ip": r[11],
        }
        for r in rows
    ]
    return items, total


def redact_audit_for_erasure(subject_terms: list[str]) -> int:
    """DSAR 擦除：将匹配词在 snapshot/detail 中替换为 <REDACTED>（保链，不删行）。返回受影响行数。"""
    if not subject_terms:
        return 0
    def redact(value):
        if isinstance(value, str):
            new_value = value
            for term in subject_terms:
                new_value = new_value.replace(term, "<REDACTED>")
            return new_value, new_value != value
        if isinstance(value, dict):
            changed = False
            result = {}
            for key, item in value.items():
                new_item, item_changed = redact(item)
                result[key] = new_item
                changed = changed or item_changed
            return result, changed
        if isinstance(value, list):
            changed = False
            result = []
            for item in value:
                new_item, item_changed = redact(item)
                result.append(new_item)
                changed = changed or item_changed
            return result, changed
        return value, False

    rows = fetchall("SELECT id, resource_snapshot, detail FROM audit_log")
    affected = 0
    for row_id, snapshot, detail in rows:
        new_snapshot, snapshot_changed = redact(snapshot)
        new_detail, detail_changed = redact(detail)
        changed = snapshot_changed or detail_changed
        if changed:
            execute(
                "UPDATE audit_log SET resource_snapshot = %s, detail = %s WHERE id = %s",
                (
                    json.dumps(new_snapshot, ensure_ascii=False),
                    json.dumps(new_detail, ensure_ascii=False),
                    row_id,
                ),
            )
            affected += 1
    return affected
