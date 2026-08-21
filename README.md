# Joytag

> 面向跨境电商的多语言本地化 Tag 推荐与合规治理服务

Joytag 将中文商品锚点、欧洲多站点搜索建议和 AI 语义分析组合成一条可追溯的本地化标签流水线：先采集并对齐搜索词，再完成翻译、语义检索、文化适配与合规审核，最终为商品标题返回适合目标市场的 Tag。

项目当前覆盖六个欧洲站点：`DE`、`FR`、`NL`、`UK`、`IT`、`ES`。

快速入口：

- [快速开始](#快速开始)
- [系统架构](#系统架构)
- [推荐 API](#推荐-api)
- [生产部署](#生产部署)
- [安全与合规](#安全与合规)
- [开发与验证](#开发与验证)

## 解决什么问题

跨境商品搜索通常同时面临三个问题：中文商品描述与本地搜索习惯不一致、同一个词在不同国家的语义和文化风险不同、AI 生成结果难以解释和追溯。Joytag 将这三个问题拆成可独立治理的阶段：

1. 从中文锚点和 Amazon / eBay 搜索建议构建候选词。
2. 通过本地 Embedding 与 Qdrant 对齐语义相近的中文锚点。
3. 使用规则库、LLM 评估和人工审核构成合规漏斗。
4. 将标签、来源、相似度、审核理由和运行批次一起保存，供推荐、审计和 DSAR 查询使用。

Joytag 当前是后端推荐 API 与管理后台，不是面向消费者的搜索引擎，也不是已经产品化的多租户 SaaS、店铺插件或商品上架工作台。它同样不替代目标国家的法律、广告或平台政策审查。

## 核心能力

- **多站点采集**：淘宝搜索建议用于中文锚点；Amazon completion 为海外主源，eBay autosuggest 为辅助源。
- **两阶段推荐**：Qdrant 向量召回候选标签，再由 LLM 结合商品标题、类目和近期热词精排。
- **三级合规漏斗**：安全词直通、禁用词和 UCPD Annex I 规则硬拦截、其余词进入 LLM 评估与人工复核。
- **可解释推荐**：返回标签理由、相似度、来源、中文锚点和合规理由等 provenance 字段。
- **可切换模型供应商**：通过 provider 适配层支持 OpenAI-compatible、Azure OpenAI 和 Amazon Bedrock，切换以配置为主。
- **隐私最小化**：发送给 LLM 前对邮箱、电话、IP、IBAN 和信用卡等信息做假名化；日志不保存完整商品标题。
- **可审计与可追溯**：管理操作使用 hash-chain 审计；LLM 调用保存 prompt 哈希、PII token 映射和词条哈希；采集任务保存 lineage 事件。
- **统一管理后台**：内嵌原生 HTML/JS 单页，提供待审核、标签库、锚点库、规则、拦截决策、审计、DSAR 和调度管理。

## 系统架构

~~~mermaid
flowchart LR
    A[中文锚点] --> B[动态种子与搜索建议]
    B --> C[翻译 / 去重 / 位置评分]
    C --> D[Embedding]
    D --> E[(Qdrant)]
    E --> F[规则库与 UCPD 规则]
    F --> G[LLM 文化与合规评估]
    G --> H[可复用标签]
    G --> I[待人工复核]
    G --> J[拦截决策留痕]

    K[商品标题 + 目标国家] --> L[PII 假名化]
    L --> M[向量召回]
    M --> E
    E --> N[LLM 精排]
    N --> O[推荐结果 + provenance]
~~~

### 运行组件

| 组件 | 作用 | 默认版本 / 实现 |
| --- | --- | --- |
| Backend | FastAPI API、采集调度、管理后台 | Python 3.11 + FastAPI + Uvicorn |
| Qdrant | 锚点、标签和审核队列的向量检索与存储 | v1.9.0，768 维 |
| Embedding | 中文锚点、海外词和标签的多语言向量化 | `Alibaba-NLP/gte-multilingual-base` |
| LLM | 翻译、文化适配、合规评估和推荐精排 | provider 适配层 |
| Postgres | 审计、LLM trace、lineage、DSAR 和留存策略 | PostgreSQL 16 |
| Keycloak | OIDC SSO、RBAC 和 TOTP | 24.0.5 |
| Scheduler | 定时采集和每日留存清理 | APScheduler 3.x |

### 数据流

#### 海外趋势采集

~~~text
中文锚点库最新词条
    -> LLM 批量翻译为六国种子
    -> Amazon + eBay 搜索建议
    -> 跨源去重与国家配额合并
    -> GTE 多语言向量直接检索中文锚点
    -> 规则硬拦截 / LLM 评估 / 人工复核
    -> local_tags、pending_review 或 blocked_decisions
~~~

#### 推荐请求

~~~text
商品标题 + 类目 + 目标国家
    -> PII 假名化
    -> Embedding
    -> Qdrant 过滤 country 与 compliance_status
    -> 召回 top-16，LLM 精排最多 top-8
    -> 返回默认最多 5 个带解释和 provenance 的 Tag
~~~

### Qdrant 集合

| 集合 | 内容 | 写入条件 |
| --- | --- | --- |
| `cn_anchors` | 中文商品锚点 | 采集、去重、向量化后写入 |
| `local_tags` | 可复用的本地化 Tag | 通过规则和合规评估后写入 |
| `pending_review` | 需要人工判断的词条 | LLM 返回“存疑”或进入审核队列 |
| `blocked_decisions` | 被拦截词的决策证据 | 保存国家、理由、规则 ID 和来源 |

所有集合使用确定性 ID，重复采集可以幂等更新；词条同时保留 `source_type`、`collection_run_id` 和 `collected_at` 等来源字段。

## 快速开始

### 前置条件

- Windows PowerShell 或兼容的 PowerShell 环境
- Docker Desktop / Docker Engine 与 Compose
- 本地开发需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)
- 一个可用的 LLM API Key；Qdrant 密钥也必须显式配置

### 本地开发

本地模式只启动 Qdrant，Backend 在本机以 Uvicorn 热重载运行。Postgres 和 Keycloak 在本地开发中可以不启动，相关审计与 trace 会降级为 best-effort；认证旁路仅用于本地开发。

~~~powershell
Copy-Item .env.example .env
# 编辑 .env，至少填写 QDRANT_API_KEY 和 LLM_API_KEY（或 DEEPSEEK_API_KEY）

./backend/setup_dev.ps1
./dev.ps1
~~~

首次安装脚本会创建 Python 3.11 虚拟环境、安装 `backend/requirements.txt`，并将 GTE 多语言 Embedding 模型下载到 `backend/models/gte-multilingual-base/`。模型权重目录被忽略；`backend/models/__init__.py` 与 `backend/models/schemas.py` 是启动必需的运行时 API 契约源码，必须纳入 Git。

启动后：

- 管理后台：http://localhost:8000/admin
- OpenAPI 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 直接使用 Docker

生产或集成环境使用四服务 Compose：

~~~powershell
Copy-Item .env.example .env
# 为 .env 填写所有必填密钥和 OIDC_ISSUER

docker compose up -d --build
docker compose ps
~~~

服务启动后，Backend 绑定到 `127.0.0.1:8001`，管理后台为 http://localhost:8001/admin。Keycloak 默认监听 `8080`；部署到服务器时必须用安全组或反向代理限制访问来源。

## 推荐 API

### `POST /v1/tag/recommend`

服务间调用需要 Keycloak Bearer token，并包含 `joytag:recommend` scope；默认限流为每个客户端 IP 每分钟 20 次。

请求示例：

~~~bash
curl -X POST http://localhost:8001/v1/tag/recommend \
  -H "Authorization: Bearer <access-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "女士防水轻量户外徒步鞋",
    "target_country": "DE",
    "category": "鞋靴",
    "top_k": 5
  }'
~~~

响应包含：

~~~json
{
  "recommendations": [
    {
      "word": "示例标签",
      "reason": "与商品标题和类目匹配",
      "similarity": 0.86,
      "source": "amazon_suggest",
      "compliance_reason": "通过目标国家规则与审核",
      "anchor_cn_word": "户外鞋",
      "trend_score": 0.74,
      "ai_generated": true
    }
  ],
  "total_candidates": 16,
  "filtered_candidates": 8,
  "ai_assisted": true,
  "parameters_version": "...",
  "disclosure_url": "/v1/disclosure/parameters"
}
~~~

### 其他公开接口

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 基础健康检查；`?deep=1` 同时检查 Qdrant、LLM 和 Postgres |
| `GET /v1/disclosure/parameters` | DSA Art.27 机器可读的推荐参数披露 |
| `GET /v1/transparency` | 公开透明度披露 JSON；`/transparency` 提供 HTML 版本 |
| `POST /v1/dsar/request` | 公开受理数据主体访问、删除或反对请求，限流 5 次/小时 |

### 管理 API 与权限

管理页面使用会话 Cookie + CSRF 双提交；API 按角色控制：

| 角色 | 权限 |
| --- | --- |
| `admin` | 全部管理能力，包括规则、删除、审计、DSAR 和留存策略 |
| `reviewer` | 待审核词条通过 / 拒绝，以及只读能力 |
| `operator` | 采集触发、定时任务和运行统计 |

主要管理资源包括 `/admin/api/collect/*`、`/admin/api/pending/*`、`/admin/api/tags/*`、`/admin/api/anchors/*`、`/admin/api/rules/*`、`/admin/api/blocked`、`/admin/api/audit`、`/admin/api/dsar` 和 `/admin/api/retention/*`。完整请求模型可通过 `/docs` 查看。

## 配置

从 `.env.example` 复制配置后，根据运行模式填写变量。不要把 `.env`、真实 API Key 或生产数据库密码提交到仓库。

| 变量 | 用途 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | LLM 适配器 | `openai_compat`、`azure` 或 `bedrock` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 服务 | 默认配置面向 DeepSeek 兼容接口 |
| `QDRANT_URL` / `QDRANT_API_KEY` | 向量数据库 | Compose 会将 URL 覆盖为 `http://qdrant:6333` |
| `DATABASE_URL` | 合规数据库 | Compose 自动注入容器内地址 |
| `AUTH_ENABLED` | 管理端认证 | 本地开发为 `false`；生产必须为 `true` |
| `OIDC_ISSUER` / `OIDC_JWKS_URL` / `OIDC_TOKEN_URL` | Keycloak OIDC | 外部 issuer 与容器内 token/JWKS 地址可分开配置 |
| `SESSION_SECRET` | 会话签名 | 认证开启时必填，建议使用 `openssl rand -hex 32` 生成 |
| `PII_GUARD_MODE` | PII 处理 | 默认 `regex_only`；仅允许在受控环境关闭 |
| `CORS_ORIGINS` | 跨域白名单 | 默认空，同源内嵌管理页不需要配置 |
| `TLS_ENABLED` | Secure Cookie | 接入 HTTPS 反向代理后设为 `true` |

Compose 还要求填写 `POSTGRES_PASSWORD`、`JOYTAG_DB_PASSWORD`、`KEYCLOAK_DB_PASSWORD` 和 `KEYCLOAK_ADMIN_PASSWORD`。完整变量和示例注释以 [.env.example](.env.example) 为准。

## 生产部署

1. 准备一台至少约 6 GB 内存的服务器，安装 Docker Compose。
2. 复制 `.env.example` 为 `.env`，生成并填写所有基础设施密钥、LLM 配置和 `OIDC_ISSUER`。
3. 启动服务：`docker compose up -d --build`。
4. 为 `8001` 和 `8080` 设置安全组白名单；不要把 Qdrant `6333/6334` 或 Postgres `5432` 暴露到公网。
5. 通过 Keycloak 创建首个用户并分配 `admin` 角色；首次登录按要求绑定 TOTP。
6. 配置域名和 TLS 反向代理后，将 `TLS_ENABLED=true`，再开放管理端访问。

建议上线前执行：

~~~powershell
docker compose config
docker compose ps
Invoke-WebRequest http://localhost:8001/health
~~~

### 持久化与备份

| 路径 / 卷 | 内容 | 备份建议 |
| --- | --- | --- |
| `qdrant_storage/` | 锚点、标签、审核队列和拦截决策 | 与 Postgres 一起定期备份 |
| `pgdata/` | 审计、LLM trace、lineage、DSAR 和留存策略 | 必须纳入加密备份 |
| `backend/data/rules/` | 各国安全词和禁用词 | 纳入版本或配置备份 |
| `backend/*_collection_progress.json` | 采集去重进度 | 可重建，但建议备份 |
| `huggingface_cache` | Embedding 模型缓存 | 可重新下载，不是业务数据 |

Qdrant 或 Postgres 卷丢失后，词库、审计证据和合规留痕无法仅靠源代码恢复；生产环境应把数据卷备份作为部署验收项。

## 安全与合规

Joytag 提供的是工程控制措施，不构成法律意见，也不自动保证部署方满足 GDPR、DSA、UCPD 或 AI Act 的全部义务。实际合规性取决于数据来源、处理目的、合同、DPA、国家规则和上线配置。

当前实现包含：

- Keycloak OIDC 授权码 + PKCE、RBAC、强制 TOTP 和服务间 scope 校验。
- 管理变更的 hash-chain 审计；审计写入失败会阻止变更被视为成功。
- LLM provider 适配、发送前 PII 假名化、prompt 哈希和调用 trace。
- 采集批次和词级输出的 lineage 事件，支持来源追踪。
- GDPR DSAR 工单、跨 Qdrant/Postgres 检索、擦除闭环和 ERASE 证据。
- 默认留存策略与每日清理任务，支持管理员按策略调整。
- UCPD Annex I 相关的环保声明、碳抵消和法定要求宣传规则种子。

生产环境还应自行完成：密钥托管、TLS、备份恢复演练、访问日志与告警、规则库复核、供应商合规审查、数据主体通知和渗透测试。

更多法规背景与分阶段计划见 [docs/EU_COMPLIANCE_PLAN.md](docs/EU_COMPLIANCE_PLAN.md)。

## 开发与验证

项目使用标准库 `unittest` 做 API 契约测试，未引入 pytest 或专用 Python linter。CI 在 Python 3.11 下安装依赖后执行编译、应用导入和测试；其中导入检查能发现 `compileall` 无法发现的缺失运行时模块。本地修改后可运行：

~~~powershell
git diff --check
./.venv/Scripts/python.exe -m compileall -q backend
Push-Location backend
../.venv/Scripts/python.exe -c "import app"
../.venv/Scripts/python.exe -m unittest discover -s tests -v
Pop-Location
docker compose config
~~~

建议提交前重点检查：

- 新增 API 是否同步更新 OpenAPI 模型和本 README 的接口说明。
- 所有管理写操作是否保留 CSRF、角色依赖和审计装饰器。
- 新的数据源、LLM 调用和词条写入是否带 `run_id`、provenance 和 trace/lineage。
- 规则变更是否同时覆盖国家规则、UCPD 内置规则和前端国家列表。
- 是否误提交 `.env`、模型目录、运行时进度、数据库卷或日志。

## 目录结构

~~~text
backend/
  app.py                         # FastAPI 路由与生命周期
  models/
    schemas.py                   # 受版本控制的 API 契约与合规常量（启动必需）
  services/
    collectors/                  # 中文、Amazon、eBay 采集和种子构建
    alignment.py                 # 翻译、锚点对齐、合规评估和入库
    recommend.py                 # 向量召回与 LLM 精排
    qdrant_store.py              # Qdrant 集合与幂等 CRUD
    llm.py / llm_provider.py     # LLM 调用、重试和 provider 适配
    pii_guard.py                 # PII 假名化
    audit.py / llm_trace.py      # 审计与 LLM 留痕
    lineage.py / dsar.py         # 血缘与数据主体请求
    auth.py / retention.py       # 认证和留存策略
  static/                        # 内嵌管理单页
  tests/                         # unittest 接口契约与回退行为测试
docs/EU_COMPLIANCE_PLAN.md       # EU 合规改造计划
keycloak/                        # Keycloak realm 与数据库初始化脚本
docker-compose.yml               # Qdrant、Postgres、Keycloak、Backend
~~~

## 参与贡献

欢迎通过 Issue 或 Pull Request 改进采集器、规则库、推荐质量和合规控制。提交前请：

1. 说明变更目的、影响范围和验证方式。
2. 不提交密钥、真实用户数据、数据库卷、模型文件或运行时缓存。
3. 对 API、数据格式或规则语义的变化补充文档和迁移说明。
4. 运行 `git diff --check`，并确认 CI 可以在 Python 3.11 下完成。

## 许可证

Joytag 使用 [MIT License](LICENSE) 发布。
