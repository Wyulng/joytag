import os
import re
import json
import time
import hashlib
import asyncio
import logging
from typing import Literal
from dotenv import load_dotenv
from services.rule_manager import check_word_against_rules
from services.llm_provider import get_llm_provider, LLMResult
from services.pii_guard import pseudonymize_async
from services import llm_trace
from services.logging_config import get_request_id

logger = logging.getLogger(__name__)

load_dotenv()

# 用户输入清洗：限制长度 + 移除控制字符
_MAX_USER_INPUT_LENGTH = 500
_CTRL_CHAR_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _sanitize_user_input(text: str) -> str:
    """清洗用户输入，基础防 prompt injection"""
    cleaned = _CTRL_CHAR_PATTERN.sub('', text)
    return cleaned[:_MAX_USER_INPUT_LENGTH]

# 评估结果类型别名
AssessmentResult = Literal["可复用", "需拦截", "存疑"]


_THINK_TAG_RE = re.compile(r'<think>.*?</think>', re.DOTALL)

def _strip_json_fence(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = _THINK_TAG_RE.sub('', cleaned)
    cleaned = cleaned.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline >= 0 else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()

def _try_parse_json(raw: str) -> dict | None:
    """严格解析 JSON 对象；失败返回 None（调用方自行决定兜底语义）。"""
    try:
        data = json.loads(_strip_json_fence(raw))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_json_fallback(raw: str) -> dict:
    return _try_parse_json(raw) or {"result": "存疑", "reason": "LLM响应解析失败，请稍后重试"}

# ---------- 留痕（best-effort） ----------
def _record_trace(call_type: str, *, messages: list[dict], result: LLMResult | None = None,
                  error: str | None = None, latency_ms: int = 0, retry_count: int = 0,
                  prompt_pii: dict | None = None, word: str | None = None,
                  parsed: dict | None = None) -> int | None:
    """将 LLM 调用写入 llm_trace（假名化提示词哈希 + token→类型映射，不含原文值）。
    返回 trace 记录 ID（供词条 payload 关联决策留痕）；失败返回 None。"""
    try:
        provider = get_llm_provider()
        prompt_hash = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return llm_trace.record_trace(
            call_type=call_type,
            provider=result.provider if result else provider.name,
            model=result.model if result else provider.model if hasattr(provider, "model") else "",
            request_id=get_request_id(),
            prompt_hash=prompt_hash,
            prompt_pii=prompt_pii or {},
            response=result.content if result else None,
            result=parsed,
            latency_ms=latency_ms,
            retry_count=retry_count,
            error=error,
            word=word,
        )
    except Exception as e:
        logger.warning(f"[llm] 留痕失败（best-effort）: {e}")
        return None

# ---------- 带重试的 LLM 调用 ----------
async def _call_llm_with_retry(messages: list[dict], *, temperature: float,
                               max_tokens: int | None = None, max_retries: int = 3,
                               call_type: str | None = None, word: str | None = None,
                               prompt_pii: dict | None = None) -> LLMResult:
    provider = get_llm_provider()
    start = time.perf_counter()
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            result = await provider.chat_completion(
                messages, temperature=temperature, max_tokens=max_tokens
            )
            if call_type:
                _record_trace(call_type, messages=messages, result=result,
                              latency_ms=result.latency_ms, retry_count=attempt,
                              prompt_pii=prompt_pii, word=word)
            return result
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
    if call_type:
        _record_trace(call_type, messages=messages, error=str(last_exception),
                      latency_ms=int((time.perf_counter() - start) * 1000),
                      retry_count=max_retries, prompt_pii=prompt_pii, word=word)
    raise last_exception

# ---------- 单次评估（用于海外词，已为本地化语言）----------
async def assess_single(word: str, country: str, category: str | None = None) -> tuple[str, str, str | None, int | None]:
    """
    三级合规漏斗：规则库硬拦截 → LLM 软判定。
    返回 (判定, 理由, rule_id, llm_trace_id)。rule_id 为规则命中时的规则编号；
    llm_trace_id 为 LLM 留痕记录 ID（规则直判时无 LLM 调用，为 None）。
    """
    rule_result, rule_reason, rule_id = check_word_against_rules(word, country, category=category)
    if rule_result is True:
        return "可复用", rule_reason, rule_id, None
    elif rule_result is False:
        return "需拦截", rule_reason, rule_id, None

    assess_prompt = """你是一个欧洲电商合规专家。请评估以下长尾词在目标国家市场的文化适配风险，输出"可复用"、"需拦截"或"存疑"，并给出简短理由。只输出JSON格式：{"result": "可复用/需拦截/存疑", "reason": "理由（200字以内）"}。注意：对于全球通用、无负面联想的设计风格词汇，若无明确当地法律禁止，应优先判定为'可复用'。

【欧盟 2024/825 指令（UCPD Annex I，2026-09-27 起适用）强制要求】以下情形必须判定为"需拦截"，并在理由中注明对应规则编号：
- ucpd_env_unverified_label：暗示产品获得未经认证的环保/可持续认证标签（自造标签、仿 EU Ecolabel 等）；
- ucpd_env_generic：无根据的通用环保声明（eco-friendly、green、sustainable、environmentally friendly、climate-neutral 及对应欧洲语言）；
- ucpd_carbon_offset：基于碳抵消的"气候中性/净零/减碳"声明；
- ucpd_legal_requirement：将法定强制要求宣传为产品特色卖点。"""

    word = _sanitize_user_input(word)
    # 发送前假名化（最小化 + 出境风险控制）：LLM 只看到 <EMAIL_ADDRESS_0> 等 token
    clean_word, pii_map = await pseudonymize_async(word)
    user_prompt = f"长尾词：{clean_word}，目标国家：{country}"
    messages = [
        {"role": "system", "content": assess_prompt},
        {"role": "user", "content": user_prompt}
    ]

    result = await _call_llm_with_retry(
        messages, temperature=0.1, word=word, prompt_pii=pii_map
    )
    content = result.content
    parsed = _parse_json_fallback(content)
    assessment = parsed.get("result")
    if assessment not in ("可复用", "需拦截", "存疑"):
        # Never let an untrusted model string fall through to the blocking
        # branch in alignment.py. Unknown output is reviewable, not blocked.
        assessment = "存疑"
    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "LLM 返回的判定理由无效，请人工审核"
    reason = reason.strip()[:200]
    # 解析后统一留痕（含结构化结果，避免与重试包装器重复记录）
    trace_id = _record_trace("assess", messages=messages, result=result, latency_ms=result.latency_ms,
                             retry_count=0, prompt_pii=pii_map, word=word, parsed=parsed)
    return assessment, reason, None, trace_id

# ---------- 翻译海外词为中文（用于查找锚点）----------
async def translate_foreign_to_chinese(foreign_word: str) -> str:
    """
    将海外趋势词翻译为中文，用于查找对应中文锚点。
    """
    foreign_word = _sanitize_user_input(foreign_word)
    clean_word, pii_map = await pseudonymize_async(foreign_word)
    prompt = f"""将以下海外电商趋势词翻译为中文（仅返回中文词汇，不需要解释）：

趋势词：{clean_word}

只输出中文词汇，不要其他内容。"""

    messages = [{"role": "user", "content": prompt}]
    result = await _call_llm_with_retry(
        messages, temperature=0.1, call_type="translate", word=foreign_word, prompt_pii=pii_map
    )
    chinese_word = _strip_json_fence(result.content)
    chinese_word = chinese_word.strip().strip('"')
    return chinese_word


# ---------- 批量翻译中文锚点为本地语种子（用于海外采集动态种子，2026-08） ----------
TRANSLATE_BATCH_SIZE = 12   # 单次批量翻译词数上限（seed_builder 分批共用，改此一处两处同步）

async def translate_chinese_to_foreign_batch(words: list[str], language: str) -> dict[str, str]:
    """
    将中文锚点词批量翻译为目标语言（电商搜索词场景，≤TRANSLATE_BATCH_SIZE 词/次）。
    返回 {原始中文词: 译文}；解析失败或个别词缺失时只返回成功项。
    """
    if not words:
        return {}
    batch = words[:TRANSLATE_BATCH_SIZE]
    clean_words = [_sanitize_user_input(w) for w in batch]
    clean_text, pii_map = await pseudonymize_async("、".join(clean_words))
    prompt = f"""将以下中文电商搜索词批量翻译为{language}（仅返回 JSON，格式：{{"中文词": "译文"}}，译文为可搜索的短词，不需要解释）：

{clean_text}"""

    messages = [{"role": "user", "content": prompt}]
    try:
        result = await _call_llm_with_retry(
            messages, temperature=0.1, call_type="translate_seed",
            word=",".join(clean_words), prompt_pii=pii_map
        )
    except Exception as e:
        logger.warning(f"[llm] 批量翻译失败（{language}, {len(clean_words)} 词）: {e}")
        return {}
    # 解析失败（_try_parse_json 返回 None）即整体失败——兜底结构 {"result","reason"}
    # 的键会被误当翻译项，宁可丢一批也不能写伪翻译
    parsed = _try_parse_json(result.content)
    if not parsed:
        return {}
    # 按位置回填原始词：LLM 看到的键是假名化片段（与输入顺序一致），
    # 逐词三级查找（假名化片段 → 清洗词 → 原始词），保证返回键域与调用方一致
    pieces = clean_text.split("、")
    translated: dict[str, str] = {}
    for orig, clean, piece in zip(batch, clean_words, pieces):
        value = parsed.get(piece)
        if value is None:
            value = parsed.get(clean)
        if value is None:
            value = parsed.get(orig)
        if isinstance(value, str) and value.strip():
            translated[orig] = value.strip()
    return translated
