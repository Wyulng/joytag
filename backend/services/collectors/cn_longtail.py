import os
import json
import uuid
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from services.alignment import process_cn_longtail_word
from services.collectors.cn_ecommerce import get_cn_trending_words
from services.qdrant_store import cn_anchor_exists
from services.lineage import record_event, EVENT_START, EVENT_COMPLETE, EVENT_FAIL

logger = logging.getLogger(__name__)

_SAVE_INTERVAL = 10
_MAX_HISTORY = 1000

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


async def fetch_cn_longtail_words():
    return await asyncio.wait_for(
        asyncio.to_thread(get_cn_trending_words),
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

    words_with_heat = await fetch_cn_longtail_words()
    if not words_with_heat:
        record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_COMPLETE,
                     run_facets={"total": 0, "skipped": True})
        yield {"event": "done", "total": 0, "skipped": True, "duplicates": 0, "new": 0}
        return

    total = len(words_with_heat)

    # 检查哪些词是新词（不在进度文件中）
    unprocessed = [w for w, _, _ in words_with_heat if w not in processed_words]
    processed_count = total - len(unprocessed)

    # 如果所有词都已在进度文件中，跳过
    if not unprocessed:
        logger.info(f"[cn] 词库已有 {processed_count} 条，全部已处理，跳过")
        record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_COMPLETE,
                     run_facets={"total": total, "skipped": True})
        yield {
            "event": "done",
            "total": total,
            "skipped": True,
            "duplicates": total,
            "new": 0,
            "message": "已全部采集完成（无新词），等待新词出现"
        }
        return

    logger.info(f"[cn] 词库已有 {processed_count} 条，剩余 {len(unprocessed)} 条待处理")

    duplicate_count = 0
    new_count = 0
    save_counter = 0

    try:
        for i, (cn_word, heat, seed_category) in enumerate(words_with_heat):
            # 跳过已处理的词
            if cn_word in processed_words:
                duplicate_count += 1
                yield {
                    "index": i + 1,
                    "total": total,
                    "word": cn_word,
                    "heat": heat,
                    "duplicate": True,
                }
                continue

            # 检查数据库是否已存在
            exists_in_db = cn_anchor_exists(cn_word)

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
                    },
                    collection_run_id=run_id
                )
                new_count += 1

            # 更新进度（每 _SAVE_INTERVAL 次写一次磁盘）
            processed_words[cn_word] = None
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
    record_event(run_id=run_id, job_name="cn_collection", event_type=EVENT_COMPLETE,
                 run_facets={"total": total, "new": new_count, "duplicates": duplicate_count})
    yield {
        "event": "done",
        "total": total,
        "skipped": False,
        "duplicates": duplicate_count,
        "new": new_count,
        "run_id": run_id,
    }


async def run_cn_collector():
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
