import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from services import feishu_library_sync as sync


TIMEZONE = ZoneInfo("Asia/Shanghai")


def _report(
    *,
    now: datetime,
    status: str = "success",
    generated_at: datetime | None = None,
    report_date: str | None = None,
    counts: dict[str, int] | None = None,
) -> dict:
    generated = generated_at or now - timedelta(minutes=10)
    values = counts or {
        "cn_anchors": 1251,
        "local_tags": 996,
        "pending_review": 64,
        "blocked_decisions": 11,
    }
    return {
        "schema_version": "1.0",
        "report_date": report_date or now.date().isoformat(),
        "timezone": "Asia/Shanghai",
        "generated_at": generated.isoformat(),
        "status": status,
        "library": {
            name: {"count": values[name], "dimension": 768}
            for name in sync.COLLECTION_KEYS
        },
    }


def _response(data: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "https://open.feishu.cn/test")
    return httpx.Response(
        status_code,
        request=request,
        json={"code": 0, "msg": "ok", "data": data},
    )


def _blocks(source_text: str, count_text: str) -> list[dict]:
    def text_block(block_id: str, text: str) -> dict:
        return {
            "block_id": block_id,
            "parent_id": "document",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": text}}]},
        }

    return [
        {
            "block_id": "heading",
            "parent_id": "document",
            "block_type": 4,
            "heading2": {
                "elements": [{"text_run": {"content": sync.SNAPSHOT_HEADING}}]
            },
        },
        text_block("source", source_text),
        text_block("count", count_text),
    ]


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SnapshotFormattingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 9, 10, tzinfo=TIMEZONE)

    def test_formats_four_collection_counts_and_success_status(self):
        snapshot = sync.build_library_snapshot(_report(now=self.now), now=self.now)

        self.assertEqual(
            snapshot.source_line,
            "数据来源：服务器每日词库日报｜更新时间：2026-08-31 09:00（Asia/Shanghai）｜状态：正常",
        )
        self.assertEqual(
            snapshot.count_line,
            "中文锚点：1,251｜可复用标签：996｜待审核：64｜拦截决策：11",
        )
        self.assertEqual(len(snapshot.content_sha256), 64)

    def test_degraded_report_is_still_formatted(self):
        snapshot = sync.build_library_snapshot(
            _report(now=self.now, status="degraded"), now=self.now
        )
        self.assertIn("状态：含告警", snapshot.source_line)

    def test_rejects_unsupported_or_stale_reports(self):
        cases = [
            ("schema", {"schema_version": "2.0"}),
            ("status", {"status": "failed"}),
            ("date", {"report_date": "2026-08-30"}),
            (
                "stale",
                {"generated_at": (self.now - timedelta(hours=27)).isoformat()},
            ),
            (
                "missing_count",
                {"library": {"cn_anchors": {"count": 1}}},
            ),
        ]
        for name, changes in cases:
            with self.subTest(name=name):
                report = _report(now=self.now)
                if name == "missing_count":
                    report["library"] = changes["library"]
                else:
                    report.update(changes)
                with self.assertRaises(sync.ReportValidationError):
                    sync.build_library_snapshot(report, now=self.now)


class FeishuBlockTests(unittest.TestCase):
    def test_locates_heading_and_adjacent_dynamic_blocks(self):
        source = "数据来源：服务器每日词库日报｜更新时间：—｜状态：等待首次同步"
        count = "中文锚点：—｜可复用标签：—｜待审核：—｜拦截决策：—"
        target = sync.find_snapshot_target(_blocks(source, count))

        self.assertEqual(target.source_block_id, "source")
        self.assertEqual(target.count_block_id, "count")
        self.assertEqual(target.source_text, source)
        self.assertEqual(target.count_text, count)

    def test_missing_target_is_an_error(self):
        with self.assertRaises(sync.FeishuSyncError):
            sync.find_snapshot_target([])


class FeishuRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_http_errors_retry_up_to_success(self):
        client = FakeAsyncClient([_response({}, 429), _response({}, 503), _response({})])
        with patch("services.feishu_library_sync.asyncio.sleep", new=AsyncMock()) as sleep:
            data = await sync._request_json(client, "GET", "https://example.test")

        self.assertEqual(data, {})
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_permission_error_is_not_retried(self):
        client = FakeAsyncClient([_response({}, 403), _response({})])
        with self.assertRaises(sync.FeishuSyncError) as raised:
            await sync._request_json(client, "GET", "https://example.test")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(len(client.calls), 1)


class FeishuSynchronizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 9, 10, tzinfo=TIMEZONE)

    def _environment(self, temp_dir: str) -> dict[str, str]:
        return {
            "FEISHU_SYNC_ENABLED": "true",
            "FEISHU_APP_ID": "cli_test_app",
            "FEISHU_APP_SECRET": "test-secret-value",
            "FEISHU_DOCUMENT_ID": "DEdxdmWSnoqWHpxSpTjcnf2In6e",
        }

    def _write_report(self, temp_dir: str, report: dict | None = None) -> Path:
        path = Path(temp_dir) / "latest.json"
        path.write_text(
            json.dumps(report or _report(now=self.now), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    async def test_updates_two_blocks_and_writes_redacted_state(self):
        report = _report(now=self.now)
        snapshot = sync.build_library_snapshot(report, now=self.now)
        old_blocks = _blocks(
            "数据来源：服务器每日词库日报｜更新时间：—｜状态：等待首次同步",
            "中文锚点：—｜可复用标签：—｜待审核：—｜拦截决策：—",
        )
        new_blocks = _blocks(snapshot.source_line, snapshot.count_line)
        client = FakeAsyncClient(
            [
                _response({"tenant_access_token": "tenant-token"}),
                _response({"items": old_blocks, "has_more": False}),
                _response({}),
                _response({"items": new_blocks, "has_more": False}),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, self._environment(temp_dir), clear=False
        ), patch.object(sync, "get_report_dir", return_value=Path(temp_dir)):
            self._write_report(temp_dir, report)
            result = await sync.sync_latest_report_to_doc(now=self.now, client=client)

            state_path = Path(temp_dir) / "feishu-sync-state.json"
            state_text = state_path.read_text(encoding="utf-8")
            state = json.loads(state_text)

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["changed"])
        self.assertEqual(state["last_report_generated_at"], snapshot.generated_at.isoformat())
        self.assertEqual(state["content_sha256"], snapshot.content_sha256)
        self.assertNotIn("test-secret-value", state_text)
        self.assertEqual([call[0] for call in client.calls], ["POST", "GET", "PATCH", "GET"])
        patch_body = client.calls[2][2]["json"]
        self.assertEqual(
            patch_body["requests"],
            [
                {
                    "block_id": "source",
                    "update_text_elements": {
                        "elements": [{"text_run": {"content": snapshot.source_line}}]
                    },
                },
                {
                    "block_id": "count",
                    "update_text_elements": {
                        "elements": [{"text_run": {"content": snapshot.count_line}}]
                    },
                },
            ],
        )

    async def test_unchanged_content_skips_batch_update(self):
        report = _report(now=self.now)
        snapshot = sync.build_library_snapshot(report, now=self.now)
        client = FakeAsyncClient(
            [
                _response({"tenant_access_token": "tenant-token"}),
                _response(
                    {"items": _blocks(snapshot.source_line, snapshot.count_line), "has_more": False}
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, self._environment(temp_dir), clear=False
        ), patch.object(sync, "get_report_dir", return_value=Path(temp_dir)):
            self._write_report(temp_dir, report)
            result = await sync.sync_latest_report_to_doc(now=self.now, client=client)

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["changed"])
        self.assertEqual([call[0] for call in client.calls], ["POST", "GET"])

    async def test_revision_conflict_refetches_and_retries_once(self):
        report = _report(now=self.now)
        snapshot = sync.build_library_snapshot(report, now=self.now)
        old_blocks = _blocks("数据来源：服务器每日词库日报｜更新时间：—｜状态：等待首次同步", "中文锚点：—｜可复用标签：—｜待审核：—｜拦截决策：—")
        client = FakeAsyncClient(
            [
                _response({"tenant_access_token": "tenant-token"}),
                _response({"items": old_blocks, "has_more": False}),
                _response({}, 409),
                _response({"items": old_blocks, "has_more": False}),
                _response({}),
                _response({"items": _blocks(snapshot.source_line, snapshot.count_line), "has_more": False}),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, self._environment(temp_dir), clear=False
        ), patch.object(sync, "get_report_dir", return_value=Path(temp_dir)):
            self._write_report(temp_dir, report)
            result = await sync.sync_latest_report_to_doc(now=self.now, client=client)

        self.assertEqual(result["status"], "success")
        self.assertEqual([call[0] for call in client.calls], ["POST", "GET", "PATCH", "GET", "PATCH", "GET"])

    async def test_missing_report_preserves_document_and_records_failure_state(self):
        client = FakeAsyncClient([])
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, self._environment(temp_dir), clear=False
        ), patch.object(sync, "get_report_dir", return_value=Path(temp_dir)):
            with self.assertRaises(sync.ReportValidationError):
                await sync.sync_latest_report_to_doc(now=self.now, client=client)
            state = json.loads(
                (Path(temp_dir) / "feishu-sync-state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_type"], "ReportValidationError")
        self.assertEqual(client.calls, [])

    async def test_disabled_sync_makes_no_request(self):
        client = FakeAsyncClient([])
        with patch.dict(os.environ, {"FEISHU_SYNC_ENABLED": "false"}, clear=False):
            result = await sync.sync_latest_report_to_doc(now=self.now, client=client)

        self.assertEqual(result, {"status": "disabled"})
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
