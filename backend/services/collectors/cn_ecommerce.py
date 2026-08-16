"""
中文电商搜索长尾词采集模块。
数据源：淘宝搜索建议 API（已验证可用）
注：京东搜索建议 API 已不再公开可访问（返回 HTML 反爬页面），已将 JD 支持移除。
"""

import json
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# ==================== E-commerce Search Suggestion APIs ====================

TAOBAO_SUGGEST_URL = "https://suggest.taobao.com/sug?code=utf-8&q={term}"

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


def _fetch_taobao_suggest(term: str) -> list[tuple[str, int, str | None]]:
    """
    获取淘宝搜索建议词。
    返回 list[(词, 排名, 种子类目)]，排名从0开始，0为最靠前。
    """
    seed_category = SEED_CATEGORY_MAP.get(term)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.taobao.com/"
    }
    try:
        url = TAOBAO_SUGGEST_URL.format(term=term)
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        result = data.get("result", [])
        suggestions = []
        for i, item in enumerate(result):
            if isinstance(item, list) and len(item) >= 1:
                word = item[0].strip()
                if word and word != term:
                    suggestions.append((word, i, seed_category))
        return suggestions
    except Exception as e:
        logger.debug(f"[cn_ecommerce] 淘宝建议请求失败: {term} -> {e}")
        return []



def _score_suggestions(suggestions: list[tuple[str, int, str | None]], max_rank: int = 15) -> tuple[dict[str, float], dict[str, str | None]]:
    """
    对搜索建议词进行评分。
    位置越靠前分数越高：score = 1.0 - (rank / max_rank)。
    同一个词多条记录取最高分和第一个非 None 的种子类目。
    返回 (word_scores, word_categories)。
    """
    word_scores: dict[str, float] = {}
    word_categories: dict[str, str | None] = {}
    for word, rank, seed_cat in suggestions:
        score = max(0.1, 1.0 - (rank / max_rank))
        if word in word_scores:
            if score > word_scores[word]:
                word_scores[word] = score
                if seed_cat is not None:
                    word_categories[word] = seed_cat
        else:
            word_scores[word] = score
            word_categories[word] = seed_cat
    return word_scores, word_categories


def get_cn_trending_words() -> list[tuple[str, float, str | None]]:
    """
    获取中文电商搜索长尾词（带热度分数和种子类目）。
    使用淘宝搜索建议 API，并行请求提升效率。
    返回 list[(词, 热度, 类目)]，热度越高越靠前，类目可能为 None。
    """
    all_suggestions: list[tuple[str, int, str | None]] = []

    # 淘宝（全部种子词，并行）
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_taobao_suggest, term): term for term in SEED_TERMS}
        for future in as_completed(futures):
            all_suggestions.extend(future.result())

    word_scores, word_categories = _score_suggestions(all_suggestions)

    sorted_words = sorted(word_scores.items(), key=lambda x: x[1], reverse=True)
    return [(w, s, word_categories.get(w)) for w, s in sorted_words[:MAX_WORDS_PER_COLLECTION]]
