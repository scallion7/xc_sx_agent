# 项目背景与个人工程贡献说明

此页帮助说明项目演进与可展示的工程扩展，不把已有教学原型全部列为个人从零原创。仓库事实以提交和代码为依据；个人实际职责、独立完成程度和上游来源由项目维护者如实补充。

## 已有基础与新增范围

初始导入提交 [`f63c810`](https://github.com/scallion7/xc_sx_agent/commit/f63c810) 已包含电商客服原型与教学记录。Prompt、对话、ReAct、MCP、RAG、Memory、Skills 和评测的基础能力，应作为项目既有基础说明。初始导入之前的作者、外部来源和授权信息在当前材料中不足以确定，标记为**待维护者核验**，不推测归属或补造许可证。

本轮协作使用 AI 辅助实现、测试和文档整理。Git 提交作者字段不能证明全部代码由个人独立手写；简历应突出实际能解释、验证和维护的设计与功能。个人职责若与下表不一致，应按真实参与范围调整。

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

## 简历表述示例

以下是项目工作内容的候选表述，使用前确认与你的实际职责一致：

> 基于电商客服 Agent 原型开展工程扩展，将可信操作确认、并发工单幂等、持久化任务与退款对账接入业务流程；建设版本化知识检索、模型预算和人工协作工作台，并通过 HTTP/MCP、浏览器回归及故障演练验证运行边界。

可补充的验证结果：Python 3.11 本地 345 项测试通过，app 语句覆盖率 81%；对应提交的 Python 多版本、PostgreSQL/SQLite、浏览器及供应链共 7 个 CI 作业通过。每个数字应链接[具体验收基线](evaluation/acceptance.md)。

不将“540 次受控模型任务”写成真实模型准确率，不将并发短批次写成生产吞吐，不将沙箱退款写成真实支付接入，也不将全部阶段功能归为独立从零开发。

## 面试时准备解释

1. 用户确认、业务幂等和 API 请求幂等为什么是不同约束？
2. Worker 租约过期后，哪些本地提交能被 fencing 阻止，哪些远端效果不能撤销？
3. Outbox 为什么仍需要查单、Inbox 与人工对账？
4. 知识发布为什么先构建快照再切换 head，删除屏蔽为何要覆盖旧快照？
5. 人工接管和写操作竞争时如何避免双方同时执行？
6. CI 为什么既审计宽松依赖，也审计部署锁文件？Chroma 停用影响哪些功能？

演示路径见[五分钟指南](guides/five-minute-demo.md)。能够解释失败路径、验证证据和仍未验证的部分，是这份作品的重点。

## 命名维护与兼容演进

统一检索后端、人工接管和 MCP 调用器的职责命名，保留旧导入、配置值和历史测试入口；验证旧 JSON 索引可读、迁移文件不变及业务回归。具体映射见[命名与兼容约定](engineering/naming-and-compatibility.md)，测试结果见[验收状态](evaluation/acceptance.md)。此项属于维护性改进，不作为新增业务能力或效果提升描述。
