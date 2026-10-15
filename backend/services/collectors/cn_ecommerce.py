"""
中文电商搜索长尾词采集模块。
数据源：淘宝搜索建议 API（已验证可用）
注：京东搜索建议 API 已不再公开可访问（返回 HTML 反爬页面），已将 JD 支持移除。
"""

import json
import heapq
import math
import os
import threading
import time
from collections import Counter, deque
import requests
import logging
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from services import collector_state
from services.collectors.cn_sources import (
    SourceFetchResult,
    get_cn_source_adapter,
    production_sources,
)
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
    "打底裤", "背心", "开衫", "针织裙", "半身长裙", "打底裤袜", "保暖内衣",
    "马甲", "皮衣", "冲锋衣", "工装裤", "运动裤", "连体裤", "礼服", "汉服",
    "跑鞋", "登山鞋", "板鞋", "乐福鞋", "皮鞋", "凉拖", "雪地靴", "袜子",
    "斜挎包", "腰包", "行李箱", "卡包", "太阳镜", "发夹", "耳钉", "胸针",
    "防晒霜", "卸妆", "洁面", "爽肤水", "护发素", "牙膏", "香薰", "洗衣液",
    "餐具", "锅具", "储物盒", "衣架", "台灯", "靠垫", "浴巾", "地垫",
    "婴儿推车", "儿童书包", "积木", "拼图", "宠物玩具", "猫砂", "狗绳",
    "手机支架", "键盘", "鼠标", "音箱", "智能手表", "运动水壶", "瑜伽垫",
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
    "打底裤": "服装", "背心": "服装", "开衫": "服装", "针织裙": "服装",
    "半身长裙": "服装", "打底裤袜": "服装", "保暖内衣": "服装", "马甲": "服装",
    "皮衣": "服装", "冲锋衣": "服装", "工装裤": "服装", "运动裤": "服装",
    "连体裤": "服装", "礼服": "服装", "汉服": "服装",
    "跑鞋": "鞋类", "登山鞋": "鞋类", "板鞋": "鞋类", "乐福鞋": "鞋类",
    "皮鞋": "鞋类", "凉拖": "鞋类", "雪地靴": "鞋类", "袜子": "鞋类",
    "斜挎包": "配饰", "腰包": "配饰", "行李箱": "配饰", "卡包": "配饰",
    "太阳镜": "配饰", "发夹": "配饰", "耳钉": "配饰", "胸针": "配饰",
    "防晒霜": "美妆", "卸妆": "美妆", "洁面": "美妆", "爽肤水": "美妆",
    "护发素": "美妆", "牙膏": "美妆", "香薰": "家居", "洗衣液": "家居",
    "餐具": "家居", "锅具": "家居", "储物盒": "家居", "衣架": "家居",
    "台灯": "家居", "靠垫": "家居", "浴巾": "家居", "地垫": "家居",
    "婴儿推车": "家居", "儿童书包": "配饰", "积木": "家居", "拼图": "家居",
    "宠物玩具": "家居", "猫砂": "家居", "狗绳": "配饰",
    "手机支架": "数码", "键盘": "数码", "鼠标": "数码", "音箱": "数码",
    "智能手表": "数码", "运动水壶": "家居", "瑜伽垫": "家居",
    # 风格词 → None（不自动映射，交 LLM 判定）
    "复古": None, "简约": None, "潮流": None, "休闲": None,
    "通勤": None, "法式": None, "韩系": None,
}

MAX_WORDS_PER_COLLECTION = 500


def _int_config(name: str, default: int, *, minimum: int = 1, maximum: int = 5000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _float_config(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(value, minimum)


CN_INITIAL_OUTBOUND_BUDGET = _int_config("CN_DAILY_REQUEST_BUDGET_INITIAL", 200, maximum=500)
CN_MAX_OUTBOUND_BUDGET = _int_config("CN_DAILY_REQUEST_BUDGET_MAX", 500, maximum=500)
CN_INITIAL_OUTBOUND_BUDGET = min(CN_INITIAL_OUTBOUND_BUDGET, CN_MAX_OUTBOUND_BUDGET)
CN_SEED_SCAN_LIMIT = _int_config("CN_SEED_SCAN_LIMIT", 5000)
CN_HIGH_QUALITY_SHARE = 0.50
CN_FIXED_SEED_SHARE = 0.25
CN_DEPTH2_MAX_SHARE = 0.10
CN_DYNAMIC_MIN_HEAT = 0.45
CN_MAX_SEED_DEPTH = 2
CN_MAX_FRONTIER = 5000
CN_REQUEST_TIMEOUT_SECONDS = 10
CN_SOURCE_MIN_INTERVAL_SECONDS = min(
    24 * 60 * 60,
    _float_config("CN_SOURCE_MIN_INTERVAL_SECONDS", 0.5),
)
CN_SOURCE_LIST = production_sources(
    os.getenv("CN_SOURCE_LIST", os.getenv("CN_SOURCE_MODE", "taobao_suggest"))
)
CN_SOURCE_MODE = CN_SOURCE_LIST[0]
_LAST_SOURCE_REQUEST_AT: dict[str, float] = {}
_SOURCE_REQUEST_LOCK = threading.Lock()

_LAST_COLLECTION_STATS: dict = {}
_LAST_SOURCE_BY_WORD: dict[str, str] = {}
_LAST_SUPPORT_BY_WORD: dict[str, dict] = {}


def _respect_source_min_interval(source: str) -> None:
    """Serialize outbound requests and enforce the configured per-source gap."""
    with _SOURCE_REQUEST_LOCK:
        now = time.monotonic()
        previous = _LAST_SOURCE_REQUEST_AT.get(source)
        if previous is not None:
            remaining = CN_SOURCE_MIN_INTERVAL_SECONDS - (now - previous)
            if remaining > 0:
                time.sleep(remaining)
        _LAST_SOURCE_REQUEST_AT[source] = time.monotonic()


def _parse_source_heat(value) -> float | None:
    """解析淘宝建议响应的相对热度字段；缺失/异常值回退到位置评分。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _retry_after_seconds(response) -> int | None:
    value = None
    try:
        value = response.headers.get("Retry-After")
    except Exception:
        pass
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _fetch_taobao_suggest_result(
    term: str,
    seed_category: str | None = None,
) -> SourceFetchResult:
    """获取淘宝建议原始候选。

    网络错误与解析错误仍标记为已外发请求。每项保留接口返回的第二字段，
    后续在单次响应内部归一化为相对热度。
    """
    seed_category = seed_category if seed_category is not None else SEED_CATEGORY_MAP.get(term)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.taobao.com/",
        "Accept": "application/json",
    }
    try:
        _respect_source_min_interval("taobao_suggest")
        resp = requests.get(
            TAOBAO_SUGGEST_URL,
            headers=headers,
            params={"code": "utf-8", "q": term},
            timeout=CN_REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return SourceFetchResult(
                candidates=None,
                outbound=True,
                status_code=resp.status_code,
                retry_after_seconds=_retry_after_seconds(resp),
            )
        data = resp.json()
        if not isinstance(data, dict):
            return SourceFetchResult(candidates=None, outbound=True, status_code=resp.status_code)
        result = data.get("result")
        if not isinstance(result, list):
            return SourceFetchResult(candidates=None, outbound=True, status_code=resp.status_code)
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
                "source": "taobao_suggest",
            })
        return SourceFetchResult(
            candidates=suggestions,
            outbound=True,
            status_code=resp.status_code,
        )
    except Exception as e:
        logger.debug("[cn_ecommerce] 淘宝建议请求失败: %s -> %s", term, e)
        return SourceFetchResult(candidates=None, outbound=True)


def _fetch_taobao_suggest(term: str, seed_category: str | None = None) -> list[dict] | None:
    """Compatibility wrapper returning candidates only."""
    return _fetch_taobao_suggest_result(term, seed_category).candidates


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
        "source": item.get("source") if isinstance(item, dict) and isinstance(item.get("source"), str) else None,
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
            "item_rank_score": round(rank_score, 6),
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
    if limit <= 0 or not records:
        return []
    priorities: dict[tuple, dict[str, list[tuple[tuple, int, dict]]]] = {}
    for index, item in enumerate(records):
        category = item.get("category") or "未分类"
        priority = (
            0 if item.get("last_queried_at") is None else 1,
            -_timestamp(item.get("last_seen_at")),
            -float(item.get("source_heat_score") or 0.0),
        )
        tail = (
            _timestamp(item.get("last_queried_at")),
            item.get("normalized_seed") or "",
        )
        priorities.setdefault(priority, {}).setdefault(category, []).append((tail, index, item))

    selected: list[dict] = []
    selected_by_category: Counter = Counter()
    for priority in sorted(priorities):
        queues: dict[str, deque] = {}
        for category, entries in priorities[priority].items():
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            queues[category] = deque(entries)
        while queues and len(selected) < limit:
            available = [
                (
                    selected_by_category[category],
                    queue[0][0],
                    queue[0][1],
                    category,
                )
                for category, queue in queues.items()
            ]
            _count, _tail, _index, category = min(available)
            _sort_key, _source_index, item = queues[category].popleft()
            selected.append(item)
            selected_by_category[category] += 1
            if not queues[category]:
                del queues[category]
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
    collector_state.bootstrap_seed_frontier_from_qdrant(limit=CN_MAX_FRONTIER)
    collector_state.prune_seed_frontier()
    collector_state.trim_seed_frontier(CN_MAX_FRONTIER)
    return collector_state.list_seed_frontier()


def get_daily_request_budget() -> tuple[int, int]:
    """Return (effective budget, healthy run streak) for the staged request ramp."""
    streak = collector_state.get_healthy_collection_run_streak("collect.cn.auto", limit=7)
    if streak >= 7:
        return min(CN_MAX_OUTBOUND_BUDGET, 500), streak
    return min(CN_INITIAL_OUTBOUND_BUDGET, 200), streak


def _select_undercovered(records: list[dict], limit: int, coverage: Counter) -> list[dict]:
    if limit <= 0 or not records:
        return []
    grouped: dict[str, list[tuple[tuple, int, dict]]] = {}
    for index, item in enumerate(records):
        category = item.get("category") or "未分类"
        tail = (
            0 if item.get("last_queried_at") is None else 1,
            _timestamp(item.get("last_queried_at")),
            -float(item.get("source_heat_score") or 0.0),
            item.get("normalized_seed") or "",
        )
        grouped.setdefault(category, []).append((tail, index, item))
    queues: dict[str, deque] = {}
    heap: list[tuple[int, tuple, int, str]] = []
    for category, entries in grouped.items():
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        queues[category] = deque(entries)
        tail, index, _item = entries[0]
        heapq.heappush(heap, (coverage[category], tail, index, category))

    selected: list[dict] = []
    selected_by_category: Counter = Counter()
    while heap and len(selected) < limit:
        _category_coverage, _tail, _index, category = heapq.heappop(heap)
        _sort_key, _source_index, item = queues[category].popleft()
        selected.append(item)
        selected_by_category[category] += 1
        if queues[category]:
            tail, index, _next = queues[category][0]
            heapq.heappush(
                heap,
                (coverage[category] + selected_by_category[category], tail, index, category),
            )
    return selected


def _select_seed_lanes(
    budget: int = CN_SEED_SCAN_LIMIT,
    *,
    frontier: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Build distinct anchor, fixed, and low-coverage exploration queues.

    Depth-two seeds are limited to 10% of the scan/request budget and are drawn
    only from the exploration lane. The per-lane query queues are larger than
    the outbound quota so fresh-cache hits do not consume request budget.
    """
    now = datetime.now(timezone.utc)
    due = [item for item in (frontier if frontier is not None else _ensure_seed_frontier())
           if _seed_is_due(item, now)]
    scan_limit = max(0, int(budget))
    high_target = int(scan_limit * CN_HIGH_QUALITY_SHARE)
    fixed_target = int(scan_limit * CN_FIXED_SEED_SHARE)
    exploration_total = max(0, scan_limit - high_target - fixed_target)
    depth2_cap = min(int(scan_limit * CN_DEPTH2_MAX_SHARE), exploration_total)
    explore_target = max(0, exploration_total - depth2_cap)

    fixed = [item for item in due if item.get("seed_kind") == "fixed"]
    dynamic = [item for item in due if item.get("seed_kind") != "fixed"]
    high_quality = [
        item for item in dynamic
        if int(item.get("seed_depth") or 0) <= 1
        and (
            item.get("seed_kind") == "anchor"
            or float(item.get("source_heat_score") or 0.0) >= 0.65
        )
    ]
    high_selected = _select_by_category(high_quality, high_target)
    selected_keys = {item.get("normalized_seed") for item in high_selected}

    fixed_selected = _select_by_category(fixed, fixed_target)
    fixed_keys = {item.get("normalized_seed") for item in fixed_selected}

    coverage: Counter = Counter(item.get("category") or "未分类" for item in due)
    depth2 = [item for item in dynamic if int(item.get("seed_depth") or 0) == 2]
    depth2_selected = _select_undercovered(
        [item for item in depth2 if item.get("normalized_seed") not in selected_keys],
        depth2_cap,
        coverage,
    )
    selected_keys.update(item.get("normalized_seed") for item in depth2_selected)
    # Depth-two exploration is capped at 10% of the full request budget. If
    # fewer depth-two seeds are available, use the unfilled share for ordinary
    # low-coverage exploration instead of leaving part of the 25% lane idle.
    explore_target = max(0, exploration_total - len(depth2_selected))
    exploration_pool = [
        item for item in dynamic
        if item.get("normalized_seed") not in selected_keys
        and item.get("normalized_seed") not in fixed_keys
        and int(item.get("seed_depth") or 0) < 2
    ]
    exploration_selected = _select_undercovered(
        exploration_pool,
        explore_target,
        coverage,
    )

    lanes = {
        "high_quality": high_selected,
        "fixed": fixed_selected,
        "depth2": depth2_selected,
        "exploration": exploration_selected,
    }
    # If a lane has insufficient candidates, fill its scan queue from unused
    # records while still respecting the strict depth-two cap.
    used = {
        item.get("normalized_seed")
        for lane in lanes.values()
        for item in lane
    }
    remaining = [item for item in due if item.get("normalized_seed") not in used]
    for name, target in (
        ("high_quality", high_target),
        ("fixed", fixed_target),
        ("depth2", depth2_cap),
        ("exploration", explore_target),
    ):
        missing = max(0, target - len(lanes[name]))
        if not missing:
            continue
        candidates = [item for item in remaining if item.get("normalized_seed") not in used]
        if name == "fixed":
            candidates = [item for item in candidates if item.get("seed_kind") == "fixed"]
        else:
            candidates = [item for item in candidates if item.get("seed_kind") != "fixed"]
        if name != "depth2":
            candidates = [item for item in candidates if int(item.get("seed_depth") or 0) < 2]
        else:
            candidates = [item for item in candidates if int(item.get("seed_depth") or 0) == 2]
        selected = _select_undercovered(candidates, missing, coverage)
        lanes[name].extend(selected)
        used.update(item.get("normalized_seed") for item in selected)
    return lanes


def _select_seed_records() -> list[dict]:
    lanes = _select_seed_lanes(CN_SEED_SCAN_LIMIT)
    return [item for lane in lanes.values() for item in lane]


def _fetch_one_seed(
    seed: dict,
    source: str = CN_SOURCE_MODE,
) -> tuple[list[dict], str, bool, int | None, int | None]:
    """Return candidates, status, response change, HTTP status, and Retry-After."""
    word = seed["seed_word"]
    category = seed.get("category")
    country = "CN"
    snapshot = collector_state.get_source_snapshot(source, country, word)
    if collector_state.source_snapshot_is_fresh(snapshot):
        return snapshot.get("response") or [], "cache", False, None, None

    adapter = get_cn_source_adapter(source)
    try:
        fetch_with_status = getattr(adapter, "fetch_with_status", None)
        if callable(fetch_with_status):
            result = fetch_with_status(word, category)
        else:  # Compatibility for simple third-party/test adapters.
            result = SourceFetchResult(
                candidates=adapter.fetch(word, category),
                outbound=True,
                status_code=None,
            )
    except Exception as exc:
        logger.debug("[cn_ecommerce] source adapter failed: source=%s seed=%s type=%s", source, word, type(exc).__name__)
        result = SourceFetchResult(candidates=None, outbound=False)
    fetched = result.candidates
    if fetched is None:
        if result.outbound:
            collector_state.record_source_error(
                source,
                country,
                word,
                retry_after_seconds=result.retry_after_seconds,
            )
        return (
            snapshot.get("response") or [] if snapshot else [],
            "error" if result.outbound else "disabled",
            False,
            result.status_code,
            result.retry_after_seconds,
        )

    saved = collector_state.save_source_snapshot(source, country, word, fetched)
    return fetched, "request", bool(saved.get("changed")), result.status_code, None


def _aggregate_suggestions(
    seed_results: list[tuple],
) -> tuple[dict[str, dict], list[dict]]:
    aggregate: dict[str, dict] = {}
    changed_candidates: dict[str, dict] = {}
    for fetch_result in seed_results:
        seed, items, changed = fetch_result[:3]
        source = fetch_result[3] if len(fetch_result) > 3 else None
        items_by_source: dict[str, list[dict]] = {}
        for value in items:
            item = _coerce_suggestion(value, seed.get("category"))
            if item is not None:
                source_name = item.get("source") or source or "taobao_suggest"
                items_by_source.setdefault(source_name, []).append(item)
        parent_key = seed.get("normalized_seed") or collector_state.normalize_collector_key(seed["seed_word"])
        parent_depth = int(seed.get("seed_depth") or 0)
        normalised_items = [
            item
            for source_items in items_by_source.values()
            for item in _normalise_response_heat(source_items)
        ]
        for item in normalised_items:
            key = collector_state.normalize_collector_key(item["word"])
            if not key:
                continue
            if key == parent_key:
                continue
            entry = aggregate.setdefault(key, {
                "word": item["word"],
                "heat_values": [],
                "rank_scores": [],
                "parents": set(),
                "categories": set(),
                "max_item_heat": 0.0,
                "seed_depth": parent_depth,
                "parent_seed": seed["seed_word"],
                "sources": set(),
            })
            entry["heat_values"].append(float(item.get("item_heat") or 0.0))
            entry["rank_scores"].append(float(item.get("item_rank_score") or 0.0))
            entry["parents"].add(parent_key)
            entry["sources"].add(item.get("source") or source or "taobao_suggest")
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
            sum(entry["rank_scores"]) / max(len(entry["rank_scores"]), 1),
            6,
        )
        entry["category"] = sorted(entry["categories"])[0] if entry["categories"] else None
        entry["source_set"] = sorted(entry["sources"])
        entry["source_support_count"] = len(entry["source_set"])
        entry["seed_support_count"] = parent_count
    return aggregate, list(changed_candidates.values())


def get_last_collection_stats() -> dict:
    return dict(_LAST_COLLECTION_STATS)


def get_source_for_word(word: str) -> str:
    key = collector_state.normalize_collector_key(word)
    return _LAST_SOURCE_BY_WORD.get(key, CN_SOURCE_MODE)


def get_support_for_word(word: str) -> dict:
    key = collector_state.normalize_collector_key(word)
    return dict(_LAST_SUPPORT_BY_WORD.get(key) or {})


def _empty_cn_stats(*, budget: int = 0, streak: int = 0) -> dict:
    return {
        "expansion_metrics_version": 1,
        "source": CN_SOURCE_MODE,
        "source_list": list(CN_SOURCE_LIST),
        "seed_queries": 0,
        "request_budget": budget,
        "healthy_run_streak": streak,
        "source_requests": 0,
        "source_cache_hits": 0,
        "source_errors": 0,
        "source_429": 0,
        "source_response_changes": 0,
        "source_success_rate": 0.0,
        "source_429_rate": 0.0,
        "raw_candidates": 0,
        "unique_candidates": 0,
        "cross_source_duplicates": 0,
        "dynamic_seeds": 0,
        "fixed_seeds": 0,
        "depth2_seeds": 0,
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
        "source_metrics": {},
    }


def get_cn_trending_words(
    collection_run_id: str | None = None,
) -> list[tuple[str, float, str | None]]:
    """Fetch CN suggestions under actual outbound-request and seed-lane budgets."""
    global _LAST_COLLECTION_STATS
    global _LAST_SOURCE_BY_WORD
    global _LAST_SUPPORT_BY_WORD
    budget, healthy_streak = get_daily_request_budget()
    lanes = _select_seed_lanes(CN_SEED_SCAN_LIMIT)
    scan_total = sum(len(records) for records in lanes.values())
    if not scan_total:
        _LAST_COLLECTION_STATS = _empty_cn_stats(budget=budget, streak=healthy_streak)
        return []

    source_results: list[tuple] = []
    source_metrics: dict[str, dict[str, int]] = {
        source: {
            "requests": 0,
            "cache_hits": 0,
            "errors": 0,
            "http_429": 0,
            "depth2_requests": 0,
            "response_changes": 0,
            "raw_candidates": 0,
            "unique_candidates": 0,
            "cross_source_duplicates": 0,
            "qdrant_existing": 0,
            "selected_candidates": 0,
        }
        for source in CN_SOURCE_LIST
    }
    lane_shares = {
        "high_quality": CN_HIGH_QUALITY_SHARE,
        "fixed": CN_FIXED_SEED_SHARE,
        "depth2": CN_DEPTH2_MAX_SHARE,
        "exploration": 1.0 - CN_HIGH_QUALITY_SHARE - CN_FIXED_SEED_SHARE - CN_DEPTH2_MAX_SHARE,
    }
    per_source_budgets = [budget // len(CN_SOURCE_LIST)] * len(CN_SOURCE_LIST)
    for index in range(budget % len(CN_SOURCE_LIST)):
        per_source_budgets[index] += 1

    for source_index, source in enumerate(CN_SOURCE_LIST):
        source_budget = per_source_budgets[source_index]
        source_used_seeds: set[str] = set()
        source_attempts = 0
        source_halted = False
        lane_targets = {
            "high_quality": int(source_budget * lane_shares["high_quality"]),
            "fixed": int(source_budget * lane_shares["fixed"]),
            "depth2": int(source_budget * lane_shares["depth2"]),
        }
        lane_targets["exploration"] = max(0, source_budget - sum(lane_targets.values()))

        def fetch_lane(lane_name: str, request_target: int) -> None:
            nonlocal source_attempts, source_halted
            if request_target <= 0 or source_halted:
                return
            lane_attempts = 0
            for seed in lanes.get(lane_name, []):
                seed_key = seed.get("normalized_seed") or collector_state.normalize_collector_key(seed["seed_word"])
                if seed_key in source_used_seeds:
                    continue
                source_used_seeds.add(seed_key)
                try:
                    fetched = _fetch_one_seed(seed, source)
                    items, request_status, changed = fetched[:3]
                    status_code = fetched[3] if len(fetched) > 3 else None
                    retry_after = fetched[4] if len(fetched) > 4 else None
                except Exception as exc:
                    logger.debug("[cn_ecommerce] seed task failed: source=%s seed=%s type=%s", source, seed.get("seed_word"), type(exc).__name__)
                    items, request_status, changed, status_code, retry_after = [], "error", False, None, None

                metric = source_metrics[source]
                if request_status == "request":
                    metric["requests"] += 1
                    if int(seed.get("seed_depth") or 0) >= 2:
                        metric["depth2_requests"] += 1
                    source_attempts += 1
                    lane_attempts += 1
                    collector_state.mark_seed_queried(seed["seed_word"], seed_kind=seed.get("seed_kind"))
                elif request_status == "cache":
                    metric["cache_hits"] += 1
                    collector_state.mark_seed_queried(seed["seed_word"], seed_kind=seed.get("seed_kind"))
                elif request_status == "error":
                    metric["errors"] += 1
                    if int(seed.get("seed_depth") or 0) >= 2:
                        metric["depth2_requests"] += 1
                    source_attempts += 1
                    lane_attempts += 1
                    if status_code == 429:
                        metric["http_429"] += 1
                        source_halted = True
                        logger.warning(
                            "[cn_source_rate_limited] source=%s retry_after_seconds=%s; source paused for this run",
                            source,
                            retry_after,
                        )
                if changed:
                    metric["response_changes"] += 1
                metric["raw_candidates"] += len(items or [])
                source_results.append((seed, items or [], changed, source))
                if source_halted or lane_attempts >= request_target or source_attempts >= source_budget:
                    break

        for lane_name in ("high_quality", "fixed", "depth2", "exploration"):
            fetch_lane(lane_name, lane_targets.get(lane_name, 0))
            if source_halted or source_attempts >= source_budget:
                break

        # A cache-heavy lane can exhaust its available records before consuming
        # its outbound share. Use remaining due candidates to reach the overall
        # source cap while preserving the hard depth-two ceiling.
        if not source_halted and source_attempts < source_budget:
            fallback = [
                item
                for lane in ("high_quality", "fixed", "depth2", "exploration")
                for item in lanes.get(lane, [])
                if (item.get("normalized_seed") or collector_state.normalize_collector_key(item["seed_word"]))
                not in source_used_seeds
            ]
            depth2_attempts = source_metrics[source]["depth2_requests"]
            depth2_limit = int(source_budget * CN_DEPTH2_MAX_SHARE)
            for seed in fallback:
                if source_attempts >= source_budget:
                    break
                is_depth2 = int(seed.get("seed_depth") or 0) >= 2
                if is_depth2 and depth2_attempts >= depth2_limit:
                    continue
                if not is_depth2 and int(seed.get("seed_depth") or 0) > 1:
                    continue
                seed_key = seed.get("normalized_seed") or collector_state.normalize_collector_key(seed["seed_word"])
                source_used_seeds.add(seed_key)
                try:
                    fetched = _fetch_one_seed(seed, source)
                    items, request_status, changed = fetched[:3]
                    status_code = fetched[3] if len(fetched) > 3 else None
                    retry_after = fetched[4] if len(fetched) > 4 else None
                except Exception as exc:
                    logger.debug("[cn_ecommerce] fallback seed failed: source=%s type=%s", source, type(exc).__name__)
                    items, request_status, changed, status_code, retry_after = [], "error", False, None, None
                metric = source_metrics[source]
                if request_status == "request":
                    metric["requests"] += 1
                    source_attempts += 1
                    collector_state.mark_seed_queried(seed["seed_word"], seed_kind=seed.get("seed_kind"))
                    if is_depth2:
                        depth2_attempts += 1
                        metric["depth2_requests"] += 1
                elif request_status == "cache":
                    metric["cache_hits"] += 1
                    collector_state.mark_seed_queried(seed["seed_word"], seed_kind=seed.get("seed_kind"))
                elif request_status == "error":
                    metric["errors"] += 1
                    source_attempts += 1
                    if is_depth2:
                        depth2_attempts += 1
                        metric["depth2_requests"] += 1
                    if status_code == 429:
                        metric["http_429"] += 1
                        source_halted = True
                if changed:
                    metric["response_changes"] += 1
                metric["raw_candidates"] += len(items or [])
                source_results.append((seed, items or [], changed, source))
                if source_halted:
                    break

    seed_results = source_results
    seed_records = [item for lane in lanes.values() for item in lane]

    aggregate, changed_candidates = _aggregate_suggestions(seed_results)
    for entry in aggregate.values():
        sources = list(entry.get("source_set") or [CN_SOURCE_MODE])
        for source in sources:
            metric = source_metrics.setdefault(source, {
                "requests": 0,
                "cache_hits": 0,
                "errors": 0,
                "http_429": 0,
                "depth2_requests": 0,
                "response_changes": 0,
                "raw_candidates": 0,
                "unique_candidates": 0,
                "cross_source_duplicates": 0,
                "qdrant_existing": 0,
                "selected_candidates": 0,
            })
            metric["unique_candidates"] += 1
            if len(sources) > 1:
                metric["cross_source_duplicates"] += 1
    _LAST_SOURCE_BY_WORD = {
        key: (entry.get("source_set") or [CN_SOURCE_MODE])[0]
        for key, entry in aggregate.items()
    }
    _LAST_SUPPORT_BY_WORD = {
        key: {
            "source_set": list(entry.get("source_set") or []),
            "source_support_count": int(entry.get("source_support_count") or 1),
            "seed_support_count": int(entry.get("seed_support_count") or 1),
        }
        for key, entry in aggregate.items()
    }

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
            source=CN_SOURCE_MODE,
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

    for entry in sorted_entries:
        key = collector_state.normalize_collector_key(entry["word"])
        if key in existing_anchor_keys:
            for source in entry.get("source_set") or [CN_SOURCE_MODE]:
                if source in source_metrics:
                    source_metrics[source]["qdrant_existing"] += 1

    filtered_entries = [
        entry
        for entry in sorted_entries
        if collector_state.normalize_collector_key(entry["word"])
        not in existing_anchor_keys
    ]
    selected = filtered_entries[:MAX_WORDS_PER_COLLECTION]
    for entry in selected:
        for source in entry.get("source_set") or [CN_SOURCE_MODE]:
            if source in source_metrics:
                source_metrics[source]["selected_candidates"] += 1
    qdrant_existing_filtered = eligible_before_qdrant - len(filtered_entries)
    for metric in source_metrics.values():
        outbound = metric["requests"] + metric["errors"]
        metric["success_rate"] = round(metric["requests"] / outbound, 6) if outbound else 0.0
        metric["http_429_rate"] = round(metric["http_429"] / outbound, 6) if outbound else 0.0
    source_requests = sum(item["requests"] + item["errors"] for item in source_metrics.values())
    source_errors = sum(item["errors"] for item in source_metrics.values())
    source_cache_hits = sum(item["cache_hits"] for item in source_metrics.values())
    response_changes = sum(item["response_changes"] for item in source_metrics.values())
    source_429 = sum(item["http_429"] for item in source_metrics.values())
    source_success_rate = (source_requests - source_errors) / source_requests if source_requests else 0.0
    source_429_rate = source_429 / source_requests if source_requests else 0.0
    _LAST_COLLECTION_STATS = {
        "expansion_metrics_version": 1,
        "source": CN_SOURCE_MODE,
        "source_list": list(CN_SOURCE_LIST),
        "seed_queries": sum(len(records) for records in lanes.values()),
        "request_budget": budget,
        "healthy_run_streak": healthy_streak,
        "source_requests": source_requests,
        "source_cache_hits": source_cache_hits,
        "source_errors": source_errors,
        "source_429": source_429,
        "source_response_changes": response_changes,
        "source_success_rate": round(source_success_rate, 6),
        "source_429_rate": round(source_429_rate, 6),
        "raw_candidates": sum(item["raw_candidates"] for item in source_metrics.values()),
        "unique_candidates": len(aggregate),
        "cross_source_duplicates": sum(1 for entry in aggregate.values() if len(entry.get("source_set") or []) > 1),
        "dynamic_seeds": sum(1 for seed in seed_records if seed.get("seed_kind") != "fixed"),
        "fixed_seeds": sum(1 for seed in seed_records if seed.get("seed_kind") == "fixed"),
        "depth2_seeds": sum(1 for seed in seed_records if int(seed.get("seed_depth") or 0) == 2),
        "source_metrics": source_metrics,
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
