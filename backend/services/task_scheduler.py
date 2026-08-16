import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.scheduler_store import load_schedules, touch_last_run, get_schedule
from services.collectors.cn_longtail import run_cn_collector
from services.collectors.overseas_trends import run_overseas_collector

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _build_trigger(cron: str) -> CronTrigger:
    """解析 cron 表达式构建 APScheduler CronTrigger"""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron}")
    return CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
    )


async def _run_cn_async():
    """后台执行中文采集（调度器回调）"""
    try:
        logger.info("[定时任务] 开始执行中文长尾词采集")
        result = await run_cn_collector()
        logger.info(f"[定时任务] 中文采集完成: {result}")
    except Exception as e:
        logger.error(f"[定时任务] 中文采集失败: {e}")


async def _run_overseas_async():
    """后台执行海外采集（调度器回调）"""
    try:
        logger.info("[定时任务] 开始执行海外词采集")
        result = await run_overseas_collector()
        logger.info(f"[定时任务] 海外采集完成: {result}")
    except Exception as e:
        logger.error(f"[定时任务] 海外采集失败: {e}")


async def _run_retention_async():
    """每日留存清理（合规系统任务，2026-08；不进 schedules.json，配置在 retention_policy 表）"""
    try:
        from services.retention import run_all_purges
        logger.info("[定时任务] 开始执行合规留存清理")
        result = await asyncio.to_thread(run_all_purges)
        logger.info(f"[定时任务] 留存清理完成: {result}")
    except Exception as e:
        logger.error(f"[定时任务] 留存清理失败: {e}")


def init_scheduler():
    """启动时从文件恢复所有启用的任务"""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()

    # 合规系统任务：每日 03:00 UTC 留存清理（GDPR Art.5(1)(e) 存储限制）
    _scheduler.add_job(
        _run_retention_async,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="compliance_retention",
        replace_existing=True,
    )
    logger.info("[调度器] 已注册合规留存清理任务 (compliance_retention, 每日 03:00 UTC)")

    for schedule in load_schedules():
        if not schedule.enabled:
            continue
        try:
            trigger = _build_trigger(schedule.cron)
            func = _run_cn_async if schedule.task_type == "cn" else _run_overseas_async
            _scheduler.add_job(func, trigger, id=schedule.id, replace_existing=True)
            logger.info(f"[调度器] 已恢复任务: {schedule.name} ({schedule.id})")
        except Exception as e:
            logger.warning(f"[调度器] 恢复任务失败 {schedule.id}: {e}")

    _scheduler.start()
    logger.info("[调度器] APScheduler 已启动")


def shutdown_scheduler():
    """关闭调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("[调度器] APScheduler 已关闭")


def add_job(schedule) -> None:
    """添加新任务到调度器"""
    if _scheduler is None:
        return
    try:
        trigger = _build_trigger(schedule.cron)
        func = _run_cn_async if schedule.task_type == "cn" else _run_overseas_async
        _scheduler.add_job(func, trigger, id=schedule.id, replace_existing=True)
        logger.info(f"[调度器] 已添加任务: {schedule.name}")
    except Exception as e:
        logger.error(f"[调度器] 添加任务失败: {e}")
        raise


def remove_job(schedule_id: str) -> None:
    """从调度器移除任务"""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(schedule_id)
        logger.info(f"[调度器] 已移除任务: {schedule_id}")
    except Exception:
        pass


def reschedule(schedule) -> None:
    """更新调度器中的任务"""
    remove_job(schedule.id)
    if schedule.enabled:
        add_job(schedule)


async def run_job_now(schedule_id: str) -> dict:
    """立即手动执行指定任务（同步等待完成）"""
    schedule = get_schedule(schedule_id)
    if not schedule:
        return {"success": False, "message": "任务不存在"}

    touch_last_run(schedule_id)

    try:
        if schedule.task_type == "cn":
            result = await run_cn_collector()
        else:
            result = await run_overseas_collector()
        return {"success": True, "message": "执行完成", **result}
    except Exception as e:
        logger.error(f"[定时任务] 手动执行失败: {e}")
        return {"success": False, "message": f"执行失败: {e}"}