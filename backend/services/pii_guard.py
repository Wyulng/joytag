"""PII 假名化守卫（EU 合规改造新增，2026-08）。

基于 Microsoft Presidio（Apache-2.0）的本地识别引擎，在 LLM 调用**发送前**对
提示词做假名化（pseudonymization），降低数据出境风险（GDPR 最小化 + Chapter V）。

默认 PII_GUARD_MODE=regex_only：仅模式识别器（email/电话/IBAN/IP/信用卡），
不加载 spaCy 模型（en_core_web_lg 约 560MB，过重）。可选 spacy_sm（约 12MB）
增强拉丁文本 PERSON/LOCATION 识别。中文文本 Presidio 无 zh 识别器——电商标题
场景风险已接受（见 docs/EU_COMPLIANCE_PLAN.md 风险 R4），型号类长数字串由
信用卡/电话识别器的校验逻辑（Luhn/位数）拦截误报。

只存储 token→实体类型映射（如 {"<EMAIL_ADDRESS_0>": "EMAIL_ADDRESS"}），
**不存储原文值**；原文仅存在于调用方内存。
"""
import asyncio
import logging
import os
import re
from typing import Dict, Tuple

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine

logger = logging.getLogger(__name__)

# ---------- 模式定义（保守，防中文标题长数字串误报） ----------
_EMAIL_REGEX = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
_IP_REGEX = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
_IBAN_REGEX = r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"
_PHONE_REGEX = r"(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}[\s.-]?\d{2,4}[\s.-]?\d{2,4}\b"
_CARD_REGEX = r"\b(?:\d[ -]?){13,19}\b"


class _ValidatedPatternRecognizer(PatternRecognizer):
    """带校验的模式识别器：validate_result 进一步过滤误报。"""

    def __init__(self, supported_entity: str, patterns: list[Pattern],
                 validator_name: str, score: float = 0.6):
        self._validator_name = validator_name
        super().__init__(supported_entity=supported_entity, patterns=patterns)

    def validate_result(self, pattern_text: str) -> bool:
        digits = re.sub(r"\D", "", pattern_text)
        if self._validator_name == "phone":
            # 电话：至少 7 位数字（短于 7 位视为型号/编号误报）
            return len(digits) >= 7
        if self._validator_name == "luhn":
            if not (13 <= len(digits) <= 19):
                return False
            # Luhn 校验
            total = 0
            for i, ch in enumerate(reversed(digits)):
                n = int(ch)
                if i % 2 == 1:
                    n *= 2
                    if n > 9:
                        n -= 9
                total += n
            return total % 10 == 0
        if self._validator_name == "iban":
            return 15 <= len(pattern_text) <= 34
        return True


def _build_registry() -> RecognizerRegistry:
    registry = RecognizerRegistry()
    registry.add_recognizer(PatternRecognizer(
        supported_entity="EMAIL_ADDRESS",
        patterns=[Pattern(name="email", regex=_EMAIL_REGEX, score=0.85)],
    ))
    registry.add_recognizer(PatternRecognizer(
        supported_entity="IP_ADDRESS",
        patterns=[Pattern(name="ip", regex=_IP_REGEX, score=0.85)],
    ))
    registry.add_recognizer(PatternRecognizer(
        supported_entity="IBAN_CODE",
        patterns=[Pattern(name="iban", regex=_IBAN_REGEX, score=0.8)],
    ))
    registry.add_recognizer(_ValidatedPatternRecognizer(
        supported_entity="PHONE_NUMBER",
        patterns=[Pattern(name="phone", regex=_PHONE_REGEX, score=0.6)],
        validator_name="phone",
    ))
    registry.add_recognizer(_ValidatedPatternRecognizer(
        supported_entity="CREDIT_CARD",
        patterns=[Pattern(name="card", regex=_CARD_REGEX, score=0.6)],
        validator_name="luhn",
    ))
    return registry


class _NoopNlpEngine(NlpEngine):
    """regex-only 模式的空 NLP 引擎：不加载 spaCy 模型（无联网下载）。

    仅模式识别器（email/电话/IBAN/IP/信用卡）不需要 token/实体上下文，
    process_text 返回空 NlpArtifacts 即可。
    """

    def load(self) -> None:
        pass

    def is_loaded(self) -> bool:
        return True

    def process_text(self, text: str, language: str) -> NlpArtifacts:
        return NlpArtifacts(
            entities=[], tokens=[], tokens_indices=[], lemmas=[],
            nlp_engine=self, language=language,
        )

    def process_batch(self, texts, language, **kwargs):
        return [self.process_text(t, language) for t in texts]

    def get_supported_entities(self) -> list:
        return []

    def get_supported_languages(self) -> list:
        return ["en"]

    def is_punct(self, word: str) -> bool:
        return False

    def is_stopword(self, word: str) -> bool:
        return False


class PIIGuard:
    """识别并替换文本中的 PII。token 形如 <EMAIL_ADDRESS_0>。"""

    def __init__(self):
        self._analyzer = AnalyzerEngine(
            registry=_build_registry(),
            nlp_engine=_NoopNlpEngine(),
            default_score_threshold=0.5,
            supported_languages=["en"],
        )
        self._enabled = os.getenv("PII_GUARD_MODE", "regex_only") != "off"

    def pseudonymize(self, text: str) -> Tuple[str, Dict[str, str]]:
        """同步方法（CPU 密集），async 侧用 pseudonymize_async。"""
        if not text or not self._enabled:
            return text, {}
        results = self._analyzer.analyze(text=text, language="en")
        if not results:
            return text, {}

        # 去重（同区间只保留一个）+ 按位置排序
        unique: list = []
        for r in sorted(results, key=lambda x: (x.start, -(x.end - x.start))):
            if any(u.start == r.start and u.end == r.end for u in unique):
                continue
            unique.append(r)

        mapping: Dict[str, str] = {}
        pieces = []
        last_end = 0
        for i, r in enumerate(sorted(unique, key=lambda x: x.start)):
            token = f"<{r.entity_type}_{i}>"
            mapping[token] = r.entity_type
            pieces.append(text[last_end:r.start])
            pieces.append(token)
            last_end = r.end
        pieces.append(text[last_end:])
        return "".join(pieces), mapping


_guard: PIIGuard | None = None


def get_pii_guard() -> PIIGuard:
    global _guard
    if _guard is None:
        logger.info(f"[pii_guard] 初始化（mode={os.getenv('PII_GUARD_MODE', 'regex_only')}）")
        _guard = PIIGuard()
    return _guard


async def pseudonymize_async(text: str) -> Tuple[str, Dict[str, str]]:
    """LLM 调用前的假名化入口（经 to_thread，避免阻塞事件循环）。"""
    return await asyncio.to_thread(get_pii_guard().pseudonymize, text)
