"""数据血缘事件（EU 合规改造新增，2026-08）。

采用 OpenLineage（LF AI 标准）事件形状存 JSONB 自建存储，不引入 Marquez 服务：
{eventType, eventTime, run:{runId}, job:{namespace,name}, inputs:[...], outputs:[...], run:{facets}}。
支撑 GDPR Art.5(2)/DSA 可追溯性举证：采集 → 翻译 → 评估 → 审核 → 入库/拦截 全链路可查。

SQL/JSONB 直接查询，不依赖额外组件。
"""
import logging
from datetime import datetime, timezone

from services.db import execute, fetchall, is_db_available

logger = logging.getLogger(__name__)

EVENT_START = "START"
EVENT_COMPLETE = "COMPLETE"
EVENT_FAIL = "FAIL"
EVENT_ERASE = "ERASE"
EVENT_OUTPUT = "OUTPUT"  # 词级事件：单条词入库/拦截（挂同一 run_id，供溯源检索）


def record_event(*, run_id: str, job_name: str, event_type: str,
                 job_namespace: str = "joytag", inputs: list[dict] | None = None,
                 outputs: list[dict] | None = None, run_facets: dict | None = None) -> int | None:
    """记录一条 OpenLineage 形状的血缘事件。失败仅告警（best-effort）。"""
    if not is_db_available():
        logger.warning(f"[lineage] 数据库不可用，跳过血缘事件: {job_name}/{event_type}")
        return None
    import json
    event = {
        "eventType": event_type,
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "run": {"runId": run_id},
        "job": {"namespace": job_namespace, "name": job_name},
        "inputs": inputs or [],
        "outputs": outputs or [],
    }
    if run_facets:
        event["run"]["facets"] = run_facets
    try:
        from services.db import get_pool
        with get_pool().connection() as conn:
            row = conn.execute(
                """INSERT INTO lineage_event (run_id, job_name, job_namespace, event_type, facets)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (run_id, job_name, job_namespace, event_type, json.dumps(event, ensure_ascii=False)),
            ).fetchone()
        return row[0]
    except Exception as e:
        logger.warning(f"[lineage] 血缘事件写入失败（best-effort）: {e}")
        return None


def list_by_run(run_id: str) -> list[dict]:
    rows = fetchall(
        "SELECT id, ts, run_id, job_name, event_type, facets FROM lineage_event "
        "WHERE run_id = %s ORDER BY ts",
        (run_id,),
    )
    return [
        {"id": r[0], "ts": r[1].isoformat(), "run_id": r[2], "job_name": r[3],
         "event_type": r[4], "facets": r[5]}
        for r in rows
    ]


def purge_expired(days: int) -> int:
    before = fetchall("SELECT count(*) FROM lineage_event WHERE ts < now() - (%s || ' days')::interval", (str(days),))
    count = before[0][0] if before else 0
    execute("DELETE FROM lineage_event WHERE ts < now() - (%s || ' days')::interval", (str(days),))
    return count
