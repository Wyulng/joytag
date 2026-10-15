"""Daily aggregate collection and library reports.

The report is deliberately read-only with respect to business data.  It reads
Qdrant and Postgres state, keeps a small bounded sample from each collection,
and writes an atomic JSON/Markdown snapshot for the local SSH pull job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION = "1.1"
REPORT_TIMEZONE_NAME = "Asia/Shanghai"
REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
REPORT_RETENTION_DAYS = 30
SAMPLE_LIMIT = 10
COLLECTION_NAMES = (
    "cn_anchors",
    "local_tags",
    "pending_review",
    "blocked_decisions",
)


def _default_report_dir() -> Path:
    configured = os.getenv("JOYTAG_REPORT_DIR")
    if configured:
        return Path(configured)
    if Path("/app").exists():
        return Path("/app/reports")
    return Path(__file__).resolve().parents[1] / "data" / "reports"


def get_report_dir() -> Path:
    """Return the runtime report directory without creating it."""
    return _default_report_dir()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(item) for key, item in value.items()}
    return str(value)


def _source(payload: dict[str, Any]) -> str:
    return str(payload.get("source") or payload.get("source_type") or "unknown")


def _status(collection_name: str, payload: dict[str, Any]) -> str:
    if collection_name == "local_tags":
        return str(payload.get("compliance_status") or "unknown")
    if collection_name == "pending_review":
        return str(payload.get("action") or "pending")
    if collection_name == "blocked_decisions":
        return str(payload.get("action") or "blocked")
    return "anchor"


def _sample(collection_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    word = payload.get("cn_word") if collection_name == "cn_anchors" else payload.get("word")
    if not word:
        return None

    if collection_name == "cn_anchors":
        return {
            "word": str(word),
            "category": payload.get("category"),
            "trend_score": payload.get("trend_score"),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
        }

    return {
        "word": str(word),
        "country": payload.get("country"),
        "source": _source(payload),
        "status": _status(collection_name, payload),
        "similarity": payload.get("similarity"),
        "anchor_cn_word": payload.get("anchor_cn_word"),
        "rule_id": payload.get("rule_id") or payload.get("rule_ids"),
        "trend_score": payload.get("trend_score"),
        "updated_at": payload.get("updated_at") or payload.get("created_at"),
    }


def _keep_sample(samples: list[dict[str, Any]], candidate: dict[str, Any] | None) -> None:
    if candidate is None:
        return
    samples.append(candidate)
    samples.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("word") or ""),
        ),
        reverse=True,
    )
    del samples[SAMPLE_LIMIT:]


def _dimension(vectors: Any) -> int | None:
    size = getattr(vectors, "size", None)
    if size is not None:
        return int(size)
    if isinstance(vectors, dict):
        for value in vectors.values():
            size = getattr(value, "size", None)
            if size is not None:
                return int(size)
    return None


def _qdrant_summary_sync() -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[str]]:
    from services.qdrant_store import _iter_scroll, get_qdrant_client

    client = get_qdrant_client()
    library: dict[str, Any] = {}
    samples: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []

    for collection_name in COLLECTION_NAMES:
        info = client.get_collection(collection_name)
        dimension = _dimension(info.config.params.vectors)
        if dimension != 768:
            warnings.append(f"qdrant_dimension_invalid:{collection_name}")

        countries: Counter[str] = Counter()
        sources: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        sample_items: list[dict[str, Any]] = []
        anchor_linked = 0

        for record in _iter_scroll(
            collection_name=collection_name,
            payload_keys=None,
            batch_size=500,
        ):
            payload = record.payload or {}
            country = payload.get("country")
            if country:
                countries[str(country)] += 1
            if collection_name != "cn_anchors":
                sources[_source(payload)] += 1
                statuses[_status(collection_name, payload)] += 1
            if collection_name == "local_tags" and payload.get("anchor_cn_word"):
                anchor_linked += 1
            _keep_sample(sample_items, _sample(collection_name, payload))

        entry = {
            "count": client.count(collection_name=collection_name, exact=True).count,
            "dimension": dimension,
            "countries": dict(sorted(countries.items())),
            "sources": dict(sorted(sources.items())),
            "statuses": dict(sorted(statuses.items())),
        }
        if collection_name == "local_tags":
            entry["anchor_linked"] = anchor_linked
        library[collection_name] = entry
        samples[collection_name] = sample_items

    return library, samples, warnings


def _safe_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "total", "approved", "pending", "rejected", "duplicates", "new",
        "skipped", "embedding_words", "assess_calls", "source_requests",
        "source_cache_hits", "source_errors", "source_response_changes",
        "seed_queries", "dynamic_seeds", "fixed_seeds", "raw_candidates",
        "unique_candidates", "candidate_observations",
        "candidate_observations_backfilled", "candidate_observation_write_failed",
        "candidate_observation_error_type",
        "eligible_before_qdrant", "qdrant_existing_filtered", "selected_candidates",
        "active_dynamic_seeds", "frontier_trimmed", "qdrant_existing_filter_failed",
        "source_429", "source_success_rate", "source_429_rate", "request_budget",
        "healthy_run_streak", "cross_source_duplicates", "depth2_seeds",
        "source_list", "source_metrics", "country_category_metrics",
        "new_anchors_by_source",
        "anchor_matches", "candidate_observations_written", "per_country_cap",
        "expansion_metrics_version",
    }

    return {
        key: _scalar(item)
        for key, item in value.items()
        if key in allowed
        and (isinstance(item, (str, int, float, bool)) or key in {
            "source_list", "source_metrics", "country_category_metrics", "new_anchors_by_source"
        })
    }


def _lineage_summary(rows: list[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    latest_run: dict[str, str] = {}
    for job_name, run_id, _event_type, _ts in rows:
        if job_name not in latest_run:
            latest_run[job_name] = run_id

    result: dict[str, dict[str, Any]] = {}
    for job_name, run_id in latest_run.items():
        events = [row for row in rows if row[0] == job_name and row[1] == run_id]
        event_types = [str(row[2]) for row in events]
        timestamps = [row[3] for row in events]
        complete = (
            "START" in event_types
            and "OUTPUT" in event_types
            and "COMPLETE" in event_types
            and "FAIL" not in event_types
        )
        result[job_name] = {
            "event_types": sorted(set(event_types)),
            "started_at": _iso(min(timestamps)) if timestamps else None,
            "finished_at": _iso(max(timestamps)) if timestamps else None,
            "complete": complete,
        }
    return result


def _db_summary_sync() -> dict[str, Any]:
    from services.db import fetchall, is_db_available

    if not is_db_available():
        raise RuntimeError("postgres_unavailable")

    auto_rows = fetchall(
        "SELECT DISTINCT ON (action) action, ts, detail "
        "FROM audit_log "
        "WHERE action IN ('collect.cn.auto', 'collect.overseas.auto') "
        "ORDER BY action, ts DESC"
    )
    auto_runs: dict[str, Any] = {}
    for action, timestamp, detail in auto_rows:
        safe_detail = detail if isinstance(detail, dict) else {}
        result = safe_detail.get("result") if isinstance(safe_detail, dict) else {}
        auto_runs[action] = {
            "status": safe_detail.get("status", "unknown"),
            "finished_at": _iso(timestamp),
            "result": _safe_result(result),
        }

    lineage_rows = fetchall(
        "SELECT job_name, run_id, event_type, ts "
        "FROM lineage_event "
        "WHERE job_name IN ('cn_collection', 'overseas_collection') "
        "ORDER BY ts DESC LIMIT 5000"
    )
    lineage = _lineage_summary(lineage_rows)

    llm_rows = fetchall(
        "SELECT call_type, count(*), coalesce(sum(retry_count), 0), "
        "count(*) FILTER (WHERE error IS NOT NULL) "
        ", coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) "
        "FROM llm_trace "
        "WHERE ts >= now() - interval '24 hours' "
        "GROUP BY call_type ORDER BY call_type"
    )
    llm = {
        "window_hours": 24,
        "by_type": {
            str(call_type): {
                "count": int(count),
                "retry_count": int(retries),
                "error_count": int(errors),
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
            }
            for call_type, count, retries, errors, input_tokens, output_tokens in llm_rows
        },
    }

    def optional_rate(name: str) -> float | None:
        try:
            value = float(os.getenv(name, ""))
            return value if math.isfinite(value) and value >= 0 else None
        except (TypeError, ValueError):
            return None

    input_rate = optional_rate("LLM_INPUT_COST_PER_1M_USD")
    output_rate = optional_rate("LLM_OUTPUT_COST_PER_1M_USD")
    pricing_configured = input_rate is not None and output_rate is not None
    context_rows = fetchall(
        "SELECT country, category, call_type, count(*), "
        "coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) "
        "FROM llm_trace WHERE ts >= now() - interval '24 hours' "
        "GROUP BY country, category, call_type ORDER BY country, category, call_type"
    )
    llm_context: dict[str, dict[str, Any]] = {}
    estimated_total = 0.0
    for country, category, call_type, count, input_tokens, output_tokens in context_rows:
        country_key = str(country or "未分类")
        category_key = str(category or "未分类")
        context = llm_context.setdefault(country_key, {}).setdefault(category_key, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0 if pricing_configured else None,
            "by_type": {},
        })
        input_count = int(input_tokens or 0)
        output_count = int(output_tokens or 0)
        context["calls"] += int(count or 0)
        context["input_tokens"] += input_count
        context["output_tokens"] += output_count
        context["by_type"][str(call_type)] = int(count or 0)
        if pricing_configured:
            estimated = (input_count * input_rate + output_count * output_rate) / 1_000_000
            context["estimated_cost_usd"] += estimated
            estimated_total += estimated
    llm.update({
        "by_country_category": llm_context,
        "pricing_configured": pricing_configured,
        "estimated_cost_usd": round(estimated_total, 8) if pricing_configured else None,
        "cost_rate_unit": "USD per 1M tokens",
    })

    state_rows = fetchall(
        "SELECT 'source_snapshot_total', count(*) FROM collector_source_snapshot "
        "UNION ALL SELECT 'source_snapshot_recent', count(*) "
        "FROM collector_source_snapshot WHERE last_fetched_at >= now() - interval '24 hours' "
        "UNION ALL SELECT 'candidate_observation_total', count(*) "
        "FROM collector_candidate_observation "
        "UNION ALL SELECT 'candidate_observation_recent', count(*) "
        "FROM collector_candidate_observation WHERE last_seen_at >= now() - interval '24 hours' "
        "UNION ALL SELECT 'seed_frontier_total', count(*) FROM collector_seed_frontier "
        "UNION ALL SELECT 'seed_frontier_queried_recent', count(*) "
        "FROM collector_seed_frontier WHERE last_queried_at >= now() - interval '24 hours'"
    )
    collector_state = {str(key): int(value) for key, value in state_rows}

    return {
        "auto_runs": auto_runs,
        "lineage": lineage,
        "llm": llm,
        "collector_state": collector_state,
    }


def _merge_collection_status(
    scheduler_status: dict[str, Any],
    db_summary: dict[str, Any],
) -> dict[str, Any]:
    job_by_type = {
        job.get("task_type"): job
        for job in scheduler_status.get("jobs", [])
        if isinstance(job, dict)
    }
    auto_runs = db_summary.get("auto_runs", {})
    lineage = db_summary.get("lineage", {})
    result: dict[str, Any] = {}
    for task_type, job_name, action in (
        ("cn", "cn_collection", "collect.cn.auto"),
        ("overseas", "overseas_collection", "collect.overseas.auto"),
    ):
        result[task_type] = {
            "scheduler": job_by_type.get(task_type, {}),
            "last_auto_run": auto_runs.get(action),
            "lineage": lineage.get(job_name),
        }
    return result


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Joytag 每日采集与词库日报（{report['report_date']}）",
        "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 时区：`{report['timezone']}`",
        f"- 状态：`{report['status']}`",
        "",
        "## 自动采集",
        "",
        "| 类型 | 调度状态 | 最近运行 | lineage |",
        "|---|---|---|---|",
    ]
    for task_type, label in (("cn", "中文"), ("overseas", "海外")):
        item = report.get("collections", {}).get(task_type, {})
        scheduler = item.get("scheduler", {})
        auto_run = item.get("last_auto_run") or {}
        lineage = item.get("lineage") or {}
        result = auto_run.get("result") or {}
        summary = ", ".join(f"{key}={value}" for key, value in result.items()) or "无"
        lines.append(
            f"| {label} | {scheduler.get('last_status', 'unknown')} | "
            f"{auto_run.get('status', 'unknown')}（{summary}） | "
            f"{'complete' if lineage.get('complete') else 'incomplete'} |"
        )

    lines.extend([
        "",
        "## 当前词库",
        "",
        "| 集合 | 数量 | 维度 |",
        "|---|---:|---:|",
    ])
    for collection_name in COLLECTION_NAMES:
        item = report.get("library", {}).get(collection_name, {})
        lines.append(
            f"| `{collection_name}` | {item.get('count', '–')} | {item.get('dimension', '–')} |"
        )

    lines.extend(["", "## 采集状态", ""])
    for key, value in (report.get("collector_state") or {}).items():
        lines.append(f"- `{key}`：{value}")

    cn_auto_run = (
        (report.get("collections") or {}).get("cn") or {}
    ).get("last_auto_run") or {}
    cn_result = cn_auto_run.get("result") or {}
    source_metrics = cn_result.get("source_metrics") or {}
    lines.extend([
        "",
        "## 中文来源与请求质量（最近一次运行）",
        "",
        "| 来源 | 外发请求 | 成功率 | 429率 | 缓存命中 | 响应变化 | 错误 | 429 | 原始候选 | 唯一候选 | 跨源重复 | Qdrant 已有 | 选入锚点 | 新增锚点 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    if source_metrics:
        for source, value in sorted(source_metrics.items()):
            lines.append(
                f"| `{source}` | {value.get('requests', 0) + value.get('errors', 0)} | "
                f"{value.get('success_rate', 0):.1%} | {value.get('http_429_rate', 0):.1%} | "
                f"{value.get('cache_hits', 0)} | {value.get('response_changes', 0)} | "
                f"{value.get('errors', 0)} | {value.get('http_429', 0)} | "
                f"{value.get('raw_candidates', 0)} | {value.get('unique_candidates', 0)} | "
                f"{value.get('cross_source_duplicates', 0)} | {value.get('qdrant_existing', 0)} | "
                f"{value.get('selected_candidates', 0)} | {value.get('new_anchors', 0)} |"
            )
        lines.append(
            f"\n- 当轮预算：{cn_result.get('request_budget', '–')}；"
            f"已升档健康运行数：{cn_result.get('healthy_run_streak', 0)}；"
            f"跨源重复候选：{cn_result.get('cross_source_duplicates', 0)}；"
            f"Qdrant 已有锚点过滤：{cn_result.get('qdrant_existing_filtered', 0)}；"
            f"本轮新增锚点：{cn_result.get('new', 0)}"
        )
    else:
        lines.append("| – | 0 | 0.0% | 0.0% | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |")

    overseas_auto_run = (
        (report.get("collections") or {}).get("overseas") or {}
    ).get("last_auto_run") or {}
    overseas_result = overseas_auto_run.get("result") or {}
    country_metrics = overseas_result.get("country_category_metrics") or {}
    llm_report = report.get("llm") or {}
    llm_context = llm_report.get("by_country_category", {})
    llm_pricing_configured = bool(llm_report.get("pricing_configured"))
    lines.extend([
        "",
        "## 海外国家与类目处理结果（最近一次运行）",
        "",
        "| 国家 | 类目 | 候选 | 锚点匹配 | 可复用 | 待审核 | 拦截 | LLM 调用 | 估算费用 (USD) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    rendered_rows = 0
    for country, categories in sorted(country_metrics.items()):
        for category, value in sorted(categories.items()):
            llm_value = llm_context.get(country, {}).get(category, {})
            cost = llm_value.get("estimated_cost_usd")
            cost_text = (
                f"{cost if isinstance(cost, (int, float)) else 0.0:.6f}"
                if llm_pricing_configured
                else "未配置单价"
            )
            lines.append(
                f"| {country} | {category} | {value.get('candidates', 0)} | "
                f"{value.get('anchor_matches', 0)} | {value.get('reusable', 0)} | "
                f"{value.get('pending_review', 0)} | {value.get('blocked', 0)} | "
                f"{llm_value.get('calls', value.get('llm_calls', 0))} | {cost_text} |"
            )
            rendered_rows += 1
    if not rendered_rows:
        lines.append("| – | – | 0 | 0 | 0 | 0 | 0 | 0 | 未配置单价 |")
    lines.append(
        f"\n- 海外每国合并上限：{overseas_result.get('per_country_cap', '–')}；"
        f"候选观察写入失败：{overseas_result.get('candidate_observation_write_failed', 0)}"
    )

    lines.extend(["", "## 最近 24 小时 LLM 调用", ""])
    llm_by_type = (report.get("llm") or {}).get("by_type", {})
    if llm_by_type:
        for call_type, value in llm_by_type.items():
            lines.append(
                f"- `{call_type}`：{value.get('count', 0)} 次，"
                f"重试 {value.get('retry_count', 0)} 次，"
                f"错误 {value.get('error_count', 0)} 次"
            )
    else:
        lines.append("- 无 LLM 调用记录")
    llm_summary = report.get("llm") or {}
    lines.append(
        f"- 24 小时估算费用：{llm_summary.get('estimated_cost_usd') if llm_summary.get('pricing_configured') else '未配置单价'}"
    )

    lines.extend(["", "## 词条样例", ""])
    for collection_name in COLLECTION_NAMES:
        lines.append(f"### `{collection_name}`")
        items = (report.get("samples") or {}).get(collection_name, [])
        if not items:
            lines.append("- 无样例")
            continue
        for item in items:
            word = str(item.get("word") or "").replace("|", "\\|").replace("\n", " ")
            country = item.get("country") or ""
            source = item.get("source") or ""
            status = item.get("status") or ""
            lines.append(f"- `{word}` {country} {source} {status}".strip())

    warnings = report.get("warnings") or []
    errors = report.get("errors") or []
    lines.extend(["", "## 异常与提醒", ""])
    if not warnings and not errors:
        lines.append("- 无")
    else:
        for item in [*errors, *warnings]:
            lines.append(f"- `{item}`")
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _cleanup_old_reports(report_dir: Path, report_date: date) -> None:
    cutoff = report_date - timedelta(days=REPORT_RETENTION_DAYS)
    for path in report_dir.glob("daily-*"):
        if path.suffix not in {".json", ".md"}:
            continue
        try:
            date_text = path.stem.removeprefix("daily-")
            file_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("[daily_report_cleanup_failed] error_type=%s", type(exc).__name__)


def _write_report_files(report: dict[str, Any]) -> dict[str, str]:
    report_dir = get_report_dir()
    report_date = datetime.strptime(report["report_date"], "%Y-%m-%d").date()
    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown_text = _markdown(report)
    date_stem = f"daily-{report['report_date']}"
    json_path = report_dir / f"{date_stem}.json"
    markdown_path = report_dir / f"{date_stem}.md"
    _write_atomic(json_path, json_text)
    _write_atomic(markdown_path, markdown_text)
    _write_atomic(report_dir / "latest.json", json_text)
    _write_atomic(report_dir / "latest.md", markdown_text)
    _cleanup_old_reports(report_dir, report_date)
    return {
        "json": json_path.name,
        "markdown": markdown_path.name,
    }


async def generate_daily_report(now: datetime | None = None) -> dict[str, Any]:
    """Build and atomically persist one aggregate report."""
    now_local = now.astimezone(REPORT_TIMEZONE) if now else datetime.now(REPORT_TIMEZONE)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_date": now_local.date().isoformat(),
        "timezone": REPORT_TIMEZONE_NAME,
        "generated_at": now_local.isoformat(),
        "status": "success",
        "collections": {},
        "library": {},
        "llm": {},
        "collector_state": {},
        "samples": {},
        "warnings": [],
        "errors": [],
    }

    try:
        from services.task_scheduler import get_collection_status

        scheduler_status = get_collection_status()
    except Exception as exc:
        scheduler_status = {"timezone": REPORT_TIMEZONE_NAME, "jobs": []}
        report["errors"].append(f"scheduler_status_error:{type(exc).__name__}")

    try:
        library, samples, warnings = await asyncio.to_thread(_qdrant_summary_sync)
        report["library"] = library
        report["samples"] = samples
        report["warnings"].extend(warnings)
    except Exception as exc:
        report["errors"].append(f"qdrant_summary_error:{type(exc).__name__}")

    try:
        db_summary = await asyncio.to_thread(_db_summary_sync)
        report["collections"] = _merge_collection_status(scheduler_status, db_summary)
        report["llm"] = db_summary["llm"]
        report["collector_state"] = db_summary["collector_state"]

        for job_name, item in db_summary.get("lineage", {}).items():
            if not item.get("complete"):
                report["warnings"].append(f"lineage_incomplete:{job_name}")
        if db_summary["collector_state"].get("candidate_observation_total", 0) == 0:
            report["warnings"].append("collector_candidate_observation_empty")
        cn_result = (
            db_summary.get("auto_runs", {})
            .get("collect.cn.auto", {})
            .get("result", {})
        )
        if cn_result.get("candidate_observation_write_failed"):
            report["warnings"].append("collector_candidate_observation_write_failed")
        if cn_result.get("qdrant_existing_filter_failed"):
            report["warnings"].append("cn_anchor_prefilter_failed")
        if int(cn_result.get("source_requests") or 0) > 0:
            if float(cn_result.get("source_success_rate") or 0.0) < 0.95:
                report["warnings"].append("cn_source_success_rate_below_95_percent")
            if float(cn_result.get("source_429_rate") or 0.0) > 0.01:
                report["warnings"].append("cn_source_429_rate_above_1_percent")
        overseas_result = (
            db_summary.get("auto_runs", {})
            .get("collect.overseas.auto", {})
            .get("result", {})
        )
        if int(overseas_result.get("candidate_observation_write_failed") or 0) > 0:
            report["warnings"].append("overseas_candidate_observation_write_failed")
        if int(cn_result.get("active_dynamic_seeds") or 0) > 2000:
            report["warnings"].append("cn_dynamic_seed_frontier_exceeded")
        rerank_count = db_summary["llm"].get("by_type", {}).get("rerank", {}).get("count", 0)
        if os.getenv("RECOMMEND_RERANK_MODE", "vector").strip().lower() == "vector" and rerank_count:
            report["warnings"].append("unexpected_rerank_trace_in_vector_mode")
    except Exception as exc:
        report["errors"].append(f"postgres_summary_error:{type(exc).__name__}")
        report["collections"] = _merge_collection_status(scheduler_status, {})

    for task_type in ("cn", "overseas"):
        item = report.get("collections", {}).get(task_type, {})
        scheduler_item = item.get("scheduler") or {}
        last_status = scheduler_item.get("last_status")
        if last_status in {"failed", "skipped"}:
            report["warnings"].append(f"collection_{task_type}_{last_status}")
        lineage = item.get("lineage") or {}
        if not lineage:
            report["warnings"].append(f"lineage_missing:{task_type}")
        elif not lineage.get("complete"):
            report["warnings"].append(f"lineage_incomplete:{task_type}")

    report["warnings"] = sorted(set(report["warnings"]))
    report["errors"] = sorted(set(report["errors"]))
    if report["warnings"] or report["errors"]:
        report["status"] = "degraded"

    try:
        report["files"] = await asyncio.to_thread(_write_report_files, report)
    except Exception as exc:
        logger.error("[daily_report_write_failed] error_type=%s", type(exc).__name__)
        raise

    logger.info(
        "[daily_report_completed] report_date=%s status=%s warnings=%d errors=%d",
        report["report_date"],
        report["status"],
        len(report["warnings"]),
        len(report["errors"]),
    )
    return report
