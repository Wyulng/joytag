# Joytag EU 合规改造方案（GDPR / DSA / UCPD + AI Act 附带）

> 状态：**代码内合规控制已按当前仓库落地**（截至 2026-08-20）。本文件同时保留历史实施蓝图；能力是否已实现以当前代码为准，法律结论仍需独立法律审查。
>
> ## 〇、执行进度
>
> | 阶段 | 内容 | 状态 |
> |------|------|------|
> | P0-1~P0-8 | 合规基座：db/audit（hash-chain）、llm_provider + pii_guard + llm_trace、日志脱敏、rule_manager（UCPD 种子 + schema v2 + BE/LU bug）、qdrant_store（blocked_decisions + provenance + DSAR 原语）、alignment/采集器（拦截持久化 + source 透传 + run_id lineage）、auth.py（Keycloak OIDC + CSRF）+ app.py 接线 + 审计装饰器、Keycloak realm + compose 4 服务、文档更新 + 首次 git commit | ✅ 完成 |
> | P1 | recommend 响应 provenance（source/compliance_reason/anchor_cn_word/trend_score/ai_generated + ai_assisted/parameters_version/disclosure_url）、`/v1/disclosure/parameters`（DSA Art.27 机器可读，公开）、审计查看页 + hash-chain 校验按钮、blocked 列表页、规则页 rule_id/added_by 显示 | ✅ 完成 |
> | P2 | dsar.py + 端点（`/v1/dsar/request` 公开 5/hour + `/admin/api/dsar/*` 检索/擦除）+ DSAR 管理页、retention.py + `compliance_retention` 每日 03:00 UTC 系统任务 + 配置/手动触发端点、`/transparency` 公开透明度页（Art.14(5)(b) + DSA 摘要 + AI Act 声明 + DSAR 表单） | ✅ 完成（代码） |
> | 部署验证 | 服务器 `docker-compose down && up -d --build`、Keycloak 首用户 kcadm 引导、安全组收紧、curl 验证清单（见第十节） | ⏳ 待执行（部署时） |
> | TLS | 域名就绪后 Caddy TLS + `TLS_ENABLED=true`（Let's Encrypt 不签发裸 IP） | ⏳ 待域名 |

## 一、背景与目标

Joytag 为 Joybuy 欧洲站点提供长尾标签推荐：淘宝搜索建议构建中文锚点，Amazon 搜索建议作为海外主源，eBay 搜索建议作为海外辅助源；海外词经 GTE 多语言向量直接匹配中文锚点，再通过规则检查和 LLM 合规评估后写入 Qdrant，由推荐 API 输出。LLM 通过 provider 适配层调用，默认配置为 DeepSeek/OpenAI-compatible，并可通过配置切换 Azure 或 Bedrock；翻译仅用于从中文锚点批量生成采集种子。生产配置已实现 Keycloak SSO、RBAC、CSRF 与端口改绑；TLS 仍需在实际部署时完成。

目标：**技术层面**（代码与架构）使系统满足 GDPR（数据合规、透明度、删除权、出境）、DSA（推荐系统透明度、可解释）、UCPD（标签不得构成误导性商业行为）要求。不做合规申报文档，法规强制披露以代码功能落地（参数披露接口、DSAR 受理端点、透明度页面）。

## 二、法规调研结论

### GDPR 要点（对 Joytag 的影响）
1. **外部搜索建议数据的合法性与最小化**：当前采集对象是淘宝、Amazon 和 eBay 的搜索建议，不采集账号、帖子或用户档案。正式部署前仍需分别核验接口条款、数据库权利、合法处理依据、必要性与留存范围，并在透明度材料中列明来源和反对渠道；是否需要 DPIA 由法律顾问结合实际处理活动判断。
2. **DeepSeek 出境是最大风险点**：中国无充分性认定（adequacy decision）；Berlin DPA 2025-05 认定 DeepSeek 违反 Art. 46(1) 并推动应用下架，意大利 Garante 2025-01 封锁。SCC + TIA（Schrems II）因 PIPL 强制披露义务极难通过。→ **LLM 提供方策略必须调整**（见决策 D1）。
3. **删除权（Art. 17）**：删除须 30 天内完成；向量库层要求**入库时给每条 embedding 打主体/来源标识**，支持按元数据过滤删除；软删除有泄漏风险 → 用硬删除；日志需脱敏 + TTL 清理任务；LLM 调用痕迹需留存策略。删除操作本身要有审计记录。
4. **处理活动记录（Art. 30）+ 可责性（Art. 5(2)）**：记录"谁在何时做了什么"，管理操作全审计。

### DSA 要点（对 Joytag 的影响）
1. **Art. 27 推荐系统透明度**：适用于**所有**在线平台（非仅 VLOP），若 Joytag 的标签影响 Joybuy 站内搜索/推荐排序，平台须在条款中**通俗语言**披露主要参数（最显著标准 + 相对重要性理由），并提供**用户可随时修改参数的选项**。→ Joytag 需提供：参数文档（向量相似度 + LLM 重排 + 合规过滤 + 国家过滤）、逐条推荐理由（相似度得分/来源/合规依据）。
2. **Art. 30-32 商家可追溯（KYBC）**：平台自身义务，Joytag 通过数据结构支持。
3. **P2B 法规 Art. 5**：对商业用户（卖家）的排名透明度，与 DSA Art. 27 叠加。

### UCPD 要点（对 Joytag 的影响）
1. **2024/825 指令（EmpCo）**：修正 UCPD 并新增 Annex I 黑名单**本身禁止**的行为：无认证的可持续标签、无根据的通用环保声明（"eco-friendly"等）、以偏概全声明、基于碳抵消的声明（"climate neutral"）、把法定要求当卖点。成员国 2026-03-27 前转化，**2026-09-27 起适用**（即将生效）。
2. 对 Joytag：标签合规审核（可复用/需拦截/存疑）需将 Annex I 类别纳入 banned 词规则与 LLM 审核提示词，并**留存审核理由作为可举证记录**。

### AI Act（附带，2026-08-02 已生效的透明度义务）
- Art. 50(1)：与自然人交互的 AI 系统须告知其正在与 AI 交互（除非语境显然）。作为部署方（deployer）Joytag 需最低限度的 AI 披露机制（响应携带 ai_generated 标记 + 透明度页声明）。

## 三、云服务厂商合规做法借鉴（共性模式）

| 模式 | AWS | Azure | 阿里云 |
|------|-----|-------|--------|
| 处理协议/证明库 | Artifact | Service Trust Portal | GDPR 白皮书 + ISO 27001/27701 |
| 全量管理操作审计 | CloudTrail | Activity Log | ActionTrail |
| 合规证据自动化 | Audit Manager | Purview Compliance Manager | SLS 日志服务（内置 PII 脱敏） |
| 敏感数据发现 | Macie | Purview 数据地图 | — |
| 数据驻留 | 区域固定 + 限制策略 | Geographies | 都柏林/法兰克福 EU 区域 |
| 策略即代码 | Config Rules / SCP | Azure Policy | OPA/Rego 通用 |

**可借鉴落地的 6 条**：①所有管理操作全审计（CloudTrail 模式）；②审计日志 ≥6 个月留存；③合规证据自动化归档（→ 本项目 `/admin/api/audit` + verify）；④数据分类 + 敏感发现（→ Presidio）；⑤数据驻留区域固定；⑥策略即代码（规则文件版本化）。

## 四、可复用开源项目评估

| 项目 | 许可证 | 用途 | 采纳 |
|------|--------|------|------|
| **Microsoft Presidio** | Apache-2.0 | 本地 PII 检测/脱敏，LLM 调用前假名化 | ✅ 采纳（pip 库，无独立服务） |
| **OpenLineage**（LF AI 标准） | Apache-2.0 | 数据血缘事件规范（dataset/job/run/facet） | ✅ 采纳事件格式（自存 Postgres JSONB，不上 Marquez） |
| Marquez | Apache-2.0 | OpenLineage 参考实现（需独立服务） | ⚠️ 本项目规模不建议 |
| Langfuse | MIT | LLM trace 全链路（需 Postgres+ClickHouse+Redis+S3，重型） | ⚠️ P0 自建轻量 trace 表（参考其数据模型） |
| OPA | Apache-2.0 | 合规策略即代码 | ✅ 概念采纳（规则 JSON 版本化，暂不引服务） |
| **Keycloak** | Apache-2.0 | 管理后台 SSO/RBAC/MFA | ✅ 采纳（用户要求最高标准） |
| Trillian/sigstore | Apache-2.0 | 审计日志防篡改 | ⚠️ P0 自建 hash-chain 足够 |

## 五、历史差距与当前闭环状态

下表中的“改造前差距”用于解释实施缘由，不代表当前代码状态。

| # | 改造前差距 | 当前仓库实现 | 状态 |
|---|-------------|---------------|------|
| G1 | 管理变更无审计 | Postgres hash-chain 审计、操作者与 request ID 留痕、全链校验 | 已实现 |
| G2 | LLM 地址硬编码、原文外发、无 trace | provider 适配层、PII 假名化、日志最小化、`llm_trace` | 已实现；供应商与跨境安排待部署方审查 |
| G3 | 删除仅覆盖单库、无留存策略 | DSAR 跨 Qdrant/Postgres 检索与擦除、ERASE 事件、定时留存清理 | 已实现 |
| G4 | 无公开披露与反对渠道 | `/transparency`、`/v1/transparency`、`/v1/dsar/request` | 已实现 |
| G5 | 管理端无认证、基础设施端口暴露 | Keycloak OIDC、RBAC、TOTP、CSRF、服务 scope、端口绑定与必填密钥 | 已实现；TLS 待部署 |
| D1 | 推荐参数与来源不可解释 | 版本化参数披露，响应保留 source、reason、anchor、trend 与 AI 标识 | 已实现 |
| D2/T1 | 数据源、批次和审核链路断裂 | 采集 run/词级 lineage、provenance、拦截决策持久化 | 已实现 |
| U1 | 环保声明规则与拦截证据不足 | UCPD Annex I 规则种子、规则 ID、LLM 评估与 `blocked_decisions` | 已实现；规则内容待法律复核 |
| T2 | 完整标题日志与无限留存 | 哈希化/脱敏日志、容器轮转、Postgres 留存策略 | 已实现 |

### 仍未完成或不能由代码单独完成

- 域名、Caddy/等效反向代理、TLS 证书与 `TLS_ENABLED=true` 的生产部署。
- 与真实客户商品库、运营上架系统或消费者搜索引擎的集成与效果验证。
- Qdrant 与 Postgres 的自动备份、恢复演练、RPO/RTO 验收和灾难恢复记录。
- 对数据源条款、处理依据、DPIA、供应商 DPA/SCC/TIA、国家广告规则和披露文本的独立法律审查。
- 生产告警、密钥托管、渗透测试和访问控制复核。

### 关键决策（已确认）
- **D1（LLM）**：保留 DeepSeek + 假名化 + 适配层，EU API 切换 = 纯配置变更（OpenAI 兼容系改 env 即换；Azure/Bedrock 预置薄分支）。
- **D2（认证）**：最高标准 → Keycloak SSO（每人身份 + RBAC 角色 + MFA + OIDC 授权码+PKCE）；recommend API 走 client_credentials（scope `joytag:recommend`）。
- 审计/溯源/trace 存 Postgres（Keycloak 共用实例分两库）。
- 当前国家范围以代码中的 `DE/FR/NL/UK/IT/ES` 为准；LLM 调用统一走 provider 适配层，不在推荐模块重复硬编码供应商端点。

## 六、目标架构

```
Internet / 云安全组
   │（P2: Caddy :443/:80，需域名；此前 HTTP + 安全组 IP 白名单）
   ▼
backend FastAPI :8001（映射 127.0.0.1）── qdrant :6333（改绑 127.0.0.1，4 个集合）
   │  OIDC 资源服务器（JWKS 验 JWT）+ 管理会话 cookie
   │  └─ postgres :5432（改绑 127.0.0.1）
   │       ├─ keycloak 库
   │       └─ joytag 库（audit_log / llm_trace / lineage_event /
   │            dsar_request / retention_policy / audit_chain_head）
   ▼
Keycloak :8080（对外但安全组限办公 IP；realm 导入；MFA TOTP 强制）
```

内存预算：qdrant 1G + backend 2G + keycloak ~1G + postgres 512M ≈ 5G → 部署前必须 `free -h` 确认 ≥6GB。

## 七、实施改造方案

### 7.1 新增模块（backend/services/）

| 模块 | 职责 |
|------|------|
| `llm_provider.py` | provider 适配层：`BaseLLMProvider` + `OpenAICompatProvider`（env: `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`）+ `AzureProvider` + `BedrockProvider`（httpx+SigV4）；`get_llm_provider()` 惰性单例；复用 http_client.py |
| `pii_guard.py` | Presidio 假名化：默认 `PII_GUARD_MODE=regex_only`（email/6 国电话/IBAN/IP/信用卡），可选 `spacy_sm`；`pseudonymize(text) -> (clean_text, {token: entity_type})` 只存 token→类型映射；经 `asyncio.to_thread` |
| `db.py` | psycopg3 + psycopg-pool；`ensure_schema()` 幂等 DDL + retention_policy 种子 |
| `audit.py` | hash-chain 审计：`record_event()` / `verify_chain()` / `list_audit()` / `redact_audit_for_erasure()` |
| `llm_trace.py` | LLM 留痕：`record_trace()` / `purge_expired()` / `find_by_word_hash()` |
| `lineage.py` | OpenLineage 形状血缘事件（JSONB 自存）：`record_event(run_id, job_name, event_type, inputs, outputs, facets)` |
| `auth.py` | authlib 验 JWT（JWKS 缓存 + 未知 kid 重取）+ `require_role`/`require_scope` 依赖；itsdangerous 会话 cookie（HttpOnly/SameSite=Lax/TLS 时 Secure）+ CSRF 双提交 + PKCE |
| `dsar.py` | `create_request`/`search_subject_data`/`execute_erasure`/`record_objection` |
| `retention.py` | 留存策略表读写 + `run_all_purges()`；APScheduler 系统任务每日 03:00 UTC |

**角色映射**（Keycloak realm roles）：`admin` = 全部；`reviewer` = pending 审核 + 只读 tags/rules；`operator` = 采集/调度 + stats；服务账号 scope `joytag:recommend` = recommend API。

### 7.2 Postgres schema（joytag 库，幂等 DDL）

- `audit_chain_head`：单行链头（`FOR UPDATE` 串行化）
- `audit_log`：id、ts、actor_sub、actor_username、actor_roles TEXT[]、action（`pending.approve`/`tag.delete`/`rule.add_banned`…）、resource_type、resource_id、resource_snapshot JSONB、detail JSONB、request_id、ip、prev_hash、hash、expires_at（396 天）；索引 ts/actor/resource/expires_at
- hash = `sha256(prev_hash || id || ts || actor_sub || action || resource_type || resource_id || snapshot::text)`，同事务写链头
- `llm_trace`：call_type（assess/translate/rerank）、provider、model、request_id、prompt_hash（假名化提示词 sha256）、prompt_pii JSONB、word_sha256、response（假名化）、result JSONB、latency_ms、retry_count、error、expires_at（90 天）
- `lineage_event`：run_id、job_name、event_type、facets JSONB（OpenLineage 形状）、expires_at（396 天）
- `dsar_request`：ticket_id UUID、request_type（access/erasure/objection）、status、contact、subject_note、findings、erasure_proof、created_by
- `retention_policy`：key/days（默认 llm_trace=90、lineage=396、audit=396）

### 7.3 文件级改动

**backend/app.py**：lifespan 加 init_db + provider 预热；所有 `/admin/api/*` 加 `require_admin_session` + 角色依赖；recommend 加 `require_scope("joytag:recommend")`；新端点 `/auth/login|callback|logout`、`/admin/api/audit`(+verify)、`/admin/api/dsar`(+search/erase/complete)、`/admin/api/blocked`、`/admin/api/retention/config`(+run)、公开 `/v1/disclosure/parameters`、公开 `/v1/dsar/request`（5/hour）；`@audited` 装饰器挂全部管理变更端点（审计失败 → 500）；删 app.py:134 全文标题日志；版本升 2.0.0。

**backend/models/schemas.py**：`RecommendItem` 加 `source/compliance_reason/anchor_cn_word/trend_score/ai_generated`；`RecommendResponse` 加 `ai_assisted/parameters_version/disclosure_url`；`RejectRequest(reason 必填)`；新增 Audit/Dsar/Blocked/Retention/DisclosureParameters 模型。

**backend/services/llm.py**：去硬编码 URL 走 provider；retry 包装记录耗时 + llm_trace；assess 提示词升级 UCPD Annex I 判定（引用 rule_id）；发送前假名化。

**backend/services/recommend.py**：去重复硬编码；rerank 补重试；标题假名化；llm_trace（rerank）；fallback 打 `ai_generated=False`；候选透传 provenance。

**backend/services/alignment.py**：加 source/collection_run_id 参数；需拦截分支写 `blocked_decisions`（不再丢弃）；CN 流程加 provenance；每词 lineage 事件。

**backend/services/qdrant_store.py**：新集合 `blocked_decisions`（零向量占位，惰性创建）；三个 upsert payload 加 `provenance/llm_trace_id/rule_ids/assessed_by/updated_at`；新增 blocked CRUD、`search_words_exact`、`delete_points_by_word`、`get_point`。

**backend/services/rule_manager.py**：VALID_COUNTRIES 加 be/lu（修 bug）；规则文件 schema v2（word/categories/rule_id/added_by/added_at，兼容旧格式）；内置 UCPD 六语种子；banned 子串匹配、safe 精确匹配；返回 rule_id。

**backend/services/collectors/overseas_trends.py + cn_longtail.py**：source 透传修复；run_id + lineage START/COMPLETE/FAIL。

**backend/services/logging_config.py**：JSONFormatter 加脱敏 patterns；导出 `sanitize_for_log()`。

**backend/services/task_scheduler.py**：注册系统任务 `compliance_retention`（每日 03:00 UTC）。

**backend/web_ui.py + static/**：新页面 transparency/audit/dsar；页面路由加 `require_admin_session` 未登录 302；admin.js 加 credentials/CSRF 头/401 跳登录；pending.html 拒绝必填理由。

**docker-compose.yml**（保持 v1 兼容）：qdrant 端口改绑 127.0.0.1 + 日志轮转 + 密钥改 env 必填；backend 端口改绑 + depends_on 加 postgres/keycloak；新增 postgres:16-alpine（127.0.0.1:5432、pgdata 卷、512M、pg_isready）；新增 keycloak:24.0.5（8080 对外但安全组限 IP、1G、`--import-realm`）；新增 keycloak/realm-export.json + init-keycloak-db.sql。

**backend/requirements.txt**：+ authlib、psycopg[binary]、psycopg-pool、presidio-analyzer、itsdangerous、cryptography。

**.env.example**：+ LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL、PII_GUARD_MODE、DATABASE_URL、OIDC_ISSUER/OIDC_JWKS_URL/OIDC_CLIENT_ID、SESSION_SECRET、TLS_ENABLED、KEYCLOAK_PUBLIC_HOSTNAME、POSTGRES_PASSWORD。

**README.md / 部署说明**：更新"无认证"章节。

### 7.4 关键设计决策

1. JWT 校验用 authlib（python-jose 实质停维护）；JWKS 缓存 TTL 3600s + 未知 kid 重取；iss/JWKS 双 env 解耦双主机名。
2. 管理 UI 用服务端会话 cookie（token 不进 JS）；CSRF 双提交。
3. 审计 hash-chain + `/admin/api/audit/verify` 全链校验；擦除用脱敏替换保链。
4. Lineage 存 OpenLineage 形状 JSONB，不引 Marquez。
5. 留存默认：llm_trace 90 天、lineage/audit 396 天；容器日志 10MB×5。
6. `需拦截` 决策进 Qdrant `blocked_decisions`（词级数据集中一处）。

## 八、分阶段执行清单

**P0 合规基座**：①依赖 + db.py + audit.py ②llm_provider + pii_guard + llm_trace + llm.py/recommend.py 接线 ③日志脱敏 ④rule_manager 修 bug + UCPD 种子 + assess 提示词 ⑤qdrant_store provenance + blocked_decisions + 采集器透传 ⑥auth.py + app.py 接线 + 审计装饰器 + 前端 CSRF/401 + reject 理由 ⑦Keycloak realm + docker-compose ⑧文档更新 + 首次 commit。

**P1 解释与披露**：recommend 响应 provenance + `/v1/disclosure/parameters`；规则页显示 rule_id；审计查看页 + verify。

**P2 生命周期与透明页**：dsar + retention + transparency 页；域名就绪后 Caddy TLS。

## 九、验证方案

1. 每阶段 `python -m compileall backend`
2. 服务器 `docker-compose down && docker-compose up -d --build`，确认 `[db] schema ready`、`compliance_retention` 注册
3. `/admin` → Keycloak 登录 → TOTP → 回跳；cookie HttpOnly 确认
4. 带会话无 CSRF 头的 POST → 403
5. UI 通过一条 pending → 审计表出现记录（含操作者）；verify ok；篡改一行后 verify 报 mismatch
6. client_credentials token → recommend 200 带 provenance；无 token → 401
7. DSAR 擦除：Qdrant 点消失、trace 清除、audit 行 `<REDACTED>`、erasure_proof 有证据
8. **Provider 切换测试**：`LLM_BASE_URL` 指本地端点重启 → 零代码改动跑通 recommend
9. `POST /admin/api/retention/run` 返回清理计数
10. 采集含 "eco friendly fashion" → 出现在 blocked 且理由引用 `ucpd_env_generic`
11. 服务器 `docker-compose config -q` 静默通过（v1.29.2）

## 十、风险与前置条件

- **R1 内存**：部署前 `free -h` 确认 ≥6GB；不足则降 qdrant/Keycloak 限额或 Keycloak 独立机器。
- **R2 TLS 需域名**：Let's Encrypt 不签发裸 IP；域名就绪前 HTTP + 安全组 IP 白名单过渡（写入 README.md 部署警示），P2 接 Caddy。
- **R3 Keycloak 远程引导**：`kcadm.sh` 脚本化创建首用户；realm 层强制 OTP。
- **R4 Presidio 中文误报**：regex-only 保守模式 + 型号 token 白名单；P0 后观察。
- **R5 审计失败=500**：可责性优先；healthcheck 扩展 DB 连通性。
- **R6 blocked_decisions**：惰性创建无迁移。
- **待确认**：办公 IP 白名单维护流程。

## 参考来源（法规调研）

- EDPB Guidelines 03/2026 web scraping（licentium.io、Slaughter and May、Clifford Chance 解读）
- Berlin DPA DeepSeek 决定（vitallaw.com、tahota.com）
- DSA Art. 27（eu-digital-services-act.com、pinsentmasons.com）
- UCPD 2024/825（climate-laws.org、reach24h.com）
- AI Act 时间线（licentium.io、sota.io）
- 云厂商：AWS Artifact/CloudTrail/Audit Manager、Azure Purview/Compliance Manager、阿里云 ActionTrail/SLS
- 开源：Presidio（microsoft/presidio）、OpenLineage（openlineage.io）、Langfuse（langfuse.com）、Keycloak
