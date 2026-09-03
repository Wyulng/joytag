import os
import json
import uuid
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from services.alignment import process_cn_longtail_word
from services.collectors.cn_ecommerce import get_cn_trending_words, get_last_collection_stats
from services import collector_state
from services.embedding import get_embeddings
from services.qdrant_store import cn_anchors_exist
from services.lineage import record_event, EVENT_START, EVENT_COMPLETE, EVENT_FAIL

logger = logging.getLogger(__name__)

_SAVE_INTERVAL = 10
_MAX_HISTORY = 1000
_RUN_LOCK = asyncio.Lock()

# 进度文件路径（基于项目根目录）
BASE_DIR = Path(__file__).parent.parent.parent
_PROGRESS_FILE = BASE_DIR / "cn_collection_progress.json"


def _load_progress() -> dict:
    if not os.path.exists(_PROGRESS_FILE):
        return {"processed_words": [], "last_time": None}
    try:
        with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"processed_words": [], "last_time": None}


def _save_progress(progress: dict):
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False)
    except Exception:
        pass


async def fetch_cn_longtail_words(collection_run_id: str | None = None):
    return await asyncio.wait_for(
        asyncio.to_thread(get_cn_trending_words, collection_run_id),
        timeout=300
    )


async def _collect_cn_generator():
    """
    采集中文锚点词。
    基于进度文件去重：只采集新词，已在进度文件中的词直接跳过。
    EU 合规改造：每轮采集生成 run_id，记录 lineage START/COMPLETE/FAIL 事件，
    词条写入 provenance（source_type=taobao_suggest/collection_run_id/collected_at）。
    """
    run_id = str(uuid.uuid4())
    record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_START)

    progress = _load_progress()
    # 使用 dict 代替 set 以保留插入顺序，确保进度切片确定性
    processed_words: dict[str, None] = {w: None for w in progress.get("processed_words", [])}
    processed_keys = {collector_state.normalize_collector_key(word) for word in processed_words}

    try:
        words_with_heat = await fetch_cn_longtail_words(run_id)
    except Exception:
        record_event(
            run_id=run_id,
            job_name="cn_collection",
            event_type=EVENT_FAIL,
            run_facets={"total": 0, "error": "fetch_cn_longtail_words failed"},
        )
        raise
    if not words_with_heat:
        record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_COMPLETE,
                     run_facets={"total": 0, "skipped": True})
        yield {"event": "done", "total": 0, "skipped": True, "duplicates": 0, "new": 0}
        return

    total = len(words_with_heat)

    observation_keys = [collector_state.normalize_collector_key(word) for word, _, _ in words_with_heat]
    observations = collector_state.get_candidate_observations("cn", "CN", observation_keys)

    duplicate_count = 0
    new_count = 0
    save_counter = 0

    try:
        # 先完成进度/观察表/Qdrant 去重，再为真正需要入库的新词批量编码。
        # 观察表在 Postgres 可用时负责跨运行的规范化去重；本地无 Postgres 时
        # 退回原有进度文件与 Qdrant 精确 ID 逻辑。
        exists_by_key: dict[str, bool] = {}
        new_words: list[str] = []
        word_by_key: dict[str, str] = {}
        qdrant_lookup_words: list[str] = []
        for cn_word, _heat, _category in words_with_heat:
            key = collector_state.normalize_collector_key(cn_word)
            if not key or key in word_by_key:
                continue
            word_by_key[key] = cn_word
            if key in processed_keys:
                exists_by_key[key] = True
                continue
            observation = observations.get(key)
            next_eligible = observation.get("next_eligible_at") if observation else None
            if observation and (
                observation.get("decision_status")
                or (next_eligible is not None and next_eligible > datetime.now(timezone.utc))
            ):
                exists_by_key[key] = True
                continue
            qdrant_lookup_words.append(cn_word)

        existing_anchor_words = cn_anchors_exist(qdrant_lookup_words)
        existing_anchor_keys = {
            collector_state.normalize_collector_key(word)
            for word in existing_anchor_words
        }
        for cn_word in qdrant_lookup_words:
            key = collector_state.normalize_collector_key(cn_word)
            exists_in_db = key in existing_anchor_keys
            exists_by_key[key] = exists_in_db
            if not exists_in_db:
                new_words.append(cn_word)
        vectors = await get_embeddings(new_words) if new_words else []
        vectors_by_key = {
            collector_state.normalize_collector_key(word): vector
            for word, vector in zip(new_words, vectors)
        }

        if not new_words:
            logger.info("[cn] 本轮候选均已处理或在观察冷却期内，跳过向量化")
            record_event(
                run_id=run_id,
                job_name="cn_collection",
                event_type=EVENT_COMPLETE,
                run_facets={
                    "total": total,
                    "skipped": True,
                    "duplicates": total,
                    **get_last_collection_stats(),
                    "embedding_words": 0,
                    "assess_calls": 0,
                },
            )
            yield {
                "event": "done",
                "total": total,
                "skipped": True,
                "duplicates": total,
                "new": 0,
                "message": "已全部采集完成（无新词），等待新词出现",
                **get_last_collection_stats(),
            }
            return

        for i, (cn_word, heat, seed_category) in enumerate(words_with_heat):
            key = collector_state.normalize_collector_key(cn_word)

            # 跳过已处理的词，包括规范化后的大小写/空白变体
            if key in processed_keys:
                duplicate_count += 1
                yield {
                    "index": i + 1,
                    "total": total,
                    "word": cn_word,
                    "heat": heat,
                    "duplicate": True,
                }
                continue

            # 使用批处理前完成的去重结果；前序重复词入库后更新状态，
            # 保持原有逐词顺序下的幂等语义。
            exists_in_db = exists_by_key.get(key, False)

            if exists_in_db:
                # 词已在数据库中
                duplicate_count += 1
            else:
                # 新词，入库（带溯源元数据）
                await process_cn_longtail_word(
                    cn_word,
                    category=seed_category,
                    provenance={
                        "source_type": "taobao_suggest",
                        "collection_run_id": run_id,
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                        "trend_score_source": "taobao_suggest_relative",
                        "trend_score_is_absolute": False,
                    },
                    collection_run_id=run_id,
                    vector=vectors_by_key[key],
                    trend_score=heat,
                    trend_score_source="taobao_suggest_relative",
                )
                new_count += 1
                exists_by_key[key] = True

            collector_state.mark_candidate_processed(
                "cn",
                "CN",
                cn_word,
                decision_status="anchor",
                run_id=run_id,
            )

            # 更新进度（每 _SAVE_INTERVAL 次写一次磁盘）
            processed_words[cn_word] = None
            processed_keys.add(key)
            save_counter += 1
            if save_counter % _SAVE_INTERVAL == 0:
                history = list(processed_words.keys())[-_MAX_HISTORY:] if len(processed_words) > _MAX_HISTORY else list(processed_words.keys())
                _save_progress({
                    "processed_words": history,
                    "last_time": datetime.now(timezone.utc).isoformat()
                })

            yield {
                "index": i + 1,
                "total": total,
                "word": cn_word,
                "heat": heat,
                "duplicate": exists_in_db,
            }
    except Exception:
        record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_FAIL,
                     run_facets={"total": total, "error": "collection interrupted"})
        raise

    logger.info(f"[cn] 完成，新增: {new_count}，重复: {duplicate_count}")
    # 最终保存进度
    history = list(processed_words.keys())[-_MAX_HISTORY:] if len(processed_words) > _MAX_HISTORY else list(processed_words.keys())
    _save_progress({
        "processed_words": history,
        "last_time": datetime.now(timezone.utc).isoformat()
    })
    source_stats = get_last_collection_stats()
    record_event(
        run_id=run_id,
        job_name="cn_collection",
        event_type=EVENT_COMPLETE,
        run_facets={
            "total": total,
            "new": new_count,
            "duplicates": duplicate_count,
            **source_stats,
            "embedding_words": len(new_words),
            "assess_calls": 0,
        },
    )
    yield {
        "event": "done",
        "total": total,
        "skipped": False,
        "duplicates": duplicate_count,
        "new": new_count,
        "run_id": run_id,
        **source_stats,
        "embedding_words": len(new_words),
        "assess_calls": 0,
    }


async def run_cn_collector():
    if _RUN_LOCK.locked():
        return {"total": 0, "approved": 0, "pending": 0, "duplicates": 0,
                "new": 0, "skipped": True, "message": "中文采集任务已在运行"}
    async with _RUN_LOCK:
        final = None
        async for p in _collect_cn_generator():
            final = p
    if final is None:
        return {"total": 0, "approved": 0, "pending": 0, "duplicates": 0, "new": 0, "skipped": False}

    if final.get("skipped"):
        return {
            "total": final["total"],
            "approved": 0,
            "pending": 0,
            "duplicates": 0,
            "new": 0,
            "skipped": True,
            "message": final.get("message", "已全部采集完成"),
        }

    return {
        "total": final["total"],
        "approved": final.get("new", 0),
        "pending": 0,
        "duplicates": final.get("duplicates", 0),
        "new": final.get("new", 0),
        "skipped": False,
    }
