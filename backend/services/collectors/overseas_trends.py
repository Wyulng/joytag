import os
import json
import uuid
import asyncio
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from services.alignment import process_overseas_word
from services.collectors.countries import EU_COUNTRIES
from services.collectors import amazon_suggest, ebay_suggest, seed_builder
from services.embedding import get_embeddings
from services.qdrant_store import get_existing_word_decision
from services.lineage import record_event, EVENT_START, EVENT_COMPLETE, EVENT_FAIL

logger = logging.getLogger(__name__)

MAX_TOTAL = 120
_SOURCE_TIMEOUT = 280  # 单源整体超时（秒），超时降级空列表不拖垮整轮
_SEED_TIMEOUT = 90     # 动态种子翻译整体超时（秒）：超时后剩余国家用固定种子，不再无限等 LLM
_SAVE_INTERVAL = 10
_MAX_HISTORY = 1000

# 进度文件路径（基于项目根目录）
BASE_DIR = Path(__file__).parent.parent.parent
_PROGRESS_FILE = BASE_DIR / "overseas_collection_progress.json"


def _load_progress() -> dict:
    if not os.path.exists(_PROGRESS_FILE):
        return {"processed_keys": [], "last_time": None}
    try:
        with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"processed_keys": [], "last_time": None}


def _save_progress(progress: dict):
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False)
    except Exception:
        pass


def _merge_sources(
    amazon_words: list[dict],
    ebay_words: list[dict],
    countries: list[str],
    per_country: int = 20,
    amazon_quota: int = 15,
) -> list[tuple]:
    """逐国合并双源候选：Amazon 先取 amazon_quota，eBay 跨源去重补到 per_country
    （Amazon 不足时 eBay 多补，仍不足时 Amazon 余量回补）。
    保持国家顺序与组内分数序（进度切片确定性），casefold 去重防大小写变体重复
    （配额切片内同样去重，跨种子返回的大小写变体不双份入库）。"""
    amazon_by_country = {c: [w for w in amazon_words if w.get("country") == c] for c in countries}
    ebay_by_country = {c: [w for w in ebay_words if w.get("country") == c] for c in countries}
    merged: list[tuple] = []
    for c in countries:
        amz = amazon_by_country.get(c, [])
        ebay = ebay_by_country.get(c, [])
        picked: list[dict] = []
        seen: set[str] = set()
        for w in amz[:amazon_quota]:
            key = w["query"].casefold()
            if key in seen:
                continue
            seen.add(key)
            picked.append(w)
        for extra in (ebay, amz[amazon_quota:]):
            for w in extra:
                if len(picked) >= per_country:
                    break
                key = w["query"].casefold()
                if key in seen:
                    continue
                seen.add(key)
                picked.append(w)
        for w in picked:
            merged.append((
                w["query"],
                c,
                w.get("category"),  # 从种子透传（动态种子带锚点类目），供 LLM 评估品类上下文
                w["trend_score"],
                w.get("source", "overseas"),
            ))
    return merged[:MAX_TOTAL]


async def _fetch_pair(country: str, seeds: list[tuple[str, str | None]]):
    """单国双源并行抓取；单源超时/失败降级 []，不拖垮整国。

    Amazon/eBay 的 fanout_fetch 共用固定 16 worker 线程池；单国粒度下
    wait_for 超时后的孤儿线程最多再跑约 2 批，而不会为每个国家重复创建线程池。
    """
    amz, ebay = await asyncio.gather(
        asyncio.wait_for(
            asyncio.to_thread(
                amazon_suggest.get_amazon_suggest_words,
                [country],
                {country: seeds},
                amazon_suggest.OVERSEAS_FETCH_WORKERS,
            ),
            timeout=_SOURCE_TIMEOUT,
        ),
        asyncio.wait_for(
            asyncio.to_thread(
                ebay_suggest.get_ebay_suggest_words,
                [country],
                {country: seeds},
                amazon_suggest.OVERSEAS_FETCH_WORKERS,
            ),
            timeout=_SOURCE_TIMEOUT,
        ),
        return_exceptions=True,
    )
    if isinstance(amz, BaseException):
        logger.warning(f"[overseas] {country} Amazon 源失败/超时，降级空列表: {amz}")
        amz = []
    if isinstance(ebay, BaseException):
        logger.warning(f"[overseas] {country} eBay 源失败/超时，降级空列表: {ebay}")
        ebay = []
    return amz, ebay


async def fetch_all_trends():
    """采集候选：动态种子（逐国流水线翻译）→ Amazon + eBay 双源并行 → 逐国配额合并。

    种子翻译与抓取流水线重叠：某国翻译完成立即开抓该国双源，同时翻译下一国
    （双源不再空等全部翻译）。种子阶段整体 _SEED_TIMEOUT 上限：超时后剩余国家
    用固定种子表开抓（LLM 慢/半挂不再拖数小时）；build_seeds 抛异常（如 Qdrant
    不可用）时全部国家回退固定种子，采集轮不空。
    """
    countries = EU_COUNTRIES
    pair_tasks: list[asyncio.Task] = []
    ready: set[str] = set()
    try:
        async with asyncio.timeout(_SEED_TIMEOUT):
            async for country, seeds in seed_builder.iter_country_seeds(countries):
                ready.add(country)
                pair_tasks.append(asyncio.create_task(_fetch_pair(country, seeds)))
    except TimeoutError:
        logger.warning(f"[overseas] 动态种子翻译超时（>{_SEED_TIMEOUT}s），已就绪 {sorted(ready)}，"
                       f"其余国家回退固定种子表")
    except Exception as e:
        logger.warning(f"[overseas] 动态种子构建失败，全部国家回退固定种子表: {e}")

    # 未就绪国家用固定种子立即开抓（与已就绪国家并行）
    for c in countries:
        if c not in ready:
            pair_tasks.append(asyncio.create_task(_fetch_pair(c, amazon_suggest.SEEDS_BY_COUNTRY.get(c, []))))

    amazon_words: list[dict] = []
    ebay_words: list[dict] = []
    for task in pair_tasks:
        try:
            amz, ebay = await task
        except Exception as e:
            logger.warning(f"[overseas] 双源抓取任务异常: {e}")
            continue
        amazon_words.extend(amz)
        ebay_words.extend(ebay)

    per_country = MAX_TOTAL // len(countries) if countries else MAX_TOTAL
    return _merge_sources(amazon_words, ebay_words, countries, per_country=per_country)


async def _collect_overseas_generator():
    """
    采集海外趋势词。
    基于进度文件去重：只采集新词，已在进度文件中的词直接跳过。
    EU 合规改造：每轮采集生成 run_id，记录 lineage START/COMPLETE/FAIL 事件，
    granular 来源（amazon_suggest/ebay_suggest）透传到词条 provenance。
    """
    run_id = str(uuid.uuid4())
    record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_START)

    progress = _load_progress()
    # 使用 dict 代替 set 以保留插入顺序，确保进度切片确定性
    processed_keys: dict[str, None] = {k: None for k in progress.get("processed_keys", [])}

    try:
        trends = await fetch_all_trends()
    except Exception:
        # fetch 阶段异常也要闭环 lineage（否则 START 成孤儿事件）
        record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_FAIL,
                     run_facets={"total": 0, "error": "fetch_all_trends failed"})
        raise
    if not trends:
        record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_COMPLETE,
                     run_facets={"total": 0, "skipped": True})
        yield {"event": "done", "total": 0, "skipped": True, "duplicates": 0, "new": 0}
        return

    total = len(trends)

    # 检查哪些词是新词（不在进度文件中）
    unprocessed = [t for t in trends if f"{t[0]}:{t[1]}" not in processed_keys]
    processed_count = total - len(unprocessed)

    # 如果所有词都已在进度文件中，跳过
    if not unprocessed:
        logger.info(f"[overseas] 词库已有 {processed_count} 条，全部已处理，跳过")
        record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_COMPLETE,
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

    logger.info(f"[overseas] 词库已有 {processed_count} 条，剩余 {len(unprocessed)} 条待处理")

    approved_count = 0
    pending_count = 0
    rejected_count = 0
    duplicate_count = 0
    new_count = 0
    save_counter = 0

    try:
        # 在任何向量化之前读取三类 Qdrant 决策缓存。缓存命中的词不再执行
        # embedding、锚点搜索、规则检查或 LLM；无缓存的新词按原顺序批量编码。
        decisions_by_key: dict[str, dict | None] = {}
        words_to_encode: list[str] = []
        words_to_encode_set: set[str] = set()
        for word, country, _category, _trend_score, _source in unprocessed:
            key = f"{word}:{country}"
            if key in decisions_by_key:
                continue
            decision = get_existing_word_decision(word, country)
            decisions_by_key[key] = decision
            if decision is None and word not in words_to_encode_set:
                words_to_encode.append(word)
                words_to_encode_set.add(word)

        vectors = await get_embeddings(words_to_encode) if words_to_encode else []
        vectors_by_word = dict(zip(words_to_encode, vectors))

        for i, (word, country, category, trend_score, source) in enumerate(trends):
            key = f"{word}:{country}"

            # 跳过已处理的词
            if key in processed_keys:
                duplicate_count += 1
                yield {
                    "index": i + 1,
                    "total": total,
                    "word": word,
                    "country": country,
                    "approved": approved_count,
                    "pending": pending_count,
                    "rejected": rejected_count,
                    "duplicate": True,
                }
                continue

            # 统一缓存中任一决策都视为重复，且不增加本轮新增状态统计。
            cached_decision = decisions_by_key.get(key)
            exists_in_db = cached_decision is not None

            if exists_in_db:
                duplicate_count += 1
                logger.info(
                    "[overseas] decision_cache_hit country=%s status=%s word_sha256=%s",
                    country,
                    cached_decision.get("action"),
                    hashlib.sha256(word.encode("utf-8")).hexdigest(),
                )
            else:
                result = await process_overseas_word(
                    word=word,
                    country=country,
                    category=category,
                    trend_score=trend_score,
                    source=source,
                    collection_run_id=run_id,
                    query_vector=vectors_by_word[word],
                    existing_decision=None,
                )
                action = result.get("action")
                # 同一轮候选可能跨种子重复出现；把本次新决策放入内存缓存，
                # 保持原有单轮幂等性，后续重复词只计 duplicate。
                decisions_by_key[key] = {
                    "action": action,
                    "status": result.get("status"),
                    "stored": result.get("stored", False),
                }
                if action == "approved":
                    approved_count += 1
                    new_count += 1
                elif action in ("pending", "pending_no_anchor"):
                    pending_count += 1
                else:
                    rejected_count += 1

            # 更新进度（每 _SAVE_INTERVAL 次写一次磁盘）
            processed_keys[key] = None
            save_counter += 1
            if save_counter % _SAVE_INTERVAL == 0:
                history = list(processed_keys.keys())[-_MAX_HISTORY:] if len(processed_keys) > _MAX_HISTORY else list(processed_keys.keys())
                _save_progress({
                    "processed_keys": history,
                    "last_time": datetime.now(timezone.utc).isoformat()
                })

            yield {
                "index": i + 1,
                "total": total,
                "word": word,
                "country": country,
                "approved": approved_count,
                "pending": pending_count,
                "rejected": rejected_count,
                "duplicate": exists_in_db,
            }
    except Exception:
        record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_FAIL,
                     run_facets={"total": total, "error": "collection interrupted"})
        raise

    logger.info(f"[overseas] 完成，新增: {new_count}，通过: {approved_count}，待审: {pending_count}，拦截: {rejected_count}，重复: {duplicate_count}")
    # 最终保存进度
    history = list(processed_keys.keys())[-_MAX_HISTORY:] if len(processed_keys) > _MAX_HISTORY else list(processed_keys.keys())
    _save_progress({
        "processed_keys": history,
        "last_time": datetime.now(timezone.utc).isoformat()
    })
    record_event(run_id=run_id, job_name="overseas_collection", event_type=EVENT_COMPLETE,
                 run_facets={
                     "total": total, "approved": approved_count, "pending": pending_count,
                     "rejected": rejected_count, "duplicates": duplicate_count, "new": new_count,
                 })
    yield {
        "event": "done",
        "total": total,
        "skipped": False,
        "approved": approved_count,
        "pending": pending_count,
        "rejected": rejected_count,
        "duplicates": duplicate_count,
        "new": new_count,
        "run_id": run_id,
    }


async def run_overseas_collector():
    final = None
    async for p in _collect_overseas_generator():
        final = p
    if final is None:
        return {"total": 0, "approved": 0, "pending": 0, "rejected": 0, "duplicates": 0, "new": 0, "skipped": False}

    if final.get("skipped"):
        return {
            "total": final["total"],
            "approved": 0,
            "pending": 0,
            "rejected": 0,
            "duplicates": 0,
            "new": 0,
            "skipped": True,
            "message": final.get("message", "已全部采集完成"),
        }

    return {
        "total": final["total"],
        "approved": final.get("approved", 0),
        "pending": final.get("pending", 0),
        "rejected": final.get("rejected", 0),
        "duplicates": final.get("duplicates", 0),
        "new": final.get("new", 0),
        "skipped": False,
    }
