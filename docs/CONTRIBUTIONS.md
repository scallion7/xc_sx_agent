# 项目背景与个人工程贡献说明

[文档中心](README.md) · [当前验收](evaluation/acceptance.md)

此页帮助说明项目演进与可展示的工程扩展，不把已有教学原型全部列为个人从零原创。仓库事实以提交和代码为依据；个人实际职责、独立完成程度和上游来源由项目维护者如实补充。

## 已有基础与新增范围

初始导入提交 [`f63c810`](https://github.com/scallion7/xc_sx_agent/commit/f63c810) 已包含电商客服原型与教学记录。Prompt、对话、ReAct、MCP、RAG、Memory、Skills 和评测的基础能力，应作为项目既有基础说明。初始导入之前的作者、外部来源和授权信息在当前材料中不足以确定，标记为**待维护者核验**，不推测归属或补造许可证。

项目扩展使用 AI 辅助实现、测试和文档整理。Git 提交作者字段不能证明全部代码由个人独立手写；简历应突出实际能解释、验证和维护的设计与功能。个人职责若与下表不一致，应按真实参与范围调整。

## 可核验的工程扩展

| 扩展 | 重点解释的工程问题 | 提交与代码依据 |
|---|---|---|
| 运行时与服务分层 | 隔离实例状态、统一装配、降低工具与存储耦合 | [`2ca43f5`](https://github.com/scallion7/xc_sx_agent/commit/2ca43f5)、[bootstrap.py](../app/bootstrap.py) |
| 计划与执行治理 | 依赖调度、检查点、租约、取消与权限边界 | [计划执行模块](../app/agent/strategies/)、[阶段演进](history/README.md) |
| 可信操作确认 | 模型参数不能伪造授权，参数变更使旧确认失效 | [`da17ba4`](https://github.com/scallion7/xc_sx_agent/commit/da17ba4)、[actions.py](../app/services/actions.py) |
| 工单并发与幂等 | 唯一约束、创建回执与工单同事务提交 | [`287216c`](https://github.com/scallion7/xc_sx_agent/commit/287216c)、[工单创建](../app/agent/after_sale/creation.py) |
| 异步恢复与退款对账 | 处理 Worker 退出、远端响应丢失和重复回调 | [`5a68204`](https://github.com/scallion7/xc_sx_agent/commit/5a68204)、[`2653a8f`](https://github.com/scallion7/xc_sx_agent/commit/2653a8f) |
| 知识与模型治理 | 知识快照、共享预算、评测发布、人工接管与 MCP 契约 | [`8b4d456`](https://github.com/scallion7/xc_sx_agent/commit/8b4d456)、[platform](../app/platform/) |
| 质量与供应链修复 | 复现 CI 失败、审计真实部署锁文件、更新漏洞依赖 | [`7d720b6`](https://github.com/scallion7/xc_sx_agent/commit/7d720b6)、[修复记录](history/CI-第27-34期上传修复.md) |
| 检索诊断与人工复核 | 冻结标签、原文定位、CAS 审核、逐阶段检索诊断与任务级断言 | [`b2df06d`](https://github.com/scallion7/xc_sx_agent/commit/b2df06d)、[复核工作台](guides/review-workbench.md)、[任务基线](evaluation/task-baseline.md) |
| 接管与复杂售后 | 所有权版本检查、提案失效、申请修订幂等与未知退款恢复 | [`7565683`](https://github.com/scallion7/xc_sx_agent/commit/7565683)、[人工接管](engineering/human-handoff.md)、[售后申请](guides/after-sale.md) |

## 简历表述与证据

以下是候选表述，使用前确认与你实际承担的职责一致：

> 基于电商客服 Agent 原型开展工程扩展，实现可信操作确认、并发工单幂等、持久化任务与退款对账；建设知识版本管理、检索诊断和人工复核工作台，并通过 HTTP/MCP、浏览器回归及故障恢复验证执行边界。

可引用的当前结果统一见[验收状态](evaluation/acceptance.md)，不在各轮贡献说明中重复维护测试数量。可以解释受控任务中接管后调用减少与重复副作用控制；不能宣称真实模型准确率、检索效果提升、生产吞吐或真实支付落地。

## 面试时准备解释

1. 用户确认、业务幂等和 API 请求幂等分别约束什么？
2. Worker 过期后的本地提交如何被 fencing 阻止，哪些远端效果无法撤销？
3. Outbox 为什么仍需要查单、Inbox 与人工对账？
4. 知识快照、活动版本切换与删除屏蔽如何共同作用？
5. 人工接管与调用登记竞争时，以哪个事务边界判断在途请求？
6. 用户修改申请后，为什么旧授权必须失效？
7. 如何区别候选召回、证据过滤、真实模型效果和工程契约测试？

[五分钟演示](guides/five-minute-demo.md)提供业务展示路径；[命名与兼容约定](engineering/naming-and-compatibility.md)说明维护性改动。上游来源、本人实际参与及自动化辅助范围仍须按事实说明。
