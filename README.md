# Ecom-Service-Agent

一个以电商客服为业务场景的 AI Agent 教学与二次开发项目。

项目的第一阶段已经完成从 Prompt、ReAct、Function Calling 到 MCP、RAG、Multi-Agent、Memory、Skills 和 Agent Evaluation 的渐进式教学实现。目前正在进入面向作品展示和工程实践的二次开发阶段，重点不再是继续堆叠概念，而是把已有能力组合成具备确定性业务流程、安全边界和人工协作机制的完整系统。

> 当前二次开发进度：已完成“Agent 深化”阶段，包括售后工单 Skill、Policy RAG、Human-in-the-Loop 和风险路由。

## 项目定位

电商客服是 Agent 较典型的落地场景，既包含自然语言理解，也包含订单查询、政策检索、退款申请等真实业务动作。本项目用一个统一场景展示以下问题：

- LLM 如何通过 ReAct 循环选择和调用工具；
- 如何用结构化输出约束 Agent 的最终结果；
- 如何在单 Agent 与 Multi-Agent 模式之间切换；
- 如何通过 RAG、Memory 和 Skills 扩展 Agent 能力；
- 如何评估工具调用过程和最终回答；
- 如何把敏感业务动作收敛到状态机、风险规则和人工审批中。

当前仓库适合作为 Agent 学习样例和作品展示基础，但订单、商品、物流及退款执行仍使用 Mock 数据，不应直接用于生产环境。

## 当前核心能力

### 已有教学能力

| 模块 | 实现内容 |
|---|---|
| Prompt 与结构化输出 | 电商客服 System Prompt、意图识别、置信度、转人工标记和追问字段 |
| 多轮对话 | 会话 JSON 持久化、历史摘要压缩、上下文恢复与重置 |
| ReAct 与工具调用 | LLM → 工具调用 → Observation → 最终回答的循环执行 |
| MCP | Streamable HTTP MCP Server、工具发现、Schema 转换和本地降级 |
| RAG | Markdown 切分、OpenAI Embedding、Numpy/Chroma 双向量后端 |
| Multi-Agent | 售前、售后、投诉三个子 Agent，以及 LLM 意图路由和工具隔离 |
| Memory | 会话内短期记忆、跨会话长期记忆和记忆查询工具 |
| Skills | 渐进式发现和加载退货、订单跟踪、商品推荐等标准流程 |
| Evaluation | 隔离沙箱、Trace 采集、规则指标、LLM-as-Judge 和聚合报告 |

### 正在进行的二次开发

二次开发的目标是把原来的“能够回答和调用工具”升级为“能够安全地推进售后业务流程”。本阶段采用以下原则：

> LLM 负责理解用户、收集信息和解释结果；确定性代码负责状态转换、风险判断、审批权限和敏感操作。

当前已经落地四项 Agent 深化能力：

1. 售后工单 Skill；
2. Policy RAG；
3. Human-in-the-Loop；
4. 风险路由。

## 二次开发架构

```text
用户消息
  │
  ├─ 单 Agent：EcomAgent
  │
  └─ Multi-Agent：Router → 售前 / 售后 / 投诉 Agent
                         │
                         ▼
                  消息级风险路由
                         │
                         ▼
                    ReAct 工具循环
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
      订单/物流工具   Policy RAG    售后工单工具
                                      │
                                      ▼
                               工单级风险路由
                         ┌────────────┴────────────┐
                         │                         │
                         ▼                         ▼
                  low：自动批准          medium/high/critical
                         │                         │
                         ▼                         ▼
                    执行售后操作              人工审批队列
                                                   │
                                          approve / reject
                                                   │
                                                   ▼
                                         执行或终止售后流程
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

## 项目结构

```text
ecom-service-agent/
├── main.py                              # CLI 入口，切换单 Agent / Multi-Agent
├── app/
│   ├── agent/
│   │   ├── chat.py                      # 单 Agent ReAct 主循环
│   │   ├── after_sale/                  # 二次开发：售后工单领域层
│   │   │   ├── models.py                # 工单、状态、风险和审计模型
│   │   │   ├── repository.py            # JSON 工单持久化
│   │   │   ├── risk.py                  # 消息级/工单级风险路由
│   │   │   └── service.py               # 创建、审批和执行状态转换
│   │   ├── tools/
│   │   │   ├── manager.py               # 本地/MCP 工具聚合和安全过滤
│   │   │   ├── after_sale.py            # 售后工单 Agent 工具
│   │   │   ├── knowledge.py             # Policy RAG 工具
│   │   │   └── registry.py              # Function Calling Schema 与分发
│   │   ├── skills/definitions/
│   │   │   └── process-return/SKILL.md   # 深化后的售后流程 Skill
│   │   ├── rag/                          # Chunk、Embedding、Retriever、双后端
│   │   └── memory/                       # 短期和长期记忆
│   ├── multi_agent/                      # Router、售前/售后/投诉 Agent
│   ├── evaluation/                       # 沙箱、Trace、指标与 LLM Judge
│   └── scripts/
│       ├── build_kb_index.py             # 构建 Policy RAG 索引
│       ├── review_after_sale.py           # 独立人工审批入口
│       └── run_eval.py                   # 离线评估
├── mcp_server/server.py                  # Streamable HTTP MCP Server
├── tests/                                # 原有测试和深化回归测试
└── docs/                                 # 各阶段教学与二次开发文档
```

## 快速开始

### 1. 创建环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
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
AFTER_SALE_ENABLED=true
AFTER_SALE_CASE_PATH=app/sessions/after_sale_cases.json
AFTER_SALE_AUTO_APPROVE_MAX_AMOUNT=200
AFTER_SALE_HIGH_RISK_AMOUNT=2000
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

```powershell
python main.py
```

CLI 命令：

- `skills`：查看已发现的 Skills；
- `memory`：查看短期和长期记忆；
- `reset`：重置当前对话；
- `quit` / `exit`：保存并退出。

启用 Multi-Agent：

```dotenv
MULTI_AGENT_ENABLED=true
```

### 5. 可选：启动 MCP Server

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

Mock 订单 `ORD-20240125-006` 金额为 29.90 元，低于默认自动审批阈值。

```text
用户：订单 ORD-20240125-006 的保护壳不合适，我想退货。
Agent：查询订单和售后政策，并向用户复述订单与原因。
用户：确认申请。
Agent：创建售后工单，风险路由返回 low/auto_process。
Agent：执行已批准工单并返回工单号和处理结果。
```

### 高金额人工审批

Mock 订单 `ORD-20240110-003` 金额为 5999 元。

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

## 测试与评估

运行本次二次开发的纯业务回归测试，不会调用外部模型：

```powershell
python -m unittest tests.test_agent_deepening -v
```

测试覆盖：

- 未确认不能创建售后工单；
- 低风险工单自动批准并执行；
- 高金额工单进入人工审批；
- 待审工单无法执行；
- 人工批准后可以执行；
- 同一订单的活跃工单幂等；
- 严重风险信号强制转人工；
- Policy 元数据正确进入 Chunk。

运行原有专项测试：

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
# 仅使用确定性规则
python app/scripts/run_eval.py --no-judge

# 包含 LLM-as-Judge
python app/scripts/run_eval.py --mode multi --output app/sessions/eval_report.json
```

## 开发历程

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
| 二次开发阶段 1 | 数据库、业务服务和真实状态机 | 规划中 |
| 二次开发阶段 2 | FastAPI、流式响应和管理后台 | 规划中 |
| 二次开发阶段 3 | 售后 Skill、Policy RAG、HITL、风险路由 | 已完成 |
| 二次开发阶段 4 | 可观测性、安全护栏和自动化回归 | 规划中 |

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

## 当前边界与后续计划

当前二次开发已经完成 Agent 售后流程深化，但仍有以下工程边界：

- 订单、商品、物流和退款执行仍为 Mock；
- 售后工单使用单 JSON 文件持久化，不支持多进程并发写；
- 尚未实现用户鉴权和订单归属校验；
- 人工审批目前是独立 CLI，还没有 RBAC 管理后台；
- 尚未提供 FastAPI、Web 页面和流式交互；
- 风险规则目前写在代码中，尚未版本化为规则配置；
- Policy RAG 尚未接入 reranker 和政策失效检测。

后续二次开发将优先推进：

1. PostgreSQL、Repository/Service 分层和用户数据隔离；
2. FastAPI、SSE/WebSocket 和客服管理后台；
3. Prompt Injection、敏感信息过滤和操作权限 Guardrails；
4. Agent Trace、Token/延迟指标、工具成功率和回归看板；
5. Docker 化部署和完整演示环境。
