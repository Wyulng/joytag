"""中文搜索建议来源适配器。

当前生产默认只使用淘宝公开 suggest。官方关键词推荐接口需要单独的
AppKey/AppSecret、签名、授权 session 和产品/计划上下文，因此这里只提供
参数构造与明确的未启用适配器，不会在没有权限配置时偷偷切换或发起请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ChineseSourceAdapter(Protocol):
    name: str

    def fetch(self, term: str, category: str | None = None) -> list[dict] | None:
        """返回候选列表；None 表示来源请求/解析失败。"""

    def fetch_with_status(self, term: str, category: str | None = None) -> "SourceFetchResult":
        """返回统一响应记录，以区分缓存、外发请求和失败状态。"""


@dataclass(frozen=True)
class SourceFetchResult:
    candidates: list[dict] | None
    outbound: bool
    status_code: int | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class TaobaoSuggestAdapter:
    name: str = "taobao_suggest"

    def fetch(self, term: str, category: str | None = None) -> list[dict] | None:
        return self.fetch_with_status(term, category).candidates

    def fetch_with_status(self, term: str, category: str | None = None) -> SourceFetchResult:
        # 延迟导入，避免 cn_ecommerce 与适配器接口形成循环导入。
        from services.collectors.cn_ecommerce import _fetch_taobao_suggest_result

        return _fetch_taobao_suggest_result(term, category)


@dataclass(frozen=True)
class AlibabaKeywordRecommendAdapter:
    """官方关键词推荐接口的 shadow 适配器，不默认执行网络请求。"""

    name: str = "alibaba_keyword_recommend"
    api_name: str = "alibaba.scbp.ad.keyword.recommend.word"
    requires_authorization: bool = True

    @staticmethod
    def build_request_params(
        keyword: str,
        *,
        order_by: str = "searchIndex",
        order: str = "desc",
    ) -> dict[str, Any]:
        allowed = {"searchIndex", "buyIndex", "star", "keyword"}
        if order_by not in allowed:
            raise ValueError(f"unsupported order_by: {order_by}")
        if order not in {"asc", "desc"}:
            raise ValueError(f"unsupported order: {order}")
        return {
            "keyword": keyword,
            "order_by": order_by,
            "order": order,
        }

    def fetch(self, term: str, category: str | None = None) -> list[dict] | None:
        raise RuntimeError(
            f"{self.api_name} requires signed authorized access and is shadow-only"
        )

    def fetch_with_status(self, term: str, category: str | None = None) -> SourceFetchResult:
        return SourceFetchResult(candidates=self.fetch(term, category), outbound=False)


SOURCE_REGISTRY = {
    "taobao_suggest": {"production_eligible": True, "requires_authorization": False},
    # Official documentation requires authorization and product-line context.
    "alibaba_keyword_recommend": {"production_eligible": False, "requires_authorization": True},
}


def parse_source_list(value: str | None, *, fallback: str = "taobao_suggest") -> tuple[str, ...]:
    names = [part.strip().lower() for part in (value or "").split(",") if part.strip()]
    if not names:
        names = [fallback]
    return tuple(dict.fromkeys(name for name in names if name in SOURCE_REGISTRY))


def production_sources(value: str | None) -> tuple[str, ...]:
    configured = parse_source_list(value)
    eligible = tuple(name for name in configured if SOURCE_REGISTRY[name]["production_eligible"])
    # Keep a validated baseline if the configured list contains only unapproved sources.
    return eligible or ("taobao_suggest",)


def get_cn_source_adapter(mode: str = "taobao_suggest") -> ChineseSourceAdapter:
    if (mode or "").strip().lower() == "taobao_suggest":
        return TaobaoSuggestAdapter()
    if (mode or "").strip().lower() == "alibaba_keyword_recommend":
        return AlibabaKeywordRecommendAdapter()
    raise ValueError(f"unsupported Chinese source mode: {mode}")
