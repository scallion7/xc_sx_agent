# 验收证据索引

核验日期：2026-11-13。本目录保存已产生的原始结果；当前结论见[验收状态](../evaluation/acceptance.md)，复现方法见[文档中心](../README.md)。目录名称中的 round、阶段和日期是产物批次标识，保留用于追溯。

## 功能修复基线的远端验收

归档代码基线 [`c5aee67`](https://github.com/scallion7/xc_sx_agent/commit/c5aee6716723ae5a4243497db919e094d3b238a5) 对应 [Actions 42813941711](https://github.com/scallion7/xc_sx_agent/actions/runs/42813941711)，**9/9 作业成功**；结果快照见 [ci.json](boundary-fixes-20261110/ci.json)。Python 3.10/3.11/3.12 各发现 468 项，执行 459 项通过、跳过 9 项浏览器测试；独立浏览器作业的 18 项包含 9 项浏览器和 9 项 HTTP 测试，全部通过。app 语句覆盖率为 83%，不同作业的重复用例不相加为新的独立样本。

| CI 产物 | 内容 |
|---|---|
| `platform-acceptance` | 平台容量/故障报告、开发集离线检索、18 项任务矩阵与检索配置对比 |
| `compose-deployment-evidence` | 当前提交的完整 Compose 部署及恢复验收 |
| `sustained-postgres-evidence` | 15 秒 PostgreSQL 负载与故障契约、1000 chunk 索引一致性 |
| `dependency-sbom` | 锁文件对应的 SBOM；依赖审计与镜像扫描结果另查同作业日志 |

这些产物从对应运行页面获取，受 Actions 保存期限约束。后续文档或代码提交的 CI 在 [Actions 工作流](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml) 按提交 SHA 查看。本目录中的旧报告不会因新 CI 通过而变成新运行的证据。

## 最近的本地批次

| 目录 | 阅读入口 | 生成时的证据身份 |
|---|---|---|
| [release-workflow-20261113](release-workflow-20261113/) | [验收清单](release-workflow-20261113/checks.json)、[27 格原始数据入口](release-workflow-20261113/evaluation.json) | 基于 `75d364a` 的本地工作树，覆盖受控模型的逐题评分与发布、两种数据库、退款对账和浏览器续签 |
| [docs-refresh-20261110](docs-refresh-20261110/) | [checks.json](docs-refresh-20261110/checks.json) | 2026-11-10，基于 `c5aee67` 的未提交文档工作树；468 项回归通过、无跳过、95.859 秒，另核验技能 helper、配置键、CLI 模块、OpenAPI、静态检查、文档链接与售后演示；未重新测量覆盖率 |
| [boundary-fixes-20261110](boundary-fixes-20261110/) | [checks.json](boundary-fixes-20261110/checks.json) | 基于 `2526710` 的未提交源码副本；467 项通过、无跳过、83% 覆盖率，双数据库、原生 PostgreSQL 短时负载、索引与离线契约；早于 `c5aee67` 新增的工作台浏览器回归 |
| [improvement-loop](improvement-loop/) | [checks.json](improvement-loop/checks.json) | 基于 `eccd6eb` 的 2026-11-09 本地批次；428 项通过、82% 覆盖率、180 秒 PostgreSQL 负载、万级索引、双数据库与版本化数据 CLI |
| [retrieval-handoff-round2](retrieval-handoff-round2/) | [checks.json](retrieval-handoff-round2/checks.json) | 提交 `7565683` 前的本地证据；408 项回归、18 项任务矩阵、64 组检索配置比较与接管回归 |
| [complex-after-sale-round3](complex-after-sale-round3/) | [未知退款恢复](complex-after-sale-round3/unknown-refund-recovery.json) | 同阶段三条复杂业务工作流：补证审批、变更撤回、退款未知恢复，含真实 HTTP 轨迹和测试库终态 |

各检查清单中的未提交状态、源码哈希、未运行项目和环境限制均保留生成时事实。例如本地 boundary-fixes 批次未运行 Compose，而最新 CI 已独立完成 Compose；不删除旧 `not_run` 字段，也不把旧日志改成新提交记录。

## 更早证据

| 目录 | 内容与用途 |
|---|---|
| [review-round1](review-round1/) | 385 项回归、人工复核页面、检索诊断与 2010 模拟 tokens 的旧任务基线 |
| [reliability](reliability/) | 370 项回归、可靠性修复、容量/故障演练、冻结 review 开发集与当时的部署恢复报告 |
| [naming-round2](naming-round2/) | 354 项回归、模块命名及导入/配置/索引兼容审计 |
| [docs-round1](docs-round1/) | 首次展示整理、演示 smoke、HTTP 超时复现与修复回归 |
| [ci-20261025](ci-20261025/) | 345 项回归、锁文件与依赖范围审计、产物哈希 |
| [platform](platform/) | 阶段 27–34 的早期验收、540 次受控任务、容量演练、界面截图和录像 |

旧提交 `7565683` 的 [Actions 42054871319](https://github.com/scallion7/xc_sx_agent/actions/runs/42054871319) 为当时的 8/8 作业结果；更早批次说明见[历史验收台账](../history/2026-11-05-acceptance-ledger.md)。早期 platform 的 holdout 曾参与诊断，不能作为 review-v1 的独立保留集。

## 阅读规则

- 先核对代码、输入、配置、环境与数据集哈希，再比较数字；报告中若有 `.raw.json.gz` 和 SHA256，应一并核验。
- 回归数量、任务矩阵 trial 数、业务工作流数与负载请求数使用不同口径，不能合并成一个成功率。
- 负向测试可能故意打印失败样例，应结合整体退出状态和断言判断结果；跳过需单独报告。
- 覆盖率不含脚本及独立进程；模拟 usage 不是实际账单，历史容量和恢复时间不是生产承诺。
- 历史日志、JSON、压缩轨迹、截图与冻结数据在文档修订中保持原样。不提交令牌、私有业务数据或真实数据库备份。
