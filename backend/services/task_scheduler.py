"""Fixed automatic collection scheduler.

The service intentionally exposes no user-configurable cron jobs. Collection
cadence is a product invariant so operators cannot accidentally create
duplicate network/LLM work through the admin UI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.collectors.cn_longtail import run_cn_collector
from services.collectors.overseas_trends import run_overseas_collector

logger = logging.getLogger(__name__)

SCHEDULER_TIMEZONE_NAME = "Asia/Shanghai"
SCHEDULER_TIMEZONE = ZoneInfo(SCHEDULER_TIMEZONE_NAME)
UTC = timezone.utc

CN_JOB_ID = "auto_cn_collection"
CN_CRON = "0 2 * * *"
OVERSEAS_JOB_ID = "auto_overseas_collection"
OVERSEAS_CRON = "0 4,16 * * *"
RETENTION_JOB_ID = "compliance_retention"
DAILY_REPORT_JOB_ID = "daily_collection_report"
DAILY_REPORT_CRON = "0 9 * * *"

_JOB_OPTIONS = {
    "replace_existing": True,
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 60,
}
_REPORT_JOB_OPTIONS = {
    **_JOB_OPTIONS,
    # A restart around 09:00 may miss the report window; catch up within one hour.
    "misfire_grace_time": 3600,
}

_scheduler: AsyncIOScheduler | None = None
_collection_lock = asyncio.Lock()
_job_state: dict[str, dict[str, Any]] = {
    CN_JOB_ID: {"running": False, "last_status": "never"},
    OVERSEAS_JOB_ID: {"running": False, "last_status": "never"},
}


def _build_trigger(cron: str, timezone_value: ZoneInfo = SCHEDULER_TIMEZONE) -> CronTrigger:
    """Build a fixed five-field cron trigger with an explicit timezone."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron}")
    return CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
        timezone=timezone_value,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_result(result: Any) -> dict[str, Any]:
    """Keep audit details aggregate-only and exclude raw collection data."""
    if not isinstance(result, dict):
        return {}
    allowed = {
        "total", "approved", "pending", "rejected", "duplicates", "new",
        "skipped", "embedding_words", "assess_calls", "source_requests",
        "source_cache_hits", "source_errors", "source_response_changes",
        "seed_queries", "dynamic_seeds", "fixed_seeds", "raw_candidates",
        "unique_candidates",
    }
    return {
        key: value
        for key, value in result.items()
        if key in allowed and isinstance(value, (str, int, float, bool))
    }


async def _record_auto_audit(
    action: str,
    resource_id: str,
    *,
    status: str,
    detail: dict[str, Any] | None = None,
    resource_type: str = "collection",
) -> None:
    """Record scheduler activity without allowing audit failure to kill the loop."""
    try:
        from services.audit import record_event

        await asyncio.to_thread(
            record_event,
            actor_sub="system:scheduler",
            actor_username="scheduler",
            actor_roles=["system"],
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail={"status": status, **(detail or {})},
        )
    except Exception as exc:
        logger.error(
            "[scheduler_audit_failed] action=%s status=%s error_type=%s",
            action,
            status,
            type(exc).__name__,
        )


def _set_job_state(job_id: str, **updates: Any) -> None:
    _job_state.setdefault(job_id, {}).update(updates)


async def _run_collection_job(
    job_id: str,
    task_type: str,
    runner: Callable[[], Awaitable[dict]],
) -> dict:
    """Run one collection only when the shared collection lock is available."""
    action = f"collect.{task_type}.auto"
    resource_id = f"{job_id}:{uuid4()}"
    if _collection_lock.locked():
        _set_job_state(
            job_id,
            running=False,
            last_status="skipped",
            last_finished_at=_now_iso(),
            last_detail={"reason": "collection_lock_busy"},
        )
        logger.info(
            "[scheduler_job_skipped] job_id=%s task_type=%s reason=collection_lock_busy",
            job_id,
            task_type,
        )
        await _record_auto_audit(
            action,
            resource_id,
            status="skipped",
            detail={"reason": "collection_lock_busy"},
        )
        return {"skipped": True, "reason": "collection_lock_busy"}

    async with _collection_lock:
        started_at = _now_iso()
        _set_job_state(
            job_id,
            running=True,
            last_status="running",
            last_started_at=started_at,
        )
        logger.info(
            "[scheduler_job_started] job_id=%s task_type=%s timezone=%s",
            job_id,
            task_type,
            SCHEDULER_TIMEZONE_NAME,
        )
        try:
            result = await runner()
        except Exception as exc:
            _set_job_state(
                job_id,
                running=False,
                last_status="failed",
                last_finished_at=_now_iso(),
                last_detail={"error_type": type(exc).__name__},
            )
            logger.error(
                "[scheduler_job_failed] job_id=%s task_type=%s error_type=%s",
                job_id,
                task_type,
                type(exc).__name__,
            )
            await _record_auto_audit(
                action,
                resource_id,
                status="failed",
                detail={"error_type": type(exc).__name__},
            )
            return {"skipped": False, "status": "failed"}

        safe_result = _safe_result(result)
        _set_job_state(
            job_id,
            running=False,
            last_status="success",
            last_finished_at=_now_iso(),
            last_detail=safe_result,
        )
        logger.info(
            "[scheduler_job_completed] job_id=%s task_type=%s result=%s",
            job_id,
            task_type,
            safe_result,
        )
        await _record_auto_audit(
            action,
            resource_id,
            status="success",
            detail={"result": safe_result},
        )
        return result


async def _run_cn_async() -> dict:
    return await _run_collection_job(CN_JOB_ID, "cn", run_cn_collector)


async def _run_overseas_async() -> dict:
    return await _run_collection_job(OVERSEAS_JOB_ID, "overseas", run_overseas_collector)


async def _run_retention_async() -> None:
    """Daily retention cleanup remains a system task in UTC."""
    try:
        from services.retention import run_all_purges

        logger.info("[scheduler_job_started] job_id=%s task_type=retention", RETENTION_JOB_ID)
        result = await asyncio.to_thread(run_all_purges)
        logger.info("[scheduler_job_completed] job_id=%s task_type=retention result=%s", RETENTION_JOB_ID, result)
    except Exception as exc:
        logger.error(
            "[scheduler_job_failed] job_id=%s task_type=retention error_type=%s",
            RETENTION_JOB_ID,
            type(exc).__name__,
        )


async def _run_daily_report_async() -> dict[str, Any]:
    """Generate the local-delivery report without touching collection inputs."""
    from services.daily_report import generate_daily_report

    report_date = datetime.now(SCHEDULER_TIMEZONE).date().isoformat()
    resource_id = f"daily-report:{report_date}"
    try:
        report = await generate_daily_report()
    except Exception as exc:
        logger.error(
            "[daily_report_failed] report_date=%s error_type=%s",
            report_date,
            type(exc).__name__,
        )
        await _record_auto_audit(
            "report.daily.auto",
            resource_id,
            status="failed",
            resource_type="daily_report",
            detail={"report_date": report_date, "error_type": type(exc).__name__},
        )
        return {"status": "failed", "report_date": report_date}

    status = str(report.get("status", "degraded"))
    safe_detail = {
        "report_date": report.get("report_date", report_date),
        "warning_count": len(report.get("warnings") or []),
        "error_count": len(report.get("errors") or []),
        "status": status,
    }
    logger.info(
        "[daily_report_completed] report_date=%s status=%s warnings=%d errors=%d",
        safe_detail["report_date"],
        status,
        safe_detail["warning_count"],
        safe_detail["error_count"],
    )
    await _record_auto_audit(
        "report.daily.auto",
        resource_id,
        status=status,
        resource_type="daily_report",
        detail=safe_detail,
    )
    return {"status": status, "report_date": safe_detail["report_date"]}


def _add_fixed_job(func: Callable, *, job_id: str, cron: str, timezone_value: ZoneInfo) -> None:
    assert _scheduler is not None
    _scheduler.add_job(
        func,
        _build_trigger(cron, timezone_value),
        id=job_id,
        **_JOB_OPTIONS,
    )


def init_scheduler() -> None:
    """Register the fixed automatic jobs idempotently at application startup."""
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
    _add_fixed_job(
        _run_cn_async,
        job_id=CN_JOB_ID,
        cron=CN_CRON,
        timezone_value=SCHEDULER_TIMEZONE,
    )
    _add_fixed_job(
        _run_overseas_async,
        job_id=OVERSEAS_JOB_ID,
        cron=OVERSEAS_CRON,
        timezone_value=SCHEDULER_TIMEZONE,
    )
    _scheduler.add_job(
        _run_retention_async,
        CronTrigger(hour=3, minute=0, timezone=UTC),
        id=RETENTION_JOB_ID,
        **_JOB_OPTIONS,
    )
    _scheduler.add_job(
        _run_daily_report_async,
        _build_trigger(DAILY_REPORT_CRON, SCHEDULER_TIMEZONE),
        id=DAILY_REPORT_JOB_ID,
        **_REPORT_JOB_OPTIONS,
    )
    _scheduler.start()
    logger.info(
        "[scheduler_started] timezone=%s jobs=%s",
        SCHEDULER_TIMEZONE_NAME,
        [CN_JOB_ID, OVERSEAS_JOB_ID, RETENTION_JOB_ID, DAILY_REPORT_JOB_ID],
    )


def shutdown_scheduler() -> None:
    """Stop the scheduler without touching collection data or progress files."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("[scheduler_stopped]")


def get_collection_status() -> dict[str, Any]:
    """Return read-only status for the fixed collection jobs."""
    definitions = (
        (CN_JOB_ID, "cn", CN_CRON),
        (OVERSEAS_JOB_ID, "overseas", OVERSEAS_CRON),
    )
    jobs = []
    for job_id, task_type, cron in definitions:
        state = dict(_job_state.get(job_id, {}))
        next_run = None
        if _scheduler is not None:
            job = _scheduler.get_job(job_id)
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()
        jobs.append(
            {
                "id": job_id,
                "task_type": task_type,
                "cron": cron,
                "next_run_at": next_run,
                "running": bool(state.pop("running", False)),
                **state,
            }
        )
    return {
        "timezone": SCHEDULER_TIMEZONE_NAME,
        "manual_collection_enabled": False,
        "jobs": jobs,
    }
