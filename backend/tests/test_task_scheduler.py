import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from services import task_scheduler


class FixedSchedulerTests(unittest.TestCase):
    def tearDown(self):
        if task_scheduler._scheduler is not None:
            task_scheduler.shutdown_scheduler()

    def test_fixed_triggers_use_declared_timezones(self):
        cn_trigger = task_scheduler._build_trigger(task_scheduler.CN_CRON)
        retention_trigger = task_scheduler.CronTrigger(
            hour=3, minute=0, timezone=task_scheduler.UTC
        )

        self.assertEqual(cn_trigger.timezone, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(retention_trigger.timezone, task_scheduler.UTC)
        self.assertEqual(task_scheduler.OVERSEAS_CRON, "0 4,16 * * *")

    def test_init_registers_five_idempotent_jobs(self):
        fake_scheduler = MagicMock()
        task_scheduler._scheduler = None
        with patch.object(task_scheduler, "AsyncIOScheduler", return_value=fake_scheduler) as constructor:
            task_scheduler.init_scheduler()
            task_scheduler.init_scheduler()

        constructor.assert_called_once_with(timezone=task_scheduler.SCHEDULER_TIMEZONE)
        self.assertEqual(fake_scheduler.add_job.call_count, 5)
        ids = {call.kwargs["id"] for call in fake_scheduler.add_job.call_args_list}
        self.assertEqual(
            ids,
            {
                task_scheduler.CN_JOB_ID,
                task_scheduler.OVERSEAS_JOB_ID,
                task_scheduler.RETENTION_JOB_ID,
                task_scheduler.DAILY_REPORT_JOB_ID,
                task_scheduler.FEISHU_SYNC_JOB_ID,
            },
        )
        for call in fake_scheduler.add_job.call_args_list:
            self.assertTrue(call.kwargs["replace_existing"])
            self.assertTrue(call.kwargs["coalesce"])
            self.assertEqual(call.kwargs["max_instances"], 1)
            expected_grace = (
                3600
                if call.kwargs["id"] in {
                    task_scheduler.DAILY_REPORT_JOB_ID,
                    task_scheduler.FEISHU_SYNC_JOB_ID,
                }
                else 60
            )
            self.assertEqual(call.kwargs["misfire_grace_time"], expected_grace)
        fake_scheduler.start.assert_called_once_with()

    def test_daily_report_trigger_uses_declared_timezone(self):
        trigger = task_scheduler._build_trigger(task_scheduler.DAILY_REPORT_CRON)
        self.assertEqual(trigger.timezone, ZoneInfo("Asia/Shanghai"))

    def test_feishu_sync_trigger_uses_declared_timezone(self):
        trigger = task_scheduler._build_trigger(task_scheduler.FEISHU_SYNC_CRON)
        self.assertEqual(trigger.timezone, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(task_scheduler.FEISHU_SYNC_CRON, "10 9 * * *")

    def test_status_is_read_only_and_exposes_fixed_jobs(self):
        task_scheduler._scheduler = None
        status = task_scheduler.get_collection_status()

        self.assertEqual(status["timezone"], "Asia/Shanghai")
        self.assertFalse(status["manual_collection_enabled"])
        self.assertEqual(
            [(job["id"], job["cron"]) for job in status["jobs"]],
            [
                (task_scheduler.CN_JOB_ID, "0 2 * * *"),
                (task_scheduler.OVERSEAS_JOB_ID, "0 4,16 * * *"),
            ],
        )


class DailyReportSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_report_records_aggregate_audit(self):
        report = {
            "report_date": "2026-08-26",
            "status": "degraded",
            "warnings": ["collector_candidate_observation_empty"],
            "errors": [],
        }
        with patch(
            "services.daily_report.generate_daily_report",
            new=AsyncMock(return_value=report),
        ), patch.object(
            task_scheduler, "_record_auto_audit", new_callable=AsyncMock
        ) as audit:
            result = await task_scheduler._run_daily_report_async()

        self.assertEqual(result["status"], "degraded")
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.args[0], "report.daily.auto")
        self.assertEqual(audit.await_args.kwargs["resource_type"], "daily_report")

    async def test_feishu_sync_records_aggregate_audit(self):
        report_date = datetime.now(task_scheduler.SCHEDULER_TIMEZONE).date().isoformat()
        with patch(
            "services.feishu_library_sync.sync_latest_report_to_doc",
            new=AsyncMock(
                return_value={
                    "status": "skipped",
                    "report_date": report_date,
                    "changed": False,
                }
            ),
        ), patch.object(
            task_scheduler, "_record_auto_audit", new_callable=AsyncMock
        ) as audit:
            result = await task_scheduler._run_feishu_sync_async()

        self.assertEqual(result["status"], "skipped")
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.args[0], "report.feishu.auto")
        self.assertEqual(audit.await_args.kwargs["resource_type"], "feishu_snapshot")
        self.assertEqual(audit.await_args.kwargs["detail"]["changed"], False)

    async def test_feishu_sync_failure_records_error_type_only(self):
        with patch(
            "services.feishu_library_sync.sync_latest_report_to_doc",
            new=AsyncMock(side_effect=RuntimeError("secret response")),
        ), patch.object(
            task_scheduler, "_record_auto_audit", new_callable=AsyncMock
        ) as audit:
            result = await task_scheduler._run_feishu_sync_async()

        self.assertEqual(result["status"], "failed")
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["status"], "failed")
        detail = audit.await_args.kwargs["detail"]
        self.assertEqual(detail["error_type"], "RuntimeError")
        self.assertNotIn("secret response", str(detail))


class CollectionJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        task_scheduler._job_state = {
            task_scheduler.CN_JOB_ID: {"running": False, "last_status": "never"},
            task_scheduler.OVERSEAS_JOB_ID: {"running": False, "last_status": "never"},
        }

    async def test_success_records_aggregate_result_only(self):
        runner = AsyncMock(
            return_value={"total": 3, "new": 2, "word": "secret", "run_id": "private"}
        )
        with patch.object(task_scheduler, "_record_auto_audit", new_callable=AsyncMock) as audit:
            result = await task_scheduler._run_collection_job(
                task_scheduler.CN_JOB_ID, "cn", runner
            )

        self.assertEqual(result["total"], 3)
        self.assertEqual(task_scheduler._job_state[task_scheduler.CN_JOB_ID]["last_status"], "success")
        self.assertNotIn("word", task_scheduler._job_state[task_scheduler.CN_JOB_ID]["last_detail"])
        self.assertNotIn("run_id", task_scheduler._job_state[task_scheduler.CN_JOB_ID]["last_detail"])
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.args[0], "collect.cn.auto")

    async def test_failure_is_recorded_without_blocking_next_run(self):
        runner = AsyncMock(side_effect=RuntimeError("upstream failure"))
        with patch.object(task_scheduler, "_record_auto_audit", new_callable=AsyncMock) as audit:
            result = await task_scheduler._run_collection_job(
                task_scheduler.OVERSEAS_JOB_ID, "overseas", runner
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            task_scheduler._job_state[task_scheduler.OVERSEAS_JOB_ID]["last_status"],
            "failed",
        )
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["status"], "failed")

    async def test_busy_collection_is_skipped_and_audited(self):
        await task_scheduler._collection_lock.acquire()
        try:
            runner = AsyncMock()
            with patch.object(task_scheduler, "_record_auto_audit", new_callable=AsyncMock) as audit:
                result = await task_scheduler._run_collection_job(
                    task_scheduler.CN_JOB_ID, "cn", runner
                )
        finally:
            task_scheduler._collection_lock.release()

        self.assertEqual(result, {"skipped": True, "reason": "collection_lock_busy"})
        runner.assert_not_awaited()
        self.assertEqual(task_scheduler._job_state[task_scheduler.CN_JOB_ID]["last_status"], "skipped")
        audit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["status"], "skipped")


class CollectionUiContractTests(unittest.TestCase):
    def test_admin_ui_has_no_manual_collection_or_schedule_controls(self):
        html_path = Path(__file__).resolve().parents[1] / "static" / "admin.html"
        html = html_path.read_text(encoding="utf-8")

        self.assertIn("/admin/api/collection/status", html)
        self.assertIn("人工采集已关闭", html)
        for removed in (
            "/admin/api/collect/",
            "/admin/api/schedules",
            "cl-btn-cn",
            "cl-btn-overseas",
            "cl-new-cron",
        ):
            self.assertNotIn(removed, html)


if __name__ == "__main__":
    unittest.main()
