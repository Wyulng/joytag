"""
Amazon 搜索建议采集模块（2026-08 起海外主源，替代 Reddit/DDG）。

数据源：Amazon completion API（国内直连可用，无需代理，六国站点实测全 200）。
返回本地语言高质量电商搜索词（与淘宝 suggest 同质）。

种子词来源（优先级）：
1. 动态种子：seed_builder.build_seeds() 由最新中文锚点翻译而来（调用方传入）；
2. 兜底种子：SEEDS_BY_COUNTRY 固定词表（LLM 翻译全挂 / 锚点库为空时）。
"""

import logging
import threading
import time

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.collectors.countries import EU_COUNTRIES

logger = logging.getLogger(__name__)

AMAZON_SUGGEST_URL = "https://completion.amazon.{domain}/api/2017/suggestions"

# 六国站点映射（mid 为 Amazon 市场 ID，已实测）
SITE_MAP = {
    "DE": {"domain": "de", "mid": "A1PA6795UKMFR9"},
    "UK": {"domain": "co.uk", "mid": "A1F83G8C2ARO7P"},
    "FR": {"domain": "fr", "mid": "A13V1IB3VIYZZH"},
    "IT": {"domain": "it", "mid": "APJ6JRA9NG5V4"},
    "ES": {"domain": "es", "mid": "A1RKKUPIHCS9HS"},
    "NL": {"domain": "nl", "mid": "A1805IZSGTT6HS"},
}

# 兜底种子（仅动态种子不可用时使用）：每国 20 个本地语服饰/鞋类/配饰/美妆词
SEEDS_BY_COUNTRY: dict[str, list[tuple[str, str | None]]] = {
    "DE": [
        ("kleid", "服装"), ("hemd", "服装"), ("hose", "服装"), ("jeans", "服装"),
        ("bluse", "服装"), ("mantel", "服装"), ("jacke", "服装"), ("pullover", "服装"),
        ("t-shirt", "服装"), ("rock", "服装"), ("bademode", "服装"),
        ("schuhe", "鞋类"), ("stiefel", "鞋类"), ("sneaker", "鞋类"), ("sandalen", "鞋类"),
        ("handtasche", "配饰"), ("rucksack", "配饰"), ("schmuck", "配饰"),
        ("kosmetik", "美妆"), ("parfum", "美妆"),
    ],
    "FR": [
        ("robe", "服装"), ("chemise", "服装"), ("pantalon", "服装"), ("jean", "服装"),
        ("blouse", "服装"), ("manteau", "服装"), ("veste", "服装"), ("pull", "服装"),
        ("t-shirt", "服装"), ("jupe", "服装"), ("maillot de bain", "服装"),
        ("chaussures", "鞋类"), ("bottes", "鞋类"), ("baskets", "鞋类"), ("sandales", "鞋类"),
        ("sac à main", "配饰"), ("sac", "配饰"), ("bijoux", "配饰"),
        ("cosmétique", "美妆"), ("parfum", "美妆"),
    ],
    "NL": [
        ("jurk", "服装"), ("overhemd", "服装"), ("broek", "服装"), ("spijkerbroek", "服装"),
        ("blouse", "服装"), ("jas", "服装"), ("winterjas", "服装"), ("trui", "服装"),
        ("t-shirt", "服装"), ("rok", "服装"), ("badmode", "服装"),
        ("schoenen", "鞋类"), ("laarzen", "鞋类"), ("sneakers", "鞋类"), ("sandalen", "鞋类"),
        ("handtas", "配饰"), ("rugzak", "配饰"), ("sieraden", "配饰"),
        ("cosmetica", "美妆"), ("parfum", "美妆"),
    ],
    "UK": [
        ("dress", "服装"), ("shirt", "服装"), ("trousers", "服装"), ("jeans", "服装"),
        ("blouse", "服装"), ("coat", "服装"), ("jacket", "服装"), ("jumper", "服装"),
        ("t-shirt", "服装"), ("skirt", "服装"), ("swimwear", "服装"),
        ("shoes", "鞋类"), ("boots", "鞋类"), ("trainers", "鞋类"), ("sandals", "鞋类"),
        ("handbag", "配饰"), ("backpack", "配饰"), ("jewellery", "配饰"),
        ("cosmetics", "美妆"), ("perfume", "美妆"),
    ],
    "IT": [
        ("vestito", "服装"), ("camicia", "服装"), ("pantaloni", "服装"), ("jeans", "服装"),
        ("camicetta", "服装"), ("cappotto", "服装"), ("giacca", "服装"), ("maglione", "服装"),
        ("t-shirt", "服装"), ("gonna", "服装"), ("costume da bagno", "服装"),
        ("scarpe", "鞋类"), ("stivali", "鞋类"), ("sneakers", "鞋类"), ("sandali", "鞋类"),
        ("borsa", "配饰"), ("zaino", "配饰"), ("gioielli", "配饰"),
        ("cosmetici", "美妆"), ("profumo", "美妆"),
    ],
    "ES": [
        ("vestido", "服装"), ("camisa", "服装"), ("pantalones", "服装"), ("vaqueros", "服装"),
        ("blusa", "服装"), ("abrigo", "服装"), ("chaqueta", "服装"), ("jersey", "服装"),
        ("camiseta", "服装"), ("falda", "服装"), ("bañador", "服装"),
        ("zapatos", "鞋类"), ("botas", "鞋类"), ("zapatillas", "鞋类"), ("sandalias", "鞋类"),
        ("bolso", "配饰"), ("mochila", "配饰"), ("joyas", "配饰"),
        ("cosméticos", "美妆"), ("perfume", "美妆"),
    ],
}

MAX_WORDS_PER_COUNTRY = 50
_HOT_CACHE_TTL_SECONDS = 600  # 推荐接口热词上下文缓存 10 分钟

# Amazon/eBay 共用一个有界线程池。此前每个国家和来源都会创建独立线程池，
# 六国双源同时运行时可能产生约 96 个 worker；统一池将网络请求并发固定为 16。
OVERSEAS_FETCH_WORKERS = 16
_FETCH_EXECUTOR: ThreadPoolExecutor | None = None
_FETCH_EXECUTOR_LOCK = threading.Lock()


def _get_fetch_executor() -> ThreadPoolExecutor:
    """延迟创建 Amazon/eBay 共用的海外建议词线程池。"""
    global _FETCH_EXECUTOR
    with _FETCH_EXECUTOR_LOCK:
        if _FETCH_EXECUTOR is None:
            _FETCH_EXECUTOR = ThreadPoolExecutor(
                max_workers=OVERSEAS_FETCH_WORKERS,
                thread_name_prefix="overseas-suggest",
            )
        return _FETCH_EXECUTOR


def close_fetch_executor() -> None:
    """关闭共享采集线程池；应用重启或测试下一轮采集时可再次延迟创建。"""
    global _FETCH_EXECUTOR
    with _FETCH_EXECUTOR_LOCK:
        executor = _FETCH_EXECUTOR
        _FETCH_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def _fetch_suggestions(country: str, seed: str, seed_category: str | None) -> list[tuple[str, int, str | None]]:
    """获取单个种子的 Amazon 搜索建议。返回 list[(词, 排名, 种子类目)]。"""
    site = SITE_MAP.get(country)
    if not site:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.amazon.{site['domain']}/",
        "Accept": "application/json",
    }
    params = {
        "limit": 11,
        "prefix": seed,
        "alias": "aps",
        "site-variant": "desktop",
        "mid": site["mid"],
    }
    try:
        resp = requests.get(
            AMAZON_SUGGEST_URL.format(domain=site["domain"]),
            headers=headers, params=params, timeout=10
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        suggestions = []
        for i, item in enumerate(data.get("suggestions", [])):
            word = (item.get("value") or "").strip() if isinstance(item, dict) else ""
            if not word or word.casefold() == seed.casefold():
                continue
            if word.isdigit() or len(word) < 2:
                continue
            suggestions.append((word, i, seed_category))
        return suggestions
    except Exception as e:
        logger.debug(f"[amazon_suggest] {country} 建议请求失败: {seed} -> {e}")
        return []


def score_suggestions(
    suggestions: list[tuple[str, int, str | None]], max_rank: int = 15
) -> tuple[dict[str, float], dict[str, str | None]]:
    """位置评分（eBay 复用）：score = max(0.1, 1.0 - rank/max_rank)，同词取最高分。

    聚合前先按 (词, 排名, 类目) 确定性排序——同分同词的类目归属不随线程完成序波动。
    返回 (word_scores, word_categories)。
    """
    word_scores: dict[str, float] = {}
    word_categories: dict[str, str | None] = {}
    for word, rank, seed_cat in sorted(suggestions, key=lambda x: (x[0].casefold(), x[1], x[2] or "")):
        score = max(0.1, 1.0 - (rank / max_rank))
        current = word_scores.get(word)
        if current is None or score > current:
            word_scores[word] = score
            word_categories[word] = seed_cat
        elif score == current and word_categories.get(word) is None and seed_cat is not None:
            word_categories[word] = seed_cat
    return word_scores, word_categories


def fanout_fetch(
    countries: list[str],
    seeds_by_country: dict[str, list[tuple[str, str | None]]],
    fetch_fn,
    source_name: str,
    max_workers: int = OVERSEAS_FETCH_WORKERS,
    fallback_seeds: dict[str, list[tuple[str, str | None]]] | None = None,
) -> list[dict]:
    """跨国扁平扇出（amazon/ebay 共用）：共享 executor 覆盖全部 (国家, 种子) 对。

    max_workers 参数保留以兼容现有内部调用，但实际并发由
    OVERSEAS_FETCH_WORKERS 固定控制。结果按 (trend_score 降序, query 升序)
    确定性排序，每国截断 MAX_WORDS_PER_COUNTRY，保证进度切片确定性。
    """
    fallback_seeds = fallback_seeds if fallback_seeds is not None else {}
    jobs: list[tuple[str, str, str | None]] = []
    for c in countries:
        seeds = seeds_by_country.get(c) or fallback_seeds.get(c) or []
        jobs.extend((c, s, cat) for s, cat in seeds)

    if not jobs:
        return []

    by_country: dict[str, list[tuple[str, int, str | None]]] = {c: [] for c in countries}
    if max_workers != OVERSEAS_FETCH_WORKERS:
        logger.debug(
            "[%s] 忽略调用方 max_workers=%s，使用全局上限 %s",
            source_name,
            max_workers,
            OVERSEAS_FETCH_WORKERS,
        )
    executor = _get_fetch_executor()
    futures = {executor.submit(fetch_fn, c, s, cat): (c, s) for c, s, cat in jobs}
    for future in as_completed(futures):
        country, _ = futures[future]
        try:
            by_country[country].extend(future.result())
        except Exception as e:
            logger.debug(f"[{source_name}] 扇出任务异常: {e}")

    results = []
    for c in countries:
        word_scores, word_categories = score_suggestions(by_country[c])
        sorted_words = sorted(word_scores.items(), key=lambda x: (-x[1], x[0]))
        for w, s in sorted_words[:MAX_WORDS_PER_COUNTRY]:
            results.append({
                "query": w,
                "country": c,
                "trend_score": round(s, 4),
                "category": word_categories.get(w),
                "source": source_name,
            })
    return results


def get_amazon_suggest_words(
    countries: list[str] | None = None,
    seeds_by_country: dict[str, list[tuple[str, str | None]]] | None = None,
    max_workers: int = OVERSEAS_FETCH_WORKERS,
) -> list[dict]:
    """获取六国 Amazon 搜索建议词（动态种子优先，缺省国家回退固定种子表）。

    返回 [{"query", "country", "trend_score", "category", "source"}]，每国 ≤50。
    max_workers 仅为兼容旧调用方保留，实际使用固定全局线程池上限。
    """
    if countries is None:
        countries = EU_COUNTRIES
    if seeds_by_country is None:
        seeds_by_country = {}
    return fanout_fetch(countries, seeds_by_country, _fetch_suggestions,
                        "amazon_suggest", max_workers=max_workers,
                        fallback_seeds=SEEDS_BY_COUNTRY)


# ---------- 推荐接口热词上下文（stale-while-revalidate 缓存，2026-08 修复推荐延迟根因） ----------
_HOT_CACHE: dict[str, tuple[float, list[dict]]] = {}
_HOT_LOCK = threading.Lock()
_HOT_REFRESHING: set[str] = set()   # in-flight 刷新去重（single-flight）
_HOT_FAIL_TTL_SECONDS = 60          # 刷新全挂且无旧值时的负冷却，避免空结果锁死 10 分钟


def get_country_hot_words(country: str, limit: int = 10) -> list[dict]:
    """获取单国 Amazon 热词（推荐接口注入上下文用）。

    stale-while-revalidate：命中缓存立即返回，过期后在后台线程刷新（不在推荐请求
    路径内同步扇出）；single-flight 去重避免 TTL 过期瞬间的请求风暴；刷新失败保留
    旧值继续服务，全挂且无旧值时短负冷却 60s。缓存存全量列表、调用方切片——
    不同 limit 的调用方互不污染。非六国返回 []。
    热词上下文用固定兜底种子即可（动态种子需 LLM 翻译，对推荐延迟不可接受）。
    """
    country = country.upper()
    if country not in SITE_MAP:
        return []
    now = time.monotonic()
    with _HOT_LOCK:
        hit = _HOT_CACHE.get(country)
        if hit and now - hit[0] < _HOT_CACHE_TTL_SECONDS:
            return hit[1][:limit]
        if country in _HOT_REFRESHING:
            return hit[1][:limit] if hit else []
        _HOT_REFRESHING.add(country)

    def _refresh() -> None:
        try:
            words = fanout_fetch([country], {}, _fetch_suggestions, "amazon_suggest",
                                 fallback_seeds=SEEDS_BY_COUNTRY)
        except Exception as e:
            logger.warning(f"[amazon_suggest] {country} 热词刷新异常: {e}")
            words = []
        with _HOT_LOCK:
            _HOT_REFRESHING.discard(country)
            if words:
                _HOT_CACHE[country] = (time.monotonic(), words)
            elif country not in _HOT_CACHE:
                # 负冷却：空结果条目 TTL 缩短为 _HOT_FAIL_TTL_SECONDS
                _HOT_CACHE[country] = (
                    time.monotonic() - (_HOT_CACHE_TTL_SECONDS - _HOT_FAIL_TTL_SECONDS), []
                )
            # 有旧值时不覆盖：保留 stale 数据继续服务，等下次过期再试

    threading.Thread(target=_refresh, daemon=True, name=f"hot-words-{country}").start()
    return hit[1][:limit] if hit else []
