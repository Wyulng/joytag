"""采集器状态与去重元数据。

该模块只保存采集元数据，不保存 embedding，也不参与业务决策本身：

* source snapshot：同一来源/国家/种子的最近响应及刷新时间；
* candidate observation：候选词跨运行的发现和处理状态；
* seed frontier：中文动态种子及其热度、轮换状态。

Postgres 可用时使用持久化表；本地没有 Postgres 时，来源快照和候选观察退化
为无缓存/无观察，保持原有采集流程可用。动态种子使用进程内状态作为开发期
fallback，因此不会因为合规数据库不可用而阻断中文采集。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable

from services import db

logger = logging.getLogger(__name__)

SOURCE_REFRESH_STEPS_HOURS: dict[str, tuple[int, ...]] = {
    "taobao_suggest": (24, 72, 168),
    "amazon_suggest": (6, 24, 72, 168),
    "ebay_suggest": (24, 72, 168),
}
SOURCE_ERROR_RETRY_HOURS = (1, 2, 4, 6)
SEED_COOLDOWN = timedelta(days=7)
FIXED_SEED_COOLDOWN = timedelta(days=1)
CANDIDATE_REPROCESS_AFTER = timedelta(days=30)

_MEMORY_LOCK = threading.RLock()
_MEMORY_SOURCE_SNAPSHOTS: dict[tuple[str, str, str], dict] = {}
_MEMORY_SEEDS: dict[tuple[str, str], dict] = {}
_WARNED_DB_FEATURES: set[str] = set()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_collector_key(value: str) -> str:
    """生成采集去重键；保留标点、连字符和重音，只做保守规范化。"""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"\s+", " ", value).casefold()


def key_sha256(value: str) -> str:
    return hashlib.sha256(normalize_collector_key(value).encode("utf-8")).hexdigest()


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _warn_db(feature: str, exc: Exception) -> None:
    with _MEMORY_LOCK:
        if feature in _WARNED_DB_FEATURES:
            return
        _WARNED_DB_FEATURES.add(feature)
    logger.warning("[collector_state] %s 持久化不可用，使用安全降级: %s", feature, exc)


def _snapshot_key(source: str, country: str, seed: str) -> tuple[str, str, str]:
    return source, country or "", key_sha256(seed)


def get_source_snapshot(source: str, country: str, seed: str) -> dict | None:
    """读取某个来源种子的最近快照；数据库不可用时不返回伪缓存。"""
    if not db.is_db_available():
        return None
    seed_hash = key_sha256(seed)
    try:
        row = db.fetchone(
            """
            SELECT response, response_hash, last_fetched_at, last_changed_at,
                   next_fetch_at, unchanged_streak, error_streak, etag, last_modified
            FROM collector_source_snapshot
            WHERE source = %s AND country = %s AND seed_hash = %s
            """,
            (source, country or "", seed_hash),
        )
    except Exception as exc:
        _warn_db("source snapshot", exc)
        return None
    if not row:
        return None
    response = _json_value(row[0])
    if not isinstance(response, list):
        response = []
    return {
        "source": source,
        "country": country or "",
        "seed_hash": seed_hash,
        "response": response,
        "response_hash": row[1] or "",
        "last_fetched_at": _parse_datetime(row[2]),
        "last_changed_at": _parse_datetime(row[3]),
        "next_fetch_at": _parse_datetime(row[4]),
        "unchanged_streak": int(row[5] or 0),
        "error_streak": int(row[6] or 0),
        "etag": row[7] or "",
        "last_modified": row[8] or "",
    }


def source_snapshot_is_fresh(snapshot: dict | None, now: datetime | None = None) -> bool:
    if not snapshot:
        return False
    next_fetch_at = _parse_datetime(snapshot.get("next_fetch_at"))
    return bool(next_fetch_at and next_fetch_at > (now or utc_now()))


def _response_hash(response: list) -> str:
    encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _next_refresh(source: str, unchanged_streak: int, now: datetime) -> datetime:
    steps = SOURCE_REFRESH_STEPS_HOURS.get(source, (24, 72, 168))
    index = min(max(unchanged_streak, 0), len(steps) - 1)
    return now + timedelta(hours=steps[index])


def save_source_snapshot(
    source: str,
    country: str,
    seed: str,
    response: list,
    *,
    fetched_at: datetime | None = None,
    etag: str = "",
    last_modified: str = "",
) -> dict:
    """保存成功响应并返回 changed/next_fetch_at 等本轮状态。"""
    now = fetched_at or utc_now()
    seed_hash = key_sha256(seed)
    response = response if isinstance(response, list) else []
    response_hash = _response_hash(response)
    previous = get_source_snapshot(source, country, seed)
    changed = not previous or previous.get("response_hash") != response_hash
    unchanged_streak = 0 if changed else int(previous.get("unchanged_streak") or 0) + 1
    next_fetch_at = _next_refresh(source, unchanged_streak, now)
    last_changed_at = now if changed else (previous.get("last_changed_at") or now)
    payload = {
        "source": source,
        "country": country or "",
        "seed_hash": seed_hash,
        "response": response,
        "response_hash": response_hash,
        "last_fetched_at": now,
        "last_changed_at": last_changed_at,
        "next_fetch_at": next_fetch_at,
        "unchanged_streak": unchanged_streak,
        "error_streak": 0,
        "etag": etag or (previous or {}).get("etag", ""),
        "last_modified": last_modified or (previous or {}).get("last_modified", ""),
    }
    if not db.is_db_available():
        return {**payload, "changed": changed}
    try:
        db.execute(
            """
            INSERT INTO collector_source_snapshot
                (source, country, seed_hash, response, response_hash,
                 last_fetched_at, last_changed_at, next_fetch_at,
                 unchanged_streak, error_streak, etag, last_modified)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, 0, %s, %s)
            ON CONFLICT (source, country, seed_hash) DO UPDATE SET
                response = EXCLUDED.response,
                response_hash = EXCLUDED.response_hash,
                last_fetched_at = EXCLUDED.last_fetched_at,
                last_changed_at = EXCLUDED.last_changed_at,
                next_fetch_at = EXCLUDED.next_fetch_at,
                unchanged_streak = EXCLUDED.unchanged_streak,
                error_streak = 0,
                etag = EXCLUDED.etag,
                last_modified = EXCLUDED.last_modified
            """,
            (
                source,
                country or "",
                seed_hash,
                json.dumps(response, ensure_ascii=False),
                response_hash,
                now,
                last_changed_at,
                next_fetch_at,
                unchanged_streak,
                payload["etag"],
                payload["last_modified"],
            ),
        )
    except Exception as exc:
        _warn_db("source snapshot", exc)
    return {**payload, "changed": changed}


def record_source_error(source: str, country: str, seed: str) -> None:
    """错误不覆盖有效响应，只缩短下一次重试间隔。"""
    if not db.is_db_available():
        return
    previous = get_source_snapshot(source, country, seed)
    if not previous:
        return
    error_streak = int(previous.get("error_streak") or 0) + 1
    retry_hours = SOURCE_ERROR_RETRY_HOURS[min(error_streak - 1, len(SOURCE_ERROR_RETRY_HOURS) - 1)]
    try:
        db.execute(
            """
            UPDATE collector_source_snapshot
            SET error_streak = %s, next_fetch_at = %s
            WHERE source = %s AND country = %s AND seed_hash = %s
            """,
            (
                error_streak,
                utc_now() + timedelta(hours=retry_hours),
                source,
                country or "",
                key_sha256(seed),
            ),
        )
    except Exception as exc:
        _warn_db("source snapshot", exc)


def get_candidate_observations(
    pipeline: str,
    country: str,
    normalized_words: Iterable[str],
) -> dict[str, dict]:
    words = [normalize_collector_key(word) for word in normalized_words if word]
    words = list(dict.fromkeys(words))
    if not words or not db.is_db_available():
        return {}
    try:
        rows = db.fetchall(
            """
            SELECT normalized_word, display_word, source_set, category,
                   first_seen_at, last_seen_at, seen_count, last_processed_at,
                   next_eligible_at, decision_status, source_heat_score,
                   last_collection_run_id
            FROM collector_candidate_observation
            WHERE pipeline = %s AND country = %s AND normalized_word = ANY(%s)
            """,
            (pipeline, country or "", words),
        )
    except Exception as exc:
        _warn_db("candidate observation", exc)
        return {}
    result = {}
    for row in rows:
        source_set = row[2] or []
        if isinstance(source_set, str):
            source_set = [item for item in source_set.split(",") if item]
        result[row[0]] = {
            "normalized_word": row[0],
            "display_word": row[1],
            "source_set": list(source_set),
            "category": row[3],
            "first_seen_at": _parse_datetime(row[4]),
            "last_seen_at": _parse_datetime(row[5]),
            "seen_count": int(row[6] or 0),
            "last_processed_at": _parse_datetime(row[7]),
            "next_eligible_at": _parse_datetime(row[8]),
            "decision_status": row[9],
            "source_heat_score": float(row[10] or 0.0),
            "last_collection_run_id": row[11],
        }
    return result


def observe_candidates(
    pipeline: str,
    country: str,
    candidates: Iterable[dict],
    *,
    source: str,
    run_id: str | None = None,
    observed_at: datetime | None = None,
) -> dict[str, int | bool | str | None]:
    """批量记录候选词并返回不含原文的写入摘要。"""
    now = observed_at or utc_now()
    rows = []
    for candidate in candidates:
        word = candidate.get("word")
        normalized = normalize_collector_key(word)
        if not normalized:
            continue
        source_set = candidate.get("source_set") or [source]
        rows.append((
            pipeline,
            country or "",
            normalized,
            word,
            json.dumps(list(dict.fromkeys(source_set)), ensure_ascii=False),
            candidate.get("category"),
            now,
            float(candidate.get("source_heat_score") or candidate.get("heat") or 0.0),
            run_id or "",
        ))
    if not rows:
        return {
            "attempted": 0,
            "written": 0,
            "write_failed": False,
            "error_type": None,
        }
    if not db.is_db_available():
        logger.warning(
            "[collector_candidate_observation_write_failed] pipeline=%s country=%s "
            "attempted=%d error_type=database_unavailable",
            pipeline,
            country or "",
            len(rows),
        )
        return {
            "attempted": len(rows),
            "written": 0,
            "write_failed": True,
            "error_type": "database_unavailable",
        }
    try:
        db.execute_many(
            """
            INSERT INTO collector_candidate_observation
                (pipeline, country, normalized_word, display_word, source_set,
                 category, first_seen_at, last_seen_at, seen_count,
                 source_heat_score, last_collection_run_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, 1, %s, %s)
            ON CONFLICT (pipeline, country, normalized_word) DO UPDATE SET
                display_word = CASE
                    WHEN EXCLUDED.source_heat_score > collector_candidate_observation.source_heat_score
                    THEN EXCLUDED.display_word
                    ELSE collector_candidate_observation.display_word
                END,
                source_set = (
                    SELECT jsonb_agg(DISTINCT value)
                    FROM jsonb_array_elements(
                        collector_candidate_observation.source_set || EXCLUDED.source_set
                    )
                ),
                category = COALESCE(collector_candidate_observation.category, EXCLUDED.category),
                last_seen_at = EXCLUDED.last_seen_at,
                seen_count = collector_candidate_observation.seen_count + 1,
                source_heat_score = GREATEST(
                    collector_candidate_observation.source_heat_score,
                    EXCLUDED.source_heat_score
                ),
                last_collection_run_id = EXCLUDED.last_collection_run_id
            """,
            rows,
        )
        return {
            "attempted": len(rows),
            "written": len(rows),
            "write_failed": False,
            "error_type": None,
        }
    except Exception as exc:
        _warn_db("candidate observation", exc)
        logger.warning(
            "[collector_candidate_observation_write_failed] pipeline=%s country=%s "
            "attempted=%d error_type=%s",
            pipeline,
            country or "",
            len(rows),
            type(exc).__name__,
        )
        return {
            "attempted": len(rows),
            "written": 0,
            "write_failed": True,
            "error_type": type(exc).__name__,
        }


def mark_candidate_processed(
    pipeline: str,
    country: str,
    word: str,
    *,
    decision_status: str,
    run_id: str | None = None,
    processed_at: datetime | None = None,
) -> None:
    if not db.is_db_available():
        return
    now = processed_at or utc_now()
    try:
        db.execute(
            """
            UPDATE collector_candidate_observation
            SET last_processed_at = %s,
                next_eligible_at = %s,
                decision_status = %s,
                last_collection_run_id = COALESCE(NULLIF(%s, ''), last_collection_run_id)
            WHERE pipeline = %s AND country = %s AND normalized_word = %s
            """,
            (
                now,
                now + CANDIDATE_REPROCESS_AFTER,
                decision_status,
                run_id or "",
                pipeline,
                country or "",
                normalize_collector_key(word),
            ),
        )
    except Exception as exc:
        _warn_db("candidate observation", exc)


def clear_candidate_observation(pipeline: str, country: str, word: str) -> None:
    """管理员删除业务记录后允许该词重新进入采集。"""
    if not db.is_db_available():
        return
    try:
        db.execute(
            """
            UPDATE collector_candidate_observation
            SET decision_status = NULL, last_processed_at = NULL, next_eligible_at = %s
            WHERE pipeline = %s AND country = %s AND normalized_word = %s
            """,
            (utc_now(), pipeline, country or "", normalize_collector_key(word)),
        )
    except Exception as exc:
        _warn_db("candidate observation", exc)


def upsert_seed_frontier(
    seed_word: str,
    *,
    seed_kind: str,
    category: str | None = None,
    parent_seed: str | None = None,
    seed_depth: int = 0,
    source_heat_score: float = 0.0,
    seen_at: datetime | None = None,
) -> None:
    normalized = normalize_collector_key(seed_word)
    if not normalized:
        return
    now = seen_at or utc_now()
    depth = max(0, int(seed_depth))
    key = ("cn", normalized)
    with _MEMORY_LOCK:
        existing = _MEMORY_SEEDS.get(key)
        if existing is None:
            _MEMORY_SEEDS[key] = {
                "pipeline": "cn",
                "seed_word": seed_word,
                "normalized_seed": normalized,
                "seed_kind": seed_kind,
                "category": category,
                "parent_seed": parent_seed,
                "seed_depth": depth,
                "source_heat_score": float(source_heat_score or 0.0),
                "first_seen_at": now,
                "last_seen_at": now,
                "last_queried_at": None,
                "next_query_at": None,
                "query_count": 0,
                "active": True,
            }
        else:
            existing["last_seen_at"] = now
            existing["source_heat_score"] = max(
                float(existing.get("source_heat_score") or 0.0),
                float(source_heat_score or 0.0),
            )
            if not existing.get("category") and category:
                existing["category"] = category
            if existing.get("seed_kind") != "fixed" and seed_kind == "fixed":
                existing["seed_kind"] = "fixed"
            if existing.get("parent_seed") is None and parent_seed:
                existing["parent_seed"] = parent_seed
            existing["seed_depth"] = min(int(existing.get("seed_depth") or depth), depth)
    if not db.is_db_available():
        return
    try:
        db.execute(
            """
            INSERT INTO collector_seed_frontier
                (pipeline, normalized_seed, seed_word, seed_kind, category,
                 parent_seed, seed_depth, source_heat_score, first_seen_at,
                 last_seen_at, active)
            VALUES ('cn', %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
            ON CONFLICT (pipeline, normalized_seed) DO UPDATE SET
                seed_word = EXCLUDED.seed_word,
                seed_kind = CASE
                    WHEN collector_seed_frontier.seed_kind = 'fixed' THEN 'fixed'
                    ELSE EXCLUDED.seed_kind
                END,
                category = COALESCE(collector_seed_frontier.category, EXCLUDED.category),
                parent_seed = COALESCE(collector_seed_frontier.parent_seed, EXCLUDED.parent_seed),
                seed_depth = LEAST(collector_seed_frontier.seed_depth, EXCLUDED.seed_depth),
                source_heat_score = GREATEST(
                    collector_seed_frontier.source_heat_score,
                    EXCLUDED.source_heat_score
                ),
                last_seen_at = EXCLUDED.last_seen_at,
                active = true
            """,
            (
                normalized,
                seed_word,
                seed_kind,
                category,
                parent_seed,
                depth,
                float(source_heat_score or 0.0),
                now,
                now,
            ),
        )
    except Exception as exc:
        _warn_db("seed frontier", exc)


def list_seed_frontier() -> list[dict]:
    if db.is_db_available():
        try:
            rows = db.fetchall(
                """
                SELECT normalized_seed, seed_word, seed_kind, category, parent_seed,
                       seed_depth, source_heat_score, first_seen_at, last_seen_at,
                       last_queried_at, next_query_at, query_count, active
                FROM collector_seed_frontier
                WHERE pipeline = 'cn' AND active = true
                """
            )
            return [
                {
                    "normalized_seed": row[0], "seed_word": row[1], "seed_kind": row[2],
                    "category": row[3], "parent_seed": row[4], "seed_depth": int(row[5] or 0),
                    "source_heat_score": float(row[6] or 0.0),
                    "first_seen_at": _parse_datetime(row[7]), "last_seen_at": _parse_datetime(row[8]),
                    "last_queried_at": _parse_datetime(row[9]), "next_query_at": _parse_datetime(row[10]),
                    "query_count": int(row[11] or 0), "active": bool(row[12]),
                }
                for row in rows
            ]
        except Exception as exc:
            _warn_db("seed frontier", exc)
    with _MEMORY_LOCK:
        return [dict(value) for value in _MEMORY_SEEDS.values() if value.get("active", True)]


def mark_seed_queried(
    seed_word: str,
    *,
    seed_kind: str | None = None,
    queried_at: datetime | None = None,
) -> None:
    normalized = normalize_collector_key(seed_word)
    if not normalized:
        return
    now = queried_at or utc_now()
    # 动态种子需要较长冷却，固定 bootstrap 种子保留每日轮换能力。
    if seed_kind is None:
        with _MEMORY_LOCK:
            item = _MEMORY_SEEDS.get(("cn", normalized))
            if item is not None:
                seed_kind = item.get("seed_kind")
    next_query = now + (FIXED_SEED_COOLDOWN if seed_kind == "fixed" else SEED_COOLDOWN)
    with _MEMORY_LOCK:
        item = _MEMORY_SEEDS.get(("cn", normalized))
        if item is not None:
            item["last_queried_at"] = now
            item["next_query_at"] = next_query
            item["query_count"] = int(item.get("query_count") or 0) + 1
    if not db.is_db_available():
        return
    try:
        db.execute(
            """
            UPDATE collector_seed_frontier
            SET last_queried_at = %s, next_query_at = %s, query_count = query_count + 1
            WHERE pipeline = 'cn' AND normalized_seed = %s
            """,
            (now, next_query, normalized),
        )
    except Exception as exc:
        _warn_db("seed frontier", exc)


def prune_seed_frontier(now: datetime | None = None) -> None:
    """停用长期未出现且低热度的动态种子；固定种子永不清理。"""
    cutoff = (now or utc_now()) - timedelta(days=90)
    if db.is_db_available():
        try:
            db.execute(
                """
                UPDATE collector_seed_frontier
                SET active = false
                WHERE pipeline = 'cn' AND seed_kind <> 'fixed'
                  AND last_seen_at < %s AND source_heat_score < 0.45
                """,
                (cutoff,),
            )
            return
        except Exception as exc:
            _warn_db("seed frontier", exc)
    with _MEMORY_LOCK:
        for item in _MEMORY_SEEDS.values():
            if item.get("seed_kind") != "fixed" and item.get("last_seen_at"):
                if item["last_seen_at"] < cutoff and float(item.get("source_heat_score") or 0) < 0.45:
                    item["active"] = False


def trim_seed_frontier(max_dynamic: int = 2000) -> None:
    """限制动态 frontier 规模，固定 bootstrap 种子永不计入淘汰额度。"""
    max_dynamic = max(0, int(max_dynamic))
    if db.is_db_available():
        try:
            db.execute(
                """
                UPDATE collector_seed_frontier
                SET active = false
                WHERE pipeline = 'cn' AND seed_kind <> 'fixed' AND active = true
                  AND normalized_seed NOT IN (
                      SELECT normalized_seed
                      FROM collector_seed_frontier
                      WHERE pipeline = 'cn' AND seed_kind <> 'fixed' AND active = true
                      ORDER BY source_heat_score DESC, last_seen_at DESC, normalized_seed
                      LIMIT %s
                  )
                """,
                (max_dynamic,),
            )
            return
        except Exception as exc:
            _warn_db("seed frontier trim", exc)
    with _MEMORY_LOCK:
        dynamic = [
            item for item in _MEMORY_SEEDS.values()
            if item.get("seed_kind") != "fixed" and item.get("active", True)
        ]
        dynamic.sort(key=lambda item: (
            -float(item.get("source_heat_score") or 0.0),
            -(_parse_datetime(item.get("last_seen_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            item.get("normalized_seed") or "",
        ))
        for item in dynamic[max_dynamic:]:
            item["active"] = False


def bootstrap_seed_frontier_from_qdrant(limit: int = 200) -> None:
    """把已有中文锚点作为动态 seed；失败不阻断采集。"""
    try:
        from services.collectors.seed_builder import get_recent_anchor_words

        for word, category in get_recent_anchor_words(limit=limit):
            upsert_seed_frontier(
                word,
                seed_kind="anchor",
                category=category,
                seed_depth=0,
                source_heat_score=0.5,
            )
    except Exception as exc:
        logger.debug("[collector_state] 中文锚点 seed frontier 回填跳过: %s", exc)
