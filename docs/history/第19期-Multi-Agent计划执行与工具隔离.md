# 第 19 期：Multi-Agent 计划执行与工具隔离

> 历史归档：记录当时的设计、实现与验收范围，不代表当前运行状态。当前入口见 [文档导航](../README.md) 与 [验收状态](../evaluation/acceptance.md)。原始证据保持原版本，不追改历史测量结果。

本阶段把阶段 16–18 已完成的 Plan-and-Execute、跨步骤引用、确认恢复和计划生命周期
接入 Multi-Agent，使复杂请求在完成业务路由后，仍由确定性代码约束工具范围和副作用
执行。

> 阶段状态：已完成；完成日期：2026-08-05；验收不访问真实或本地模型服务。

## 1. 设计目标

Multi-Agent 的路由和计划执行遵循以下顺序：

```text
用户请求
  → 确定性风险判断
  → Router 选择业务子 Agent
  → StrategySelector 选择 ReAct 或 Plan-and-Execute
  → 只向规划器暴露该子 Agent 的工具定义
  → PlanExecutor 再次校验工具白名单
  → 自动执行只读步骤
  → 副作用步骤等待用户确认
```

Router 负责确定业务职责，计划执行器负责确定工具边界。规划模型不能选择另一个子
Agent，也不能通过生成工具名称扩大当前子 Agent 的权限。

## 2. 路由身份持久化

`PlanExecutionResult` 新增 `agent_key`：

- `presale`：售前咨询；
- `postsale`：订单与售后服务；
- `complaint`：投诉和高风险事件。

初次规划时，`agent_key` 与 `plan_id`、步骤结果、风险转人工标记和生命周期状态一起
写入会话。单 Agent 计划保持空 `agent_key`，用于兼容阶段 16–18 的会话数据。

确认轮以持久化的 `agent_key` 为唯一执行身份：

1. 不再调用 Router；
2. 不再请求 Planner 生成新计划；
3. 恢复该子 Agent 绑定的 `PlanExecuteStrategy`；
4. 重新校验原计划中的全部工具；
5. 复用已完成步骤，只执行原来等待确认的步骤。

因此，即使确认轮的模型路由结果可能变化，也不会把待确认操作交给另一个子 Agent。

## 3. 工具隔离

每个子 Agent 已有独立 `ToolManager` 和工具白名单。本阶段为每个子 Agent 再绑定一个
使用同一 `ToolManager` 的 `PlanExecuteStrategy`。

| 子 Agent | 允许的计划工具 |
|---|---|
| 售前 | `query_product`、`search_knowledge`、`list_user_orders`、`load_skill` |
| 售后 | 订单、物流、政策、Skill，以及创建、查询和执行售后工单 |
| 投诉 | 订单、政策、Skill，以及创建和查询售后工单 |

工具隔离有两层：

1. Planner 输入只包含当前子 Agent 的工具 Schema；
2. PlanExecutor 在调用前校验工具是否仍存在于当前 `ToolManager`。

如果售前计划生成 `query_order`，或者投诉计划生成 `execute_after_sale_case`，计划会在
工具调用前失败，并降级到同一个子 Agent 的 ReAct 路径。

## 4. 副作用确认与模式切换保护

Multi-Agent 沿用阶段 17–18 的确认规则：

- 只读步骤可以按依赖层并发执行；
- 创建工单和执行工单等副作用步骤保持串行；
- 用户确认后先把计划持久化为 `running`，再调用工具；
- 重启后的重复确认不会再次执行副作用；
- 过期、超过恢复次数或工具作用域失效的计划不会执行。

Multi-Agent 创建的待确认计划不能切换为单 Agent 后继续。单 Agent 的工具集合更大，
如果允许跨模式恢复，会绕过原子 Agent 的工具白名单，因此系统会把该计划标记为失败并
要求重新发起请求。

## 5. 事件与可观测性

以下事件增加或保留子 Agent 上下文：

- `agent.routed`：记录初次路由或恢复路由，恢复时 `resumed=true`；
- `strategy.selected`：记录策略及 `agent_key`；
- `plan.created`、`plan.resumed`：记录计划所属 `agent_key`；
- `strategy.fallback`：记录发生降级的子 Agent；
- `plan.cancelled`、`plan.resume.failed`：记录计划身份和失败原因。

现有 SSE、WebSocket 和评估订阅器无需修改即可接收这些事件。

## 6. 使用方式

CLI：

```bash
python main.py --mode multi --strategy react
python main.py --mode multi --strategy plan_execute
python main.py --mode multi --strategy auto
```

API：

```json
{
  "message": "查询订单，顺便查询物流",
  "session_id": "demo",
  "mode": "multi",
  "strategy": "plan_execute"
}
```

`auto` 会在检测到“同时、顺便、然后”等复合诉求信号时使用计划模式，普通请求继续走
子 Agent ReAct。

## 7. 代码结构

- `agent/strategies/models.py`：计划快照增加子 Agent 身份和风险转人工标记；
- `agent/strategies/executor.py`：创建与恢复事件携带工具作用域；
- `agent/strategies/plan_execute.py`：规划入口接收路由身份；
- `multi_agent/orchestrator.py`：策略选择、计划执行、确认恢复和降级；
- `bootstrap.py`：把策略参数传入 Multi-Agent；
- `agent/chat.py`：阻止单 Agent 恢复 Multi-Agent 作用域计划；
- `tests/test_strategy_development.py`：阶段 19 纯离线回归。

## 8. 离线验证

阶段 19 测试使用固定 Router 输出、固定计划和本地工具，不访问任何模型服务：

```bash
.venv/bin/python -m unittest \
  tests.test_strategy_development.MultiAgentStrategyTests -v

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

专项测试覆盖正常计划、工具越权、跨会话确认恢复、严重风险延续和跨模式阻断。

阶段验收结果：

- 完整离线测试：56 项通过；
- Plan-and-Execute 专项测试：25 项通过；
- Ruff 静态检查：通过；
- Ruff 格式检查：142 个项目文件通过；
- 真实模型调用：0 次；
- 本地模型调用：0 次。

新增测试覆盖：

- Multi-Agent 路由后在售后子 Agent 工具范围内执行计划；
- 售前子 Agent 的越权计划在工具调用前被拒绝；
- 会话重载后确认轮保持原子 Agent，不重新路由或规划；
- Multi-Agent 计划不能切换到单 Agent 恢复；
- 严重风险计划在确认轮继续保持强制转人工标记。

## 9. 后续路线完成状态

阶段 19 交付时列出的后续路线均已完成：

1. 阶段 20 已增加独立 Plan Repository、计划历史、步骤时间线和管理台审计；
2. 阶段 21 已增加售后副作用持久化幂等键、事务状态、租约恢复和补偿；
3. 阶段 22 已增加步骤/计划超时、实时取消、资源配额、fencing 和固定故障数据集。
