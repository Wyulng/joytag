"""
中文电商搜索长尾词采集模块。
数据源：淘宝搜索建议 API（已验证可用）
注：京东搜索建议 API 已不再公开可访问（返回 HTML 反爬页面），已将 JD 支持移除。
"""

import json
import math
import os
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from services import collector_state
from services.collectors.cn_sources import get_cn_source_adapter
from services.qdrant_store import cn_anchors_exist

logger = logging.getLogger(__name__)

# ==================== E-commerce Search Suggestion APIs ====================

TAOBAO_SUGGEST_URL = "https://suggest.taobao.com/sug"

# 种子词（电商搜索词，聚焦 Joybuy 时尚 + 生活品类）
SEED_TERMS = [
    "女装", "男装", "连衣裙", "T恤", "衬衫", "裤子", "短裤", "牛仔裤",
    "外套", "夹克", "风衣", "羽绒服", "毛衣", "卫衣", "运动服",
    "西装", "裙子", "半身裙", "短裙", "针织衫", "打底衫", "吊带",
    "内衣", "睡衣", "泳装", "鞋子", "运动鞋", "靴子", "凉鞋", "拖鞋",
    "包包", "双肩包", "单肩包", "手提包", "钱包", "手表",
    "配饰", "项链", "耳环", "戒指", "手链", "发饰", "帽子", "围巾", "眼镜",
    "美妆", "口红", "粉底", "眼影", "眉笔", "护肤品", "面膜", "精华",
    "香水", "洗发水", "沐浴露", "身体乳",
    "家居", "床上用品", "窗帘", "地毯", "收纳", "厨房用品",
    "母婴", "童装", "玩具",
    "手机壳", "数码配件", "耳机", "充电宝",
    "运动", "瑜伽", "健身", "户外", "露营",
    "宠物", "猫粮", "狗粮",
    "复古", "简约", "潮流", "休闲", "通勤", "法式", "韩系",
]

# 种子词 → 类目映射（用于新采集词自动带类目，style/通用词归 None 走 LLM）
SEED_CATEGORY_MAP: dict[str, str | None] = {
    # 服装
    "女装": "服装", "男装": "服装", "连衣裙": "服装", "T恤": "服装",
    "衬衫": "服装", "裤子": "服装", "短裤": "服装", "牛仔裤": "服装",
    "外套": "服装", "夹克": "服装", "风衣": "服装", "羽绒服": "服装",
    "毛衣": "服装", "卫衣": "服装", "运动服": "服装", "西装": "服装",
    "裙子": "服装", "半身裙": "服装", "短裙": "服装", "针织衫": "服装",
    "打底衫": "服装", "吊带": "服装", "内衣": "服装", "睡衣": "服装",
    "泳装": "服装", "童装": "服装",
    # 鞋类
    "鞋子": "鞋类", "运动鞋": "鞋类", "靴子": "鞋类", "凉鞋": "鞋类", "拖鞋": "鞋类",
    # 配饰
    "包包": "配饰", "双肩包": "配饰", "单肩包": "配饰", "手提包": "配饰",
    "钱包": "配饰", "手表": "配饰", "配饰": "配饰", "项链": "配饰",
    "耳环": "配饰", "戒指": "配饰", "手链": "配饰", "发饰": "配饰",
    "帽子": "配饰", "围巾": "配饰", "眼镜": "配饰",
    # 美妆
    "美妆": "美妆", "口红": "美妆", "粉底": "美妆", "眼影": "美妆",
    "眉笔": "美妆", "护肤品": "美妆", "面膜": "美妆", "精华": "美妆",
    "香水": "美妆", "洗发水": "美妆", "沐浴露": "美妆", "身体乳": "美妆",
    # 家居
    "家居": "家居", "床上用品": "家居", "窗帘": "家居", "地毯": "家居",
    "收纳": "家居", "厨房用品": "家居", "母婴": "家居", "玩具": "家居",
    "宠物": "家居", "猫粮": "家居", "狗粮": "家居",
    # 数码
    "手机壳": "数码", "数码配件": "数码", "耳机": "数码", "充电宝": "数码",
    # 运动健身 → 服装（运动服类）或鞋类
    "运动": "服装", "瑜伽": "服装", "健身": "服装", "户外": "服装", "露营": "家居",
    # 风格词 → None（不自动映射，交 LLM 判定）
    "复古": None, "简约": None, "潮流": None, "休闲": None,
    "通勤": None, "法式": None, "韩系": None,
}

MAX_WORDS_PER_COLLECTION = 200
CN_SEED_QUERY_BUDGET = 80
CN_DYNAMIC_SEED_QUOTA = 64
CN_FIXED_SEED_QUOTA = 16
CN_DYNAMIC_MIN_HEAT = 0.45
CN_MAX_SEED_DEPTH = 2
CN_MAX_FRONTIER = 2000
CN_SOURCE_MODE = os.getenv("CN_SOURCE_MODE", "taobao_suggest").strip().lower() or "taobao_suggest"

if CN_SOURCE_MODE != "taobao_suggest":
    logger.warning(
        "[cn_ecommerce] unsupported CN_SOURCE_MODE=%s; falling back to taobao_suggest",
        CN_SOURCE_MODE,
    )
    CN_SOURCE_MODE = "taobao_suggest"

_LAST_COLLECTION_STATS: dict = {}


def _parse_source_heat(value) -> float | None:
    """解析淘宝建议响应的相对热度字段；缺失/异常值回退到位置评分。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _fetch_taobao_suggest(term: str, seed_category: str | None = None) -> list[dict] | None:
    """获取淘宝建议原始候选。

    返回 None 表示 HTTP/解析失败，返回 [] 表示成功但没有候选。每项保留
    接口返回的第二字段，后续在单次响应内部归一化为相对热度。
    """
    seed_category = seed_category if seed_category is not None else SEED_CATEGORY_MAP.get(term)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.taobao.com/",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            TAOBAO_SUGGEST_URL,
            headers=headers,
            params={"code": "utf-8", "q": term},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        result = data.get("result", [])
        if not isinstance(result, list):
            return None
        suggestions: list[dict] = []
        seed_key = collector_state.normalize_collector_key(term)
        for rank, item in enumerate(result):
            if not isinstance(item, list) or not item:
                continue
            word = item[0].strip() if isinstance(item[0], str) else ""
            if not word or collector_state.normalize_collector_key(word) == seed_key:
                continue
            if word.isdigit() or len(word) < 2:
                continue
            suggestions.append({
                "word": word,
                "rank": rank,
                "raw_heat": _parse_source_heat(item[1] if len(item) > 1 else None),
                "category": seed_category,
            })
        return suggestions
    except Exception as e:
        logger.debug("[cn_ecommerce] 淘宝建议请求失败: %s -> %s", term, e)
        return None


def _coerce_suggestion(item, seed_category: str | None = None) -> dict | None:
    """兼容测试和旧调用方提供的三元组/四元组候选。"""
    if isinstance(item, dict):
        word = item.get("word")
        rank = item.get("rank", 0)
        raw_heat = item.get("raw_heat", item.get("heat"))
        category = item.get("category", seed_category)
    elif isinstance(item, (list, tuple)) and len(item) >= 3:
        word, rank = item[0], item[1]
        if len(item) >= 4:
            raw_heat, category = item[2], item[3]
        else:
            raw_heat, category = None, item[2]
    else:
        return None
    if not isinstance(word, str) or not word.strip():
        return None
    word = word.strip()
    if len(word) < 2 or word.isdigit() or "://" in word or word.lower().startswith(("http:", "https:")):
        return None
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        rank = 0
    return {
        "word": word,
        "rank": max(rank, 0),
        "raw_heat": _parse_source_heat(raw_heat),
        "category": category if isinstance(category, str) else seed_category,
    }


def _normalise_response_heat(items: list[dict]) -> list[dict]:
    # 快照来自 JSON，测试/旧快照中的热度可能仍是字符串；统一在排序前解析，
    # 非法值继续走位置评分而不是让整轮采集失败。
    items = [
        {**item, "raw_heat": _parse_source_heat(item.get("raw_heat"))}
        for item in items
    ]
    valid = [item["raw_heat"] for item in items if item.get("raw_heat") is not None]
    max_log_heat = max((math.log1p(value) for value in valid), default=0.0)
    denominator = max(len(items) - 1, 1)
    result = []
    for item in items:
        rank_score = max(0.0, 1.0 - (item["rank"] / denominator))
        raw_heat = item.get("raw_heat")
        if raw_heat is None or max_log_heat <= 0:
            weight_score = rank_score
        else:
            weight_score = math.log1p(raw_heat) / max_log_heat
        result.append({
            **item,
            "item_heat": round(0.7 * weight_score + 0.3 * rank_score, 6),
        })
    return result


def _seed_is_due(seed: dict, now: datetime) -> bool:
    next_query_at = seed.get("next_query_at")
    if next_query_at is None:
        return True
    if isinstance(next_query_at, str):
        try:
            next_query_at = datetime.fromisoformat(next_query_at.replace("Z", "+00:00"))
        except ValueError:
            return True
    if next_query_at.tzinfo is None:
        next_query_at = next_query_at.replace(tzinfo=timezone.utc)
    return next_query_at <= now


def _timestamp(value) -> float:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return float("-inf")


def _select_by_category(records: list[dict], limit: int) -> list[dict]:
    """按新颖度、热度和类目覆盖选择种子，保证轮换结果稳定。"""
    selected: list[dict] = []
    remaining = list(records)
    category_counts: dict[str, int] = {}
    while remaining and len(selected) < limit:
        remaining.sort(key=lambda item: (
            0 if item.get("last_queried_at") is None else 1,
            -_timestamp(item.get("last_seen_at")),
            -float(item.get("source_heat_score") or 0.0),
            category_counts.get(item.get("category") or "未分类", 0),
            _timestamp(item.get("last_queried_at")),
            item.get("normalized_seed") or "",
        ))
        item = remaining.pop(0)
        selected.append(item)
        category = item.get("category") or "未分类"
        category_counts[category] = category_counts.get(category, 0) + 1
    return selected


def _ensure_seed_frontier() -> list[dict]:
    for term in SEED_TERMS:
        collector_state.upsert_seed_frontier(
            term,
            seed_kind="fixed",
            category=SEED_CATEGORY_MAP.get(term),
            seed_depth=0,
            source_heat_score=0.5,
        )
    collector_state.bootstrap_seed_frontier_from_qdrant(limit=200)
    collector_state.prune_seed_frontier()
    collector_state.trim_seed_frontier(CN_MAX_FRONTIER)
    return collector_state.list_seed_frontier()


def _select_seed_records() -> list[dict]:
    now = datetime.now(timezone.utc)
    frontier = [item for item in _ensure_seed_frontier() if _seed_is_due(item, now)]
    dynamic = [item for item in frontier if item.get("seed_kind") != "fixed"]
    fixed = [item for item in frontier if item.get("seed_kind") == "fixed"]
    dynamic = _select_by_category(dynamic, CN_DYNAMIC_SEED_QUOTA)
    fixed = _select_by_category(fixed, CN_FIXED_SEED_QUOTA)
    selected = dynamic + fixed

    if len(selected) < CN_SEED_QUERY_BUDGET:
        used = {item.get("normalized_seed") for item in selected}
        rest = [item for item in frontier if item.get("normalized_seed") not in used]
        selected.extend(_select_by_category(rest, CN_SEED_QUERY_BUDGET - len(selected)))
    return selected[:CN_SEED_QUERY_BUDGET]


def _fetch_one_seed(seed: dict) -> tuple[list[dict], str, bool]:
    """返回 (候选, 状态=request/cache/error, 响应是否变化)。"""
    word = seed["seed_word"]
    category = seed.get("category")
    source = "taobao_suggest"
    country = "CN"
    snapshot = collector_state.get_source_snapshot(source, country, word)
    if collector_state.source_snapshot_is_fresh(snapshot):
        return snapshot.get("response") or [], "cache", False

    fetched = get_cn_source_adapter(CN_SOURCE_MODE).fetch(word, category)
    if fetched is None:
        collector_state.record_source_error(source, country, word)
        return (snapshot.get("response") or [] if snapshot else []), "error", False

    saved = collector_state.save_source_snapshot(source, country, word, fetched)
    return fetched, "request", bool(saved.get("changed"))


def _aggregate_suggestions(
    seed_results: list[tuple[dict, list[dict], bool]],
) -> tuple[dict[str, dict], list[dict]]:
    aggregate: dict[str, dict] = {}
    changed_candidates: dict[str, dict] = {}
    for seed, items, changed in seed_results:
        normalised = _normalise_response_heat([
            item for item in (_coerce_suggestion(value, seed.get("category")) for value in items)
            if item is not None
        ])
        parent_key = seed.get("normalized_seed") or collector_state.normalize_collector_key(seed["seed_word"])
        parent_depth = int(seed.get("seed_depth") or 0)
        for item in normalised:
            key = collector_state.normalize_collector_key(item["word"])
            if not key:
                continue
            if key == parent_key:
                continue
            entry = aggregate.setdefault(key, {
                "word": item["word"],
                "heat_values": [],
                "ranks": [],
                "parents": set(),
                "categories": set(),
                "max_item_heat": 0.0,
                "seed_depth": parent_depth,
                "parent_seed": seed["seed_word"],
            })
            entry["heat_values"].append(float(item.get("item_heat") or 0.0))
            entry["ranks"].append(int(item.get("rank") or 0))
            entry["parents"].add(parent_key)
            if item.get("category"):
                entry["categories"].add(item["category"])
            entry["max_item_heat"] = max(entry["max_item_heat"], float(item.get("item_heat") or 0.0))
            entry["seed_depth"] = min(entry["seed_depth"], parent_depth)
            if changed:
                changed_candidates[key] = entry

    for entry in aggregate.values():
        parent_count = len(entry["parents"])
        mean_heat = sum(entry["heat_values"]) / max(len(entry["heat_values"]), 1)
        entry["parent_count"] = parent_count
        entry["source_heat_score"] = round(
            0.6 * entry["max_item_heat"]
            + 0.2 * mean_heat
            + 0.2 * min(1.0, max(parent_count - 1, 0) / 2.0),
            6,
        )
        entry["rank_score"] = round(
            1.0 - min(entry["ranks"]) / max(15, len(entry["ranks"]) + 1),
            6,
        )
        entry["category"] = sorted(entry["categories"])[0] if entry["categories"] else None
        entry["source_set"] = ["taobao_suggest"]
    return aggregate, list(changed_candidates.values())


def get_last_collection_stats() -> dict:
    return dict(_LAST_COLLECTION_STATS)


def get_cn_trending_words(
    collection_run_id: str | None = None,
) -> list[tuple[str, float, str | None]]:
    """获取中文建议词：动态种子优先、固定种子轮换、响应热度排序。"""
    global _LAST_COLLECTION_STATS
    # 选择器本身负责 64/16 配额，这里再次硬截断，防止后续适配器或测试替换
    # 选择器时突破单轮外部请求上限。
    seed_records = _select_seed_records()[:CN_SEED_QUERY_BUDGET]
    if not seed_records:
        _LAST_COLLECTION_STATS = {
            "source": "taobao_suggest",
            "seed_queries": 0,
            "source_requests": 0,
            "source_cache_hits": 0,
            "source_errors": 0,
            "source_response_changes": 0,
            "raw_candidates": 0,
            "unique_candidates": 0,
            "dynamic_seeds": 0,
            "fixed_seeds": 0,
            "candidate_observations": 0,
            "candidate_observations_backfilled": 0,
            "candidate_observation_write_failed": 0,
            "candidate_observation_error_type": None,
            "eligible_before_qdrant": 0,
            "qdrant_existing_filtered": 0,
            "selected_candidates": 0,
            "active_dynamic_seeds": 0,
            "frontier_trimmed": 0,
            "qdrant_existing_filter_failed": False,
        }
        return []

    seed_results: list[tuple[dict, list[dict], bool]] = []
    source_requests = 0
    source_cache_hits = 0
    source_errors = 0
    response_changes = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one_seed, seed): seed for seed in seed_records}
        for future in as_completed(futures):
            seed = futures[future]
            try:
                items, request_status, changed = future.result()
            except Exception as exc:
                logger.debug("[cn_ecommerce] 种子任务失败: %s -> %s", seed.get("seed_word"), exc)
                items, request_status, changed = [], "error", False
            if request_status == "request":
                source_requests += 1
                collector_state.mark_seed_queried(
                    seed["seed_word"], seed_kind=seed.get("seed_kind")
                )
            elif request_status == "cache":
                source_cache_hits += 1
            else:
                source_errors += 1
            if changed:
                response_changes += 1
            seed_results.append((seed, items, changed))

    aggregate, changed_candidates = _aggregate_suggestions(seed_results)

    keys = list(aggregate)
    observations_before = collector_state.get_candidate_observations("cn", "CN", keys)

    # 候选观察是跨轮次去重的基础，不应受动态种子晋升的热度/深度门槛影响。
    # 对当前快照中尚无记录的候选做一次惰性补登记；对响应发生变化的候选
    # 更新热度和最近发现时间。两类候选合并为一次批量写入，避免重复 DB 往返。
    observation_candidates: list[dict] = []
    observation_keys: set[str] = set()
    for entry in aggregate.values():
        key = collector_state.normalize_collector_key(entry["word"])
        if key not in observations_before:
            observation_candidates.append(entry)
            observation_keys.add(key)
    for entry in changed_candidates:
        key = collector_state.normalize_collector_key(entry["word"])
        if key and key not in observation_keys:
            observation_candidates.append(entry)
            observation_keys.add(key)

    observation_result = {"write_failed": False, "attempted": 0, "error_type": None}
    if observation_candidates:
        observation_result = collector_state.observe_candidates(
            "cn",
            "CN",
            observation_candidates,
            source="taobao_suggest",
            run_id=collection_run_id,
        ) or observation_result

    # 只在响应首次出现或发生变化时扩展动态种子，避免缓存命中重复污染 frontier。
    for entry in changed_candidates:
        if (
            entry["source_heat_score"] < CN_DYNAMIC_MIN_HEAT
            and entry["parent_count"] < 2
        ):
            continue
        depth = int(entry.get("seed_depth") or 0) + 1
        if depth > CN_MAX_SEED_DEPTH:
            continue
        collector_state.upsert_seed_frontier(
            entry["word"],
            seed_kind="style" if not entry.get("category") else "suggestion",
            category=entry.get("category"),
            parent_seed=entry.get("parent_seed"),
            seed_depth=depth,
            source_heat_score=entry["source_heat_score"],
        )

    # 动态种子扩展发生在任务开始时的裁剪之后，因此必须在扩展完成后
    # 再裁剪一次，保证任务结束时始终满足 frontier 上限。
    frontier_before_trim = collector_state.list_seed_frontier()
    collector_state.trim_seed_frontier(CN_MAX_FRONTIER)
    frontier_after_trim = collector_state.list_seed_frontier()
    active_dynamic_seeds = sum(
        1 for item in frontier_after_trim if item.get("seed_kind") != "fixed"
    )
    frontier_trimmed = max(
        0,
        sum(1 for item in frontier_before_trim if item.get("seed_kind") != "fixed")
        - active_dynamic_seeds,
    )

    observations = collector_state.get_candidate_observations("cn", "CN", keys)
    now = datetime.now(timezone.utc)

    def is_old(item: dict) -> int:
        observation = observations.get(collector_state.normalize_collector_key(item["word"]))
        if not observation:
            return 0
        next_eligible = observation.get("next_eligible_at")
        if next_eligible and next_eligible <= now and not observation.get("decision_status"):
            return 0
        return 1

    eligible_entries = []
    for item in aggregate.values():
        observation = observations.get(collector_state.normalize_collector_key(item["word"]))
        if observation and observation.get("decision_status"):
            last_processed = observation.get("last_processed_at")
            if last_processed and (now - last_processed) < collector_state.CANDIDATE_REPROCESS_AFTER:
                continue
        next_eligible = observation.get("next_eligible_at") if observation else None
        if next_eligible and next_eligible > now:
            continue
        eligible_entries.append(item)

    sorted_entries = sorted(
        eligible_entries,
        key=lambda item: (
            is_old(item),
            -float(item.get("source_heat_score") or 0.0),
            -int(item.get("parent_count") or 0),
            -float(item.get("rank_score") or 0.0),
            collector_state.normalize_collector_key(item["word"]),
        ),
    )
    eligible_before_qdrant = len(sorted_entries)
    existing_anchor_keys: set[str] = set()
    qdrant_existing_filter_failed = False
    if sorted_entries:
        try:
            existing_anchor_words = cn_anchors_exist(
                [entry["word"] for entry in sorted_entries]
            )
            existing_anchor_keys = {
                collector_state.normalize_collector_key(word)
                for word in existing_anchor_words
            }
        except Exception as exc:
            qdrant_existing_filter_failed = True
            logger.warning(
                "[cn_anchor_prefilter_failed] error_type=%s candidates=%d",
                type(exc).__name__,
                len(sorted_entries),
            )

    filtered_entries = [
        entry
        for entry in sorted_entries
        if collector_state.normalize_collector_key(entry["word"])
        not in existing_anchor_keys
    ]
    selected = filtered_entries[:MAX_WORDS_PER_COLLECTION]
    qdrant_existing_filtered = eligible_before_qdrant - len(filtered_entries)
    raw_candidates = sum(len(items) for _, items, _ in seed_results)
    _LAST_COLLECTION_STATS = {
        "source": "taobao_suggest",
        "seed_queries": len(seed_records),
        "source_requests": source_requests,
        "source_cache_hits": source_cache_hits,
        "source_errors": source_errors,
        "source_response_changes": response_changes,
        "raw_candidates": raw_candidates,
        "unique_candidates": len(aggregate),
        "dynamic_seeds": sum(1 for seed in seed_records if seed.get("seed_kind") != "fixed"),
        "fixed_seeds": sum(1 for seed in seed_records if seed.get("seed_kind") == "fixed"),
        "candidate_observations": len(observation_candidates),
        "candidate_observations_backfilled": sum(
            1 for entry in observation_candidates
            if collector_state.normalize_collector_key(entry["word"])
            not in observations_before
        ),
        "candidate_observation_write_failed": (
            int(observation_result.get("attempted") or 0)
            if observation_result.get("write_failed")
            else 0
        ),
        "candidate_observation_error_type": observation_result.get("error_type"),
        "eligible_before_qdrant": eligible_before_qdrant,
        "qdrant_existing_filtered": qdrant_existing_filtered,
        "selected_candidates": len(selected),
        "active_dynamic_seeds": active_dynamic_seeds,
        "frontier_trimmed": frontier_trimmed,
        "qdrant_existing_filter_failed": qdrant_existing_filter_failed,
    }
    return [
        (entry["word"], float(entry["source_heat_score"]), entry.get("category"))
        for entry in selected
    ]
