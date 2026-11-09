# 文档中心

Ecom Service Agent｜电商售后智能体平台。本文按读者要完成的任务组织阅读入口；所有命令默认从仓库根目录执行。项目介绍见[首页](../README.md)，结果与限制统一见[当前验收状态](evaluation/acceptance.md)。

## 从这里开始

| 你的目的 | 阅读顺序 |
|---|---|
| 首次运行 | [配置与安装](guides/configuration.md) → [五分钟演示](guides/five-minute-demo.md) |
| 简历与面试展示 | [五分钟演示](guides/five-minute-demo.md) → [工程贡献](CONTRIBUTIONS.md) → [验收证据](evaluation/acceptance.md) |
| 接手代码开发 | [系统架构](architecture/overview.md) → [可信执行](engineering/reliable-execution.md) → 下方对应主题 |
| 诊断检索效果 | [离线评测协议](evaluation/offline-review.md) → [复核工作台](guides/review-workbench.md) → [检索实验](evaluation/retrieval-experiments.md) |
| 部署与故障处理 | [部署清单](operations/deployment-checklist.md) → [隔离部署验收](operations/compose-acceptance.md) → [故障手册](operations/incident-runbook.md) |

## 使用指南 · guides

| 文档 | 回答的问题 |
|---|---|
| [配置与运行](guides/configuration.md) | 如何安装、测试、区分隔离演示与自行配置的服务？ |
| [五分钟演示](guides/five-minute-demo.md) | 如何依次展示查询、政策证据、确认执行与客服接管？ |
| [售后申请](guides/after-sale.md) | 如何补资料、关联证据、修改或撤回申请，如何接入申请 API？ |
| [人工复核工作台](guides/review-workbench.md) | 如何核对原文、保存审核、处理冲突和导入导出？ |

## 架构与工程 · architecture / engineering / decisions

| 文档 | 回答的问题 |
|---|---|
| [系统架构](architecture/overview.md) | API、Agent、SQL、Worker 与外部业务如何协作？ |
| [可信执行与业务一致性](engineering/reliable-execution.md) | 确认、幂等、租约与对账分别保证什么？ |
| [人工接管与执行边界](engineering/human-handoff.md) | 接管与调用竞争如何处理，为什么释放后不能恢复旧授权？ |
| [知识、模型与发布治理](engineering/knowledge-and-governance.md) | 知识版本、预算、灰度发布与 MCP 契约如何管理？ |
| [模块命名与兼容约定](engineering/naming-and-compatibility.md) | 旧导入、配置和索引如何继续使用？ |
| [ADR：知识快照与共享治理](decisions/0002-knowledge-snapshots-and-governance.md) | 为什么选择不可变快照与 SQL 治理？ |

## 评测与验收 · evaluation

| 文档 | 回答的问题 |
|---|---|
| [当前验收状态](evaluation/acceptance.md) | 哪个提交通过哪些检查，哪些结论尚无证据？ |
| [离线评测协议](evaluation/offline-review.md) | 数据如何冻结，人工复核、holdout 和发布门禁有什么要求？ |
| [检索诊断与配置对比](evaluation/retrieval-experiments.md) | 如何定位召回/过滤损失并比较两个配置？ |
| [任务级离线基线](evaluation/task-baseline.md) | 如何用真实 HTTP、调用账本与数据库终态验证任务？ |

## 运维 · operations

- [部署与维护清单](operations/deployment-checklist.md)：配置、迁移、备份与发布检查。
- [完整部署与恢复验收](operations/compose-acceptance.md)：可丢弃 Compose 环境的复现步骤和限制。
- [告警与恢复手册](operations/incident-runbook.md)：队列、租约、死信、退款未知与模型预算的处理。

## 证据、素材与历史

- [证据索引](evidence/README.md)：原始日志、JSON 报告、轨迹与产物归属；先看摘要，再读原始文件。
- [工程贡献说明](CONTRIBUTIONS.md)：来源、实现依据和可用于简历的表述边界。
- [历史归档](history/README.md)：阶段 1–34、近期交付快照与历次验收台账；历史说明不充当当前操作指南。
- [旧路径迁移对照](history/path-migrations.md)：根目录旧跳转页与两份综合说明的去向。
- [界面素材](assets/)：当前指南使用的截图；旧录像和截图仍保留在 `evidence/platform/`。

## 维护约定

1. 当前用法按主题维护，不再用“第几期”“本轮/上一轮”命名新指南；交付过程与阶段总结进入 `history/`。
2. 测试数、CI 状态和待完成项以 `evaluation/acceptance.md` 为唯一汇总入口。专题页只保留解释该机制所需的数据；首页引用汇总，不堆积历史结果。
3. 原始 `evidence/` 文件保留生成时的提交、环境与哈希，不改写旧测试数、不覆盖轨迹；新增证据另建目录，并更新证据索引。
4. 更改路径时更新仓库内链接和迁移对照。移除的旧路径不再提供跳转；外部引用需要更新，旧 Git 提交仍可查阅。
5. 每份当前指南链接回本导航和验收状态；检查 `python -m app.scripts.validate_docs` 与 `git diff --check`。内容还需核对代码及真实证据，链接通过不等于结论已验证。
6. 展示名使用 Ecom Service Agent；不因文档整理改动 API、配置键、迁移编号、数据格式或历史原文。付费模型与真实人工审核须另有实际授权和证据。
