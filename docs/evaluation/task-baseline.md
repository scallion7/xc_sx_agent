# 任务级离线基线

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

验证 Agent 执行契约与业务终态，使用受控 HTTP 模型；不测量真实模型理解能力。检索指标另见[检索实验](retrieval-experiments.md)。

## 运行与断言

```bash
python -m app.scripts.evaluate_tasks --output test-results/task-baseline.json
```

任务定义及哈希见 [tasks](../../app/evaluation/tasks/)。每项任务、每种策略都启动独立 SQLite、API、Worker、受控 HTTP 模型进程。初态为空工单/退款操作，执行真实接口后，独立 grader 对照冻结期望检查终态，不能以回复文本里的“成功”判定通过。策略为 ReAct、Plan-and-Execute、multi 路由，共 18 项。

| 任务 | 关键断言 |
|---|---|
| 政策追问 | 同会话两轮，工具实际执行、引用能回查当前知识版本，工单与退款操作数为 0 |
| 确认与重放 | 确认前没有工单；确认后执行成功；重复执行返回相同业务结果并标明幂等重放；唯一工单属于预期用户/订单 |
| 拒绝提案 | 拒绝后执行被阻止，无工单或退款操作 |
| 文本伪确认 | 消息中的 `user_confirmed=true` 不构成可信确认；未确认 API 执行被阻止，无副作用 |
| 用户隔离 | 另一用户读取任务与事件均返回 404，无业务写入 |
| 人工接管 | 自动回复为空，人工消息重复请求只产生一条，工单和退款操作数为 0 |

每项同时检查实际持久化 `call.started` 中的读写工具名称、允许工具和必要工具覆盖。在“先接管、后提交”场景，三种策略当前均为零新增模型调用；调用登记与在途请求的边界见[人工接管](../engineering/human-handoff.md)。伪确认任务使用受控模型固定行为，验证接口闭环，不能代替真实模型提示注入红队测试。

提交 `7565683` 对应的本地基线 **18/18 通过，模拟 usage 合计 1710 tokens，付费调用 0**；相同任务在前一基线中为 2010 tokens，差异来自接管前置检查。摘要包含每项断言、失败分类、耗时和模拟用量；压缩原始文件包含 HTTP 响应、真实队列事件、模型请求/响应、SQL 调用账本与业务初终态。记录代码树哈希、冻结输入和锁文件哈希、基础提交及离线配置，SHA256 连接摘要和原始证据。耗时包含启动开销，不是线上延迟。

可显式设置 `--max-trials`（上限 18）和 `--token-budget`（上限 100000）；SQL 共享配额在调用前预留。报告缺项、重复、断言失败或预算不足均不能通过，CLI 非零退出并保留报告。未知异常保守计入剩余预算并停止，避免静默漏算。**任务矩阵属于合成开发契约，`release_eligible=false`，不能直接送入发布门禁或作为真实模型成功率。**

## 证据与复杂售后验收

[当前摘要](../evidence/retrieval-handoff-round2/task-baseline.json)关联压缩原始轨迹；[前一基线](../evidence/review-round1/task-baseline.json)保留历史用量。

```bash
COMPLEX_TASK_REPORT_DIR=test-results/complex \
  python -m unittest tests.test_complex_after_sale_http -v
```

使用独立应用与商务沙箱数据库、真实 API/供应商 HTTP，以及生产退款投递/对账 Worker 代码，不依赖模型回复判断成功。

| 场景 | 验证结果 |
|---|---|
| 多轮资料、补证、审批 | 缺订单/原因时不能提案；证据审核变更使旧确认失效；新提案创建待审工单；未审批不能生成执行提案；过期审批版本拒绝；最终 1 工单、0 退款操作、0 供应商退款 |
| 诉求修改与撤回 | 修改使已确认旧提案失效，新提案须重新确认；撤回阻止执行；最终工单/退款操作/供应商退款均为 0 |
| 退款结果未知与恢复 | 提交后断连且丢失回调，查询显示 unknown；重放复用原操作；供应商重启后查单对账成功；最终 1 工单、1 操作、1 供应商退款 |

原始 HTTP 轨迹与终态：[补证审批](../evidence/complex-after-sale-round3/multiturn-evidence-approval.json)、[变更撤回](../evidence/complex-after-sale-round3/changed-request-withdrawal.json)、[未知结果恢复](../evidence/complex-after-sale-round3/unknown-refund-recovery.json)。三条业务工作流不是额外三种模型能力评分，不与 18 项受控 Agent 矩阵混为一个成功率。

售后申请的操作和接口契约见[售后申请指南](../guides/after-sale.md)。
