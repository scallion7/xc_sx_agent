# Ecom-Service-Agent

一个以电商客服为业务场景的 AI Agent 教学与二次开发项目。

项目先完成了从 Prompt、ReAct、Function Calling 到 MCP、RAG、Multi-Agent、Memory、Skills 和 Agent Evaluation 的渐进式教学实现，随后完成面向工程实践的二次开发：把已有能力组合成具备确定性业务流程、持久化、服务接口、安全边界、可观测性和人工协作机制的完整演示系统。

> 当前二次开发进度：阶段 22 已完成计划运行治理、Alembic 迁移、OIDC/JWT 认证
> 端口、业务 Provider 与回调、独立补偿、规则版本、API/运维治理、数据生命周期和
> 离线质量门禁。当前完整离线回归共 144 项，不访问真实或本地模型、真实 OIDC 或
> 真实业务供应商。

## 项目定位

电商客服是 Agent 较典型的落地场景，既包含自然语言理解，也包含订单查询、政策检索、退款申请等真实业务动作。本项目用一个统一场景展示以下问题：

- LLM 如何通过 ReAct 循环选择和调用工具；
- 如何用结构化输出约束 Agent 的最终结果；
- 如何在单 Agent 与 Multi-Agent 模式之间切换；
- 如何通过 RAG、Memory 和 Skills 扩展 Agent 能力；
- 如何评估工具调用过程和最终回答；
- 如何把敏感业务动作收敛到状态机、风险规则和人工审批中。

默认 SQL 模式会把演示订单、商品和物流种子写入 SQLite/PostgreSQL，退款状态、
售后工单和副作用操作日志均真实持久化。种子数据可通过
`COMMERCE_SEED_DEMO_DATA=false` 关闭，以接入真实业务数据。仓库仍是教学与作品
演示项目，生产使用前应关闭演示数据、执行数据库迁移，并接入密钥管理和组织级身份系统。

## 当前核心能力

### 已有教学能力

| 模块 | 实现内容 |
|---|---|
| Prompt 与结构化输出 | 电商客服 System Prompt、意图识别、置信度、转人工标记和追问字段 |
| 多轮对话 | 会话 JSON 持久化、历史摘要压缩、上下文恢复与重置 |
| ReAct 与工具调用 | LLM → 工具调用 → Observation → 最终回答的循环执行 |
| Plan-and-Execute | 单/Multi-Agent 复杂请求规划、依赖调度、只读并发和副作用确认恢复 |
| MCP | Streamable HTTP MCP Server、工具发现、Schema 转换和本地降级 |
| RAG | Markdown 切分、OpenAI Embedding、Numpy/Chroma 双向量后端与离线词法降级 |
| Multi-Agent | 售前、售后、投诉路由，ReAct/计划策略切换和子 Agent 工具隔离 |
| Memory | 会话内短期记忆、跨会话长期记忆和记忆查询工具 |
| Skills | 渐进式发现和加载退货、订单跟踪、商品推荐等标准流程 |
| Evaluation | 隔离沙箱、Trace 采集、规则指标、LLM-as-Judge 和聚合报告 |

### 已完成的二次开发

二次开发的目标是把原来的“能够回答和调用工具”升级为“能够安全地推进售后业务流程”。本阶段采用以下原则：

> LLM 负责理解用户、收集信息和解释结果；确定性代码负责状态转换、风险判断、审批权限和敏感操作。

当前已落地：

1. 售后工单、Policy RAG、Human-in-the-Loop 与版本化风险路由；
2. 实例级 ToolRegistry 和依赖隔离；
3. 统一模型网关（超时、重试、限流和指标）；
4. 统一事件总线、Trace、日志、WebSocket 与评估订阅器；
5. Repository/Service 分层、SQLite/PostgreSQL 持久化和用户数据隔离；
6. FastAPI、实时 SSE、管理后台和离线回归看板；
7. Prompt Injection 防护、敏感信息脱敏、RBAC 和敏感工具权限；
8. Docker Compose API + PostgreSQL 演示环境；
9. Pydantic 工具参数契约、单/Multi-Agent Plan-and-Execute 与离线 CI 门禁；
10. 跨步骤结果引用、有限并发、计划身份、确认恢复、过期/取消和跨模式权限保护；
11. 独立 Plan Repository、计划版本与乐观锁、步骤投影、审计时间线和历史 API；
12. 售后副作用事务日志、持久化幂等、原子抢占、租约恢复、重试耗尽和补偿登记。
13. 计划运行记录、逐步骤检查点、执行租约、fencing、超时、取消、预算、背压与熔断；
14. Alembic 升降级、静态/OIDC 认证端口、Provider 契约、签名回调去重和独立补偿操作；
15. 风险规则 draft/active/retired、试运行、激活、回滚和工单规则版本留痕；
16. 统一错误码、请求追踪、cursor 分页、持久化幂等键、分层限流、就绪诊断、备份清理
    以及 40 条固定计划案例的确定性 CI 门禁。

## 二次开发架构

```text
CLI / REST / SSE / WebSocket
  │
  ▼
Application Factory → TraceContext → Guardrails
  │
  ├─ 单 Agent：EcomAgent ─────────────────────────┐
  └─ Multi-Agent：RiskRouter → Router → 业务子 Agent ─┤
                                                  ▼
                                         StrategySelector
                                  ┌───────────────┴───────────────┐
                                  ▼                               ▼
                                ReAct                    Plan-and-Execute
                                             （检查点/租约/超时/取消/预算/恢复）
                                  └───────────────┬───────────────┘
                                                  ├──────────────→ ModelGateway
                                                  │              （超时/重试/限流/指标）
                                                  ├──────────────→ PlanLifecycleService
                                                  │              → PlanRepository
                                                  │                （JSON / SQL / 审计）
                                                  ├──────────────→ PlanRunService
                                                  │              （租约 / fencing / 检查点）
                                                  ▼
                                         实例级 ToolRegistry
                                   （Multi-Agent 使用子 Agent 白名单）
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
   CommerceService   Policy RAG    AfterSaleService
          │                              │
          ▼                    ┌─────────┴─────────┐
 CommerceRepository            ▼                   ▼
（SQL / Mock 适配器）     工单级风险路由      OperationRepository
                         │                 （JSON / SQL / 乐观锁）
                         ▼                          │
                 自动批准 / 人工审批                ▼
                                         幂等抢占 → Provider 契约
                                         → 重试 / 恢复 / 独立补偿操作

所有核心路径 ──emit──> EventBus
                         ├─ JSONL 日志
                         ├─ 指标聚合
                         ├─ SSE / WebSocket
                         └─ 离线评估 Trace
```

## 二次开发内容详解

### 1. 售后工单 Skill

原来的 `process-return` Skill 主要指导 Agent 查询订单、检索政策并调用退款工具。二次开发后，它被升级为完整的售后工单流程：

```text
确认订单
  → 判断订单状态
  → 检索适用政策
  → 校验售后资格和举证要求
  → 复述订单号与售后原因
  → 获得用户明确确认
  → 创建售后工单
  → 根据风险等级自动批准或等待人工审批
  → 执行已批准工单
  → 查询并告知处理进度
```

售后工单包含：

- 工单号、订单号、售后类型和原因；
- 订单金额、风险等级、路由结果和风险原因；
- 工单状态和审批状态；
- 举证情况、审批人和审批意见；
- 执行结果以及完整审计日志。

支持的售后类型包括：

- 未发货取消；
- 退货退款；
- 换货；
- 商品质量问题；
- 错发、漏发或缺件；
- 物流破损；
- 其他复杂售后。

当前状态流转：

```text
低风险：approved → processing → completed

中高风险：pending_review → approved → processing → completed
                         └→ rejected
```

状态转换全部由 `AfterSaleService` 控制。待审批、被拒绝或状态不合法的工单无法执行。

### 2. Policy RAG

通用知识检索已经升级为带政策元数据和可信判断的 Policy RAG。

知识文档支持以下元数据：

```yaml
policy_type: after_sale
version: 2026.1
effective_date: 2026-01-01
scope: mainland_china
expires_at: 2026-12-31
```

当前政策类型包括：

- `after_sale`：退换货与退款政策；
- `shipping`：配送政策；
- `membership`：会员权益；
- `faq`：常见问题。

`search_knowledge` 除了返回检索片段，还会返回：

- `grounded`：是否获得达到阈值的政策证据；
- `requires_human`：证据不足时是否应该转人工；
- `citation`：文档和章节引用；
- `version`、`effective_date` 和 `policy_type`；
- 相似度分数和适用性说明。

Agent 回答政策问题时必须引用检索结果。当 `grounded=false` 时，不允许继续编造结论，应说明依据不足并转人工确认。

首次启动尚未构建向量索引，或 Embedding 服务暂时不可用时，系统会自动使用本地
Markdown 的确定性词法检索作为降级路径；重排、政策有效期、可信阈值和引用规则保持
不变。可通过 `RAG_LEXICAL_FALLBACK_ENABLED=false` 强制要求向量索引。

### 3. Human-in-the-Loop

高风险售后不会由 Agent 自动完成，而是进入独立人工审批队列。

为了避免模型“自己申请、自己审批”，审批动作没有注册为 Function Calling 工具。Agent 只能：

- 创建售后工单；
- 查询工单状态；
- 执行已经批准的工单。

人工审核通过独立脚本完成：

```powershell
# 查看待审批工单
python app/scripts/review_after_sale.py list

# 批准工单
python app/scripts/review_after_sale.py approve AS-XXXXXXXXXX `
  --reviewer customer-service-01 `
  --note "订单与举证材料核验通过"

# 拒绝工单
python app/scripts/review_after_sale.py reject AS-XXXXXXXXXX `
  --reviewer customer-service-01 `
  --note "超过政策规定的售后时效"

# 查看并恢复失败或租约过期的副作用操作
python app/scripts/review_after_sale.py operations
python app/scripts/review_after_sale.py recover AS-XXXXXXXXXX `
  --reviewer customer-service-01

# 登记人工补偿
python app/scripts/review_after_sale.py compensate AS-XXXXXXXXXX `
  --status pending `
  --reviewer customer-service-01 `
  --note "需要冲正"
```

创建、提交审批、批准、拒绝、开始执行和执行结果都会写入工单审计日志。

### 4. 风险路由

风险路由分为消息级和工单级两层。

消息级风险用于识别严重投诉或安全事件，例如：

- 消协、起诉、律师、报警；
- 人身伤害、触电、爆炸；
- 隐私泄露、账户盗刷；
- 媒体曝光和监管投诉。

命中严重风险后：

- 单 Agent 模式强制将 `requires_human` 设置为 `true`；
- Multi-Agent 模式优先路由到投诉 Agent；
- 最终结果同样被确定性标记为需要人工处理。

工单级风险会综合判断：

- 订单金额；
- 售后类型；
- 是否提供图片或视频证据；
- 订单是否已经存在退款流程；
- 是否包含投诉、安全或法律风险信号。

| 风险等级 | 路由 | 系统行为 |
|---|---|---|
| low | `auto_process` | 自动批准，可以执行 |
| medium | `manual_review` | 进入普通人工审批 |
| high | `human_escalation` | 进入高优先级人工处理 |
| critical | `human_escalation` | 强制转人工，不允许自动处理 |

每个风险结论都包含 `risk_reasons`，便于解释、审计和后续调优。

### 5. 新增工具与安全边界

二次开发新增三个 Agent 工具：

| 工具 | 作用 |
|---|---|
| `create_after_sale_case` | 用户确认后创建工单并执行风险路由 |
| `query_after_sale_case` | 查询风险、审批、执行进度和审计记录 |
| `execute_after_sale_case` | 执行已经自动批准或人工批准的工单 |

旧版 `apply_refund` 的底层实现仍用于兼容历史课程和 MCP 服务，但启用 `AFTER_SALE_ENABLED=true` 后，`ToolManager` 不再将其暴露给模型，Agent 无法绕过工单和人工审批直接退款。

### 6. 服务化、持久化与用户隔离

- `SessionRepository`、`AfterSaleRepository`、`AfterSaleOperationRepository` 和
  `CommerceRepository` 是业务层依赖的协议；
- SQLAlchemy 适配器同时支持本地 SQLite 与 PostgreSQL；
- 订单、商品、物流、退款、会话、售后工单和副作用操作均有 SQL 表；
- 订单查询、退款、会话和工单查询都按 `user_id` 校验归属；
- JSON 与 Mock 适配器只保留给旧课程和轻量本地开发；
- 演示数据初始化是幂等的，生产接入真实库时可关闭。

### 7. 模型网关与实例级工具

业务 Agent 不再直接调用模型 SDK。`ModelGateway` 统一负责超时、指数退避重试、
应用运行时共享的滑动窗口限流、Token/延迟指标和失败事件，并允许注入离线测试客户端
或私有兼容接口。

每个 Agent 拥有独立的 `ToolRegistry`、Memory、Skill、KnowledgeService、
CommerceService 和 AfterSaleService，不再通过模块级 setter 或 Retriever
单例共享运行时依赖。

### 8. API、安全与可观测性

FastAPI 提供同步聊天、实时 SSE、WebSocket 事件、会话、订单、商品、售后查询、
人工审批、指标和回归报告接口。静态 Token 映射只用于演示，但权限检查与用户隔离
位于 API 和业务服务两层。

安全护栏在模型调用前拦截常见 Prompt Injection，限制输入长度，并对 API Key、
手机号、邮箱、身份证号和银行卡号脱敏。脱敏覆盖模型上下文、会话持久化、
工具事件和最终输出；退款与售后执行工具还有独立权限校验。

事件总线统一记录 `trace_id`、`session_id`、`user_id`、模型 Token/延迟、
工具成功/失败和路由事件。CLI、JSONL、内存指标、SSE/WebSocket 和评估沙箱
使用不同订阅器，不需要修改 Agent 核心代码。

### 9. 确定性计划执行与生命周期

单 Agent 和 Multi-Agent 均支持 `react`、`plan_execute` 和 `auto` 三种策略。
Plan-and-Execute 先生成结构化计划，再由本地执行器完成确定性校验和调度：

- 拒绝未知工具、无效参数、未知依赖、自依赖和循环依赖；
- 通过 `${steps.<step_id>.output.<field_path>}` 在步骤之间传递原始类型结果；
- 同一依赖层的只读步骤按 `PLAN_MAX_CONCURRENCY` 有限并发；
- 创建工单、执行工单等副作用步骤保持串行，并等待用户明确确认；
- 计划持久化 `plan_id`、生命周期、TTL、恢复次数和待确认步骤；
- 确认轮先写入 `running`，再恢复原计划，不重新规划或重复只读步骤；
- 计划支持查询、取消、过期和重复确认保护。

Multi-Agent 会先由 Router 选定售前、售后或投诉子 Agent，再使用该子 Agent 的
`ToolManager` 规划和执行。计划会持久化 `agent_key` 与原风险转人工标记；确认轮恢复
原子 Agent，不重新路由。Multi-Agent 计划也不能切换到工具范围更大的单 Agent 执行。

### 10. 售后副作用可靠执行

每个售后工单使用稳定的 `after_sale.execute:<case_id>` 幂等键和请求摘要。执行前必须
先在 `AfterSaleOperationRepository` 中原子抢占租约；重复请求会根据持久化状态返回
“执行中”、成功回放或重试耗尽，不会直接再次调用退款适配器。

操作状态为 `running / failed / succeeded / exhausted`，同时记录尝试次数、版本、租约、
结果和补偿状态。失败操作可以在限制内重试，进程中断留下的过期租约可以由管理员恢复；
SQL 后端用唯一幂等键和 `WHERE version = ?` 条件更新处理跨进程竞争，JSON 后端使用
同进程共享锁和原子文件替换。退款适配器收到同一个幂等键，若发现外部退款已存在，
服务会把工单与操作日志收敛到成功状态。

重试耗尽后工单进入 `execution_failed`，不再自动执行。管理员可以登记
`pending → completed/failed` 补偿状态，全部抢占、失败、完成、回放和补偿变更都会发送
EventBus 事件并保留工单审计记录。

### 11. 计划执行治理与工程化闭环

阶段 22 为每次计划执行建立独立运行记录，使用 worker、租约和递增 fencing token
控制唯一写入者；步骤进入运行态和终态时都会保存检查点及追加式尝试证据。执行器统一
处理步骤/计划超时、实时取消、只读重试、副作用禁止通用重试、并发组、输出上限、
总预算、背压和熔断，恢复时复用已完成步骤。

SQL 结构由 Alembic 统一管理，认证支持静态 Token 与可注入 JWKS 的 OIDC JWT；
订单、物流、退款和补偿通过 Provider 协议隔离，回调必须通过 HMAC 签名并按事件 ID
持久化去重。补偿操作、风险规则版本和管理写接口幂等结果均有独立 SQL/JSON 状态。

API 增加请求/Trace 标识、统一错误码、cursor 分页、用户/管理员/高成本端点限流、
`/ready`、脱敏诊断和计划运行视图。维护命令默认 dry-run，并保护活跃计划、运行中
操作和未完成补偿；SQLite 备份恢复在写入前后执行完整性检查。

离线门禁把 40 条固定计划案例逐条映射到可执行的确定性测试，并聚合 12 项指标；
安全相关指标必须为 `1.0`。
CI 同时覆盖 Python 3.10–3.12、SQLite/PostgreSQL 迁移、覆盖率、OpenAPI、SBOM、
依赖审计和容器扫描。

## 项目结构

```text
ecom-service-agent/
├── main.py                              # CLI 入口，切换单 Agent / Multi-Agent
├── app/
│   ├── agent/
│   │   ├── base.py                      # 共享会话与依赖生命周期
│   │   ├── chat.py                      # 单 Agent ReAct/计划执行与恢复入口
│   │   ├── after_sale/                  # 二次开发：售后工单领域层
│   │   │   ├── models.py                # 工单、副作用操作、风险和审计模型
│   │   │   ├── repository.py            # 仓储协议与 JSON 适配器
│   │   │   ├── operation_repository.py  # 幂等抢占、租约与 JSON 操作日志
│   │   │   ├── risk.py                  # 消息级/工单级风险路由
│   │   │   └── service.py               # 创建、审批和执行状态转换
│   │   ├── tools/
│   │   │   ├── manager.py               # 本地/MCP 工具聚合和安全过滤
│   │   │   ├── contracts.py             # Pydantic 工具参数契约
│   │   │   ├── after_sale.py            # 售后工单 Agent 工具
│   │   │   ├── knowledge.py             # 实例级 Policy RAG 服务
│   │   │   ├── definitions.py            # 无运行时依赖的工具 Schema
│   │   │   └── registry.py               # 处理器、用户上下文和权限绑定
│   │   ├── skills/definitions/
│   │   │   └── process-return/SKILL.md   # 深化后的售后流程 Skill
│   │   ├── strategies/
│   │   │   ├── selector.py               # 确定性策略选择
│   │   │   ├── models.py                 # 计划、步骤与生命周期模型
│   │   │   ├── executor.py               # 依赖、超时、取消、预算与恢复执行器
│   │   │   ├── policies.py               # 工具策略、资源预算与熔断器
│   │   │   └── plan_execute.py           # 规划、持久化与结果合成
│   │   ├── rag/                          # Chunk、Embedding、Retriever、双后端
│   │   └── memory/                       # 短期和长期记忆
│   ├── api.py                            # REST 与 WebSocket 路由注册
│   ├── web/                              # Web 请求模型、服务组装、SSE 和静态页
│   ├── infrastructure/                   # 模型网关、数据库、Alembic 与 SQL 适配器
│   ├── repositories/                     # 会话/计划运行/规则的 JSON/SQL 仓储
│   ├── services/
│   │   ├── commerce.py                   # 订单、商品与物流应用服务
│   │   ├── plans.py                      # 计划生命周期、旧快照迁移与 API 适配
│   │   ├── plan_runs.py                  # 运行租约、检查点与 fencing
│   │   ├── providers.py                  # 业务 Provider、回调验证与去重
│   │   ├── compensations.py              # 独立补偿操作与 exactly-once
│   │   ├── idempotency.py                # 管理写接口幂等结果回放
│   │   └── data_maintenance.py           # 数据保留、备份、恢复与脱敏导出
│   ├── observability/                    # EventBus、Trace、日志和指标
│   ├── security/                         # Token RBAC、注入防护和脱敏
│   ├── multi_agent/                      # Router、子 Agent、工具隔离与计划恢复
│   ├── evaluation/                       # 沙箱、故障注入、确定性指标与 LLM Judge
│   └── scripts/
│       ├── build_kb_index.py             # 构建 Policy RAG 索引
│       ├── review_after_sale.py           # 独立人工审批入口
│       ├── manage_migrations.py          # Alembic 统一迁移入口
│       ├── maintain_data.py              # 清理与 SQLite 备份入口
│       ├── doctor.py                     # 就绪、迁移与运行租约诊断
│       ├── run_eval.py                   # 离线评估
│       └── run_api.py                    # API 启动入口
├── mcp_server/server.py                  # Streamable HTTP MCP Server
├── Dockerfile
├── docker-compose.yml                    # API + PostgreSQL
├── tests/                                # 原有测试和阶段 22 治理回归
└── docs/                                 # 第 1–22 期教学与二次开发文档
```

## 快速开始

### 1. 创建环境并安装依赖

需要 Python 3.10 或更高版本。

macOS / Linux：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

```powershell
Copy-Item .env.example .env
```

至少填写：

```dotenv
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o-mini
```

二次开发相关配置：

```dotenv
POLICY_RAG_MIN_SCORE=0.2
AGENT_STRATEGY=react
MAX_PLAN_STEPS=6
PLAN_MAX_CONCURRENCY=3
PLAN_TTL_SECONDS=900
PLAN_MAX_RESUME_COUNT=3
MULTI_AGENT_ENABLED=false
AFTER_SALE_ENABLED=true
AFTER_SALE_AUTO_APPROVE_MAX_AMOUNT=200
AFTER_SALE_HIGH_RISK_AMOUNT=2000
RISK_RULES_PATH=app/config/risk_rules.json
PERSISTENCE_BACKEND=sql
DATABASE_URL=sqlite:///app/sessions/xc_sx_agent.db
COMMERCE_SEED_DEMO_DATA=true
API_USER_TOKENS=dev-user-token:user-1
API_ADMIN_TOKENS=dev-admin-token:admin
```

### 3. 构建 Policy RAG 索引

使用默认 Numpy 后端：

```powershell
python app/scripts/build_kb_index.py --backend numpy
```

使用 Chroma 后端：

```powershell
python app/scripts/build_kb_index.py --backend chroma
```

知识文档或政策元数据发生变化后，需要重新构建索引。

### 4. 启动客服 Agent

启动前可先检查 Python、API Key、Skills 和 RAG 索引：

```bash
python main.py --check
```

按 `.env` 自动选择运行模式：

```powershell
python main.py
```

也可以在不修改 `.env` 的情况下显式选择模式：

```bash
python main.py --mode single
python main.py --mode multi
python main.py --mode single --strategy plan_execute
python main.py --mode single --strategy auto
python main.py --mode multi --strategy plan_execute
python main.py --mode multi --strategy auto
```

`react` 保持原有行为；`plan_execute` 始终先规划；`auto` 仅在检测到“同时、
顺便、然后”等多诉求表达时启用计划模式。Multi-Agent 会先路由到售前、售后或
投诉子 Agent，再在该子 Agent 的工具白名单内规划和执行；确认轮恢复原子 Agent，
不会重新路由。

CLI 命令：

- `skills`：查看已发现的 Skills；
- `memory`：查看短期和长期记忆；
- `doctor`：检查当前运行环境；
- `reset`：重置当前对话；
- `quit` / `exit`：保存并退出。

启用 Multi-Agent：

```dotenv
MULTI_AGENT_ENABLED=true
```

### 5. 启动 FastAPI

```bash
python app/scripts/run_api.py
```

常用入口：

- OpenAPI：`http://127.0.0.1:8000/docs`
- 管理后台：`http://127.0.0.1:8000/admin`
- 健康检查：`GET /health`
- 同步聊天：`POST /v1/chat`
- 实时事件流：`POST /v1/chat/stream`
- WebSocket：`/ws/events/{session_id}?token=...`
- 会话查询/删除：`GET/DELETE /v1/sessions/{session_id}`
- 计划查询/取消：`GET/DELETE /v1/sessions/{session_id}/plan`
- 计划历史/详情/取消：`GET /v1/plans`、`GET/DELETE /v1/plans/{plan_id}`
- 计划审计：`GET /v1/admin/plans`、`GET /v1/admin/plans/{plan_id}`
- 事件查询：`GET /v1/events`
- 订单、商品和售后查询：`GET /v1/orders`、`GET /v1/products`、
  `GET /v1/after-sales/{case_id}`
- 售后执行记录：`GET /v1/after-sales/{case_id}/operation`
- 副作用运维：`GET /v1/admin/after-sales/operations`、
  `GET /v1/admin/after-sales/operations/recoverable`、
  `POST /v1/admin/after-sales/{case_id}/recover`、
  `POST /v1/admin/after-sales/{case_id}/compensation`
- 人工审批与指标：`/v1/admin/after-sales/*`、`/v1/admin/metrics`、
  `/v1/admin/regression`

请求使用 `X-API-Key`。`.env.example` 中的开发 Token 只用于本地演示。

### 6. Docker Compose 演示

```bash
docker compose up --build
```

该命令启动 PostgreSQL 17 和非 root API 容器，并配置数据库与 API 健康检查。

### 7. 可选：启动 MCP Server

第一个终端：

```powershell
python mcp_server/server.py
```

第二个终端将 `.env` 设置为：

```dotenv
MCP_ENABLED=true
MCP_SERVER_URL=http://127.0.0.1:9123/mcp
```

然后运行：

```powershell
python main.py
```

## 二次开发演示场景

### 低风险自动处理

演示种子订单 `ORD-20240125-006` 金额为 29.90 元，低于默认自动审批阈值。

```text
用户：订单 ORD-20240125-006 的保护壳不合适，我想退货。
Agent：查询订单和售后政策，并向用户复述订单与原因。
用户：确认申请。
Agent：创建售后工单，风险路由返回 low/auto_process。
Agent：执行已批准工单并返回工单号和处理结果。
```

### 高金额人工审批

演示种子订单 `ORD-20240110-003` 金额为 5999 元。

```text
用户：订单 ORD-20240110-003 的手机有质量故障，我要退款，已经提供视频。
Agent：查询订单、检索政策并要求最终确认。
用户：确认。
Agent：创建 pending_review 工单，不执行退款。
人工：通过 review_after_sale.py 审批。
用户：查询工单进度。
Agent：读取 approved 状态后继续执行或告知下一步。
```

### 严重风险强制转人工

```text
用户：商品爆炸导致受伤，我已经联系律师和消协。
```

该消息会命中确定性高风险规则，路由到投诉处理并强制标记 `requires_human=true`。

### Multi-Agent 复杂计划与确认恢复

使用 `python main.py --mode multi --strategy plan_execute` 启动后，可演示同一子 Agent
内的查询、规划、确认和精确恢复：

```text
用户：查询订单 ORD-20240125-006，然后为不合适的保护壳申请退货。
Router：路由到售后 Agent，并仅开放售后工具白名单。
Agent：完成只读查询，保存计划，在创建工单前请求确认。
用户：确认执行。
Agent：恢复原售后计划，仅执行待确认步骤，不重新路由、规划或重复查询。
```

若确认时改用单 Agent 模式、计划已过期/取消，或恢复次数超过上限，系统会拒绝
继续执行，避免扩大工具权限或重复触发副作用。

## 测试与评估

当前完整离线回归共 144 项，不会调用外部模型或本地模型、OIDC 和 Provider 服务：

```bash
python -m unittest discover -s tests -v
```

开发环境可额外安装 Ruff，并在提交前执行格式化与静态检查：

```bash
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
```

测试覆盖：

- 未确认不能创建售后工单；
- 低风险工单自动批准并执行；
- 高金额工单进入人工审批；
- 待审工单无法执行；
- 人工批准后可以执行；
- 同一订单的活跃工单幂等；
- 严重风险信号强制转人工；
- Policy 元数据正确进入 Chunk；
- ToolRegistry 实例隔离与敏感操作权限；
- 模型网关超时、重试与事件指标；
- SQL 会话、订单、退款和工单的用户隔离与持久化；
- FastAPI 鉴权、SSE、管理后台和回归接口；
- Prompt Injection 阻断及模型调用前敏感信息脱敏；
- Docker Compose 结构与离线评估事件采集；
- Plan 拓扑排序、循环依赖拒绝和未知工具拦截；
- 跨步骤结果引用、原始类型传递和无效引用拒绝；
- 只读步骤有限并发、稳定调度顺序和 Trace 上下文传播；
- 副作用步骤确认、失败依赖阻断和已完成步骤不重复执行；
- 计划身份、TTL、恢复次数、查询/取消 API 和重复确认保护；
- Multi-Agent 子 Agent 工具隔离、原路由恢复、风险标记持久化与跨模式保护；
- JSON/SQL 独立计划仓储、旧快照迁移、用户隔离与乐观锁冲突；
- 计划历史、详情、按身份取消、步骤快照和审计时间线 API；
- JSON/SQL 售后操作日志、唯一幂等键、并发原子抢占和版本冲突；
- 失败重试、重试耗尽、过期租约恢复和外部已生效结果收敛；
- 操作用户隔离、管理员恢复权限、管理台审计和人工补偿状态流转；
- 步骤/计划超时、实时取消、依赖跳过、只读重试、副作用阻断、配额和熔断；
- JSON/SQL 运行租约、崩溃接管、fencing、检查点和追加式尝试证据；
- OIDC JWT 校验、JWKS 刷新、Alembic 空库升级/兼容升级/降级；
- Provider 超时与错误映射、签名回调去重、独立补偿 exactly-once；
- 规则版本 diff/试运行/激活/回滚、持久化管理幂等键和安全启动；
- 就绪诊断、分层限流、cursor 分页、数据清理、事件保留及 SQLite 备份恢复；
- 40 条固定计划案例、12 项确定性指标、OpenAPI 与迁移漂移门禁；
- 工具 Schema 与 Pydantic 参数契约一致性；
- GitHub Actions Python 3.10–3.12、SQLite/PostgreSQL、覆盖率、SBOM 与安全扫描。

仅运行计划策略专项回归：

```bash
python -m unittest tests.test_strategy_development -v
python -m unittest tests.test_plan_repository -v
python -m unittest tests.test_side_effect_reliability -v
python -m unittest tests.test_execution_governance -v
python -m unittest tests.test_plan_recovery -v
python -m unittest tests.test_auth_and_migrations -v
python -m unittest tests.test_provider_and_policy_governance -v
python -m unittest tests.test_stage_22_quality_gate -v
python -m app.scripts.validate_plan_quality
```

数据库与运维验收：

```bash
python -m app.scripts.manage_migrations upgrade
python -m app.scripts.manage_migrations check
python -m app.scripts.doctor
python -m app.scripts.maintain_data cleanup
```

`cleanup` 默认只预览；只有显式增加 `--apply` 才会清理超过保留期且已进入终态的数据。

运行原有专项测试（其中 Agent/RAG/评估脚本可能调用配置的模型，不属于上面的纯离线验收）：

```powershell
python tests/test_agent.py
python tests/test_react_agent.py
python tests/test_rag.py --backend numpy
python tests/test_multi_agent.py
python tests/test_memory.py
python tests/test_skills.py
python tests/test_evaluation.py
```

运行离线评估：

```powershell
# 不启用 LLM-as-Judge；被测 Agent 本身仍会调用配置的模型
python app/scripts/run_eval.py --no-judge

# 包含 LLM-as-Judge
python app/scripts/run_eval.py --mode multi --output app/sessions/eval_report.json
```

## 开发历程

### 第 11 期：架构演进与统一应用工厂

- 提取 `BaseConversationAgent`，统一会话、记忆、Skill、结构化输出和历史压缩；
- 新增 `app.bootstrap.create_agent()`，CLI、评估和 API 入口使用同一组装方式；
- Agent 支持注入配置与模型客户端，便于离线测试、私有模型和多环境部署；
- ToolManager 的售后模式改为实例配置，减少运行时对全局 settings 的隐式依赖；
- 评估沙箱使用配置副本，不再污染主进程全局配置；
- 新增 `python main.py --check` 与交互式 `doctor` 运行诊断。

详细设计与已完成的替换路线见
[第11期-架构演进与应用工厂](docs/第11期-架构演进与应用工厂.md)。

### 第 16–22 期：确定性计划与副作用执行演进

- 第 16 期引入强类型工具契约和基础 Plan-and-Execute；
- 第 17 期补齐跨步骤引用、计划身份和精确确认恢复；
- 第 18 期增加有限并发、TTL、恢复上限以及查询/取消 API；
- 第 19 期将计划执行接入 Multi-Agent，并绑定子 Agent 工具范围与原路由。
- 第 20 期引入独立 Plan Repository、乐观锁、历史 API 和审计时间线。
- 第 21 期为售后副作用增加持久化幂等、原子抢占、租约恢复和补偿状态。
- 第 22 期完成计划运行治理、迁移与认证、Provider/规则治理、运维诊断和离线质量闭环。

| 阶段 | 主题 | 状态 |
|---|---|---|
| 第 1 期 | 项目框架、Prompt 客服、结构化输出 | 已完成 |
| 第 2 期 | 多轮对话、摘要压缩、JSON 持久化 | 已完成 |
| 第 3 期 | ReAct 与 Function Calling | 已完成 |
| 第 4 期 | MCP Streamable HTTP 集成 | 已完成 |
| 第 5 期 | RAG 与政策知识库 | 已完成 |
| 第 6 期 | Multi-Agent 售前/售后/投诉分流 | 已完成 |
| 第 7 期 | 短期与长期 Memory | 已完成 |
| 第 8 期 | 可复用 Agent Skills | 已完成 |
| 第 9 期 | Agent 沙箱、Trace 与双层评估 | 已完成 |
| 二次开发阶段 1 | 数据库、业务服务和真实状态机 | 已完成 |
| 二次开发阶段 2 | FastAPI、流式响应和管理后台 | 已完成 |
| 二次开发阶段 3 | 售后 Skill、Policy RAG、HITL、风险路由 | 已完成 |
| 二次开发阶段 4 | 可观测性、安全护栏和自动化回归 | 已完成 |
| 阶段 12 | 实例级 ToolRegistry 与依赖隔离 | 已完成 |
| 阶段 13 | ModelGateway、超时、重试与限流 | 已完成 |
| 阶段 14 | EventBus、Trace、日志、指标与实时订阅 | 已完成 |
| 阶段 15 | API、Repository/Service 与 SQL 持久化 | 已完成 |
| 阶段 16 | Plan-and-Execute、强类型工具契约与离线 CI | 已完成 |
| 阶段 17 | 跨步骤引用、计划持久化与精确确认恢复 | 已完成 |
| 阶段 18 | 有限并发、计划生命周期与查询取消 API | 已完成 |
| 阶段 19 | Multi-Agent 计划执行与子 Agent 工具隔离 | 已完成 |
| 阶段 20 | 独立计划仓储、乐观锁与审计时间线 | 已完成 |
| 阶段 21 | 售后副作用可靠执行、恢复与补偿 | 已完成 |
| 阶段 22 | 计划执行治理、数据安全、业务适配与工程化闭环 | 已完成 |

## 相关文档

- [第1期：Prompt 工程与结构化输出](docs/第1期-Prompt工程与结构化输出.md)
- [第2期：多轮对话管理](docs/第2期-多轮对话管理.md)
- [第3期：ReAct 与工具调用](docs/第3期-ReAct与工具调用.md)
- [第4期：MCP 集成](docs/第4期-MCP集成.md)
- [第5期：RAG 检索增强生成](docs/第5期-RAG检索增强生成.md)
- [第6期：Multi-Agent 协作](docs/第6期-Multi-Agent协作.md)
- [第7期：Memory 短期记忆与长期记忆](docs/第7期-Memory短期记忆与长期记忆.md)
- [第8期：Skill 可复用能力模块](docs/第8期-Skill可复用能力模块.md)
- [第9期：Agent 评估体系](docs/第9期-Agent评估体系.md)
- [二次开发：售后工单、Policy RAG 与人工审批](docs/第10期-Agent深化售后工单PolicyRAG与人工审批.md)
- [第11期：架构演进与应用工厂](docs/第11期-架构演进与应用工厂.md)
- [阶段12-15：工程化二次开发完成说明](docs/阶段12-15-工程化二次开发完成说明.md)
- [第16期：Plan-and-Execute 与强类型工具契约](docs/第16期-Plan-and-Execute与工具契约.md)
- [第17期：Plan-and-Execute 2.0 与确认恢复](docs/第17期-Plan-and-Execute2.0与确认恢复.md)
- [第18期：计划调度与生命周期](docs/第18期-计划调度与生命周期.md)
- [第19期：Multi-Agent 计划执行与工具隔离](docs/第19期-Multi-Agent计划执行与工具隔离.md)
- [第20期：独立计划仓储与审计](docs/第20期-独立计划仓储与审计.md)
- [第21期：售后副作用可靠执行](docs/第21期-售后副作用可靠执行.md)
- [第22期：计划执行治理与工程化闭环](docs/第22期-计划执行治理与工程化闭环.md)

## 后续路线

阶段 22 已将当前能够离线开发和验证的二次开发内容收口到四个工作包：

1. **计划运行时治理**：完整步骤状态、逐步骤检查点、运行租约、fencing、步骤/计划
   超时、实时取消传播、工具策略、有限重试、熔断、资源预算、背压和崩溃接管；
2. **数据、安全与业务适配**：Alembic 迁移、静态 Token/OIDC AuthProvider、订单/
   物流/退款/补偿 Provider、回调去重、补偿执行闭环以及风险与政策版本治理；
3. **API、运维与数据生命周期**：统一错误码、请求追踪、分页、幂等与端点限流、
   配置分层、安全启动、就绪探针、管理诊断、数据保留、备份恢复和审计；
4. **故障评估与 CI 门禁**：固定计划数据集、可控故障注入、确定性可靠性指标、
   Python 版本矩阵、SQLite/PostgreSQL、迁移升级/降级、SBOM 和依赖安全检查。

阶段 22 实际新增 76 项离线测试，完整回归由 68 项增至 144 项；固定计划数据集
40 条，12 项确定性指标均为 `1.0`。详细实现和验收边界见
[第22期：计划执行治理与工程化闭环](docs/第22期-计划执行治理与工程化闭环.md)。

## 当前边界

阶段 22 已完成通用 OIDC、Alembic、Provider、补偿执行和规则治理的代码端口、Fake
适配器及离线契约测试。以下内容仍必须由真实部署环境提供，不能由当前仓库独立完成：

- 真实组织 OIDC tenant、issuer、scope、单点登录和密钥轮换联调；
- 真实订单、物流、退款和支付冲正供应商的账号、字段、签名及凭据；
- 云 KMS、Secrets Manager、WAF、网关限流和生产网络策略；
- `COMMERCE_SEED_DEMO_DATA=true` 仅用于演示，真实环境应关闭并接入业务主数据；
- 风险规则和政策文档仍需法务、风控和客服运营正式审核；
- 真实模型质量、费用、供应商故障、生产流量和 SLA 压测；
- 生产备份恢复、跨区域容灾和正式发布流程。
