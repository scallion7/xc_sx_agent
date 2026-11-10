# 维护者与工程演进

[文档中心](README.md) · [当前验收](evaluation/acceptance.md)

项目维护者与仓库贡献者为 **[scallion7](https://github.com/scallion7)**。文档署名、Git 提交作者和提交者统一使用此账号，不添加工具或其他共同作者署名。

## 贡献者记录

2026-11-10 核对 GitHub Contributors 接口，返回的贡献者只有 `scallion7`。历史提交中的 `scallion` 与 `scallion7` 使用同一邮箱；仓库 [.mailmap](../.mailmap) 将该邮箱下的显示名称统一为 `scallion7`，保留原始提交及其哈希。GitHub 页面归属与 Git 作者显示是不同的数据来源，修改 Markdown 不会直接改写 GitHub 的贡献统计。

仓库使用的框架、协议与依赖按其实际技术名称说明；技术来源引用不作为本项目的共同作者名单。项目开发中使用的自动化工具也不单独列为贡献者。

## 从原型到可恢复服务

初始提交 [`f63c810`](https://github.com/scallion7/xc_sx_agent/commit/f63c810) 包含电商客服原型与阶段学习记录。项目随后围绕业务状态、可信确认、可靠执行、人工协作和可复现评测持续扩展。以下按工程问题说明演进，具体行为以当前代码、测试及验收证据为准。

| 工程范围 | 实现内容 | 代码与提交依据 |
|---|---|---|
| 服务分层与运行时 | 应用装配、实例状态隔离、存储和工具依赖解耦 | [`2ca43f5`](https://github.com/scallion7/xc_sx_agent/commit/2ca43f5)、[bootstrap.py](../app/bootstrap.py) |
| 计划与执行治理 | 依赖调度、检查点、租约、取消、工具权限与预算 | [策略实现](../app/agent/strategies/)、[阶段记录](history/README.md) |
| 可信操作确认 | 参数与身份绑定、有效期、拒绝及失效后的新提案 | [`da17ba4`](https://github.com/scallion7/xc_sx_agent/commit/da17ba4)、[actions.py](../app/services/actions.py) |
| 工单唯一性与幂等 | 并发创建约束，工单和请求回执同事务提交 | [`287216c`](https://github.com/scallion7/xc_sx_agent/commit/287216c)、[工单创建](../app/agent/after_sale/creation.py) |
| 任务恢复与退款对账 | 持久化执行、fencing、Outbox/Inbox、查单与人工核对 | [`5a68204`](https://github.com/scallion7/xc_sx_agent/commit/5a68204)、[`2653a8f`](https://github.com/scallion7/xc_sx_agent/commit/2653a8f) |
| 知识与模型治理 | 不可变快照、CAS 发布、共享额度、调用账本、配置版本和 MCP 契约 | [`8b4d456`](https://github.com/scallion7/xc_sx_agent/commit/8b4d456)、[platform](../app/platform/) |
| 评测与人工复核 | 原文定位、逐阶段检索诊断、CAS 审核、任务级数据库断言 | [`b2df06d`](https://github.com/scallion7/xc_sx_agent/commit/b2df06d)、[任务基线](evaluation/task-baseline.md) |
| 多轮售后与失败回流 | 来源追踪、申请修订、旧确认失效、版本数据、双审配置和预编译索引 | [`7565683`](https://github.com/scallion7/xc_sx_agent/commit/7565683)、[`2526710`](https://github.com/scallion7/xc_sx_agent/commit/2526710)、[实现说明](engineering/improvement-loop.md) |
| 身份、检索与恢复边界 | OIDC issuer/sub、WAL 快照、凭证结案、截断前冲突检查、多轮上下文隔离与可靠重试 | [`7222467`](https://github.com/scallion7/xc_sx_agent/commit/722246758b87315feff0876be803e2e1ee9a08fa) |
| 客服工作台并发控制 | 接管与完整刷新期间锁定操作，避免回复落入旧会话或状态被覆盖 | [`c5aee67`](https://github.com/scallion7/xc_sx_agent/commit/c5aee6716723ae5a4243497db919e094d3b238a5)、[人工接管](engineering/human-handoff.md) |

## 项目介绍口径

> 电商售后 Agent 应用，将查询与政策证据、可信操作确认、持久化任务、退款对账和人工接管整合到统一流程；通过真实 HTTP/MCP、浏览器回归、SQL 并发约束和恢复演练验证执行边界。

测试、负载和检索结果须标注对应提交及数据范围，统一引用[验收状态](evaluation/acceptance.md)。受控模型与合成数据的结果可以证明已覆盖的工程契约，不能替代真实模型、人工金标准或生产业务验收。

## 文档与署名维护

- 新提交沿用 `scallion7` 的现有 Git 身份，不添加 `Co-authored-by` 尾注。
- 当前用法写入主题指南；阶段设计与交付记录进入 `history/`，原始证据保留生成时的内容。
- 修改文档后核对代码、命令参数与证据，并执行文档链接验证；提交作者和 Contributors 列表分别核对。
- 发布结果链接到具体提交和 CI 运行，避免将旧截图、旧测试数或待办直接写成当前状态。
