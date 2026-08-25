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


@dataclass(frozen=True)
class TaobaoSuggestAdapter:
    name: str = "taobao_suggest"

    def fetch(self, term: str, category: str | None = None) -> list[dict] | None:
        # 延迟导入，避免 cn_ecommerce 与适配器接口形成循环导入。
        from services.collectors.cn_ecommerce import _fetch_taobao_suggest

        return _fetch_taobao_suggest(term, category)


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


def get_cn_source_adapter(mode: str = "taobao_suggest") -> ChineseSourceAdapter:
    if (mode or "").strip().lower() == "taobao_suggest":
        return TaobaoSuggestAdapter()
    if (mode or "").strip().lower() == "alibaba_keyword_recommend":
        return AlibabaKeywordRecommendAdapter()
    raise ValueError(f"unsupported Chinese source mode: {mode}")
