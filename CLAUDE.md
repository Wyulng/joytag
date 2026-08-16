# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Joytag is a localized long-tail tag recommendation system for Joybuy's EU marketplace. It collects trending words (Taobao search suggestions for CN anchors, Amazon/eBay search suggestions for overseas trends in six EU markets DE/FR/NL/UK/IT/ES), processes them through AI semantic analysis + cultural compliance checks, and stores them as searchable tags in Qdrant vector DB. A recommendation API takes product titles and returns compliant localised tags.

> **2026-08 EU 合规改造**（GDPR / DSA / UCPD + AI Act 附带）：完整计划见 `docs/EU_COMPLIANCE_PLAN.md`。已落地：Keycloak SSO 认证 + RBAC、hash-chain 审计、LLM provider 适配层 + Presidio 假名化、LLM trace/血缘（OpenLineage 形状）留痕、blocked_decisions 拦截决策持久化、UCPD Annex I 内置规则、日志脱敏与轮转、端口改绑 127.0.0.1。

## Tech Stack

- **Backend**: Python 3.11 + FastAPI + Uvicorn (async, hot-reload enabled)
- **Vector DB**: Qdrant v1.9.0 (4 collections: `cn_anchors`, `local_tags`, `pending_review`, `blocked_decisions`)
- **Embedding**: BAAI/bge-small-zh-v1.5 (512-dim) via local sentence-transformers. 模型优先从 `backend/models/bge-small-zh-v1.5/`（gitignore，不随 git 走）加载，目录不存在才回退 HF Hub 名称——hf-mirror 大文件 302 到 AWS CDN（xet-bridge）国内不通，**获取模型用 ModelScope**：`pip install modelscope` 后 `python -c "from modelscope.hub.snapshot_download import snapshot_download; snapshot_download('AI-ModelScope/bge-small-zh-v1.5', local_dir='backend/models/bge-small-zh-v1.5')"`（生产换服务器时需单独传此目录）
- **LLM**: provider 适配层（`services/llm_provider.py`）：默认 DeepSeek（OpenAI 兼容），env 切换 Mistral/OpenAI/Azure/Bedrock（EU 切换 = 纯配置变更）。发送前 Presidio 假名化（`services/pii_guard.py`，regex-only 模式）。
- **Compliance DB**: Postgres 16（`services/db.py`）：audit_log（hash-chain）/ llm_trace / lineage_event / dsar_request / retention_policy / audit_chain_head
- **Auth**: Keycloak 24.0.5（OIDC 授权码 + PKCE，服务端会话 cookie，RBAC 角色 admin/reviewer/operator，强制 TOTP）；`services/auth.py` 同时作资源服务器（JWKS 验 JWT，scope `joytag:recommend`）
- **Web UI**: 内嵌极简管理单页（`backend/static/admin.html`，纯静态 HTML + 原生 JS，无前端框架/构建链）
- **Scheduling**: APScheduler 3.x (AsyncIOScheduler, cron triggers)
- **Infra**: Docker Compose v2.4 语法（服务器 docker-compose v1.29.2）：qdrant / postgres / keycloak / backend 四服务，backend 直连 8001 对外

## Dev Commands

### 本地开发（推荐，改代码即时生效，无需 Docker 重建）
```powershell
# 首次搭建（uv 装 Python 3.11 + venv + 依赖，走清华镜像；本机默认 3.14 无 torch wheel，必须 3.11）
./backend/setup_dev.ps1

# 日常启动：Docker 只跑 qdrant，backend 本地 uvicorn --reload
./dev.ps1
```
- 管理后台（本地）: http://localhost:8000/admin
- ⚠️ `dev.ps1` 会将 `QDRANT_URL` 覆盖为 `http://localhost:6333`——本地 `.env` 里的 `joytag-qdrant:6333` 是 Docker 内部主机名，本地跑不通
- 开发时可 `docker compose stop backend`（本地与容器共用 qdrant）
- **本地无认证**：`AUTH_ENABLED` 默认 false，`/admin/api/*` 直接可用；不依赖 postgres/keycloak（审计/trace 自动降级为 best-effort 告警）

### 生产 / 全栈 Docker
```bash
docker compose up -d --build
```
- 管理后台（内嵌单页）: http://localhost:8001/admin → Keycloak 登录（TOTP）
- Backend API: http://localhost:8001
- Keycloak 管理控制台: http://<server>:8080（安全组限办公 IP）
- Qdrant dashboard: http://localhost:6333/dashboard

### Notes
- No test framework is configured (no pytest).
- No Python formatter/linter is configured.
- Backend hot-reload mounts `./backend:/app` in Docker — 修改 Python 或 `static/` 页面后重启容器即生效，无需重建镜像。
- Dockerfile 有 BuildKit pip 缓存挂载：requirements.txt 变更重建时依赖 wheel 从宿主机缓存复用，不再重复下载。

## Architecture

### Directory Structure

```
backend/
  Dockerfile                 # python:3.11-slim, uvicorn --reload
  app.py                     # FastAPI routes, CORS, scheduler lifespan, @audited 装饰器
  models/schemas.py          # Pydantic models for all request/response types
  services/
    __init__.py              # Empty
    qdrant_store.py          # Qdrant CRUD: 4 collections, deterministic UUIDs (length-prefixed MD5), VECTOR_SIZE=512
    embedding.py             # Local sentence-transformers BAAI/bge-small-zh-v1.5, via asyncio.to_thread
    llm.py                   # 合规评估/翻译：规则库优先 → LLM（假名化 + 留痕 + 重试 3× exp backoff）
    llm_provider.py          # LLM provider 适配层：OpenAICompat/Azure/Bedrock（SigV4，无 boto3），LLM_PROVIDER env 选择
    pii_guard.py             # Presidio 假名化（regex-only）：pseudonymize(text) -> (clean_text, {token: type})
    recommend.py             # 2-stage: async vector search (top-16) + LLM rerank (top-5)，假名化 + 留痕
    alignment.py             # Per-word orchestration: 翻译 → 锚点检索 → 规则/LLM 评估 → upsert / pending / blocked_decisions
    rule_manager.py          # Country banned/safe word lists (JSON files + TTL cache + FileLock) + UCPD 内置种子
    db.py                    # Postgres 连接池（psycopg3）+ 幂等 DDL + 默认留存策略种子
    audit.py                 # hash-chain 审计（CloudTrail 模式）：record_event/verify_chain/list_audit/redact_audit_for_erasure
    llm_trace.py             # LLM 留痕（prompt 哈希 + PII token 映射 + word_sha256，DSAR 可查/可删）
    lineage.py               # OpenLineage 形状血缘事件（JSONB 自存，不引 Marquez）：START/COMPLETE/FAIL/OUTPUT/ERASE
    dsar.py                  # GDPR Art.15/17/21：工单 + 跨库检索（Qdrant+trace+audit）+ 全链路擦除（硬删除+脱敏+ERASE 事件+证据）
    retention.py             # 留存策略（Art.5(1)(e)）：llm_trace 90 / lineage·audit 396 天，run_all_purges()
    transparency.py          # 透明度披露单一内容源（Art.13/14+DSA+AI Act）：SECTIONS 六节 + render_transparency_text() + transparency_payload()
    auth.py                  # Keycloak OIDC：JWKS 验 JWT + 会话 cookie（itsdangerous）+ CSRF 双提交 + PKCE 登录流
    http_client.py           # Shared httpx.AsyncClient singleton (timeout=180s), closed on shutdown
    task_scheduler.py        # APScheduler AsyncIOScheduler wrapper, cron triggers, manual run + 系统任务 compliance_retention（每日 03:00 UTC）
    scheduler_store.py       # Persist/load task configs from JSON (threading.Lock for concurrent CRUD)
    logging_config.py        # JSON structured logs + request_id contextvar + 日志脱敏；setup_logging()
    collectors/
      __init__.py            # Empty
      countries.py           # EU_COUNTRIES 六国唯一权威源（DE/FR/NL/UK/IT/ES，与 Amazon 站点对齐）
      cn_ecommerce.py        # CN 电商搜索词：淘宝搜索建议 API
      cn_longtail.py         # CN 长尾词 runner：电商建议 → 进度去重 → alignment 流水线（run_id + lineage）
      seed_builder.py        # 动态采集种子：最新 50 锚点 → LLM 批量翻译（translation_cache.json 增量缓存 + 锁内乐观合并）；build_seeds / iter_country_seeds（逐国流水线产出，每国完成即落缓存）
      amazon_suggest.py      # Amazon completion API（六国直连主源，2026-08 起）+ fanout_fetch 共享扇出助手（ebay 复用）+ get_country_hot_words（推荐热词上下文，stale-while-revalidate + single-flight）+ SEEDS_BY_COUNTRY 兜底种子
      ebay_suggest.py        # eBay autosug API（六国直连辅助源，薄包装：扇出/评分复用 amazon_suggest.fanout_fetch）
      overseas_trends.py     # 海外采集 runner：逐国种子流水线（翻译与双源抓取重叠，种子 90s 上限，超时/异常该国回退固定种子）→ 逐国配额合并（casefold 去重）→ 进度去重 → alignment 流水线（source 透传 + run_id + lineage）
  data/
    rules/                   # {country}.json (banned), {country}_safe.json (safe words), *.json.lock
    schedules/schedules.json # Persisted cron task configs（scheduler_store 运行时自动 mkdir）
  web_ui.py                  # 内嵌管理路由：/ → 302 /admin、/admin 单页（会话守卫 302 /auth/login）、/transparency HTML（披露文本 + 轻量 DSAR 表单，免认证）、旧多页子路径 8 条 302 → hash tab（保旧书签）
  static/                    # 内嵌页静态资源（修改后重启容器即生效，无需重建）
    admin.html               # 管理单页：9 个 hash tab（概览/待审核/标签/采集/规则/锚点/拦截/审计/DSAR），懒加载 + IIFE 隔离
    js/admin.js              # 共享逻辑：api()（会话 cookie + X-CSRF-Token + 401 跳登录）/esc()/toast()/renderTabs()（TAB_ORDER 单一来源）/createPager()/createOffsetPager()/withLoading() 等
keycloak/
  realm-export.json          # realm joytag：joytag-admin（公开+PKCE）、joytag-service（机密+scope joytag:recommend）；角色 admin/reviewer/operator；浏览器流强制条件 OTP
  init-keycloak-db.sh        # Postgres 首次启动建库建角色（密码来自容器 env，不落 git）
docs/
  EU_COMPLIANCE_PLAN.md      # EU 合规改造完整计划（法规调研/差距/架构/分阶段清单/验证/风险）
```

### Key Data Flows

1. **Overseas Collection**: **逐国家流水线**（翻译与抓取重叠，种子阶段 90s 上限，超时/异常该国回退固定种子）——动态种子（cn_anchors 最新 50 词 → LLM 批量翻译六国语言，增量缓存）-> Amazon completion (六国站点) + eBay autosug 双源并行 -> 逐国配额合并（15 Amazon + 5 eBay，casefold 跨源去重）-> LLM translation -> CN anchor vector search (threshold 0.75) -> 规则硬拦截（含 UCPD 内置种子）-> LLM compliance assessment -> `local_tags` / `pending_review` / **`blocked_decisions`（拦截决策持久化，不再丢弃）**
2. **CN Anchor Collection**: Taobao search suggestion API (80+ seed categories) -> position-based scoring -> top 200 -> `cn_anchors`
3. **Tag Recommendation**: product title -> embedding -> Qdrant vector search (filtered by country + `compliance_status=="可复用"`) -> LLM rerank (top-16 -> top-8 -> top-5)
4. **全部词条带 provenance**（source_type/collection_run_id/collected_at）+ 采集 run_id 血缘事件（lineage START/COMPLETE/FAIL + 词级 OUTPUT）

### Qdrant Collections

| Collection | Vector | ID Generation | Purpose |
|-----------|--------|---------------|---------|
| `cn_anchors` | 512-dim (bge-small-zh-v1.5) | MD5(cn_word) | Chinese anchor words |
| `local_tags` | 512-dim (bge-small-zh-v1.5) | MD5(word + country) | Final localized tags |
| `pending_review` | 512-dim (zero vector) | MD5(word + country) | Words needing human review |
| `blocked_decisions` | 512-dim (zero vector) | MD5(word + country) | 被拦截词决策留痕（UCPD/GDPR 举证，2026-08 新增） |

All deterministic UUIDs via `_generate_deterministic_id(*parts)` in `qdrant_store.py`. Uses length-prefixed encoding (`f"{len(p)}:{p}"`) instead of colon-joining to avoid collisions when word values contain colons.

### API Endpoints

All routes defined in `backend/app.py`:
- `/v1/tag/recommend` — POST, tag recommendation (rate limit 20/min；生产需 Bearer token + scope `joytag:recommend`；响应带 provenance + ai_generated + parameters_version)
- `/v1/disclosure/parameters` — GET, DSA Art.27 机器可读参数披露（**公开免认证**，版本号 DISCLOSURE_VERSION）
- `/v1/transparency` — GET, 公开透明度披露 JSON（免认证；与 /transparency 纯文本共用 services/transparency.py 内容源）
- `/v1/dsar/request` — POST, 公开 DSAR 受理（免认证，5/hour 限流，返回 ticket_id）
- `/auth/login` / `/auth/callback` / `/auth/logout` — Keycloak OIDC（授权码 + PKCE）
- `/admin/api/collect/*` — trigger collection (overseas/cn, 非流式) [operator，审计]
- `/admin/api/schedules/*` — cron task CRUD + manual run [operator，审计]
- `/admin/api/pending/*` — pending review (list, count, approve, reject) [reviewer，审计；reject 理由必填]
- `/admin/api/tags/*` — tag library (list, count, delete) [delete: admin，审计]
- `/admin/api/anchors/*` — CN anchors (list, count, delete) [delete: admin，审计]
- `/admin/api/rules/{country}/*` — banned/safe word CRUD（GET 含 entries 带 rule_id）[写操作: admin，审计]
- `/admin/api/blocked` — 拦截决策列表（GET 会话 / DELETE admin，审计）
- `/admin/api/audit` — 审计列表 [admin]；`/admin/api/audit/verify` — hash-chain 全链校验 [admin]
- `/admin/api/dsar` — 工单列表/详情 [admin]；`/{ticket_id}/search` 跨库检索 [admin，审计]；`/{ticket_id}/erase` 执行擦除并闭环 [admin，审计]
- `/admin/api/retention/config` — GET/PUT 留存策略 [admin，审计]；`/admin/api/retention/run` — 手动触发清理 [admin，审计]
- `/admin/api/stats` — GET, dashboard stats（含 total_blocked）
- `/health` — GET, health check; `?deep=1` also checks Qdrant + LLM + Postgres (503 when degraded)

### Auth

**Keycloak SSO**（2026-08 起，替代原无认证架构）：
- **管理端**：浏览器 → `/admin`（无会话 302 `/auth/login`）→ 授权码 + PKCE → backend 内网换 token → 验 ID token（签名/iss/aud/nonce）→ itsdangerous 签名会话 cookie（HttpOnly/SameSite=Lax，12h，token 不进 JS）+ CSRF 双提交（`joytag_csrf` cookie ↔ `X-CSRF-Token` 头，写方法强制）。角色：`admin` 全量、`reviewer` 审核 + 只读、`operator` 采集/调度。realm 浏览器流条件 OTP + 用户首登强制绑 TOTP。
- **服务间**：`/v1/tag/recommend` 走 client_credentials（`joytag-service` 机密客户端 + scope `joytag:recommend`）；backend 用 authlib 验 JWT（JWKS 缓存 3600s + 未知 kid 重取；`iss` 对 `OIDC_ISSUER`，JWKS 从 `OIDC_JWKS_URL` 取——双主机名用两个 env 显式解耦）。
- **AUTH_ENABLED**：默认 false（本地 dev，会话/CSRF/Bearer 校验全部旁路）；生产 compose 显式 true（CSRF 双提交严格生效）。true 时 `SESSION_SECRET` 必填，缺失即启动失败。
- **首用户引导（R3）**：realm 导入后用户为空，用 kcadm 建（写入服务器笔记）：
  ```bash
  docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user admin --password $KEYCLOAK_ADMIN_PASSWORD
  docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh create users -r joytag -s username=xxx -s enabled=true -s requiredActions='["CONFIGURE_TOTP"]'
  docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh set-password -r joytag --username xxx --new-password xxx --temporary
  docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh add-roles -r joytag --uusername xxx --rolename admin
  ```
- **joytag-service secret**（导入时未写死）：管理控制台 Clients → joytag-service → Credentials 查看；调用方取 token：`curl -s -X POST http://<server>:8080/realms/joytag/protocol/openid-connect/token -d grant_type=client_credentials -d client_id=joytag-service -d client_secret=<secret> -d scope=joytag:recommend`

### LLM Integration (services/llm.py + llm_provider.py)

- **Provider**: `LLM_PROVIDER` env 选择（openai_compat 默认 → DeepSeek `deepseek-chat`；azure / bedrock 预置分支）。EU 合规切换 = 改 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`。
- System prompt asks for JSON output: `{"translation": "...", "result": "可复用/需拦截/存疑", "reason": "..."}`
- **UCPD 2024/825 Annex I 提示词**（2026-09-27 起适用）：无认证环保标签/通用环保声明/碳抵消/法定要求当卖点 → 需拦截并引用 rule_id（ucpd_env_unverified_label / ucpd_env_generic / ucpd_carbon_offset / ucpd_legal_requirement）
- Retry logic: 3 attempts with exponential backoff (2^attempt seconds), shared `httpx.AsyncClient` via `http_client.py`.
- JSON parsing with fallback: strips `<think>` tags, markdown fences, then `json.loads`.
- Temperature: 0.1 throughout (rerank 0.2).
- **假名化**：发送前 `pseudonymize_async()`（Presidio regex-only：email/IP/IBAN/电话/信用卡），LLM 只看到 `<EMAIL_ADDRESS_0>` 等 token；trace 只存 token→类型映射与假名化提示词 sha256。
- **留痕**：assess/translate/rerank 三处写 `llm_trace`（含 word_sha256 供 DSAR 检索）；写入失败仅告警不阻断业务。
- `assess_single(word, country, category=None) -> (判定, 理由, rule_id, llm_trace_id)` — 规则库优先（safe/banned/内置 UCPD 种子），否则 LLM。

### Embedded Web UI (backend/static/)

React 前端与多页 HTML 已删除（2026-08），管理界面为**单页** `admin.html`（纯工程风格：系统字体、1px 边框表格、无花哨样式，零依赖、零构建）：

- **路由** (`backend/web_ui.py`): `/` → 302 `/admin`；`/admin` → admin.html（FileResponse，会话守卫 401 时 302 `/auth/login`）；`/transparency` → HTML（披露文本 `<pre>` + 轻量 DSAR 表单，公开免认证）；旧多页子路径 8 条（`/admin/anchors` 等）302 → 对应 hash tab；`/static/*` 挂载 admin.html/js。
- **单页结构** (`static/admin.html`): 9 个功能 tab（概览/待审核/标签库/采集/规则/锚点库/拦截记录/审计/DSAR）用 URL hash 路由（如 `/admin#pending`），`hidden` 切换 + 首激活懒加载（切 tab 保留筛选/分页状态）；每 tab JS 包 IIFE + 元素 ID 前缀（`pd-`/`tg-` 等）防全局冲突；audit/dsar 两个 admin-only tab 遇 403 在表格内渲染"无权限（仅 admin 角色）"空态。
- **共享逻辑** (`static/js/admin.js`): `api()` 封装（`credentials: same-origin` + 写方法带 `X-CSRF-Token` + 401 跳 `/auth/login?next=路径+hash`、抛错带 `status` 属性）、`esc()` HTML 转义（所有动态文本必须过）、`toast()`、`renderTabs()`（`TAB_ORDER`/`TAB_LABELS` 单一来源，admin.html 路由共用）、`createPager()`（cursor 分页）、`createOffsetPager()`（blocked/audit/dsar 共用的 offset 分页，下一页用 PAGE_SIZE 计算非本页条数）、`withLoading()`（防重复提交）、`EU_COUNTRIES` 硬编码（与 `services/collectors/countries.py` 保持一致，改国家需两处同步）。
- **透明度披露** (`services/transparency.py` 单一内容源): `/transparency`（HTML：披露文本 + DSAR 表单）与 `/v1/transparency`（JSON）共用 `SECTIONS` 六节 + `DSAR_SUBMISSION`；契约常量（版本号/留存天数/限流/DSAR 字段上限/推荐调参）全部单源于 `models/schemas.py`（纯层无循环导入），②③ 节与 disclosure/parameters 相关、⑥ 节留存天数与 db.py 种子一致——参数变更时须同步复核措辞并递增 `TRANSPARENCY_VERSION`。
- **慢操作**: approve（内嵌 embedding，`withLoading()` 按钮 loading 态）与采集触发（数分钟，运行/停止双按钮互斥 disabled + finally 恢复）已全部加防重复提交。
- **注意**: rules 删除词是路径参数，必须 `encodeURIComponent(word)`。

### Data Persistence (Non-Qdrant)

- **Rules**: JSON files in `backend/data/rules/{country}.json` (banned) and `{country}_safe.json` (safe). Schema v2 条目 `{"word", "categories", "rule_id", "added_by", "added_at"}`（旧纯字符串向后兼容）。Uses `FileLock` + in-memory TTL cache (60s, max 128 entries). UCPD 内置种子硬编码在 `rule_manager.UCPD_ENV_BANNED`（不可被误删），匹配用 `\b` 词边界正则（忽略大小写）；国家规则文件 banned 用 casefold 子串匹配（防短语嵌长词绕过），safe 保持精确匹配。
- **Schedules**: `backend/data/schedules/schedules.json`, persisted task configs with `threading.Lock` for concurrent CRUD.
- **Progress**: `cn_collection_progress.json`, `overseas_collection_progress.json` in `backend/`. Dict-based (insertion order) for deterministic slicing.
- **Postgres**（容器 `./pgdata` 卷，`joytag` 库）: audit_log / audit_chain_head（hash-chain 头，FOR UPDATE 串行化写）、llm_trace、lineage_event、dsar_request、retention_policy（默认 llm_trace=90 天、lineage/audit=396 天）。DSAR 检索/擦除跨 Qdrant + Postgres。
- **⚠️ 空库重建风险（2026-08 精简后）**: 种子脚本与词库导入工具已全部删除，数据完全依赖 `qdrant_storage` 持久化卷 + 采集器/审核管线。若 Qdrant 卷丢失或换新服务器，冷启动只能靠采集器逐步恢复（依赖淘宝/Amazon/eBay 网络 + LLM 审核 + 人工通过，慢且不完整）。务必定期备份 qdrant_storage（以及 pgdata 审计证据）。

## Deployment Notes

- **Docker Compose** (`docker-compose.yml`): 4 services — `qdrant` (v1.9.0, 127.0.0.1:6333/6334), `postgres` (16-alpine, 127.0.0.1:5432, healthcheck pg_isready), `keycloak` (24.0.5, 8080 对外), `backend` (127.0.0.1:8001:8000, 直连对外)。qdrant 无 healthcheck（镜像内无 curl）；backend healthcheck 用 python urllib /health。
- **⚠️ 端口与安全组**: 除 Keycloak 8080（浏览器直连，**必须**安全组限办公 IP）外全部绑 127.0.0.1。8001 也需安全组限办公 IP（过渡期 HTTP 无 TLS；域名就绪后 P2 接 Caddy TLS 并 `TLS_ENABLED=true`）。
- **⚠️ 内存预算（R1）**: qdrant 1G + backend 2G + keycloak 1G + postgres 512M ≈ 5G——部署前 `free -h` 确认 VM ≥6GB。
- **⚠️ 密钥全部必填**（compose 用 `:?required` 语法）：QDRANT_API_KEY / POSTGRES_PASSWORD / JOYTAG_DB_PASSWORD / KEYCLOAK_DB_PASSWORD / KEYCLOAK_ADMIN_PASSWORD / SESSION_SECRET——无任何硬编码默认值。
- **HuggingFace cache**: named volume `huggingface_cache` mounted at `/home/appuser/.cache/huggingface/hub` — the bge model download persists across container rebuilds.
- **SOCIAL_MEDIA_PROXY** env var（legacy）: 曾用于 Reddit 访问的 HTTP 代理。2026-08 海外采集改用 Amazon/eBay 直连后 Reddit 模块已删除，**此变量当前无任何消费方**，仅保留历史注记。
- **CI** (`.github/workflows/deploy.yml`): on push/PR to **`main`** — backend `pip install` + `python -m compileall .`. Note the local default branch is `master`, which does **not** trigger CI.
- **No test framework** (no pytest). **No Python linter/formatter** configured.

## Design Decisions & Gotchas

### Rate Limiting (slowapi)
`slowapi` Limiter keyed by client IP: 仅 `/v1/tag/recommend` 20/min。Rate-limited handlers must take a `request: Request` parameter (slowapi requirement) — follow the existing pattern in `app.py` when adding rate limits.

### Middleware Order (app.py)
仅 `request_id_middleware`（X-Request-ID + JSON logging contextvar）。CORS origins from `CORS_ORIGINS` env (comma-separated, **default empty** — 同源内嵌页无需 CORS；外部跨域调用方按需配置)。

### UUID Generation
`_generate_deterministic_id(*parts)` in `qdrant_store.py` uses length-prefixed encoding (`f"{len(p)}:{p}"` for each part) to prevent collisions when word values contain colons. Deterministic UUIDs ensure idempotent upserts — same natural key always produces same UUID.

### Async Blocking
- **QdrantClient**: The sync `QdrantClient` is used throughout (not `AsyncQdrantClient`). In hot-path async endpoints (`/v1/tag/recommend`), blocking calls are wrapped with `await asyncio.to_thread()`.
- **sentence-transformers**: `embedding.py` runs the local model via `asyncio.to_thread()` to avoid blocking the event loop. Model is cached via `@lru_cache`.
- **Scheduler store**: CRUD functions are synchronous but called from async route handlers; `threading.Lock` protects concurrent read-modify-write.
- **db.py/audit/llm_trace/lineage**: psycopg 同步执行，调用方 `asyncio.to_thread` 包装（沿用现有阻塞 IO 模式）。

### Progress Files & Dedup
Both collectors persist progress to JSON files using dict-keys (insertion order) instead of sets. This ensures the "last N processed" slice is deterministic across restarts, preventing wasted re-processing of words already in the database.

### Rules Two-Tier Check
`rule_manager.py` implements a two-tier compliance check:
1. **Safe list** (`{country}_safe.json`) — words manually approved; skip LLM entirely
2. **Banned list** (`{country}.json` + **UCPD 内置种子**) — words blocked by law/culture; reject immediately
3. If neither matches → pass to LLM for cultural assessment

### CN vs Overseas Data Flow
- **CN anchors**: `cn_ecommerce.py` (Taobao API, JD 已移除) → `cn_longtail.py` (dedup via `cn_anchor_exists` DB check, run_id + lineage) → `alignment.py` (embed + upsert to `cn_anchors` with provenance)
- **Overseas tags**: `seed_builder.py` (动态种子) + `amazon_suggest.py`/`ebay_suggest.py` (Amazon completion + eBay autosug 双源) → `overseas_trends.py` (逐国配额合并 + dedup via `local_tag_exists` DB check, granular source 透传 amazon_suggest/ebay_suggest) → `alignment.py` (translate → find CN anchor → rules/LLM assess → `local_tags` / `pending_review` / `blocked_decisions`)

### Audit（hash-chain，2026-08）
- `@audited(action, resource_type, ...)` 装饰器挂全部管理变更端点（app.py）。变更成功后才写审计；**审计写入失败 → 500**（可责性优先：操作已执行但无法举证时立即告警——消息明示"已执行但审计失败"，非静默）。
- hash 仅覆盖不可变字段（prev_hash/id/ts/actor_sub/action/resource_type/resource_id），snapshot/detail 不参与哈希——DSAR 擦除可对其 `<REDACTED>` 替换而不破坏链（"擦除权优先于快照防篡改"）。
- 单行 `audit_chain_head FOR UPDATE` 串行化推进链头；`/admin/api/audit/verify` 全链重算。

### LLM Trace / Lineage（best-effort）
- trace/lineage 写入失败仅告警不阻断业务（审计除外——见上）。
- lineage 事件：采集 run START/COMPLETE/FAIL + 词级 OUTPUT（挂 run_id，供溯源检索）；DSAR 擦除发 ERASE。

### Environment Variables (.env)
```
QDRANT_URL               # default: http://localhost:6333
QDRANT_API_KEY           # 生产必填（无硬编码默认值）
CORS_ORIGINS             # comma-separated, default empty (同源内嵌页无需 CORS)
LLM_PROVIDER             # openai_compat(默认) | azure | bedrock
LLM_BASE_URL             # default: https://api.deepseek.com/v1
LLM_API_KEY              # 未设置回退 DEEPSEEK_API_KEY
LLM_MODEL                # default: deepseek-chat
DEEPSEEK_API_KEY         # 兼容旧变量
PII_GUARD_MODE           # regex_only(默认) | off
DATABASE_URL             # default: postgresql://joytag:joytag@localhost:5432/joytag（compose 注入容器内地址）
AUTH_ENABLED             # default false；生产 compose 置 true
OIDC_ISSUER              # iss 校验（外部地址，如 http://<你的服务器地址>:8080/realms/joytag）
OIDC_JWKS_URL            # 默认 issuer/certs（容器内覆盖为 http://keycloak:8080/...）
OIDC_TOKEN_URL           # 默认 issuer/token（同上）
OIDC_ADMIN_CLIENT_ID     # default joytag-admin
OIDC_API_CLIENT_ID       # default joytag-service
SESSION_SECRET           # AUTH_ENABLED=true 必填（openssl rand -hex 32）
SESSION_MAX_AGE          # default 43200 (12h)
TLS_ENABLED              # default false；Caddy TLS 就绪后 true（cookie Secure）
SOCIAL_MEDIA_PROXY       # legacy：Reddit 模块已删除，此变量无消费方
HF_ENDPOINT              # optional: HF mirror for model download (e.g. https://hf-mirror.com)
```
