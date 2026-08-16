"""
eBay 搜索建议采集模块（2026-08 起海外辅助源，与 Amazon 互补）。

数据源：eBay autosug API（国内直连可用，六国站点实测全 200）。
响应 JSON 为 ["前缀", [...建议]]，取第二元素为建议列表（含品牌词——由
LLM 合规评估自然拦截）。兜底种子与位置评分函数复用 amazon_suggest。
"""

import logging

import requests

from services.collectors.countries import EU_COUNTRIES
from services.collectors.amazon_suggest import SEEDS_BY_COUNTRY, fanout_fetch

logger = logging.getLogger(__name__)

EBAY_AUTOSUG_URL = "https://autosug.ebay.com/autosug"

EBAY_SITE_IDS = {"UK": 3, "DE": 77, "FR": 71, "IT": 101, "ES": 186, "NL": 146}


def _fetch_suggestions(country: str, seed: str, seed_category: str | None) -> list[tuple[str, int, str | None]]:
    """获取单个种子的 eBay 搜索建议。返回 list[(词, 排名, 种子类目)]。"""
    s_id = EBAY_SITE_IDS.get(country)
    if s_id is None:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.ebay.com/",
        "Accept": "application/json",
    }
    params = {"kwd": seed, "sId": s_id, "fmt": "osr"}
    try:
        resp = requests.get(EBAY_AUTOSUG_URL, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
            return []
        suggestions = []
        for i, item in enumerate(data[1]):
            if not isinstance(item, str):
                # 结构异常条目（dict/None 等）强转会产出垃圾词，直接丢弃
                continue
            word = item.strip()
            if not word or word.casefold() == seed.casefold():
                continue
            if word.isdigit() or len(word) < 2:
                continue
            suggestions.append((word, i, seed_category))
        return suggestions
    except Exception as e:
        logger.debug(f"[ebay_suggest] {country} 建议请求失败: {seed} -> {e}")
        return []


def get_ebay_suggest_words(
    countries: list[str] | None = None,
    seeds_by_country: dict[str, list[tuple[str, str | None]]] | None = None,
    max_workers: int = 32,
) -> list[dict]:
    """获取六国 eBay 搜索建议词（动态种子优先，缺省国家回退固定种子表）。

    扇出/评分/确定性排序/截断复用 amazon_suggest.fanout_fetch（与 Amazon 侧
    行为保持一致）。返回 [{"query", "country", "trend_score", "category", "source"}]。
    max_workers 供逐国流水线调用方控制总并发（如 overseas_trends 传 8）。
    """
    if countries is None:
        countries = EU_COUNTRIES
    if seeds_by_country is None:
        seeds_by_country = {}
    return fanout_fetch(countries, seeds_by_country, _fetch_suggestions,
                        "ebay_suggest", max_workers=max_workers,
                        fallback_seeds=SEEDS_BY_COUNTRY)
