import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from services import daily_report


class DailyReportQdrantTests(unittest.TestCase):
    def test_summary_streams_and_keeps_at_most_ten_samples(self):
        class FakeClient:
            def get_collection(self, name):
                return SimpleNamespace(
                    config=SimpleNamespace(
                        params=SimpleNamespace(vectors=SimpleNamespace(size=768))
                    )
                )

            def count(self, collection_name, exact=True):
                return SimpleNamespace(count=12 if collection_name == "cn_anchors" else 1)

        records = [
            SimpleNamespace(
                payload={
                    "cn_word": f"锚点-{index}",
                    "updated_at": f"2026-08-26T09:{index:02d}:00+08:00",
                    "trend_score": index / 10,
                }
            )
            for index in range(12)
        ]
        with patch("services.qdrant_store.get_qdrant_client", return_value=FakeClient()), patch(
            "services.qdrant_store._iter_scroll", return_value=iter(records)
        ):
            library, samples, warnings = daily_report._qdrant_summary_sync()

        self.assertEqual(library["cn_anchors"]["count"], 12)
        self.assertEqual(library["cn_anchors"]["dimension"], 768)
        self.assertEqual(len(samples["cn_anchors"]), 10)
        self.assertEqual(warnings, [])


class DailyReportGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_writes_schema_and_degraded_warning(self):
        db_summary = {
            "auto_runs": {},
            "lineage": {},
            "llm": {"window_hours": 24, "by_type": {}},
            "collector_state": {
                "candidate_observation_total": 0,
                "candidate_observation_recent": 0,
            },
        }
        scheduler_status = {
            "timezone": "Asia/Shanghai",
            "jobs": [
                {
                    "task_type": "cn",
                    "last_status": "success",
                    "running": False,
                },
                {
                    "task_type": "overseas",
                    "last_status": "success",
                    "running": False,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "services.daily_report.get_report_dir", return_value=Path(temp_dir)
        ), patch(
            "services.daily_report._qdrant_summary_sync",
            return_value=(
                {
                    "cn_anchors": {"count": 1, "dimension": 768},
                    "local_tags": {"count": 0, "dimension": 768},
                    "pending_review": {"count": 0, "dimension": 768},
                    "blocked_decisions": {"count": 0, "dimension": 768},
                },
                {name: [] for name in daily_report.COLLECTION_NAMES},
                [],
            ),
        ), patch(
            "services.daily_report._db_summary_sync", return_value=db_summary
        ), patch(
            "services.task_scheduler.get_collection_status",
            return_value=scheduler_status,
        ):
            report = await daily_report.generate_daily_report(
                datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            )

            self.assertEqual(report["schema_version"], "1.0")
            self.assertEqual(report["report_date"], "2026-08-26")
            self.assertEqual(report["status"], "degraded")
            self.assertIn("collector_candidate_observation_empty", report["warnings"])
            self.assertTrue((Path(temp_dir) / "latest.json").exists())
            self.assertTrue((Path(temp_dir) / "latest.md").exists())
            parsed = json.loads((Path(temp_dir) / "latest.json").read_text(encoding="utf-8"))
            self.assertNotIn("prompt", json.dumps(parsed, ensure_ascii=False))
            self.assertNotIn("response", json.dumps(parsed, ensure_ascii=False))

    async def test_qdrant_failure_is_reported_without_llm_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "services.daily_report.get_report_dir", return_value=Path(temp_dir)
        ), patch(
            "services.daily_report._qdrant_summary_sync",
            side_effect=RuntimeError("qdrant unavailable"),
        ), patch(
            "services.daily_report._db_summary_sync",
            return_value={
                "auto_runs": {},
                "lineage": {},
                "llm": {"window_hours": 24, "by_type": {}},
                "collector_state": {},
            },
        ), patch(
            "services.task_scheduler.get_collection_status",
            return_value={"jobs": []},
        ):
            report = await daily_report.generate_daily_report()

            self.assertEqual(report["status"], "degraded")
            self.assertTrue(any(item.startswith("qdrant_summary_error:") for item in report["errors"]))
            self.assertTrue((Path(temp_dir) / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
