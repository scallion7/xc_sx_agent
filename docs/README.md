# 文档中心

Ecom Service Agent｜电商售后智能体平台。本文按读者要完成的任务组织阅读入口；所有命令默认从仓库根目录执行。项目介绍见[首页](../README.md)，结果与限制统一见[当前验收状态](evaluation/acceptance.md)。

当前指南已按 2026-11-10 代码基线修订，涵盖身份绑定、政策上下文、人工结案、申请重试和客服操作并发控制。维护者与仓库贡献者为 [scallion7](https://github.com/scallion7)；署名与工程演进见[贡献说明](CONTRIBUTIONS.md)。

## 从这里开始

| 你的目的 | 阅读顺序 |
|---|---|
| 首次运行 | [配置与安装](guides/configuration.md) → [五分钟演示](guides/five-minute-demo.md) |
| 展示工程能力 | [五分钟演示](guides/five-minute-demo.md) → [工程演进](CONTRIBUTIONS.md) → [验收证据](evaluation/acceptance.md) |
| 接手代码开发 | [系统架构](architecture/overview.md) → [可信执行](engineering/reliable-execution.md) → 下方对应主题 |
| 诊断检索效果 | [离线评测协议](evaluation/offline-review.md) → [复核工作台](guides/review-workbench.md) → [检索实验](evaluation/retrieval-experiments.md) |
| 部署与故障处理 | [部署清单](operations/deployment-checklist.md) → [隔离部署验收](operations/compose-acceptance.md) → [故障手册](operations/incident-runbook.md) |

## 使用指南 · guides

| 文档 | 回答的问题 |
|---|---|
| [配置与运行](guides/configuration.md) | 如何安装、测试、配置 OIDC 身份映射，并区分隔离演示与外部服务？ |
| [五分钟演示](guides/five-minute-demo.md) | 如何依次展示查询、政策证据、确认执行与客服接管？ |
| [售后申请](guides/after-sale.md) | 如何补资料、关联证据、修改或撤回申请，如何接入申请 API？ |
| [人工复核工作台](guides/review-workbench.md) | 如何核对原文、保存审核、处理冲突和导入导出？ |

## 架构与工程 · architecture / engineering / decisions

| 文档 | 回答的问题 |
|---|---|
| [系统架构](architecture/overview.md) | API、Agent、SQL、Worker 与外部业务如何协作？ |
| [可信执行与业务一致性](engineering/reliable-execution.md) | 确认、幂等、租约、退款对账和任务人工结案分别保证什么？ |
| [人工接管与执行边界](engineering/human-handoff.md) | 接管与调用竞争如何处理，为什么释放后不能恢复旧授权？ |
| [知识、模型与发布治理](engineering/knowledge-and-governance.md) | 如何判断政策适用性与冲突，并管理知识版本、预算、灰度和 MCP 契约？ |
| [对话办理、证据约束与索引扩展](engineering/improvement-loop.md) | 如何追溯多轮草稿、校验结论并复用预编译索引？ |
| [模块命名与兼容约定](engineering/naming-and-compatibility.md) | 旧导入、配置和索引如何继续使用？ |
| [ADR：知识快照与共享治理](decisions/0002-knowledge-snapshots-and-governance.md) | 为什么选择不可变快照与 SQL 治理？ |

## 评测与验收 · evaluation

| 文档 | 回答的问题 |
|---|---|
| [当前验收状态](evaluation/acceptance.md) | 哪个提交通过哪些检查，哪些结论尚无证据？ |
| [离线评测协议](evaluation/offline-review.md) | 数据如何冻结，人工复核、holdout 和发布门禁有什么要求？ |
| [检索诊断与配置对比](evaluation/retrieval-experiments.md) | 如何定位召回/过滤损失并比较两个配置？ |
| [版本化评测与失败回流](evaluation/versioned-datasets.md) | 如何脱敏收集任务、冻结新数据并完成双人复核？ |
| [任务级离线基线](evaluation/task-baseline.md) | 如何用真实 HTTP、调用账本与数据库终态验证任务？ |

## 运维 · operations

- [部署与维护清单](operations/deployment-checklist.md)：配置、迁移、备份与发布检查。
- [完整部署与恢复验收](operations/compose-acceptance.md)：可丢弃 Compose 环境的复现步骤和限制。
- [PostgreSQL 持续负载验收](operations/sustained-load.md)：临时实例、准入控制、多 worker 和故障恢复。
- [告警与恢复手册](operations/incident-runbook.md)：队列、租约、死信、退款未知与模型预算的处理。

## 证据、素材与历史

- [证据索引](evidence/README.md)：原始日志、JSON 报告、轨迹与产物归属；先看摘要，再读原始文件。
- [维护者与工程演进](CONTRIBUTIONS.md)：唯一贡献者、历史别名归并、代码与提交依据。
- [历史归档](history/README.md)：阶段 1–34、近期交付快照与历次验收台账；历史说明不充当当前操作指南。
- [旧路径迁移对照](history/path-migrations.md)：根目录旧跳转页与两份综合说明的去向。
- [界面素材](assets/)：当前指南使用的截图；旧录像和截图仍保留在 `evidence/platform/`。

## 随代码维护的说明

| 入口 | 内容与使用方式 |
|---|---|
| [配置样例](../.env.example) | 自行配置应用的变量说明；演示启动器会显式设置自己的测试配置 |
| [迁移说明](../app/infrastructure/migrations/README) | 数据库版本检查、升级入口和已发布迁移的兼容约定 |
| [业务技能](../app/agent/skills/definitions/) | 模型可加载的退货、商品推荐和物流处理指令；属于运行时输入，修改后须验证加载与工具契约 |
| [演示知识语料](../app/agent/rag/knowledge/) | 合成的业务政策数据，用于本地检索；不是平台操作文档或真实商家承诺 |
| [CI 定义](../.github/workflows/offline-quality.yml) | 各版本测试、专门浏览器测试、迁移、镜像检查、持续负载和恢复验证的实际命令 |

## 维护约定

1. 当前用法按主题维护，不再用“第几期”“本轮/上一轮”命名新指南；交付过程与阶段总结进入 `history/`。
2. 测试数、CI 状态和待完成项以 `evaluation/acceptance.md` 为唯一汇总入口。专题页只保留解释该机制所需的数据；首页引用汇总，不堆积历史结果。
3. 原始 `evidence/` 文件保留生成时的提交、环境与哈希，不改写旧测试数、不覆盖轨迹；新增证据另建目录，并更新证据索引。
4. 更改路径时更新仓库内链接和迁移对照。移除的旧路径不再提供跳转；外部引用需要更新，旧 Git 提交仍可查阅。
5. 每份当前指南链接回本导航和验收状态；检查 `python -m app.scripts.validate_docs` 与 `git diff --check`。命令、配置、API 和权限还需核对对应实现；链接通过不等于结论已验证。业务技能和知识语料分别验证，不能通过改写数据让旧测评结果看起来更新。
6. 展示名使用 Ecom Service Agent；不因文档整理改动 API、配置键、迁移编号、数据格式或历史原文。付费模型与真实人工审核须另有实际授权和证据。
