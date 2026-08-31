"""Server-side synchronization of the daily library snapshot to Feishu.

The sync consumes the aggregate ``latest.json`` report produced by
``daily_report``.  It does not read Qdrant, run collectors, invoke models, or
change the report schema.  The target document can expose the snapshot as a
structured table; older documents with two text blocks remain supported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from services.daily_report import get_report_dir
from services.http_client import get_http_client

logger = logging.getLogger(__name__)

FEISHU_API_BASE = "https://open.feishu.cn"
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
BLOCKS_PATH = "/open-apis/docx/v1/documents/{document_id}/blocks"
BATCH_UPDATE_PATH = "/open-apis/docx/v1/documents/{document_id}/blocks/batch_update"

REPORT_SCHEMA_VERSION = "1.0"
REPORT_TIMEZONE_NAME = "Asia/Shanghai"
REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
MAX_REPORT_AGE = timedelta(hours=26)
MIN_REPORT_AGE = timedelta(hours=-1)

SNAPSHOT_HEADING = "当前词库运行快照"
SOURCE_LINE_PREFIX = "数据来源：服务器每日词库日报"
COUNT_LINE_PREFIX = "中文锚点："
SNAPSHOT_TABLE_HEADERS = ("指标", "当前值")
SNAPSHOT_TABLE_LABELS = (
    "数据来源",
    "更新时间",
    "状态",
    "中文锚点",
    "可复用标签",
    "待审核",
    "拦截决策",
)

FEISHU_REQUEST_TIMEOUT = 20.0
MAX_HTTP_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 5.0)
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

COLLECTION_KEYS = (
    "cn_anchors",
    "local_tags",
    "pending_review",
    "blocked_decisions",
)


class FeishuSyncError(RuntimeError):
    """A redacted Feishu API or target-document error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


class ReportValidationError(ValueError):
    """The daily report cannot be used as a current snapshot."""


@dataclass(frozen=True)
class FeishuSyncConfig:
    enabled: bool
    app_id: str
    app_secret: str
    document_id: str
    report_path: Path
    state_path: Path

    @classmethod
    def from_env(cls) -> "FeishuSyncConfig":
        report_dir = get_report_dir()
        return cls(
            enabled=_env_true("FEISHU_SYNC_ENABLED"),
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            document_id=os.getenv("FEISHU_DOCUMENT_ID", "").strip(),
            report_path=report_dir / "latest.json",
            state_path=report_dir / "feishu-sync-state.json",
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", self.app_id),
                ("FEISHU_APP_SECRET", self.app_secret),
                ("FEISHU_DOCUMENT_ID", self.document_id),
            )
            if not value
        ]
        if missing:
            raise FeishuSyncError(
                "Feishu sync configuration is incomplete: " + ",".join(missing)
            )


@dataclass(frozen=True)
class LibrarySnapshot:
    report_date: str
    generated_at: datetime
    status: str
    source_line: str
    count_line: str
    table_values: tuple[str, ...]
    content_sha256: str


@dataclass(frozen=True)
class SnapshotTarget:
    source_block_id: str | None
    count_block_id: str | None
    source_text: str
    count_text: str
    table_value_block_ids: tuple[str, ...] = ()
    table_value_texts: tuple[str, ...] = ()


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_local_now(now: datetime | None) -> datetime:
    value = now or datetime.now(REPORT_TIMEZONE)
    if value.tzinfo is None:
        value = value.replace(tzinfo=REPORT_TIMEZONE)
    return value.astimezone(REPORT_TIMEZONE)


def _parse_report_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReportValidationError("generated_at is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReportValidationError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=REPORT_TIMEZONE)
    return parsed.astimezone(REPORT_TIMEZONE)


def _library_count(report: dict[str, Any], collection_name: str) -> int:
    library = report.get("library")
    item = library.get(collection_name) if isinstance(library, dict) else None
    count = item.get("count") if isinstance(item, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ReportValidationError(f"library.{collection_name}.count is invalid")
    return count


def build_library_snapshot(
    report: dict[str, Any], *, now: datetime | None = None
) -> LibrarySnapshot:
    """Validate and format one current daily report for document display."""
    if not isinstance(report, dict):
        raise ReportValidationError("daily report is not an object")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ReportValidationError("unsupported daily report schema")

    status = report.get("status")
    if status not in {"success", "degraded"}:
        raise ReportValidationError("daily report status is invalid")

    now_local = _coerce_local_now(now)
    report_date = report.get("report_date")
    expected_date = now_local.date().isoformat()
    if report_date != expected_date:
        raise ReportValidationError("daily report date is not current")

    generated_at = _parse_report_datetime(report.get("generated_at"))
    age = now_local - generated_at
    if age < MIN_REPORT_AGE or age > MAX_REPORT_AGE:
        raise ReportValidationError("daily report is stale")

    counts = {
        name: _library_count(report, name)
        for name in COLLECTION_KEYS
    }
    status_label = "正常" if status == "success" else "含告警"
    generated_text = generated_at.strftime("%Y-%m-%d %H:%M")
    source_line = (
        f"{SOURCE_LINE_PREFIX}｜更新时间：{generated_text}"
        f"（{REPORT_TIMEZONE_NAME}）｜状态：{status_label}"
    )
    count_line = (
        f"中文锚点：{counts['cn_anchors']:,}｜"
        f"可复用标签：{counts['local_tags']:,}｜"
        f"待审核：{counts['pending_review']:,}｜"
        f"拦截决策：{counts['blocked_decisions']:,}"
    )
    table_values = (
        "服务器每日词库日报",
        f"{generated_text}（{REPORT_TIMEZONE_NAME}）",
        status_label,
        f"{counts['cn_anchors']:,}",
        f"{counts['local_tags']:,}",
        f"{counts['pending_review']:,}",
        f"{counts['blocked_decisions']:,}",
    )
    content_sha256 = hashlib.sha256(
        f"{source_line}\n{count_line}".encode("utf-8")
    ).hexdigest()
    return LibrarySnapshot(
        report_date=str(report_date),
        generated_at=generated_at,
        status=str(status),
        source_line=source_line,
        count_line=count_line,
        table_values=table_values,
        content_sha256=content_sha256,
    )


def load_library_snapshot(
    report_path: Path, *, now: datetime | None = None
) -> LibrarySnapshot:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportValidationError("daily report is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("daily report cannot be read") from exc
    return build_library_snapshot(report, now=now)


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(MAX_HTTP_ATTEMPTS):
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=body,
                timeout=FEISHU_REQUEST_TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt + 1 < MAX_HTTP_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            raise

        status_code = response.status_code
        if status_code in RETRYABLE_STATUS_CODES:
            if attempt + 1 < MAX_HTTP_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            raise FeishuSyncError(
                f"Feishu API transient HTTP error {status_code}",
                status_code=status_code,
            )
        if status_code == 409:
            raise FeishuSyncError(
                "Feishu document revision conflict",
                status_code=status_code,
            )
        if status_code >= 400:
            raise FeishuSyncError(
                f"Feishu API HTTP error {status_code}",
                status_code=status_code,
            )

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise FeishuSyncError("Feishu API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FeishuSyncError("Feishu API returned an invalid envelope")
        api_code = payload.get("code")
        if api_code != 0:
            numeric_code = api_code if isinstance(api_code, int) else None
            raise FeishuSyncError(
                "Feishu API returned an application error",
                status_code=status_code,
                api_code=numeric_code,
            )
        data = payload.get("data")
        if data is None:
            # The tenant-access-token endpoint returns its token at the
            # envelope root, while Docx endpoints place response fields in
            # ``data``.  Preserve root-level fields for the former and keep
            # the existing empty-data behavior for successful write calls.
            return payload
        if not isinstance(data, dict):
            raise FeishuSyncError("Feishu API returned invalid data")
        return data

    raise AssertionError("unreachable")


async def _get_tenant_access_token(
    client: httpx.AsyncClient, config: FeishuSyncConfig
) -> str:
    data = await _request_json(
        client,
        "POST",
        FEISHU_API_BASE + TENANT_TOKEN_PATH,
        body={"app_id": config.app_id, "app_secret": config.app_secret},
    )
    token = data.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise FeishuSyncError("Feishu API did not return a tenant token")
    return token


async def _list_document_blocks(
    client: httpx.AsyncClient, document_id: str, token: str
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {
            "page_size": 500,
            "document_revision_id": -1,
        }
        if page_token:
            params["page_token"] = page_token
        data = await _request_json(
            client,
            "GET",
            FEISHU_API_BASE + BLOCKS_PATH.format(document_id=document_id),
            token=token,
            params=params,
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise FeishuSyncError("Feishu block list is invalid")
        blocks.extend(item for item in items if isinstance(item, dict))
        if not data.get("has_more"):
            return blocks
        next_page_token = data.get("page_token")
        if not isinstance(next_page_token, str) or not next_page_token:
            raise FeishuSyncError("Feishu block list pagination is invalid")
        if next_page_token == page_token:
            raise FeishuSyncError("Feishu block list pagination repeated")
        page_token = next_page_token


def _block_text(block: dict[str, Any]) -> str:
    block_type = block.get("block_type")
    type_key = {
        2: "text",
        3: "heading1",
        4: "heading2",
        5: "heading3",
        6: "heading4",
        7: "heading5",
        8: "heading6",
        9: "heading7",
        10: "heading8",
        11: "heading9",
        12: "bullet",
        13: "ordered",
        14: "code",
        15: "quote",
        17: "todo",
    }.get(block_type)
    content = block.get(type_key) if type_key else None
    elements = content.get("elements") if isinstance(content, dict) else None
    if not isinstance(elements, list):
        return ""
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run")
        if isinstance(text_run, dict) and isinstance(text_run.get("content"), str):
            parts.append(text_run["content"])
    return "".join(parts)


def _table_cell_text(
    cell_id: str, blocks_by_id: dict[str, dict[str, Any]], table_id: str
) -> tuple[str, str]:
    """Return one table cell's text and its editable text-block ID."""
    cell = blocks_by_id.get(cell_id)
    if not isinstance(cell, dict) or cell.get("block_type") != 32:
        raise FeishuSyncError("snapshot table cell is missing")
    if cell.get("parent_id") != table_id:
        raise FeishuSyncError("snapshot table cell parent is invalid")
    children = cell.get("children")
    if not isinstance(children, list) or len(children) != 1:
        raise FeishuSyncError("snapshot table cell structure is invalid")
    text_block_id = children[0]
    if not isinstance(text_block_id, str):
        raise FeishuSyncError("snapshot table text block ID is invalid")
    text_block = blocks_by_id.get(text_block_id)
    if not isinstance(text_block, dict) or text_block.get("block_type") != 2:
        raise FeishuSyncError("snapshot table text block is invalid")
    text = _block_text(text_block)
    if not text:
        raise FeishuSyncError("snapshot table cell text is empty")
    return text, text_block_id


def _find_snapshot_table_target(
    blocks: list[dict[str, Any]], heading_index: int, parent_id: str
) -> SnapshotTarget | None:
    """Locate the structured snapshot table immediately after its heading."""
    if heading_index + 1 >= len(blocks):
        return None
    table_block = blocks[heading_index + 1]
    if (
        table_block.get("parent_id") != parent_id
        or table_block.get("block_type") != 31
    ):
        return None
    table_id = table_block.get("block_id")
    table = table_block.get("table")
    if not isinstance(table_id, str) or not isinstance(table, dict):
        raise FeishuSyncError("snapshot table metadata is missing")
    properties = table.get("property")
    cells = table.get("cells")
    if not isinstance(properties, dict) or not isinstance(cells, list):
        raise FeishuSyncError("snapshot table definition is invalid")
    column_size = properties.get("column_size")
    row_size = properties.get("row_size")
    if column_size != 2 or not isinstance(row_size, int) or row_size != 8:
        raise FeishuSyncError("snapshot table dimensions are invalid")
    if len(cells) != column_size * row_size or any(
        not isinstance(cell_id, str) for cell_id in cells
    ):
        raise FeishuSyncError("snapshot table cells are invalid")

    blocks_by_id = {
        block.get("block_id"): block
        for block in blocks
        if isinstance(block.get("block_id"), str)
    }
    cell_values: list[str] = []
    text_block_ids: list[str] = []
    for cell_id in cells:
        value, text_block_id = _table_cell_text(cell_id, blocks_by_id, table_id)
        cell_values.append(value)
        text_block_ids.append(text_block_id)

    if tuple(cell_values[:2]) != SNAPSHOT_TABLE_HEADERS:
        raise FeishuSyncError("snapshot table headers are invalid")
    label_to_value_index: dict[str, int] = {}
    for row in range(1, row_size):
        label_index = row * column_size
        label = cell_values[label_index]
        if label in label_to_value_index:
            raise FeishuSyncError("snapshot table labels are duplicated")
        label_to_value_index[label] = label_index + 1
    if set(label_to_value_index) != set(SNAPSHOT_TABLE_LABELS):
        raise FeishuSyncError("snapshot table labels are invalid")

    value_indices = tuple(label_to_value_index[label] for label in SNAPSHOT_TABLE_LABELS)
    table_values = tuple(cell_values[index] for index in value_indices)
    source_text = (
        f"数据来源：{table_values[0]}｜更新时间：{table_values[1]}｜"
        f"状态：{table_values[2]}"
    )
    count_text = (
        f"中文锚点：{table_values[3]}｜可复用标签：{table_values[4]}｜"
        f"待审核：{table_values[5]}｜拦截决策：{table_values[6]}"
    )
    return SnapshotTarget(
        source_block_id=None,
        count_block_id=None,
        source_text=source_text,
        count_text=count_text,
        table_value_block_ids=tuple(text_block_ids[index] for index in value_indices),
        table_value_texts=table_values,
    )


def find_snapshot_target(blocks: list[dict[str, Any]]) -> SnapshotTarget:
    headings = [
        (index, block)
        for index, block in enumerate(blocks)
        if block.get("block_type") == 4 and _block_text(block) == SNAPSHOT_HEADING
    ]
    if len(headings) != 1:
        raise FeishuSyncError(
            f"snapshot heading count is {len(headings)}; expected exactly one"
        )

    heading_index, heading = headings[0]
    parent_id = heading.get("parent_id")
    if not isinstance(parent_id, str):
        raise FeishuSyncError("snapshot heading parent is missing")
    table_target = _find_snapshot_table_target(blocks, heading_index, parent_id)
    if table_target is not None:
        return table_target
    if heading_index + 2 >= len(blocks):
        raise FeishuSyncError("snapshot dynamic blocks are missing")
    source_block = blocks[heading_index + 1]
    count_block = blocks[heading_index + 2]
    if (
        source_block.get("parent_id") != parent_id
        or count_block.get("parent_id") != parent_id
        or source_block.get("block_type") != 2
        or count_block.get("block_type") != 2
    ):
        raise FeishuSyncError("snapshot dynamic blocks are not adjacent siblings")

    source_text = _block_text(source_block)
    count_text = _block_text(count_block)
    if not source_text.startswith(SOURCE_LINE_PREFIX):
        raise FeishuSyncError("snapshot source block marker is missing")
    if not count_text.startswith(COUNT_LINE_PREFIX):
        raise FeishuSyncError("snapshot count block marker is missing")

    source_block_id = source_block.get("block_id")
    count_block_id = count_block.get("block_id")
    if not isinstance(source_block_id, str) or not isinstance(count_block_id, str):
        raise FeishuSyncError("snapshot block IDs are missing")
    return SnapshotTarget(
        source_block_id=source_block_id,
        count_block_id=count_block_id,
        source_text=source_text,
        count_text=count_text,
    )


async def _update_snapshot_blocks(
    client: httpx.AsyncClient,
    document_id: str,
    token: str,
    target: SnapshotTarget,
    snapshot: LibrarySnapshot,
) -> None:
    if target.table_value_block_ids:
        requests = [
            {
                "block_id": block_id,
                "update_text_elements": {
                    "elements": [{"text_run": {"content": value}}]
                },
            }
            for block_id, value in zip(
                target.table_value_block_ids, snapshot.table_values
            )
        ]
    else:
        if not target.source_block_id or not target.count_block_id:
            raise FeishuSyncError("snapshot paragraph block IDs are missing")
        requests = [
            {
                "block_id": target.source_block_id,
                "update_text_elements": {
                    "elements": [{"text_run": {"content": snapshot.source_line}}]
                },
            },
            {
                "block_id": target.count_block_id,
                "update_text_elements": {
                    "elements": [{"text_run": {"content": snapshot.count_line}}]
                },
            },
        ]
    await _request_json(
        client,
        "PATCH",
        FEISHU_API_BASE + BATCH_UPDATE_PATH.format(document_id=document_id),
        token=token,
        body={"requests": requests},
    )


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.warning("[feishu_sync_state_cleanup_failed]")


def _state_for_result(
    *,
    status: str,
    snapshot: LibrarySnapshot | None = None,
    changed: bool = False,
    error_type: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": status,
        "changed": changed,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    if snapshot is not None:
        state.update(
            {
                "report_date": snapshot.report_date,
                "last_report_generated_at": snapshot.generated_at.isoformat(),
                "content_sha256": snapshot.content_sha256,
            }
        )
    if error_type:
        state["error_type"] = error_type
    return state


async def sync_latest_report_to_doc(
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Synchronize the current report to the configured document."""
    config = FeishuSyncConfig.from_env()
    if not config.enabled:
        return {"status": "disabled"}

    snapshot: LibrarySnapshot | None = None
    try:
        config.validate()
        snapshot = load_library_snapshot(config.report_path, now=now)
        http_client = client or get_http_client()
        token = await _get_tenant_access_token(http_client, config)

        for conflict_attempt in range(2):
            blocks = await _list_document_blocks(
                http_client, config.document_id, token
            )
            target = find_snapshot_target(blocks)
            if (
                target.source_text == snapshot.source_line
                and target.count_text == snapshot.count_line
            ):
                result = {
                    "status": "skipped",
                    "report_date": snapshot.report_date,
                    "generated_at": snapshot.generated_at.isoformat(),
                    "changed": False,
                    "content_sha256": snapshot.content_sha256,
                }
                _write_state(
                    config.state_path,
                    _state_for_result(status="skipped", snapshot=snapshot),
                )
                return result

            try:
                await _update_snapshot_blocks(
                    http_client, config.document_id, token, target, snapshot
                )
            except FeishuSyncError as exc:
                if exc.status_code == 409 and conflict_attempt == 0:
                    continue
                raise

            verified = find_snapshot_target(
                await _list_document_blocks(http_client, config.document_id, token)
            )
            if (
                verified.source_text != snapshot.source_line
                or verified.count_text != snapshot.count_line
            ):
                raise FeishuSyncError("Feishu snapshot verification failed")
            result = {
                "status": "success",
                "report_date": snapshot.report_date,
                "generated_at": snapshot.generated_at.isoformat(),
                "changed": True,
                "content_sha256": snapshot.content_sha256,
            }
            _write_state(
                config.state_path,
                _state_for_result(status="success", snapshot=snapshot, changed=True),
            )
            return result

        raise FeishuSyncError("Feishu document revision conflict persisted")
    except Exception as exc:
        try:
            _write_state(
                config.state_path,
                _state_for_result(
                    status="failed",
                    snapshot=snapshot,
                    error_type=type(exc).__name__,
                ),
            )
        except Exception:
            logger.warning("[feishu_sync_state_write_failed]")
        raise
