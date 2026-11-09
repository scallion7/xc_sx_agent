# 人工接管与执行边界

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

用户操作演示见[五分钟演示](../guides/five-minute-demo.md)；本页解释 Worker、调用登记与最终回复如何遵守会话所有权。

## 版本检查与任务交接

任务提交时保存会话所有权版本。Worker 创建 Agent 前检查所有权，每次模型/工具调用登记 `call.started` 时在 SQL 事务中再次检查并锁定该版本；最终回复提交也检查版本。人工接管再释放会增加版本，旧任务不能因为当前状态重新变成 agent 而继续旧诉求。

已接管或版本已变更的任务不创建 Agent，不产生新的模型账本记录；保留用户输入和 `run.handoff` 事件。为兼容已有任务 API，任务终态仍为 `completed`，结果 `reply=""`、`requires_human=true`、`evidence_state="awaiting_human"`，内部记录 `requires_new_request=true`。这表示已完成交接，不表示原业务诉求已经执行。人工释放后必须由用户提交新请求，不自动恢复旧授权；新请求会读取保存的会话上下文。

调用登记完成后与外部网络之间存在不可消除的窗口，因此已经登记的请求按在途处理，仍可能执行并产生费用；随后禁止新调用。已返回但不能发送的回复仍保存在内部草稿，保持原审计兼容。未知的写副作用仍进入 `needs_reconciliation`，不能当作普通接管完成。

相同 18 项冻结离线任务全部通过；三种策略在“先接管、后提交”场景的模拟调用数由 **3 / 3 / 4 降为 0 / 0 / 0**。整个矩阵模拟 usage 由 **2010 降为 1710 tokens**，差值 300；这是受控服务用量，不是实际账单。[当前任务报告](../evidence/retrieval-handoff-round2/task-baseline.json)与[优化前报告](../evidence/review-round1/task-baseline.json)使用相同任务文件。

## 实现与回归

- [所有权服务](../../app/platform/conversation_handoff.py)、[运行上下文](../../app/services/run_context.py)、[任务仓储](../../app/repositories/runs.py)。
- [执行竞争回归](../../tests/test_handoff_execution.py)、[HTTP 接管回归](../../tests/test_handoff_http.py)。
- [离线任务基线](../evaluation/task-baseline.md)保存跨策略的调用与数据库断言。
