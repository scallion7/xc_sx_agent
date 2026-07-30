# 第 16 期：Plan-and-Execute 与强类型工具契约

本阶段在保留 ReAct 默认行为的前提下，增加适合复杂多诉求请求的
Plan-and-Execute 执行策略，并把模型生成的工具参数纳入统一校验。

## 1. 策略选择

支持三种配置：

- `react`：始终使用原有 ReAct 循环；
- `plan_execute`：先生成结构化计划，再执行计划；
- `auto`：普通请求使用 ReAct，包含“同时、顺便、然后”等多诉求信号时使用计划。

可通过环境变量 `AGENT_STRATEGY`、CLI 参数 `--strategy`，或聊天 API 的
`strategy` 字段选择。Multi-Agent 编排仍保持原有路由和子 Agent ReAct 行为。

## 2. 确定性计划执行

规划器只负责生成 `ExecutionPlan`。本地代码负责：

1. 校验步骤数量、工具白名单和参数；
2. 拒绝未知步骤依赖、自依赖和循环依赖；
3. 对步骤执行稳定拓扑排序；
4. 自动执行只读工具；
5. 阻止退款、创建工单和执行工单等有副作用工具，要求用户明确确认；
6. 依赖步骤失败时阻止下游步骤；
7. 通过 EventBus 记录计划和步骤事件。

因此，即使规划结果不合理，也不能绕过工具权限和业务状态机。
用户下一轮回复“确认”时会确定性切回 ReAct，结合完整会话历史继续执行，
避免把规划模型自己生成的确认标记当作用户授权。

评估沙箱会记录实际选择的策略和终态计划步骤；数据集可通过
`expected_strategy` 对 `react / plan_execute` 选择结果进行离线断言。

## 3. 工具参数契约

每个本地工具都有对应的 Pydantic 参数模型。ToolRegistry 在调用处理器前统一：

- 检查必填字段；
- 拒绝未知字段；
- 检查字符串长度、枚举和数值范围；
- 返回 `INVALID_ARGUMENT` 结构化错误；
- 隔离处理器异常并返回 `TOOL_EXECUTION_FAILED`。

模型可见 Schema 与参数契约有离线一致性测试，避免后续修改产生漂移。

## 4. 离线验证

新增测试使用固定计划、Fake Gateway 和本地 Mock 工具，不访问外部模型：

```bash
python -m unittest tests.test_strategy_development -v
```

GitHub Actions 会执行 Ruff 和完整离线测试集，不包含真实模型质量评估。
