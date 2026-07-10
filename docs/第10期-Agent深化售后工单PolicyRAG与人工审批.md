# 第10期：Agent 深化——售后工单、Policy RAG、Human-in-the-Loop 与风险路由

## 1. 二次开发目标

原教学版本已经具备 ReAct、Function Calling、RAG、Multi-Agent、Memory、Skills 和评估能力，但退货退款仍主要由 `apply_refund` 单次工具调用完成，缺少确定性的业务状态、风险控制和人工审批边界。

本次二次开发将售后流程改造为以下闭环：

```text
用户提出售后诉求
  → 加载 process-return Skill
  → 查询订单
  → Policy RAG 检索适用政策并返回引用
  → Agent 复述订单号和原因，获取明确确认
  → 创建售后工单
  → 确定性风险路由
      ├─ low：自动批准，可执行
      ├─ medium：进入人工审批
      ├─ high：高优先级人工处理
      └─ critical：强制转人工并标记 requires_human
  → 人工批准/拒绝
  → 仅执行 approved 工单
  → 查询工单进度与审计记录
```

核心原则是：**LLM 负责理解、追问和解释；代码负责状态、风险、审批和执行权限。**

## 2. 主要修改

### 2.1 售后工单领域层

新增 `app/agent/after_sale/`：

| 文件 | 职责 |
|---|---|
| `models.py` | 工单类型、状态、风险等级、审批状态和审计事件 |
| `risk.py` | 消息级与工单级确定性风险评估 |
| `repository.py` | JSON 工单持久化和进行中工单幂等查询 |
| `service.py` | 创建、查询、审批和执行工单的唯一状态转换入口 |

工单状态如下：

```text
                     ┌─ rejected
pending_review ──────┤
                     └─ approved ─→ processing ─→ completed

低风险工单：approved ─→ processing ─→ completed
```

不允许的状态转换会由 `AfterSaleService` 拒绝。例如 `pending_review` 工单调用执行工具会返回 `blocked=true`。

### 2.2 新增 Agent 工具

新增三个可供 Agent 使用的工具：

#### `create_after_sale_case`

创建工单并执行风险路由。关键参数：

- `order_id`：订单号；
- `reason`：已经和用户确认的售后原因；
- `case_type`：可选，默认自动识别；
- `evidence_provided`：争议类售后是否已有证据；
- `user_confirmed`：必须为 `true`，否则拒绝创建。

同一订单已有进行中工单时返回原工单，避免重复提交。

#### `query_after_sale_case`

查询工单状态、风险原因、审批信息、执行结果和审计记录。

#### `execute_after_sale_case`

只允许执行 `approved` 工单。Agent 无法通过提示词绕过这个代码检查。

旧的 `apply_refund` 底层实现为兼容历史课程和 MCP 服务继续保留；启用售后工单后，`ToolManager` 不再将它暴露给模型，Agent 无法用直退工具绕过工单。

### 2.3 售后 Skill 升级

`process-return/SKILL.md` 从一次性退款指引升级为六步流程：

1. 确认订单；
2. 判断订单状态；
3. 检索政策和资格；
4. 获得用户确认并创建工单；
5. 等待或读取人工审批；
6. 执行已批准工单并告知后续进度。

Skill 明确约束：

- 不能跳过用户确认；
- 不能直接调用 `apply_refund`；
- 不能自行声称审批通过；
- Policy RAG 未得到充分证据时必须转人工；
- 用户询问进度时必须查询工单。

### 2.4 Policy RAG

四份知识文档增加了 YAML frontmatter：

```yaml
---
policy_type: after_sale
version: 2026.1
effective_date: 2026-01-01
scope: mainland_china
---
```

Markdown Chunk 会保留这些元数据，Numpy JSON 和 Chroma 后端都会持久化。`search_knowledge` 新增：

- `policy_type` 过滤；
- `grounded`：最高召回分数是否达到配置阈值；
- `requires_human`：证据不足时为 `true`；
- `citation`：可直接用于客服回复的文档/章节引用；
- `citations`：包含政策类型、版本、生效日期和分数的结构化证据列表。

售后检索示例参数：

```json
{
  "query": "签收后七天内退货的运费由谁承担",
  "top_k": 3,
  "policy_type": "after_sale"
}
```

修改知识文档元数据后必须重建索引：

```powershell
python app/scripts/build_kb_index.py --backend numpy
```

使用 Chroma 时执行：

```powershell
python app/scripts/build_kb_index.py --backend chroma
```

### 2.5 Human-in-the-Loop

审批能力没有注册到 Function Calling 工具表。这样模型只能创建和查询工单，不能批准自己的申请。

独立人工入口为 `app/scripts/review_after_sale.py`：

```powershell
# 查看待审批工单
python app/scripts/review_after_sale.py list

# 人工批准
python app/scripts/review_after_sale.py approve AS-XXXXXXXXXX `
  --reviewer customer-service-01 `
  --note "订单与举证材料核验通过"

# 人工拒绝
python app/scripts/review_after_sale.py reject AS-XXXXXXXXXX `
  --reviewer customer-service-01 `
  --note "超过政策规定的售后时效"
```

每次创建、提交审批、批准、拒绝、开始执行、执行成功或失败都会写入 `audit_log`。

### 2.6 风险路由

风险路由分为两层。

#### 消息级风险

命中以下类型的信号时，不等待 LLM 判断：

- 消协、起诉、律师、报警；
- 人身伤害、触电、爆炸；
- 隐私泄露、盗刷；
- 媒体曝光、监管投诉。

单 Agent 会强制将结构化结果的 `requires_human` 设为 `true`；Multi-Agent 会优先路由到投诉 Agent，并同样强制标记转人工。

#### 工单级风险

风险因素包括：

- 订单金额是否超过自动审批或高风险阈值；
- 质量、缺件、物流破损是否提供证据；
- 售后类型是否可识别；
- 是否已有退款流程；
- 原因中是否包含投诉或严重安全信号。

默认规则：

| 结果 | 路由 | 行为 |
|---|---|---|
| low | `auto_process` | 自动批准，可执行 |
| medium | `manual_review` | 普通人工审批 |
| high | `human_escalation` | 高优先级人工处理 |
| critical | `human_escalation` | 强制转人工 |

风险规则是可解释的，`risk_reasons` 会保存每个命中原因。

## 3. 配置项

`.env.example` 新增：

```dotenv
POLICY_RAG_MIN_SCORE=0.2
AFTER_SALE_ENABLED=true
AFTER_SALE_CASE_PATH=app/sessions/after_sale_cases.json
AFTER_SALE_AUTO_APPROVE_MAX_AMOUNT=200
AFTER_SALE_HIGH_RISK_AMOUNT=2000
```

`app/sessions/` 已被 Git 忽略。售后工单默认保存到：

```text
app/sessions/after_sale_cases.json
```

## 4. 演示流程

### 4.1 低风险自动处理

Mock 订单 `ORD-20240125-006` 金额为 29.90 元，可用于演示低风险流程。

```text
用户：订单 ORD-20240125-006 的保护壳不合适，我想退货。
Agent：查询订单和退货政策，复述订单与原因，请用户确认。
用户：确认申请。
Agent：创建售后工单，风险路由返回 low/auto_process。
Agent：执行已批准工单，返回 completed 和工单号。
```

### 4.2 高金额人工审批

Mock 订单 `ORD-20240110-003` 金额为 5999 元。

```text
用户：订单 ORD-20240110-003 的手机有故障，我要退款，已经提供视频。
Agent：查询订单与政策并要求最终确认。
用户：确认。
Agent：创建工单，返回 pending_review，不执行退款。
人工：通过 review_after_sale.py 审批。
用户：我的售后工单 AS-XXXXXXXXXX 怎么样了？
Agent：查询到 approved，再执行或告知下一步。
```

### 4.3 严重风险强制转人工

```text
用户：商品爆炸导致受伤，我已经联系律师和消协。
```

该消息会被确定性规则标记为 `critical`，Multi-Agent 直接进入投诉处理，最终结构化结果强制 `requires_human=true`。

## 5. 验证

新增的确定性测试不调用模型和外部服务：

```powershell
python -m unittest tests.test_agent_deepening -v
```

覆盖内容：

- 未确认不能创建工单；
- 低风险工单自动批准并执行；
- 高金额工单必须人工审批；
- 待审工单执行被阻止；
- 人工批准后可以执行；
- 活跃工单幂等；
- 严重信号强制转人工；
- Policy 元数据进入 Chunk。

完整 Agent 和 RAG 端到端测试还需要安装 `requirements.txt`、配置 OpenAI 兼容 API，并重建知识库索引。

## 6. 当前边界与后续建议

本阶段重点是 Agent 深化，仍保留以下教学项目边界：

- 工单使用单 JSON 文件持久化，不适合多进程并发写；生产环境应迁移到 PostgreSQL；
- 用户身份仍来自演示环境，尚未实现订单归属鉴权；
- `apply_refund` 仍是模拟执行器，没有接入真实支付系统；
- 人工审批当前是独立 CLI，可进一步做成带 RBAC 的管理后台；
- Policy RAG 使用相似度阈值，生产环境可增加规则过滤、reranker 和政策有效期校验；
- 风险规则目前写在代码中，可迁移为版本化 YAML 或规则引擎。

这些边界不会影响本次演示售后闭环，但在对外部署前必须继续完善。
