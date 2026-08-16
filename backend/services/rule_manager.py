import json
import re
import logging
from filelock import FileLock
from typing import List, Dict, Tuple
from pathlib import Path
from time import time
from datetime import datetime, timezone

from services.collectors.countries import EU_COUNTRIES

logger = logging.getLogger(__name__)

# 规则文件存放目录（基于项目根目录）
BASE_DIR = Path(__file__).parent.parent
RULES_DIR = BASE_DIR / "data" / "rules"
RULES_DIR.mkdir(parents=True, exist_ok=True)

# 合法的国家代码白名单（防止路径遍历攻击）：派生自 countries.EU_COUNTRIES 唯一权威源
VALID_COUNTRIES = {c.lower() for c in EU_COUNTRIES}


def _validate_country(country: str) -> str:
    """校验并规范化国家代码，防止路径遍历"""
    c = country.strip().lower()
    if c not in VALID_COUNTRIES:
        raise ValueError(f"Invalid country code: {country}")
    return c

# ---------- UCPD 2024/825 Annex I 内置种子（2026-09-27 起适用） ----------
# 内置在代码中而非规则文件：确保无法被误删；管理端新增的词写规则文件。
# rule_id 与 LLM 审核提示词、拦截留痕保持一致，供溯源举证。
UCPD_ENV_BANNED: Dict[str, List[str]] = {
    "ucpd_env_generic": [
        # EN
        "eco-friendly", "eco friendly", "environmentally friendly", "environment friendly",
        "sustainable", "sustainability", "biodegradable", "100% natural", "all-natural",
        # DE
        "umweltfreundlich", "klimafreundlich", "nachhaltig", "biologisch abbaubar",
        "100% natürlich", "öko-freundlich",
        # FR
        "respectueux de l'environnement", "écologique", "ecologique", "éco-responsable",
        "eco-responsable", "biodégradable", "100% naturel",
        # NL
        "milieuvriendelijk", "biologisch afbreekbaar", "100% natuurlijk", "duurzaam",
        # IT
        "ecologico", "ecologica", "ecosostenibile", "biodegradabile", "100% naturale",
        # ES
        "ecológico", "ecologica", "ecologico", "respetuoso con el medio ambiente",
        "biodegradable", "100% natural",
    ],
    "ucpd_carbon_offset": [
        # EN
        "climate-neutral", "climate neutral", "carbon-neutral", "carbon neutral",
        "net-zero", "net zero", "zero emission", "co2 neutral", "co2-neutral",
        "carbon offset", "carbon-offset", "climateneutral",
        # DE
        "klimaneutral", "co2-neutral", "co2 neutral", "kohlenstoffneutral", "emissionsfrei",
        # FR
        "neutre en carbone", "neutre en climat", "climatiquement neutre", "zéro émission",
        # NL
        "klimaatneutraal", "co2-neutraal",
        # IT
        "a impatto zero", "carbon neutral",
        # ES
        "neutro en carbono", "climáticamente neutro", "carbono neutral",
    ],
    "ucpd_env_unverified_label": [
        # 自造/未认证的环保标签措辞（真实认证标签如 EU Ecolabel 需人工放行）
        "eco label", "eco-label", "green label", "green seal", "eco certified", "eco-certified",
    ],
}

# 匹配用正则缓存：词边界保护（"öko" 不误伤 "ökologisch"；"green" 不误伤 "greenwich"）
_TERM_PATTERNS: Dict[str, re.Pattern] = {}


def _term_pattern(term: str) -> re.Pattern:
    p = _TERM_PATTERNS.get(term)
    if p is None:
        p = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        _TERM_PATTERNS[term] = p
    return p


# 内存缓存（带上限）：{file_path_str: (data, load_time)}
_cache: Dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 60.0  # 缓存有效期（秒）
_CACHE_MAX_SIZE = 128  # 防止无限增长，超出后清除最旧条目


def _evict_if_needed():
    """缓存超出上限时清除最旧的条目。"""
    if len(_cache) >= _CACHE_MAX_SIZE:
        oldest_key = min(_cache, key=lambda k: _cache[k][1])
        _cache.pop(oldest_key, None)


def _read_json(file_path: Path) -> dict:
    """安全读取 JSON 文件，带内存缓存。"""
    key = str(file_path)
    now = time()
    if key in _cache:
        data, load_time = _cache[key]
        if now - load_time < _CACHE_TTL:
            return data
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    _evict_if_needed()
    _cache[key] = (data, now)
    return data


def _invalidate_cache(file_path: Path):
    """写入后清除对应缓存。"""
    _cache.pop(str(file_path), None)


def _write_json(file_path: Path, data: dict):
    """安全写入 JSON 文件（带跨平台文件锁）"""
    lock_path = str(file_path) + ".lock"
    lock = FileLock(lock_path, timeout=10)
    with lock:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    _invalidate_cache(file_path)


# ---------- 规则条目 schema v2 归一化 ----------
# 旧格式：["word1", "word2"]（纯字符串）
# v2 格式：{"word": ..., "categories": [...], "rule_id": ..., "added_by": ..., "added_at": ...}
def _normalize_entries(entries: list) -> list[dict]:
    normalized = []
    for e in entries:
        if isinstance(e, str):
            normalized.append({"word": e})
        elif isinstance(e, dict) and "word" in e:
            normalized.append(e)
        else:
            logger.warning(f"[rules] 忽略非法规则条目: {e!r}")
    return normalized


def _extract_words(entries: list) -> List[str]:
    return [e["word"] for e in _normalize_entries(entries)]


def get_banned_entries(country: str) -> List[dict]:
    """获取某国家的禁用词条目（v2 结构，含 rule_id/categories/added_by）"""
    country = _validate_country(country)
    data = _read_json(RULES_DIR / f"{country}.json")
    return _normalize_entries(data.get("banned", []))


def get_banned_words(country: str) -> List[str]:
    """获取某国家的禁用词列表"""
    return _extract_words(get_banned_entries(country))


def get_safe_entries(country: str) -> List[dict]:
    country = _validate_country(country)
    data = _read_json(RULES_DIR / f"{country}_safe.json")
    return _normalize_entries(data.get("safe", []))


def get_safe_words(country: str) -> List[str]:
    """获取某国家的安全词列表（人工确认通过）"""
    return _extract_words(get_safe_entries(country))


def add_banned_word(country: str, word: str, rule_id: str | None = None,
                    categories: List[str] | None = None, added_by: str = "") -> dict:
    """
    将词汇加入对应国家的禁用词库（v2 条目，含溯源信息）。
    返回更新结果摘要。
    """
    country = _validate_country(country)
    file_path = RULES_DIR / f"{country}.json"
    data = _read_json(file_path)
    if "banned" not in data:
        data["banned"] = []
    entries = _normalize_entries(data["banned"])
    words = [e["word"] for e in entries]
    if word not in words:
        entries.append({
            "word": word,
            "categories": categories or [],
            "rule_id": rule_id,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        data["banned"] = entries
        _write_json(file_path, data)
        logger.info(f"[rules] 添加禁用词: {word} ({country}, rule_id={rule_id}, by={added_by})")
        return {"action": "added", "list": "banned", "country": country, "word": word}
    logger.debug(f"[rules] 禁用词已存在: {word} ({country})")
    return {"action": "exists", "list": "banned", "country": country, "word": word}


def add_safe_word(country: str, word: str, categories: List[str] | None = None,
                  added_by: str = "") -> dict:
    """
    将词汇加入对应国家的安全词库（v2 条目，含溯源信息）。
    """
    country = _validate_country(country)
    file_path = RULES_DIR / f"{country}_safe.json"
    data = _read_json(file_path)
    if "safe" not in data:
        data["safe"] = []
    entries = _normalize_entries(data["safe"])
    words = [e["word"] for e in entries]
    if word not in words:
        entries.append({
            "word": word,
            "categories": categories or [],
            "rule_id": None,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        data["safe"] = entries
        _write_json(file_path, data)
        logger.info(f"[rules] 添加安全词: {word} ({country}, by={added_by})")
        return {"action": "added", "list": "safe", "country": country, "word": word}
    logger.debug(f"[rules] 安全词已存在: {word} ({country})")
    return {"action": "exists", "list": "safe", "country": country, "word": word}


def remove_banned_word(country: str, word: str) -> dict:
    """从禁用词库删除指定词"""
    country = _validate_country(country)
    file_path = RULES_DIR / f"{country}.json"
    data = _read_json(file_path)
    entries = _normalize_entries(data.get("banned", []))
    remaining = [e for e in entries if e["word"] != word]
    if len(remaining) != len(entries):
        data["banned"] = remaining
        _write_json(file_path, data)
        return {"action": "removed", "list": "banned", "country": country, "word": word}
    return {"action": "not_found", "list": "banned", "country": country, "word": word}


def remove_safe_word(country: str, word: str) -> dict:
    """从安全词库删除指定词"""
    country = _validate_country(country)
    file_path = RULES_DIR / f"{country}_safe.json"
    data = _read_json(file_path)
    entries = _normalize_entries(data.get("safe", []))
    remaining = [e for e in entries if e["word"] != word]
    if len(remaining) != len(entries):
        data["safe"] = remaining
        _write_json(file_path, data)
        return {"action": "removed", "list": "safe", "country": country, "word": word}
    return {"action": "not_found", "list": "safe", "country": country, "word": word}


def _match_ucpd(word: str) -> Tuple[str, str] | None:
    """UCPD 内置种子匹配（词边界、忽略大小写）。命中返回 (rule_id, 命中词)。"""
    for rule_id, terms in UCPD_ENV_BANNED.items():
        for term in terms:
            if _term_pattern(term).search(word):
                return rule_id, term
    return None


def check_word_against_rules(word: str, country: str, category: str | None = None) -> Tuple[bool | None, str, str | None]:
    """
    综合检查安全词库和禁用词库（含 UCPD 内置种子）：
    - 命中安全词 → (True, "通过安全词库", None) 表示可复用
    - 命中禁用词 → (False, 理由, rule_id) 表示需拦截
    - 否则 → (None, "", None) 需进一步 LLM 评估
    """
    safe_words = get_safe_words(country)
    if word in safe_words:
        return True, "通过安全词库", None

    # UCPD Annex I 内置种子（优先级高于国家规则文件，保证不可被误删）
    ucpd_hit = _match_ucpd(word)
    if ucpd_hit:
        rule_id, term = ucpd_hit
        return False, f"命中UCPD禁用词库({rule_id}): {term}", rule_id

    # 国家规则文件：大小写不敏感子串匹配（防短语嵌长词绕过）；安全词库保持精确匹配
    banned_entries = get_banned_entries(country)
    word_cf = word.casefold()
    for e in banned_entries:
        term = e["word"]
        if term.casefold() in word_cf:
            rule_id = e.get("rule_id") or "manual_banned"
            return False, f"命中{country}广告法禁用词库({rule_id})", rule_id

    return None, "", None
