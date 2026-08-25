"""动态采集种子构建（2026-08）：中文锚点 → 本地语种子。

用 cn_anchors 最新 50 个锚点经 LLM 翻译为六国本地语言，作为 Amazon/eBay
搜索建议 API 的查询种子（海外词天然与中文锚点对齐，锚点检索命中率更高）。
翻译结果持久化到 backend/data/translation_cache.json，逐轮增量翻译
（缓存命中的词-国家对不再调 LLM）。LLM 全挂时返回空 dict，由调用方
（amazon_suggest/ebay_suggest）回退固定兜底种子表 SEEDS_BY_COUNTRY。
"""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from services.collectors.countries import EU_COUNTRIES, LANGUAGE_NAMES
from services.llm import translate_chinese_to_foreign_batch, TRANSLATE_BATCH_SIZE
from services.qdrant_store import ANCHOR_COLLECTION, _iter_scroll

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent  # backend/
_CACHE_FILE = BASE_DIR / "data" / "translation_cache.json"
_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
_MAX_CACHE_KEYS = 2000   # 缓存上限（超限截最旧）
_RECENT_ANCHOR_LIMIT = 50
_cache_lock = threading.Lock()   # 缓存读改写互斥（并发采集轮次见 app.py 端点 + cron）


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # 值形状校验：损坏条目（键/词非 str、值非 dict、译文非 str）直接丢弃，
    # 过滤后无有效译文的词一并丢弃（不留空 dict 占位）；
    # 避免 dict()/get 在运行时抛 ValueError/TypeError 崩整轮采集
    cleaned: dict[str, dict[str, str]] = {}
    for w, per in data.items():
        if not isinstance(w, str) or not isinstance(per, dict):
            continue
        valid = {c: t for c, t in per.items() if isinstance(c, str) and isinstance(t, str)}
        if valid:
            cleaned[w] = valid
    return cleaned


def _merge_and_save_cache(updates: dict[str, dict[str, str]]) -> None:
    """合并翻译结果并写盘：锁内重读 → 乐观合并 → 截断 → FileLock 写。

    并发采集轮次各自翻译后按词合并（而非整文件覆盖），后写者不再抹掉先写者成果。
    """
    with _cache_lock:
        cache = _load_cache()
        for w, per_country in updates.items():
            cache.setdefault(w, {}).update(per_country)
        if len(cache) > _MAX_CACHE_KEYS:
            cache = dict(list(cache.items())[-_MAX_CACHE_KEYS:])
        _save_cache(cache)


def _save_cache(cache: dict):
    try:
        lock = FileLock(str(_CACHE_FILE) + ".lock")
        with lock:
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[seed_builder] 翻译缓存写盘失败: {e}")


def get_recent_anchor_words(limit: int = _RECENT_ANCHOR_LIMIT) -> list[tuple[str, str | None]]:
    """取最新 limit 个中文锚点（首次发现时间倒序），返回 [(cn_word, category)]。"""
    if limit <= 0:
        return []

    def sort_timestamp(value: str) -> float:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return float("-inf")

    # 只保留当前已见记录中的前 limit 个，不物化整个 cn_anchors 集合。
    recent: list[tuple[str, str | None, str]] = []
    for record in _iter_scroll(
        ANCHOR_COLLECTION,
        payload_keys=["cn_word", "category", "created_at", "first_seen_at"],
    ):
        payload = record.payload or {}
        word = payload.get("cn_word")
        if not word:
            continue
        first_seen = payload.get("first_seen_at") or payload.get("created_at") or ""
        recent.append((word, payload.get("category"), first_seen))
        recent.sort(key=lambda item: (-sort_timestamp(item[2]), item[0]))
        if len(recent) > limit:
            recent.pop()
    return [(word, category) for word, category, _ in recent]


async def iter_country_seeds(countries: list[str] | None = None):
    """逐国产出 (country, [(本地语种子, 中文类目)])：某国翻译完成立即产出，不等待他国。

    build_seeds 的流水线版——调用方（overseas_trends）可在等待下一国翻译的同时
    抓取已就绪国家的 Amazon/eBay 建议，消除「双源空等全部翻译」的串行瓶颈。
    每国完成后即合并写缓存（分国增量保存，整体被取消时不丢已翻译成果）。
    锚点库为空时一个不产；某国全部翻译失败时跳过该国（调用方回退固定种子表）。
    """
    if countries is None:
        countries = EU_COUNTRIES
    anchors = await asyncio.to_thread(get_recent_anchor_words)
    if not anchors:
        logger.warning("[seed_builder] cn_anchors 为空，返回空种子（调用方回退固定种子表）")
        return

    cache = await asyncio.to_thread(_load_cache)
    # 逐国增量翻译：只对缓存缺失的词-国家对调 LLM
    translated: dict[str, dict[str, str]] = {w: dict(cache.get(w) or {}) for w, _ in anchors}
    for country in countries:
        lang = LANGUAGE_NAMES.get(country, "英语")
        missing = [w for w, _ in anchors if not translated[w].get(country)]
        if not missing:
            continue
        for i in range(0, len(missing), TRANSLATE_BATCH_SIZE):
            batch = missing[i:i + TRANSLATE_BATCH_SIZE]
            results = await translate_chinese_to_foreign_batch(batch, lang)
            for w, t in results.items():
                translated[w][country] = t
        logger.info(f"[seed_builder] {country} 翻译完成: {len(missing)} 词待译，"
                    f"成功 {sum(1 for w, _ in anchors if translated[w].get(country))}/{len(anchors)}")
        # 该国完成即合并写缓存（分国增量，防整体取消丢成果）
        await asyncio.to_thread(_merge_and_save_cache, {
            w: {country: translated[w][country]}
            for w, _ in anchors
            if translated[w].get(country)
        })
        per_country = [
            (translated[w][country], cat)
            for w, cat in anchors
            if translated[w].get(country)
        ]
        if per_country:
            yield country, per_country
        else:
            logger.warning(f"[seed_builder] {country} 无可用翻译种子，调用方将回退固定种子表")


async def build_seeds(countries: list[str] | None = None) -> dict[str, list[tuple[str, str | None]]]:
    """构建每国动态种子。返回 {country: [(本地语种子, 中文类目)]}。

    锚点库为空、某国全部翻译失败、或 LLM 整体不可用时，对应国家键缺失——
    调用方对该国回退 SEEDS_BY_COUNTRY 固定种子表，保证采集轮次不空。
    """
    seeds: dict[str, list[tuple[str, str | None]]] = {}
    async for country, per_country in iter_country_seeds(countries):
        seeds[country] = per_country
    return seeds
