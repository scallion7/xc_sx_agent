# 当前验收状态

[文档中心](../README.md) · [证据索引](../evidence/README.md)

核验日期：2026-11-10。已归档的功能修复 CI 基线为 [`c5aee67`](https://github.com/scallion7/xc_sx_agent/commit/c5aee6716723ae5a4243497db919e094d3b238a5)，对应 [Actions 42813941711](https://github.com/scallion7/xc_sx_agent/actions/runs/42813941711) **9/9 作业成功**；结构化快照见 [ci.json](../evidence/boundary-fixes-20261110/ci.json)。固定提交的 CI、文档修订本地验收和历史性能测量分别列出，不合并不同环境或代码版本的数字。后续提交的状态查看 [Actions 工作流](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml)，并按提交 SHA 核对。

## 功能修复基线的 CI

| 范围 | 验证结果与口径 |
|---|---|
| Python 3.10 / 3.11 / 3.12 | 每个版本发现 **468 项测试**，实际执行 **459 项通过、9 项浏览器测试跳过**；三个版本均成功 |
| 浏览器与 HTTP 专项 | 独立 `platform-browser` 作业设置 `RUN_BROWSER_E2E=1`，**18 项通过**，包含 9 项浏览器与 9 项 HTTP 测试；其中部分 HTTP 用例也在 Python 矩阵中，不重复累加为独立测试总数 |
| app 语句覆盖率 | Python 矩阵为 **83%**；不含 `app/scripts/*` 及独立进程，配置中的门禁下限仍为 65% |
| SQLite / PostgreSQL | 两个迁移作业通过空库升级、schema drift、业务/平台 smoke、降级与重新升级；使用独立测试数据 |
| 供应链与镜像 | 依赖范围和锁文件审计、SBOM、Docker 构建与 Trivy 门禁通过；镜像检查按工作流配置处理 HIGH/CRITICAL 及可修复问题，不表示软件绝无漏洞 |
| 完整部署与恢复 | `compose-acceptance` 通过隔离 Compose 部署及恢复验收，与本机原生 PostgreSQL 检查分别计证据 |
| PostgreSQL 短时负载 | CI 配置为 15 秒、10 次请求/秒、2 worker、队列上限 12，检查负载与故障契约；同时验证 1000 个合成 chunk 的预编译索引结果一致性 |
| 静态与工程门禁 | Ruff、格式、compileall、OpenAPI、文档链接、40 项计划质量门禁与计划基准通过 |

`platform-acceptance`、`compose-deployment-evidence`、`sustained-postgres-evidence` 和 `dependency-sbom` 产物从对应运行页面获取，受 Actions 保存期限约束。工作流通过不等于旧本地报告中的性能数字已在当前 CI 重现。

当前代码已覆盖 OIDC 以 issuer/sub 绑定身份、SQLite WAL 一致备份、返回 K 之外的同键政策冲突、未知副作用的人工核对结案、多轮政策条件来源与订单隔离、失效提案重新确认，以及页面遇到 409/未知回执的恢复处理。`c5aee67` 另补客服工作台操作至刷新完成期间的全局 busy 保护与浏览器回归。

## 文档修订的本地复核

2026-11-10 在以 `c5aee67` 为基线的未提交文档工作树中再次运行完整回归：设置 `RUN_BROWSER_E2E=1` 并使用已安装的 Chromium，**468 项通过、无跳过，95.859 秒**。另检查 6 个离线技能 helper、119 个环境配置键、文档 CLI 模块、103 个 OpenAPI 路径、364 个 Python 文件的 Ruff 检查及 `demo_stack --intake --smoke`。具体命令、检查结果和最后文档链接统计见[文档修订检查清单](../evidence/docs-refresh-20261110/checks.json)。这批本地复核没有重新测量覆盖率；上方 83% 来自 `c5aee67` 的 CI。

## 提交前的本地验收

[本地检查清单](../evidence/boundary-fixes-20261110/checks.json)记录基于 `2526710` 的未提交源码副本，保留当时的代码哈希、环境与未运行项目。以下 **467 项**早于新增工作台 busy 回归，不是当前提交的测试总数。

- **467 项回归通过，无跳过；103.184 秒**，使用 Python 3.11 与项目锁定的 Playwright Chromium；[完整日志](../evidence/boundary-fixes-20261110/unittest.log)。较修复前增加 39 项回归，另包含故障、并发和不同输入的子场景。
- **app 语句覆盖率 83%**；[覆盖率报告](../evidence/boundary-fixes-20261110/coverage.log)。脚本与独立进程沿用既有排除口径。
- SQLite/PostgreSQL 升级、schema drift、业务与平台烟测、降级重建通过；新增人工结案烟测在两种数据库验证四名管理员并发只有一次结案，并清理自身测试数据。PostgreSQL 停写备份恢复的数据指纹一致。
- 隔离 PostgreSQL 的 15 秒短时验证完成 150/150 个受控任务；容量拒绝、worker 退出、数据库停启和模型 HTTP 中断恢复通过。1000/10000 个合成 chunk 的索引结果一致。18 项受控任务、开发集检索比较、平台容量和故障脚本完成，无付费模型调用。
- Ruff、格式、compileall、OpenAPI、文档链接、40 项计划门禁及计划基准通过。两份依赖清单分别审计 58/53 个包，未发现已知漏洞。
- 该次本机没有 Docker 和 Python 3.10/3.12，因此原报告保留未运行容器构建/镜像扫描/Compose 及另外两个 Python 版本的记录。上方当前 CI 已独立覆盖这些项目，不改写旧 `not_run` 字段。

OIDC 旧账号需按[配置与迁移说明](../guides/configuration.md)核验主体后显式绑定；无凭证的历史未知写入继续阻断，结案契约见[故障手册](../operations/incident-runbook.md)。本次没有变更数据库 schema、迁移业务库或连接真实模型与支付服务。

## 上一批本地验收

以下结果于 2026-11-09 在基于 `eccd6eb` 的工作树上生成；[原检查清单](../evidence/improvement-loop/checks.json)及证据保持原样，不表示本次修复重新取得同一长期负载测量。

| 范围 | 结果 | 证据与口径 |
|---|---|---|
| 完整回归 | **428 项通过，无跳过；85.492 秒** | [日志](../evidence/improvement-loop/unittest.log)，Python 3.11，含真实 HTTP/MCP、Chromium 和新增 CLI/UI 协议 |
| app 语句覆盖率 | **82%** | [覆盖率](../evidence/improvement-loop/coverage.log)，不含脚本和独立进程 |
| 持续负载 | 180 秒、3 worker、3600 次请求；接受的 **2891/2891 完成**，709 次 429 | [摘要](../evidence/improvement-loop/postgres-load.json)，PostgreSQL 17.11、受控 HTTP 模型、四种任务，限流单独计数 |
| 负载延迟与资源 | 端到端 p95 2160 ms，排队 p95 1883 ms；排空 2.14 秒；观测积压最高 24、数据库连接最高 10 | 同一报告；应用进程合计采样 RSS 峰值约 1351 MiB，不含 PostgreSQL；本机测量不能当生产容量 |
| 故障恢复 | 队列饱和、领取后 worker 退出、PG 停启、模型 HTTP 中断通过 | 同一报告 drills；worker 接管约 1.46 秒、PG 恢复约 1.73 秒，不是生产 RTO |
| 索引规模 | 1000/10000 个合成 chunk 输出一致；万级 BM25 p50 **510.59 → 17.85 ms** | [报告](../evidence/improvement-loop/index-scale.json)，预编译热缓存；冷读 381 ms、编译 998 ms，非 ANN/语义质量评测 |
| 多轮售后 | 补充资料、改诉求、旧提案失效、重启、新申请撤回、确认执行幂等通过 | 完整回归中的 dialogue_intake / improvement_boundaries；包含字段来源、过期 owner、接管释放 ABA 与计划草稿 |
| 版本评测闭环 | 任务收集去重 → 脱敏整理 → 冻结 → 导出 → 开发集比较通过 | [CLI 证据](../evidence/improvement-loop/dataset-loop.json)，1 条合成演示题、4 组比较，批准数仍为 0 |
| 双数据库 | 升级、无 schema drift、业务/知识/数据版本 smoke、降级重建通过 | [迁移摘要](../evidence/improvement-loop/migrations.json)；非空数据集降级保护另有回归 |
| 计划与静态检查 | 40 项确定性计划门禁、计划基准、Ruff、compileall、OpenAPI 通过 | [证据目录](../evidence/improvement-loop/README.md)；OpenAPI 101 个路径 |

完整回归在不含本地旧数据库、私有 .env 的临时源码副本中运行，Python 与前端内容哈希在生成时与当时的工作树一致；原默认演示数据库保留。PostgreSQL 使用随机口令、回环端口和临时目录，验收后停止清理，无业务库迁移。

## 历史结果仍可追溯

旧实现 `7565683` 的 [Actions 42054871319](https://github.com/scallion7/xc_sx_agent/actions/runs/42054871319) 曾通过 8/8 作业，包含 Python 3.10–3.12、双数据库、供应链、浏览器与 Compose。该结果不代表当前改动的 CI 状态。

18 项任务基线、三条复杂售后 HTTP 工作流和 64 组检索配置比较保留在[历史证据](../evidence/README.md)。本次没有重写旧日志、冻结数据或原第 1–34 期说明，也未重新宣称检索质量提升。

## 尚需外部条件的验收

| 工作 | 当前状态 | 必要条件 |
|---|---|---|
| 人工金标准 | 原 18 份政策、32 题仍未完成真实审核；新增版本工作流已实现 | 独立人员使用[复核工作台](../guides/review-workbench.md)，新版本通过 `requires_second_review` 标记需要双人复核的题目 |
| 原冻结保留集 | 16 题 holdout 未运行 | 完成全量真实审核，遵守[离线评测协议](offline-review.md) |
| 真实 embedding / 模型效果与费用 | 未验证；付费模型调用为 0 | 单独授权服务、预算、数据与评测方案 |
| 生产身份与供应商 | 未连接真实订单、支付或组织身份 | 凭据、幂等/查单契约与实际联调 |
| 生产 SLA 与灾备 | 本机持续负载通过；跨机、高可用、PITR、跨区域恢复未验收 | 目标环境、业务流量模型与恢复目标 |

证据支持检查是保守原文规则，不能称为语义蕴含验证；本地哈希向量、词法重排和合成标签不能证明真实语义效果。任务 completed 不等于业务答案正确率，发布报告的完整性门禁也不代替对回答内容的独立核对。对话草稿与索引缓存有明确事务和版本边界，整套旧业务仍不宣称共享库 SaaS 或真实支付 exactly-once。

复现入口：[多轮办理与索引](../engineering/improvement-loop.md) · [版本化评测](versioned-datasets.md) · [持续负载](../operations/sustained-load.md) · [配置与测试](../guides/configuration.md)。
