import os
import uuid
import json
import asyncio
import logging
from functools import wraps
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Body, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from services.logging_config import setup_logging, set_request_id, get_request_id, log_safe_hash

setup_logging()
logger = logging.getLogger(__name__)

from models.schemas import (
    PendingListResponse, PendingReviewItem,
    ApproveRequest, ApproveResponse, RejectRequest, RejectResponse,
    RecommendRequest, RecommendResponse, RecommendItem,
    LocalTagListResponse, LocalTagItem,
    AnchorListResponse, AnchorItem,
    UpdateScheduleRequest,
    DisclosureParameter, DisclosureParameters,
    DsarCreateRequest, DsarCreateResponse, DsarSearchRequest,
    TransparencyResponse,
    DISCLOSURE_VERSION, DSAR_RATE_LIMIT,
)
from services.embedding import get_embedding
from services.qdrant_store import (
    list_pending_reviews, get_pending_by_id, delete_pending_review,
    upsert_local_tag, list_local_tags, get_pending_review_count, delete_local_tag, count_local_tags,
    list_cn_anchors, count_cn_anchors, delete_cn_anchor,
    batch_count_linked_local_tags,
    list_blocked_decisions, count_blocked_decisions, delete_blocked_decision,
    get_dashboard_stats
)
from services.collectors import overseas_trends, cn_longtail
from services.collectors.countries import EU_COUNTRIES
from services.rule_manager import (
    add_safe_word, add_banned_word, get_safe_words, get_banned_words,
    get_banned_entries, get_safe_entries, remove_banned_word, remove_safe_word,
    VALID_COUNTRIES,
)
from services.recommend import (
    retrieve_candidate_tags, rerank_tags_with_llm, filter_candidates_by_category,
    TOP_K_RECALL, RERANK_DEPTH,
)
from services.task_scheduler import init_scheduler, shutdown_scheduler, add_job, remove_job, reschedule, run_job_now
from services.scheduler_store import list_schedules, add_schedule, delete_schedule, update_schedule, get_schedule
from services.http_client import close_http_client
from services.db import init_db
from services.audit import record_event as audit_record_event, verify_chain, list_audit
from services.auth import (
    require_admin_session, require_csrf, require_role, require_scope,
    login_redirect, handle_callback, logout,
)
from services.dsar import (
    create_request as dsar_create_request,
    list_requests as dsar_list_requests,
    get_request as dsar_get_request,
    search_subject_data as dsar_search_subject_data,
    update_status as dsar_update_status,
    complete_erasure_request as dsar_complete_erasure,
)
from services.retention import (
    get_retention_policies, set_retention_policy, run_all_purges,
)
from services.transparency import transparency_payload

# ==================== 审计装饰器（GDPR Art.5(2) 可责性，2026-08） ====================
def _guard_rule_country(country: str, *, status: int = 400) -> str:
    """规则库国家防御校验：非法国家直接 4xx，不让 rule_manager 的 ValueError 穿成 500。

    approve/reject 用 400（数据本身非法）；规则库路径端点用 404（该国家资源不存在）。
    """
    if country.strip().lower() not in VALID_COUNTRIES:
        raise HTTPException(status_code=status, detail=f"不支持的国家代码: {country}")
    return country


def _audit_safe(value):
    """审计快照值转 JSON 安全形式。"""
    if isinstance(value, BaseModel):
        return value.model_dump()
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def audited(action: str, resource_type: str, *, resource_id_keys: tuple = (), snapshot_keys: tuple = ()):
    """审计装饰器：变更成功后写 hash-chain 审计（操作者身份/资源快照/结果）。
    审计写入失败 → 500（可责性优先：变更不得无记录发生；操作已执行但无法举证时立即告警）。"""
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next((a for a in args if isinstance(a, Request)), None)
            session = getattr(request.state, "session", None) if request else None
            result = await fn(*args, **kwargs)
            resource_id = next((str(kwargs[k]) for k in resource_id_keys if k in kwargs), None)
            snapshot = {k: _audit_safe(kwargs[k]) for k in snapshot_keys if k in kwargs}
            try:
                audit_record_event(
                    actor_sub=(session or {}).get("sub", "anonymous"),
                    actor_username=(session or {}).get("username", ""),
                    actor_roles=(session or {}).get("roles", []),
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id or "",
                    resource_snapshot=snapshot,
                    detail={"response": _audit_safe(result)},
                    request_id=get_request_id(),
                    ip=request.client.host if request and request.client else "",
                )
            except Exception as e:
                logger.critical(f"[audit] 审计写入失败 action={action}: {e}")
                raise HTTPException(status_code=500, detail="操作已执行但审计记录失败，请立即联系管理员排查数据库")
            return result
        return wrapper
    return deco


async def lifespan(app: FastAPI):
    logger.info("[app] 启动应用，初始化调度器...")
    init_scheduler()
    # 初始化合规数据库（审计/trace/lineage），失败不阻塞启动（db.py 内部告警）
    try:
        await asyncio.to_thread(init_db)
        logger.info("[app] 合规数据库 schema 就绪")
    except Exception as e:
        logger.critical(f"[app] 合规数据库初始化异常（审计/trace 将不可用）: {e}")
    # 预热 embedding 模型，提前发现加载问题
    try:
        from services.embedding import get_embedding
        await get_embedding("test")
        logger.info("[app] embedding 模型加载成功")
    except Exception as e:
        logger.critical(f"[app] embedding 模型加载失败: {e}")
    yield
    logger.info("[app] 关闭应用，清理调度器...")
    shutdown_scheduler()
    await close_http_client()

app = FastAPI(title="Joytag API", version="2.0.0", lifespan=lifespan)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ==================== Request ID 中间件 ====================
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    set_request_id(request_id)
    logger.info(f"{request.method} {request.url.path}", extra={"request_id": request_id})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

# CORS configuration (whitelist from env)
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
)

# ==================== 健康检查（免认证：docker healthcheck 依赖） ====================
@app.get("/health")
async def health(deep: bool = Query(False)):
    checks = {}
    healthy = True

    # Qdrant 连通性（v1.9.0 无 REST /health 端点，用客户端 get_collections 检测）
    try:
        from services.qdrant_store import get_qdrant_client
        client = get_qdrant_client()
        client.get_collections()
        checks["qdrant"] = "ok"
    except Exception as e:
        checks["qdrant"] = f"error: {e}"
        healthy = False

    # Postgres（审计/trace/lineage 依赖，2026-08 新增）
    try:
        from services.db import is_db_available
        db_ok = await asyncio.to_thread(is_db_available)
        checks["postgres"] = "ok" if db_ok else "unavailable"
        if not db_ok:
            healthy = False
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        healthy = False

    # Embedding 模型就绪
    try:
        from services.embedding import _get_model
        _get_model()
        checks["embedding"] = "ok"
    except Exception as e:
        checks["embedding"] = f"error: {e}"
        healthy = False

    # LLM (仅 deep=true)
    if deep:
        try:
            from services.llm_provider import get_llm_provider
            await get_llm_provider().chat_completion(
                [{"role": "user", "content": "ping"}], temperature=0, max_tokens=5
            )
            checks["llm"] = "ok"
        except Exception as e:
            checks["llm"] = f"error: {e}"
            healthy = False

    status_code = 200 if healthy else 503
    return JSONResponse(
        content={"status": "healthy" if healthy else "degraded", "checks": checks},
        status_code=status_code
    )

# ==================== Keycloak OIDC 登录 ====================
@app.get("/auth/login")
async def auth_login(request: Request, next: str = Query(None)):
    """发起授权码 + PKCE 登录（302 到 Keycloak）"""
    return await login_redirect(request, next)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """授权码回调：换 token → 验 ID token → 签发服务端会话 cookie"""
    return await handle_callback(request)

@app.get("/auth/logout")
async def auth_logout(request: Request):
    """退出：清会话 cookie + Keycloak end_session"""
    return await logout(request)

# ==================== DSA Art.27 推荐系统参数披露（公开，机器可读） ====================
@app.get("/v1/disclosure/parameters", response_model=DisclosureParameters)
async def disclosure_parameters():
    """DSA Art.27 推荐系统透明度：机器可读参数披露（免认证——它本身就是披露）。
    版本号与调参数值引用 models.schemas / services.recommend 常量（唯一权威源），
    参数变更时只改常量并递增 DISCLOSURE_VERSION。
    """
    return DisclosureParameters(
        version=DISCLOSURE_VERSION,
        last_updated="2026-08-15",
        system_name="joytag",
        description="Joytag 为 Joybuy 欧洲站商品标题生成本地化长尾标签推荐，供站内搜索召回使用（平台功能支持型推荐系统）。",
        input_signals=[
            DisclosureParameter(
                key="product_title",
                description="商品标题（自然语言）。发送 LLM 前经 Presidio 假名化，不落完整日志。",
                relative_importance="首要信号：决定候选标签的语义召回范围。",
            ),
            DisclosureParameter(
                key="category",
                description="商品类目（可选）。参与查询向量构造与候选过滤。",
                relative_importance="次要信号：提升同品类标签的相关性。",
            ),
            DisclosureParameter(
                key="target_country",
                description=f"目标国家（{'/'.join(EU_COUNTRIES)}）。硬性过滤条件。",
                relative_importance="决定性过滤：非目标国家的标签一律不参与。",
            ),
        ],
        ranking_parameters=[
            DisclosureParameter(
                key="vector_similarity",
                description="标题向量与标签向量的余弦相似度（bge-small-zh-v1.5，512 维，本地推理）。",
                relative_importance=f"第一级排序：召回 top-{TOP_K_RECALL} 候选。",
                values={"model": "BAAI/bge-small-zh-v1.5", "dim": 512, "top_n": TOP_K_RECALL},
            ),
            DisclosureParameter(
                key="llm_rerank",
                description=f"LLM 精排：在 top-{RERANK_DEPTH} 候选中综合语义匹配、文化合规、趋势热度、去重选出 top-k。",
                relative_importance="第二级排序：决定最终展示顺序与推荐理由文本。",
                values={"provider": "LLM_PROVIDER env 配置（默认 deepseek-chat）", "temperature": 0.2, "top_n": RERANK_DEPTH},
            ),
            DisclosureParameter(
                key="trend_score",
                description="标签采集时的趋势热度分（来源平台热度归一化）。",
                relative_importance="精排参考因子：同语义下热度高者优先。",
            ),
        ],
        compliance_filters=[
            DisclosureParameter(
                key="compliance_status",
                description="仅返回合规状态为「可复用」的标签；「存疑」须人工审核通过、「需拦截」永不参与推荐。",
                relative_importance="硬性排除：拦截词（含 UCPD 2024/825 Annex I 类别）不可被推荐。",
            ),
            DisclosureParameter(
                key="country_match",
                description="标签目标国家必须等于请求目标国家。",
                relative_importance="硬性排除。",
            ),
            DisclosureParameter(
                key="category_filter",
                description="类目软过滤：标签无类目或类目与请求一致才保留。",
                relative_importance="软性过滤（可调参数）。",
            ),
        ],
        data_sources=[
            DisclosureParameter(
                key="overseas_trends",
                description=f"海外趋势词：Amazon 搜索建议接口（六国站点，{'/'.join(EU_COUNTRIES)}）+ eBay 搜索建议接口。仅取公开建议词，不存用户身份；查询种子由中文锚点词经 LLM 翻译生成。",
                relative_importance="local_tags 主要来源。",
            ),
            DisclosureParameter(
                key="cn_anchors",
                description="中文锚点词：淘宝搜索建议 API（公开接口，种子类目驱动）。",
                relative_importance="跨语言语义锚定来源。",
            ),
            DisclosureParameter(
                key="llm_assessment",
                description="LLM 翻译 + 文化合规评估（规则库优先，规则未命中才调用 LLM）。",
                relative_importance="决定标签合规状态（可复用/需拦截/存疑）。",
            ),
            DisclosureParameter(
                key="human_review",
                description="人工审核：存疑词由运营人员复核（通过/拒绝，拒绝理由必填并留痕）。",
                relative_importance="最终裁定层。",
            ),
        ],
        ai_involvement=[
            DisclosureParameter(
                key="llm_rerank",
                description="推荐排序由 LLM 完成（AI Act Art.50 披露）。",
                relative_importance="每条推荐带 ai_generated 标记；LLM 不可用时回退纯向量排序（ai_generated=false）。",
            ),
            DisclosureParameter(
                key="llm_assessment",
                description="标签的翻译与合规评估经 LLM 参与，非完全人工产生。",
                relative_importance="消费者面向文本如需使用本标签，应披露 AI 参与（Art.50(1)）。",
            ),
        ],
        user_controls=[
            DisclosureParameter(
                key="target_country",
                description="调用方可按目标国家调整推荐范围。",
                relative_importance="用户可修改参数（DSA Art.27）。",
            ),
            DisclosureParameter(
                key="category",
                description="调用方可传商品类目影响候选与排序。",
                relative_importance="用户可修改参数（DSA Art.27）。",
            ),
            DisclosureParameter(
                key="top_k",
                description="调用方可指定返回数量（1-10）。",
                relative_importance="用户可修改参数（DSA Art.27）。",
            ),
        ],
    )


@app.get("/v1/transparency", response_model=TransparencyResponse)
async def transparency_json():
    """公开透明度披露（JSON，机器可读）。与 /transparency 纯文本共用 services.transparency 内容源。"""
    return TransparencyResponse(**transparency_payload())


# ==================== Tag 推荐端点（服务间 Bearer + scope joytag:recommend） ====================
@app.post("/v1/tag/recommend", response_model=RecommendResponse)
@limiter.limit("20/minute")
async def recommend_tags(request: Request, req: RecommendRequest,
                         _scope=Depends(require_scope("joytag:recommend"))):
    """
    根据商品信息推荐本地化 Tag。
    流程：生成查询向量 → Qdrant 检索 → 业务过滤 → LLM 精排 → 返回附带推荐理由的 Tag 列表。
    """
    # GDPR 最小化：不落完整标题，只记长度 + 哈希前缀（log_safe_hash 共享约定，供排查关联）
    logger.info(
        f"[api] 标签推荐请求: title_len={len(req.title)}, "
        f"title_hash={log_safe_hash(req.title)}, "
        f"country={req.target_country}"
    )
    # 1. 构造查询文本
    query_text = req.title
    if req.category:
        query_text += f" {req.category}"

    # 2. 检索候选 Tag（内部已过滤 compliance_status 和 country）
    candidates = await retrieve_candidate_tags(
        query_text=query_text,
        target_country=req.target_country,
        category=req.category,
        top_k=TOP_K_RECALL  # 召回稍多候选供精排选择（精排深度 RERANK_DEPTH）
    )
    total_candidates = len(candidates)

    # 3. 应用层类目过滤（可选，当前不强制）
    filtered = filter_candidates_by_category(candidates, req.category)
    filtered_count = len(filtered)

    if not filtered:
        logger.info(f"[api] 标签推荐无候选结果: total={total_candidates}")
        return RecommendResponse(
            recommendations=[],
            total_candidates=total_candidates,
            filtered_candidates=filtered_count
        )

    # 4. LLM 精排
    recommendations = await rerank_tags_with_llm(
        candidates=filtered,
        product_title=req.title,
        product_category=req.category,
        target_country=req.target_country,
        max_output=req.top_k
    )

    # 5. 构造响应（补全相似度 + provenance 溯源解释字段，DSA Art.27 / AI Act Art.50）
    word_to_candidate = {c["word"]: c for c in filtered}
    items = []
    for rec in recommendations:
        cand = word_to_candidate.get(rec["word"], {})
        items.append(RecommendItem(
            word=rec["word"],
            reason=rec["reason"],
            similarity=cand.get("similarity"),
            source=cand.get("source"),
            compliance_reason=cand.get("compliance_reason") or cand.get("reason"),
            anchor_cn_word=cand.get("anchor_cn_word"),
            trend_score=cand.get("trend_score", 0.0),
            ai_generated=rec.get("ai_generated", True),
        ))

    logger.info(f"[api] 标签推荐完成: 返回 {len(items)} 个推荐")
    return RecommendResponse(
        recommendations=items,
        total_candidates=total_candidates,
        filtered_candidates=filtered_count,
        ai_assisted=True,
        parameters_version=DISCLOSURE_VERSION,
        disclosure_url="/v1/disclosure/parameters",
    )

# ==================== 采集器触发接口 ====================
@app.post("/admin/api/collect/overseas")
@audited("collect.overseas", "collection")
async def trigger_overseas_collection(request: Request = None,
                                      _auth=Depends(require_role("operator")),
                                      _csrf=Depends(require_csrf)):
    logger.info("[api] 触发海外采集")
    stats = await overseas_trends.run_overseas_collector()
    logger.info(f"[api] 海外采集完成: {stats}")
    return {"status": "done", "message": "海外采集已完成", **stats}


@app.post("/admin/api/collect/cn")
@audited("collect.cn", "collection")
async def trigger_cn_collection(request: Request = None,
                                _auth=Depends(require_role("operator")),
                                _csrf=Depends(require_csrf)):
    logger.info("[api] 触发中文采集")
    final = await cn_longtail.run_cn_collector()
    logger.info(f"[api] 中文采集完成: {final}")
    return {"status": "done", "message": "中文采集已完成", **final}


@app.post("/admin/api/schedules/{schedule_id}/run")
@audited("schedule.run", "schedule", resource_id_keys=("schedule_id",))
async def run_schedule_now(schedule_id: str, request: Request = None,
                           _auth=Depends(require_role("operator")),
                           _csrf=Depends(require_csrf)):
    """手动立即执行指定定时任务"""
    return await run_job_now(schedule_id)

# ==================== 定时任务调度接口 ====================
@app.get("/admin/api/schedules")
async def get_schedules(_auth=Depends(require_admin_session)):
    """列出所有定时任务"""
    return {"schedules": list_schedules()}


@app.post("/admin/api/schedules")
@audited("schedule.create", "schedule", snapshot_keys=("name", "task_type", "cron"))
async def create_schedule(
    request: Request = None,
    name: str = Body(...),
    task_type: str = Body(...),
    cron: str = Body(...),
    _auth=Depends(require_role("operator")),
    _csrf=Depends(require_csrf)
):
    """创建新定时任务"""
    if task_type not in ("cn", "overseas"):
        raise HTTPException(status_code=400, detail="task_type must be 'cn' or 'overseas'")
    schedule = add_schedule(name=name, task_type=task_type, cron=cron)
    add_job(schedule)
    return {"success": True, "schedule": schedule.to_dict()}


@app.delete("/admin/api/schedules/{schedule_id}")
@audited("schedule.delete", "schedule", resource_id_keys=("schedule_id",))
async def delete_schedule_api(schedule_id: str, request: Request = None,
                              _auth=Depends(require_role("operator")),
                              _csrf=Depends(require_csrf)):
    """删除定时任务"""
    remove_job(schedule_id)
    deleted = delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True}


@app.patch("/admin/api/schedules/{schedule_id}")
@audited("schedule.update", "schedule", resource_id_keys=("schedule_id",), snapshot_keys=("req",))
async def update_schedule_api(schedule_id: str, req: UpdateScheduleRequest, request: Request = None,
                              _auth=Depends(require_role("operator")),
                              _csrf=Depends(require_csrf)):
    """更新定时任务（启用/禁用/修改cron）"""
    payload = req.model_dump(exclude_none=True)
    schedule = update_schedule(schedule_id, **payload)
    if not schedule:
        raise HTTPException(status_code=404, detail="任务不存在")
    reschedule(schedule)
    return {"success": True, "schedule": schedule.to_dict()}

# ==================== 概览统计 ====================
@app.get("/admin/api/stats")
async def admin_stats(_auth=Depends(require_admin_session)):
    """后台概览统计数据"""
    return get_dashboard_stats()


# ==================== Admin 审核端点 ====================
@app.get("/admin/api/pending/count")
async def get_pending_count(country: str = Query(None), _auth=Depends(require_admin_session)):
    count = get_pending_review_count(country=country)
    return {"count": count}


@app.get("/admin/api/pending", response_model=PendingListResponse)
async def get_pending_list(
    country: str = Query(None, description="按国家筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str = Query(None, description="下一页游标（scroll token）"),
    _auth=Depends(require_admin_session)
):
    # cursor 优先于 offset，cursor 是 Qdrant scroll 返回的 offset
    actual_offset = cursor if cursor else offset
    items_raw, next_offset = list_pending_reviews(country=country, limit=limit, offset=actual_offset)
    items = []
    for raw in items_raw:
        p = raw["payload"]
        items.append(PendingReviewItem(
            id=raw["id"],
            word=p.get("word", ""),
            country=p.get("country", ""),
            assessment_reason=p.get("assessment_reason", ""),
            source=p.get("source", "unknown"),
            category=p.get("category"),
            cn_original=p.get("cn_original"),
            created_at=p.get("created_at", "")
        ))
    total = get_pending_review_count(country=country)
    return PendingListResponse(count=len(items), total_count=total, items=items, next_offset=next_offset)

@app.post("/admin/api/pending/{pending_id}/approve", response_model=ApproveResponse)
@audited("pending.approve", "pending", resource_id_keys=("pending_id",), snapshot_keys=("req",))
async def approve_pending(pending_id: str, req: ApproveRequest = ApproveRequest(), request: Request = None,
                          _auth=Depends(require_role("reviewer")),
                          _csrf=Depends(require_csrf)):
    logger.info(f"[api] 审核通过: {pending_id}")
    pending = get_pending_by_id(pending_id)
    if not pending:
        logger.warning(f"[api] 审核通过失败，记录不存在: {pending_id}")
        raise HTTPException(status_code=404, detail="待审核记录不存在")

    p = pending["payload"]
    word = p["word"]
    country = p["country"]
    reason = p["assessment_reason"]
    source = p.get("source", "overseas")
    category = req.category or p.get("category")
    cn_original = p.get("cn_original")
    session = getattr(request.state, "session", {}) if request else {}

    # 非法国家提前 400（规则库只认六国）：避免「词已入库、待审已删、审计未记」的半执行状态
    country = _guard_rule_country(country, status=400)

    try:
        vector = await get_embedding(word)
    except Exception as e:
        logger.error(f"[api] 向量生成失败: {word} - {e}")
        raise HTTPException(status_code=500, detail="向量生成失败，请稍后重试")

    # 顺序重排（2026-08）：先写规则库再动词库——后续任何一步失败都不产生无审计的入库状态
    rule_result = add_safe_word(country, word, added_by=session.get("username", ""))

    tag_id = upsert_local_tag(
        word=word,
        vector=vector,
        country=country,
        compliance_status="可复用",
        reason=reason,
        anchor_cn_id=req.anchor_cn_id,
        source=source,
        category=category,
        # 人工审核溯源：沿用采集 provenance + 审核人标识（EU 合规改造）
        provenance=p.get("provenance"),
        llm_trace_id=p.get("llm_trace_id"),
        assessed_by="manual"
    )

    delete_pending_review(pending_id)

    logger.info(f"[api] 审核通过完成: {word} ({country}, tag_id={tag_id})")
    return ApproveResponse(
        success=True,
        message=f"词汇 '{word}' 已通过审核并写入词库",
        tag_id=tag_id,
        updated_rules=rule_result
    )

@app.post("/admin/api/pending/{pending_id}/reject", response_model=RejectResponse)
@audited("pending.reject", "pending", resource_id_keys=("pending_id",), snapshot_keys=("req",))
async def reject_pending(pending_id: str, req: RejectRequest, request: Request = None,
                         _auth=Depends(require_role("reviewer")),
                         _csrf=Depends(require_csrf)):
    logger.info(f"[api] 审核拒绝: {pending_id}")
    pending = get_pending_by_id(pending_id)
    if not pending:
        logger.warning(f"[api] 审核拒绝失败，记录不存在: {pending_id}")
        raise HTTPException(status_code=404, detail="待审核记录不存在")

    p = pending["payload"]
    word = p["word"]
    country = p["country"]
    session = getattr(request.state, "session", {}) if request else {}

    # 非法国家提前 400（规则库只认六国），避免 add_banned_word 抛 ValueError 卡死词条
    country = _guard_rule_country(country, status=400)

    # 拒绝理由必填（GDPR/DSA 可解释 + UCPD 举证）：写规则库条目 + 审计快照
    rule_result = add_banned_word(
        country, word,
        rule_id="manual_reject",
        added_by=f"{session.get('username', '')}：{req.reason}"
    )
    delete_pending_review(pending_id)

    logger.info(f"[api] 审核拒绝完成: {word} ({country}, reason={req.reason[:80]})")
    return RejectResponse(
        success=True,
        message=f"词汇 '{word}' 已被拒绝并加入拦截规则",
        reason=req.reason,
        updated_rules=rule_result
    )

# ==================== 词库管理接口 ====================
@app.get("/admin/api/tags", response_model=LocalTagListResponse)
async def get_local_tags(
    country: str = Query(None, description="按国家筛选"),
    category: str = Query(None, description="按类目筛选"),
    search: str = Query(None, description="关键词搜索"),
    limit: int = Query(20, ge=1, le=200),
    cursor: str = Query(None, description="下一页游标（整数偏移或 Qdrant point ID）"),
    _auth=Depends(require_admin_session)
):
    # cursor 可以是整数偏移或 Qdrant point ID
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except ValueError:
            offset = cursor  # 字符串游标直接传递
    items_raw, next_offset = list_local_tags(
        country=country,
        category=category,
        search=search,
        limit=limit,
        offset=offset
    )
    items = [LocalTagItem(**item) for item in items_raw]
    total = count_local_tags(country=country, category=category)
    # 返回整数偏移作为 next_offset（兼容前端）
    return LocalTagListResponse(count=len(items), total_count=total, items=items, next_offset=next_offset)


@app.delete("/admin/api/tags/{tag_id}")
@audited("tag.delete", "tag", resource_id_keys=("tag_id",))
async def delete_local_tag_api(tag_id: str, request: Request = None,
                               _auth=Depends(require_role("admin")),
                               _csrf=Depends(require_csrf)):
    """删除指定词条"""
    logger.info(f"[api] 删除词条: {tag_id}")
    delete_local_tag(tag_id)
    return {"success": True, "message": "词条已删除"}

# ==================== 中文锚点库管理接口 ====================
@app.get("/admin/api/anchors", response_model=AnchorListResponse)
async def get_anchor_list(
    limit: int = Query(20, ge=1, le=200),
    cursor: str | None = Query(None),
    search: str | None = Query(None),
    category: str | None = Query(None),
    _auth=Depends(require_admin_session)
):
    """分页列出中文锚点库，支持按关键词和类目筛选"""
    items_raw, next_cursor = list_cn_anchors(
        limit=limit, cursor=cursor, search=search, category=category
    )
    items = []
    anchor_ids = [raw["id"] for raw in items_raw]
    linked_counts = batch_count_linked_local_tags(anchor_ids)
    for raw in items_raw:
        linked = linked_counts.get(raw["id"], 0)
        items.append(AnchorItem(**raw, linked_tags_count=linked))
    total = count_cn_anchors(search=search, category=category)
    return AnchorListResponse(count=len(items), total_count=total, items=items, next_offset=next_cursor)


@app.delete("/admin/api/anchors/{anchor_id}")
@audited("anchor.delete", "anchor", resource_id_keys=("anchor_id",))
async def delete_anchor_api(anchor_id: str, request: Request = None,
                            _auth=Depends(require_role("admin")),
                            _csrf=Depends(require_csrf)):
    """删除指定锚点"""
    logger.info(f"[api] 删除锚点: {anchor_id}")
    delete_cn_anchor(anchor_id)
    return {"success": True, "message": "锚点已删除"}


# ==================== 规则库维护接口 ====================
@app.get("/admin/api/rules/{country}")
async def get_rules(country: str, _auth=Depends(require_admin_session)):
    """获取指定国家的规则库（words 供兼容 + entries 带 rule_id/added_by 溯源）"""
    country = _guard_rule_country(country, status=404)
    banned_entries = get_banned_entries(country)
    safe_entries = get_safe_entries(country)
    return {
        "country": country,
        "banned": [e["word"] for e in banned_entries],
        "safe": [e["word"] for e in safe_entries],
        "banned_entries": banned_entries,
        "safe_entries": safe_entries,
    }

@app.post("/admin/api/rules/{country}/banned")
@audited("rule.add_banned", "rule", snapshot_keys=("word",))
async def add_banned_word_api(country: str, word: str = Body(..., embed=True), request: Request = None,
                              _auth=Depends(require_role("admin")),
                              _csrf=Depends(require_csrf)):
    """向禁用词库添加词"""
    country = _guard_rule_country(country, status=404)
    session = getattr(request.state, "session", {}) if request else {}
    return add_banned_word(country, word, added_by=session.get("username", ""))

@app.delete("/admin/api/rules/{country}/banned/{word}")
@audited("rule.remove_banned", "rule", snapshot_keys=("word",))
async def remove_banned_word_api(country: str, word: str, request: Request = None,
                                 _auth=Depends(require_role("admin")),
                                 _csrf=Depends(require_csrf)):
    """从禁用词库删除词"""
    country = _guard_rule_country(country, status=404)
    return remove_banned_word(country, word)

@app.post("/admin/api/rules/{country}/safe")
@audited("rule.add_safe", "rule", snapshot_keys=("word",))
async def add_safe_word_api(country: str, word: str = Body(..., embed=True), request: Request = None,
                            _auth=Depends(require_role("admin")),
                            _csrf=Depends(require_csrf)):
    """向安全词库添加词"""
    country = _guard_rule_country(country, status=404)
    session = getattr(request.state, "session", {}) if request else {}
    return add_safe_word(country, word, added_by=session.get("username", ""))

@app.delete("/admin/api/rules/{country}/safe/{word}")
@audited("rule.remove_safe", "rule", snapshot_keys=("word",))
async def remove_safe_word_api(country: str, word: str, request: Request = None,
                               _auth=Depends(require_role("admin")),
                               _csrf=Depends(require_csrf)):
    """从安全词库删除词"""
    country = _guard_rule_country(country, status=404)
    return remove_safe_word(country, word)

# ==================== 拦截决策留痕（UCPD/GDPR 举证） ====================
@app.get("/admin/api/blocked")
async def get_blocked_list(
    country: str = Query(None, description="按国家筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _auth=Depends(require_admin_session)
):
    """分页列出被拦截词决策记录（词/国家/理由/rule_id/来源）"""
    items_raw, next_offset = list_blocked_decisions(country=country, limit=limit, offset=offset)
    items = [{"id": r["id"], **r["payload"]} for r in items_raw]
    total = count_blocked_decisions(country=country)
    return {"count": len(items), "total_count": total, "items": items, "next_offset": next_offset}


@app.delete("/admin/api/blocked/{blocked_id}")
@audited("blocked.delete", "blocked", resource_id_keys=("blocked_id",))
async def delete_blocked_api(blocked_id: str, request: Request = None,
                             _auth=Depends(require_role("admin")),
                             _csrf=Depends(require_csrf)):
    """删除指定拦截决策记录（合规证据，仅 admin 可删）"""
    logger.info(f"[api] 删除拦截记录: {blocked_id}")
    delete_blocked_decision(blocked_id)
    return {"success": True, "message": "拦截记录已删除"}

# ==================== 审计查询与防篡改校验（GDPR Art.5(2)） ====================
@app.get("/admin/api/audit")
async def get_audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str = Query(None, description="按操作过滤，如 pending.approve"),
    actor_sub: str = Query(None, description="按操作者 sub 过滤"),
    resource_type: str = Query(None, description="按资源类型过滤"),
    _auth=Depends(require_role("admin"))
):
    """审计日志列表（hash-chain）"""
    items, total = await asyncio.to_thread(
        list_audit, limit=limit, offset=offset,
        action=action, actor_sub=actor_sub, resource_type=resource_type,
    )
    return {"items": items, "total": total}


@app.get("/admin/api/audit/verify")
async def verify_audit_chain(_auth=Depends(require_role("admin"))):
    """全链重算校验（防篡改证据）"""
    ok, detail = await asyncio.to_thread(verify_chain)
    return {"ok": ok, "detail": detail}

# ==================== DSAR 工单（GDPR Art.15 访问 / Art.17 删除 / Art.21 反对） ====================
@app.get("/admin/api/dsar")
async def get_dsar_list(
    status: str = Query(None, description="按状态过滤：received/in_progress/completed/rejected"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _auth=Depends(require_role("admin"))
):
    """DSAR 工单列表（仅 admin）"""
    items, total = await asyncio.to_thread(dsar_list_requests, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total}


@app.get("/admin/api/dsar/{ticket_id}")
async def get_dsar_detail(ticket_id: str, _auth=Depends(require_role("admin"))):
    """DSAR 工单详情（仅 admin）"""
    item = await asyncio.to_thread(dsar_get_request, ticket_id)
    if not item:
        raise HTTPException(status_code=404, detail="工单不存在")
    return item


@app.post("/admin/api/dsar/{ticket_id}/search")
@audited("dsar.search", "dsar", resource_id_keys=("ticket_id",), snapshot_keys=("term",))
async def dsar_search(
    ticket_id: str,
    req: DsarSearchRequest,
    request: Request = None,
    _auth=Depends(require_role("admin")),
    _csrf=Depends(require_csrf)
):
    """跨库检索词条相关数据（访问权证据），结果写入工单 findings"""
    ticket = await asyncio.to_thread(dsar_get_request, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    findings = await asyncio.to_thread(dsar_search_subject_data, req.term, req.country)
    await asyncio.to_thread(dsar_update_status, ticket_id, "in_progress", findings=findings)
    return {"ticket_id": ticket_id, "findings": findings}


@app.post("/admin/api/dsar/{ticket_id}/erase")
@audited("dsar.erase", "dsar", resource_id_keys=("ticket_id",), snapshot_keys=("term",))
async def dsar_erase(
    ticket_id: str,
    req: DsarSearchRequest,
    request: Request = None,
    _auth=Depends(require_role("admin")),
    _csrf=Depends(require_csrf)
):
    """执行删除权（Art.17）：Qdrant 硬删除 + trace 删除 + audit 脱敏 + 证据，并闭环工单"""
    ticket = await asyncio.to_thread(dsar_get_request, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    proof = await asyncio.to_thread(dsar_complete_erasure, ticket_id, req.term, req.country)
    return {"ticket_id": ticket_id, "erasure_proof": proof}


@app.post("/v1/dsar/request", response_model=DsarCreateResponse)
@limiter.limit(DSAR_RATE_LIMIT)  # 契约常量在 models.schemas（与披露正文同源）
async def dsar_public_request(request: Request, req: DsarCreateRequest):
    """公开 DSAR 受理端点（免认证——数据主体须可直接行使权利；限流防滥用）。
    GDPR Art.12(3)：受理后 30 天内响应。"""
    logger.info(f"[api] 公开 DSAR 请求: type={req.request_type}")
    try:
        result = await asyncio.to_thread(
            dsar_create_request, req.request_type, req.contact, req.subject_note, "public"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DsarCreateResponse(**result)


# ==================== 留存策略（GDPR Art.5(1)(e) 存储限制） ====================
@app.get("/admin/api/retention/config")
async def get_retention_config(_auth=Depends(require_role("admin"))):
    """当前留存策略（天）"""
    return {"policies": await asyncio.to_thread(get_retention_policies)}


@app.put("/admin/api/retention/config")
@audited("retention.update", "retention", snapshot_keys=("key", "days"))
async def update_retention_config(
    request: Request = None,
    key: str = Body(...),
    days: int = Body(...),
    _auth=Depends(require_role("admin")),
    _csrf=Depends(require_csrf)
):
    """调整某类数据的留存天数（30-3650）"""
    try:
        policies = await asyncio.to_thread(set_retention_policy, key, days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"policies": policies}


@app.post("/admin/api/retention/run")
@audited("retention.run", "retention")
async def run_retention_now(request: Request = None,
                            _auth=Depends(require_role("admin")),
                            _csrf=Depends(require_csrf)):
    """手动立即执行留存清理（平时由每日 03:00 UTC 系统任务自动执行）"""
    results = await asyncio.to_thread(run_all_purges)
    return {"purged": results}


# ==================== 内嵌管理页面 ====================
from pathlib import Path
from starlette.staticfiles import StaticFiles
from web_ui import router as web_ui_router

app.include_router(web_ui_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# ==================== 启动入口 ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
