"""透明度披露单一内容源（GDPR Art.13/14 + DSA Art.27 + AI Act Art.50，2026-08）。

web_ui.py 的 GET /transparency（纯文本）与 app.py 的 GET /v1/transparency（JSON）
共用本模块，避免双份维护。

契约常量单一化（2026-08）：版本号/DSAR 契约/留存天数/推荐调参/国家列表均引用
models.schemas 与 recommend/collectors 的常量，不再在本模块手写副本——
正文内容变更时只递增 models.schemas.TRANSPARENCY_VERSION。

版本纪律：
- 第 2/3 节与 /v1/disclosure/parameters（DISCLOSURE_VERSION）相关，
  推荐参数变更时须同步复核此处措辞并递增两个版本号；
- 第 6 节留存天数引用 DEFAULT_RETENTION_DAYS 默认值，
  管理员可经 /admin/api/retention/config 调整，故措辞注明"默认"；
- sections 的 id 是稳定机器键，新增章节只能追加、不得重命名/删除已有 id。
"""

from models.schemas import (
    DISCLOSURE_VERSION, TRANSPARENCY_VERSION,
    DSAR_REQUEST_TYPES, DSAR_CONTACT_MIN_LENGTH, DSAR_CONTACT_MAX_LENGTH,
    DSAR_NOTE_MAX_LENGTH, DSAR_RATE_LIMIT, DSAR_RESPONSE_DEADLINE_DAYS,
    DEFAULT_RETENTION_DAYS,
)
from services.recommend import (
    TOP_K_RECALL,
    RERANK_DEPTH,
    get_recommend_rerank_mode,
    get_recommend_min_similarity,
)
from services.qdrant_store import (
    ANCHOR_MATCH_THRESHOLD,
    ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD,
)
from services.collectors.countries import EU_COUNTRIES

LAST_UPDATED = "2026-08-25"
SYSTEM_NAME = "joytag"
DISCLOSURE_URL = "/v1/disclosure/parameters"

_COUNTRIES_TEXT = "/".join(EU_COUNTRIES)

DSAR_SUBMISSION = {
    "endpoint": "POST /v1/dsar/request",
    "content_type": "application/json",
    "fields": [
        {
            "name": "request_type",
            "required": True,
            "allowed_values": list(DSAR_REQUEST_TYPES),
            "description": "请求类型：access(访问权) / erasure(删除权) / objection(反对权)",
        },
        {
            "name": "contact",
            "required": True,
            "description": f"联系方式（邮箱或其他，{DSAR_CONTACT_MIN_LENGTH}-{DSAR_CONTACT_MAX_LENGTH} 字符，"
                           f"{DSAR_RESPONSE_DEADLINE_DAYS} 天响应时限内回复用）",
        },
        {
            "name": "subject_note",
            "required": False,
            "description": f"请求说明（可选，如涉及的词条或数据，最长 {DSAR_NOTE_MAX_LENGTH} 字符）",
        },
    ],
    "rate_limit": DSAR_RATE_LIMIT,
    "response_deadline_days": DSAR_RESPONSE_DEADLINE_DAYS,
}

SECTIONS = [
    {
        "id": "what_this_is",
        "title": "1. 本系统是什么",
        "paragraphs": [
            "Joytag 是为 Joybuy 欧洲站服务的本地化长尾标签推荐系统：从 Amazon 搜索建议接口"
            f"（{_COUNTRIES_TEXT} 六国站点）、eBay 搜索建议接口与中文电商搜索建议（淘宝搜索接口）采集趋势词，"
            "经 AI 种子翻译、多语言向量匹配、规则优先的文化合规评估与人工审核后，为商品标题推荐合规的搜索标签，用于站内搜索召回支持。",
            "中文锚点采集保留固定类目种子，同时使用新锚点和高热度建议词作为动态种子；每轮最多查询 80 个种子。淘宝公开 suggest 返回的热度只在单次响应内部归一化为相对分数，不代表真实搜索量、销量或成交量。",
            "数据最小化：仅采集公开搜索建议词，不存储用户名、帖子 ID 或正文；"
            "商品标题仅在推荐请求的瞬间被处理，默认只在本地向量化；仅当服务端启用 LLM 精排时，"
            "才会在发送外部 AI 前经假名化（邮箱/电话/IP/卡号等模式替换为占位符），不写入日志。",
        ],
    },
    {
        "id": "how_recommendation_works",
        "title": "2. 推荐系统如何工作（DSA Art.27）",
        "paragraphs": [
            "推荐结果由以下参数共同决定（按重要性排序）：",
            f"  向量相似度：标题向量与标签向量余弦相似度召回 top-{TOP_K_RECALL}，默认按该分数返回 top-k（本地 GTE 多语言模型，768 维）；",
            f"  最低置信度：相似度低于 {get_recommend_min_similarity():.2f} 的候选不参与排序，若没有候选达到门槛则返回空结果；",
            f"  锚点对齐：海外词先按类目筛选中文锚点，再在同一多语言向量空间匹配；默认阈值为 {ANCHOR_MATCH_THRESHOLD:.2f}，无类目时为 {ANCHOR_MATCH_UNCATEGORIZED_THRESHOLD:.2f}；",
            f"  可选 LLM 精排：仅当服务端配置为 llm 时，综合语义匹配、文化合规、趋势热度和去重，从 top-{RERANK_DEPTH} 选出最终结果并生成推荐理由；",
            "  合规过滤（硬性排除）：仅推荐合规状态为「可复用」的标签；"
            "被拦截词（含环保声明等 UCPD 2024/825 Annex I 类别）永不参与；",
            f"  国家过滤（硬性排除）：仅返回目标国家（{_COUNTRIES_TEXT}）的标签。",
            "调用方可调整的参数：target_country（目标国家）、category（类目）、top_k（返回数量 1-10）。",
            "机器可读的参数披露（版本化，变更即升级版本号）：GET " + DISCLOSURE_URL,
        ],
    },
    {
        "id": "ai_involvement",
        "title": "3. AI 参与声明（AI Act Art.50）",
        "paragraphs": [
            "本系统的动态采集种子翻译，以及有中文锚点但规则未覆盖的文化合规评估由 AI 模型参与完成；"
            "海外词与中文锚点直接进行多语言向量匹配，无锚点词不调用合规 LLM。推荐默认使用本地向量排序，"
            "服务端可选启用 LLM 精排。推荐 API 响应的每条标签带 ai_generated 标记：向量排序项为 false。",
            "若您将本系统输出用于面向消费者的内容，请另行披露 AI 参与。",
        ],
    },
    {
        "id": "data_sources_and_objection",
        "title": "4. 数据来源与反对机制（GDPR Art.14(5)(b)）",
        "paragraphs": [
            "我们处理的词条来自以下公开来源：Amazon 搜索建议接口（六国站点）、"
            "eBay 搜索建议接口、淘宝搜索建议公开接口。"
            "淘宝公开 suggest 当前没有被本系统依赖的全局热榜、分页或时间范围参数；"
            "相同来源/种子的响应会按退避策略缓存，以减少重复请求。"
            "逐项告知每个数据主体被认为不可能或需要不成比例的努力，"
            "故依据 GDPR Art.14(5)(b) 以本披露提供公开告知。",
            "您随时可以反对处理（Art.21）或行使访问权（Art.15）、删除权（Art.17）——"
            f"按第 5 节说明提交请求，我们将在 {DSAR_RESPONSE_DEADLINE_DAYS} 天内（Art.12(3)）"
            "通过您提供的联系方式回复。",
        ],
    },
    {
        "id": "dsar",
        "title": "5. 主体权利请求（DSAR）",
        "paragraphs": [
            "无需网页表单。请向以下端点提交 JSON 请求：",
            "  POST /v1/dsar/request",
            "  Content-Type: application/json",
            f'  {{"request_type": "{"/".join(DSAR_REQUEST_TYPES)}",',
            f'   "contact": "邮箱或其他联系方式（必填，{DSAR_RESPONSE_DEADLINE_DAYS} 天内回复用）",',
            '   "subject_note": "请求说明（可选，如涉及的词条）"}',
            f"限流：同一 IP 每小时最多 {DSAR_RATE_LIMIT.split('/')[0]} 次。",
            '成功响应：{"ticket_id": "...", "status": "received", "message": "..."}。',
            f"我们将依据 GDPR Art.12(3) 在 {DSAR_RESPONSE_DEADLINE_DAYS} 天内通过您提供的联系方式回复。",
        ],
    },
    {
        "id": "retention_and_security",
        "title": "6. 数据留存与安全",
        "paragraphs": [
            f"LLM 调用留痕默认留存 {DEFAULT_RETENTION_DAYS['llm_trace']} 天；"
            f"血缘与审计记录默认 {DEFAULT_RETENTION_DAYS['lineage']} 天（13 个月），"
            "之后自动清除（每日 UTC 03:00）。",
            "管理操作全部记录于防篡改哈希链审计日志。"
            "所有管理功能需 Keycloak 账号 + 双因素认证。",
        ],
    },
]


def transparency_payload() -> dict:
    """组装 /v1/transparency 的 JSON 结构（app.py 转 Pydantic 后返回）。"""
    payload = {
        "version": TRANSPARENCY_VERSION,
        "last_updated": LAST_UPDATED,
        "system_name": SYSTEM_NAME,
        "disclosure_url": DISCLOSURE_URL,
        "dsar_submission": DSAR_SUBMISSION,
        "sections": SECTIONS,
    }
    payload["sections"] = [dict(section) for section in SECTIONS]
    payload["sections"][1] = dict(payload["sections"][1])
    payload["sections"][1]["paragraphs"] = list(payload["sections"][1]["paragraphs"])
    payload["sections"][1]["paragraphs"].append(
        f"当前服务端推荐排序模式：{get_recommend_rerank_mode()}。"
    )
    return payload


def render_transparency_text() -> str:
    """由同一内容源渲染 /transparency 的纯文本（text/plain，人读）。"""
    lines = [
        "Joytag 透明度与数据主体权利",
        "GDPR Art.13/14 透明度 · DSA Art.27 推荐系统披露 · AI Act Art.50 · UCPD 合规声明",
        f"版本 {TRANSPARENCY_VERSION}（{LAST_UPDATED}）",
        "",
    ]
    for section in SECTIONS:
        lines.append(section["title"])
        lines.extend(section["paragraphs"])
        if section["id"] == "how_recommendation_works":
            lines.append(f"当前服务端推荐排序模式：{get_recommend_rerank_mode()}。")
        lines.append("")
    lines.append("相关端点：")
    lines.append(f"  机器可读参数披露  GET {DISCLOSURE_URL}")
    lines.append(f"  本披露 JSON 版    GET /v1/transparency")
    lines.append(f"  提交 DSAR 请求     {DSAR_SUBMISSION['endpoint']}")
    return "\n".join(lines)
