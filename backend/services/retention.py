"""留存策略（EU 合规改造新增，2026-08）。

GDPR Art.5(1)(e) 存储限制：llm_trace / lineage_event / audit_log 按策略表清理。
策略存 Postgres retention_policy 表（默认种子见 db.py：llm_trace=90、lineage=396、audit=396 天）。
每日系统任务 compliance_retention（03:00 UTC）调用 run_all_purges()，也可手动触发。
"""
import logging

from services.db import execute, fetchall, fetchone

logger = logging.getLogger(__name__)

POLICY_KEYS = ("llm_trace", "lineage", "audit")
MIN_DAYS, MAX_DAYS = 30, 3650


def get_retention_policies() -> dict[str, int]:
    rows = fetchall("SELECT key, days FROM retention_policy")
    return {r[0]: r[1] for r in rows}


def set_retention_policy(key: str, days: int) -> dict:
    """更新某类数据的留存天数。返回更新后的策略。"""
    if key not in POLICY_KEYS:
        raise ValueError(f"不支持的留存键: {key}（可选: {', '.join(POLICY_KEYS)}）")
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise ValueError(f"留存天数须在 {MIN_DAYS}-{MAX_DAYS} 之间")
    execute(
        "INSERT INTO retention_policy (key, days, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET days = EXCLUDED.days, updated_at = now()",
        (key, days),
    )
    return get_retention_policies()


def _purge_table(table: str, days: int) -> int:
    """按 ts 列清理单表（llm_trace 复用其自身 purge 逻辑，此处统一按留存表走）。"""
    if table == "llm_trace":
        from services.llm_trace import purge_expired
        return purge_expired(days)
    if table == "lineage_event":
        from services.lineage import purge_expired
        return purge_expired(days)
    if table == "audit_log":
        before = fetchone("SELECT count(*) FROM audit_log WHERE ts < now() - (%s || ' days')::interval", (str(days),))
        count = before[0] if before else 0
        execute("DELETE FROM audit_log WHERE ts < now() - (%s || ' days')::interval", (str(days),))
        return count
    raise ValueError(f"未知表: {table}")


def run_all_purges() -> dict:
    """执行全部留存清理。返回各键清理行数。失败降级为告警（best-effort，不阻断调度）。"""
    policies = get_retention_policies()
    results = {}
    for key in POLICY_KEYS:
        days = policies.get(key)
        if not days:
            continue
        try:
            results[key] = _purge_table(key, days)
            logger.info(f"[retention] {key} 清理完成: {results[key]} 行（留存 {days} 天）")
        except Exception as e:
            logger.warning(f"[retention] {key} 清理失败（best-effort）: {e}")
            results[key] = -1
    return results
