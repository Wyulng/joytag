# Joytag 技术文档

## 1. 项目概述

Joytag 是一款面向 Joybuy 欧洲站的本地化长尾词 Tag 推荐系统。系统从 Amazon / eBay 搜索建议接口抓取欧洲六国（DE/FR/NL/UK/IT/ES）热门搜索词，经由 AI 语义分析、文化适配校验与合规拦截后，转化为可参与电商平台搜索召回的内部 Tag 标签，在不改变前端展示风格的前提下，精准提升自然语言搜索的匹配精度与转化效率。

系统已精简为**单后端架构**：FastAPI 同时提供 JSON API 与内嵌极简管理单页（单个纯静态 HTML + 原生 JS，无前端构建链）。

> **2026-08 EU 合规改造**（GDPR / DSA / UCPD + AI Act 附带，完整计划见 [docs/EU_COMPLIANCE_PLAN.md](docs/EU_COMPLIANCE_PLAN.md)）：
> Keycloak SSO 认证 + RBAC、hash-chain 审计、LLM provider 适配层 + Presidio 假名化、LLM trace / 血缘留痕、拦截决策持久化（blocked_decisions）、UCPD Annex I 内置规则、日志脱敏与轮转、端口改绑 127.0.0.1。

## 2. 系统架构

| 端 | 说明 |
|----|------|
| **管理后台** | FastAPI 内嵌极简管理单页（`/admin`，Keycloak SSO 登录 + RBAC），完成待审核词条复核、标签/锚点/规则/拦截记录管理、采集触发与定时任务配置 |
| **推荐 API** | `/v1/tag/recommend` 等接口，商品标题 → 合规本地化 Tag 推荐（服务间 Bearer token + scope） |

**后端**：Python 3.11 + FastAPI + Uvicorn（异步）
**向量数据库**：Qdrant v1.9.0（`cn_anchors` / `local_tags` / `pending_review` / `blocked_decisions` 四集合，512 维）
**Embedding**：本地 BAAI/bge-small-zh-v1.5（sentence-transformers，512 维）
**LLM 审核**：provider 适配层（默认 DeepSeek deepseek-chat；env 切换 Mistral/OpenAI/Azure/Bedrock），发送前 Presidio 假名化——翻译、文化适配、合规评分、推荐精排
**合规数据库**：Postgres 16（audit_log hash-chain / llm_trace / lineage_event / dsar_request / retention_policy）
**认证**：Keycloak 24.0.5（OIDC 授权码 + PKCE、RBAC 角色 admin/reviewer/operator、强制 TOTP）
**调度器**：APScheduler 3.x（AsyncIOScheduler，Cron 定时采集 + 每日留存清理）
**容器化**：Docker Compose（4 服务：qdrant + postgres + keycloak + backend）

## 3. 核心数据流

### 3.1 海外趋势采集流程

```
中文锚点库最新 50 词（LLM 批量翻译为六国本地语，翻译缓存增量复用）
       ↓
Amazon completion 建议（主源）+ eBay autosug 建议（辅助，六国站点直连）
       ↓
双源合并（每国 15 Amazon + 5 eBay 配额）+ 位置评分 + 去重过滤
       ↓
中文翻译（LLM，假名化 + 留痕）
       ↓
在中文锚点库中检索语义相似词（向量相似度 > 0.75）
       ↓
文化适配 + 合规审核（规则库含 UCPD Annex I 种子 → LLM 评分 → 人工复核）
       ↓
├─ 可复用 → 写入 local_tags 集合（带 provenance + trace_id）
├─ 存疑   → 写入 pending_review 集合
└─ 需拦截 → 写入 blocked_decisions 集合（词/国家/理由/rule_id/来源，证据留痕）
```

每轮采集生成 run_id，全程记 lineage 事件（START/COMPLETE/FAIL + 词级 OUTPUT），词条入库带 provenance（source_type / collection_run_id / collected_at）。

### 3.2 中文锚点词采集流程

```
淘宝搜索建议 API（80+ 种子类目）
       ↓
位置评分 + 去重（DB 查重）
       ↓
Embedding + 入库（cn_anchors 集合，top 200，带 provenance）
```

### 3.3 推荐检索流程

```
商品标题（自然语言）
       ↓
假名化（Presidio regex-only，LLM 前）
       ↓
Embedding 向量化（bge-small-zh-v1.5）
       ↓
Qdrant 向量检索（filtered by country + 合规状态"可复用"，top-16）
       ↓
LLM 精排（top-8 → top-5，留痕）
       ↓
返回推荐结果 + 推荐理由 + provenance（source/相似度/合规理由/锚点词）
```

## 4. 三级合规漏斗

| 级别 | 方式 | 说明 |
|------|------|------|
| 第一级 | 规则库硬拦截 | 欧洲六国禁用词清单 + **UCPD 2024/825 Annex I 内置种子**（无认证环保标签/通用环保声明/碳抵消/法定要求当卖点，2026-09-27 起适用），命中即拦截并记录 rule_id |
| 第二级 | LLM 文化评分 | 未命中规则的词汇调用 LLM 评估，输出「可复用 / 需拦截 / 存疑」；被拦截词写入 blocked_decisions 持久化留痕 |
| 第三级 | 人工复核队列 | 存疑词条进入管理后台待审核队列，运营人员裁定（拒绝必须填理由，写审计 + 规则库） |

安全词清单优先：命中安全词的词汇跳过 LLM 直接通过。

## 5. 内嵌管理单页

访问 `http://<host>:8001/admin` → 未登录自动跳 Keycloak 登录（账号 + TOTP）→ 会话 cookie（HttpOnly）+ CSRF 双提交。全部管理功能集中在一个极简工程风格单页内，URL hash 切换功能区（切 tab 保留筛选/分页状态）：

| 功能区 | 路径 | 功能 |
|--------|------|------|
| 概览 | `/admin#overview` | 词库总量、待审核数、锚点数、拦截数、覆盖国家、合规率（30s 自动刷新） |
| 待审核管理 | `/admin#pending` | 按国家筛选、通过（可指定类目）/拒绝（理由必填，写规则库+审计）、分页 |
| 标签库 | `/admin#tags` | 搜索/国家/类目筛选、查看、删除（审计）、分页 |
| 采集管理 | `/admin#collect` | 手动触发海外/中文采集、定时任务 CRUD（Cron，审计） |
| 规则管理 | `/admin#rules` | 各国禁用词/安全词增删（审计；显示 rule_id/添加人溯源） |
| 锚点库 | `/admin#anchors` | 搜索/类目筛选、查看、删除（审计）、分页 |
| 拦截记录 | `/admin#blocked` | 拦截决策列表/删除（UCPD 举证，删除仅 admin） |
| 审计日志 | `/admin#audit` | 管理操作审计 + hash-chain 一键校验（仅 admin；无权限 tab 内显示空态） |
| DSAR 工单 | `/admin#dsar` | 主体权利工单列表/详情/跨库检索/执行擦除（仅 admin） |

**公开透明度披露**（免登录）：`GET /transparency`（HTML：披露文本 + 轻量 DSAR 提交表单，浏览器直达提交）与 `GET /v1/transparency`（JSON，机器可读）共用同一内容源，含 Art.14(5)(b) 告知、DSA Art.27 摘要、AI Act 声明、DSAR 受理（表单等价于 `POST /v1/dsar/request` JSON）、留存策略。

**角色**：`admin` 全部权限；`reviewer` 待审核通过/拒绝 + 只读；`operator` 采集/调度/统计。单页路由由会话守卫保护，API 由角色依赖保护（前端不隐藏功能区，无权限操作由 API 403 自然拦截）。

## 6. API 接口（节选）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/v1/tag/recommend` | POST | 商品标题 → Tag 推荐（限流 20/min；生产需 Bearer token + scope `joytag:recommend`；响应带 source/合规理由/锚点词/ai_generated 等 provenance 字段） |
| `/v1/disclosure/parameters` | GET | DSA Art.27 机器可读参数披露（公开免认证，版本化） |
| `/v1/transparency` | GET | 公开透明度披露 JSON（免认证；`/transparency` 为同源 HTML 版，含 DSAR 表单） |
| `/v1/dsar/request` | POST | 公开 DSAR 受理（免认证，限流 5/hour，返回 ticket_id，30 天内响应） |
| `/auth/login` `/callback` `/logout` | GET | Keycloak OIDC 登录流（授权码 + PKCE） |
| `/admin/api/collect/overseas` / `cn` | POST | 触发采集（非流式，耗时数分钟）[operator，审计] |
| `/admin/api/schedules` | GET/POST | 定时任务列表/新建 [operator，审计] |
| `/admin/api/schedules/{id}` | PATCH/DELETE | 启停/改 Cron/删除 [operator，审计] |
| `/admin/api/schedules/{id}/run` | POST | 手动立即执行 [operator，审计] |
| `/admin/api/pending` | GET | 待审核列表（国家筛选 + 游标分页） |
| `/admin/api/pending/{id}/approve` / `reject` | POST | 审核通过/拒绝（拒绝理由必填）[reviewer，审计] |
| `/admin/api/tags` | GET | 标签列表（搜索/筛选/分页） |
| `/admin/api/tags/{id}` | DELETE | 删除标签 [admin，审计] |
| `/admin/api/anchors` | GET | 锚点列表（搜索/筛选/分页） |
| `/admin/api/anchors/{id}` | DELETE | 删除锚点 [admin，审计] |
| `/admin/api/rules/{country}` | GET | 某国规则（含 entries 带 rule_id/added_by） |
| `/admin/api/rules/{country}/{banned\|safe}` | POST/DELETE | 增删禁用词/安全词 [写操作: admin，审计] |
| `/admin/api/blocked` | GET/DELETE | 拦截决策列表/删除 [删除: admin，审计] |
| `/admin/api/audit` `/audit/verify` | GET | 审计列表 / hash-chain 全链校验 [admin] |
| `/admin/api/dsar` `/{ticket_id}` | GET | DSAR 工单列表/详情 [admin] |
| `/admin/api/dsar/{ticket_id}/search` `/erase` | POST | 跨库检索（访问权）/ 执行擦除并闭环（删除权）[admin，审计] |
| `/admin/api/retention/config` | GET/PUT | 留存策略查看/调整 [admin，审计] |
| `/admin/api/retention/run` | POST | 手动触发留存清理（日常由每日 03:00 UTC 系统任务执行）[admin，审计] |
| `/admin/api/stats` | GET | 概览统计（含拦截数） |
| `/health` | GET | 健康检查（`?deep=1` 检查 Qdrant + LLM + Postgres） |

## 7. 部署

**双模式**：

| 模式 | 启动方式 | 管理页 | 说明 |
|------|----------|--------|------|
| 开发本地 | `backend/setup_dev.ps1`（首次搭建环境）+ `dev.ps1`（日常启动） | http://localhost:8000/admin | Docker 只跑 qdrant；backend 本地 uvicorn `--reload`，改代码即时生效；`AUTH_ENABLED=false` 免登录 |
| 生产 | `docker compose up -d --build` | http://localhost:8001/admin | 全栈 Docker（下文组件表），Keycloak SSO 认证 |

### 7.1 容器组件

| 组件 | 镜像 | 端口 | 说明 |
|------|------|------|------|
| Backend | Python 3.11 + FastAPI | 127.0.0.1:8001:8000 | API + 内嵌管理页（OIDC 资源服务器 + 会话 cookie） |
| Qdrant | qdrant/qdrant:v1.9.0 | 127.0.0.1:6333/6334 | 向量数据库（4 集合） |
| Postgres | postgres:16-alpine | 127.0.0.1:5432 | 审计/trace/lineage/DSAR（joytag 库）+ Keycloak 库 |
| Keycloak | quay.io/keycloak/keycloak:24.0.5 | 8080（对外） | SSO/RBAC/MFA；**必须用安全组将 8080 限制为办公 IP** |

### 7.2 数据持久化

| 宿主机路径 | 容器路径 | 内容 |
|------------|----------|------|
| `./qdrant_storage` | `/qdrant/storage` | 向量数据 |
| `./pgdata` | `/var/lib/postgresql/data` | Postgres 数据（审计证据/LLM trace/血缘/DSAR） |
| `./backend` | `/app`（卷挂载） | 代码 + 进度文件 + 规则文件 + 调度配置 |
| `huggingface_cache`（命名卷） | `/home/appuser/.cache/huggingface/hub` | Embedding 模型缓存 |

### 7.3 环境变量

完整清单见 `.env.example`。要点：

| 变量 | 说明 |
|------|------|
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 适配层（默认 openai_compat + DeepSeek；EU 切换 = 纯配置变更） |
| `QDRANT_URL` | Qdrant 地址（默认 http://localhost:6333） |
| `QDRANT_API_KEY` | Qdrant 服务密钥（**生产必填**，无硬编码默认值） |
| `DATABASE_URL` | Postgres（compose 自动注入容器内地址） |
| `AUTH_ENABLED` | 认证开关（默认 false；生产 compose 置 true） |
| `OIDC_ISSUER` / `OIDC_JWKS_URL` / `OIDC_TOKEN_URL` | Keycloak 端点（iss 校验用外部地址，JWKS/token 走容器内网） |
| `SESSION_SECRET` | 会话 cookie 签名密钥（生产必填：`openssl rand -hex 32`） |
| `PII_GUARD_MODE` | 假名化模式（默认 regex_only） |
| `CORS_ORIGINS` | CORS 白名单（默认空，同源内嵌页无需配置） |
| `SOCIAL_MEDIA_PROXY` | legacy：Reddit 模块已删除，此变量无消费方 |
| `POSTGRES_PASSWORD` / `JOYTAG_DB_PASSWORD` / `KEYCLOAK_DB_PASSWORD` / `KEYCLOAK_ADMIN_PASSWORD` | 基础设施密钥（生产必填） |

### 7.4 首次部署 / Keycloak 引导

1. `cp .env.example .env` 并填写全部必填密钥（`openssl rand -hex 32` 生成）。
2. `docker-compose down && docker-compose up -d --build`（服务器 compose v1 流程）。
3. 安全组：仅办公 IP 放行 8001 与 8080。
4. 用 kcadm 创建首个管理用户并强制绑 TOTP：
   ```bash
   docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user admin --password $KEYCLOAK_ADMIN_PASSWORD
   docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh create users -r joytag -s username=xxx -s enabled=true -s requiredActions='["CONFIGURE_TOTP"]'
   docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh set-password -r joytag --username xxx --new-password xxx --temporary
   docker exec -it joytag-keycloak /opt/keycloak/bin/kcadm.sh add-roles -r joytag --uusername xxx --rolename admin
   ```
5. 服务间调用方在 Keycloak 管理控制台（Clients → joytag-service → Credentials）取 client secret，用 client_credentials 换 token（scope `joytag:recommend`）调推荐 API。

⚠️ **内存预算**：qdrant 1G + backend 2G + keycloak 1G + postgres 512M ≈ 5G，部署前确认服务器 ≥6GB。⚠️ **TLS**：当前为过渡期 HTTP（Let's Encrypt 不签发裸 IP），8001/8080 依赖安全组 IP 白名单；域名就绪后接入 Caddy TLS 并置 `TLS_ENABLED=true`（详见合规计划 P2）。

## 8. 技术栈汇总

| 层级 | 技术选型 |
|------|----------|
| 后端框架 | FastAPI + Uvicorn |
| 异步任务 | APScheduler（定时调度 + 每日留存清理）、asyncio（异步采集） |
| 向量数据库 | Qdrant v1.9.0（4 集合） |
| Embedding | 本地 BAAI/bge-small-zh-v1.5（sentence-transformers，512 维） |
| LLM | provider 适配层（DeepSeek 默认；Mistral/OpenAI/Azure/Bedrock 可切）+ Presidio 假名化 |
| 合规数据库 | Postgres 16（hash-chain 审计 / LLM trace / OpenLineage 形状血缘 / DSAR 工单与擦除证据 / 留存策略 + 每日 03:00 UTC 自动清理） |
| 认证 | Keycloak 24.0.5（OIDC + PKCE、RBAC、TOTP）+ authlib JWT 校验 |
| 管理界面 | 内嵌单页静态 HTML + 原生 JS（无前端框架/构建链，hash tab，CSRF 双提交 + 401 跳登录） |
| 数据采集 | httpx（异步 HTTP）、requests（采集器） |
| 规则存储 | 本地 JSON 文件（FileLock + TTL 缓存；schema v2 带 rule_id/added_by） |
| 容器化 | Docker + Docker Compose |

## 9. 开源许可

本项目采用 [MIT License](LICENSE)：可自由使用、修改、分发、商用，仅需保留版权与许可声明。
