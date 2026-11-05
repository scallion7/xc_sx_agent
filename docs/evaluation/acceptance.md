# 验收状态与证据

更新日期：2026-11-05。此页是当前状态汇总；历史报告保留其原提交、样本和测量值。文档、界面名称整理不把历史报告重新标成新业务实验。

## 当前检索诊断、复核工作台与任务基线

基于 `d2b43fb` 扩展，2026-11-05 本地 Python 3.11、锁定依赖及 Chromium：**385 项测试通过，无跳过，64.501 秒，app 语句覆盖率 82%**。覆盖率沿用历史口径，不含脚本及独立服务子进程；因此任务基线另行保存真实 HTTP/Worker 证据。本轮最终脚本与页面整理后又完成针对性回归。

| 检查 | 结果与证据 |
|---|---|
| 全量回归与覆盖率 | [385 项测试](../evidence/review-round1/unittest.log)、[覆盖率](../evidence/review-round1/coverage.log) |
| 复核浏览器闭环 | 批准、原文证据、并发冲突、导出/导入草稿、身份切换与 holdout 拦截；[独立复测](../evidence/review-round1/browser.log) |
| 新增 SQL 迁移 | `20261105_0007` 新增三张复核表；SQLite upgrade/drift/smoke/空复核表 downgrade/re-upgrade 通过；有审核数据时拒绝降级；[日志](../evidence/review-round1/sqlite-migration.log) |
| 开发集诊断 | 16 题，15 题四模式最终结果完全一致，另 1 题仅冲突证据顺序不同；[汇总与原始轨迹](../evidence/review-round1/retrieval-diagnostics.json) |
| 任务级基线 | 6 类 × ReAct / Plan-and-Execute / multi = 18/18 通过；模拟 usage 共 2010 tokens；[摘要](../evidence/review-round1/task-baseline.json)、[原始证据](../evidence/review-round1/task-baseline.raw.json.gz) |
| 接口与静态检查 | OpenAPI 90 个路径、Ruff、编译与文档链接检查；保留全部既有接口，旧迁移和原冻结材料未修改 |

CI 已增加复核浏览器、带诊断的离线评测、任务基线及 SQLite/PostgreSQL 复核 SQL smoke；该轮提交的结果通过[当前分支 Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml)核验，不能用下文旧提交通过记录代替。

本轮没有替用户批准任何真实复核材料，没有运行新 holdout，没有调用付费模型。浏览器截图中已批准的题目来自可丢弃测试数据库。工作台功能、离线契约通过与人工金标准完成是三种不同状态；详见[范围与操作说明](review-and-task-baseline.md)。

## 前一轮可靠性改进

本地 Python 3.11、锁定依赖、Chromium：**370 项测试通过，无跳过，58.273 秒，app 语句覆盖率 81%**。新增回归覆盖部分评测拒绝发布、容量错误触发失败、数据库凭据不进入 argv、浏览器重试幂等与告警状态转换。证据见 [测试日志](../evidence/reliability/unittest.log)和[覆盖率](../evidence/reliability/coverage.log)。

- [容量门禁](../evidence/reliability/capacity.json)：1/10/50 并发共 61 请求成功；零错误、Worker 正常退出、恢复指纹匹配。P95 只作本机测量，未设生产延迟 SLA。
- [故障演练](../evidence/reliability/drills.json)：强制退出后的租约接管与 firing/resolved 告警闭环，SQLite 锁释放后的原键重试收敛。
- [离线复核材料](offline-review.md)：新 32 题冻结，16 题 development 完成四模式比较，16 题 holdout 未运行，人工复核未完成，付费模型调用为零。
- [部署验收](../operations/compose-acceptance.md)：新增双 Worker、服务重启、旧库升级、真实 HTTP/MCP 和三个独立数据库恢复的 CI 作业；本机没有 Docker；已在 GitHub Actions 独立环境完成真实验收，详见下表。

### 前一轮 CI 与部署证据

实现提交 [`157159d`](https://github.com/scallion7/xc_sx_agent/commit/157159db24b1487bf403351d80f7fe37f3185967) 的 [Actions 运行 41482524125](https://github.com/scallion7/xc_sx_agent/actions/runs/41482524125) **8/8 作业通过**：[作业清单](../evidence/reliability/ci.json)。覆盖 Python 3.10–3.12、SQLite/PostgreSQL 迁移、真实浏览器、容量/故障演练、依赖审计与镜像扫描，以及新增完整 Compose 验收。

[部署原始报告](../evidence/reliability/deployment.json)来自该作业的 artifact，模型为受控 HTTP，付费调用 0：

| 检查 | 实测结果 |
|---|---|
| 干净项目与旧库升级 | PostgreSQL 17；从 0005 升级到 head，旧表与 action 提案哈希保持一致 |
| 双 Worker 并发幂等 | 20 次提交、10 个稳定键、10 个唯一任务，每个 attempt=1 |
| 外部业务 | 确认后退款提交断连、回调丢失，经查询对账成功，供应商退款数=1；MCP 库存执行成功 |
| 服务重启 | 旧任务幂等命中，新任务完成，供应商退款数仍为 1 |
| PostgreSQL 备份恢复 | 49 表记录数/哈希匹配，恢复数据库可启动应用并读取原任务；备份约 0.383 秒、恢复约 0.466 秒 |
| 独立沙箱恢复 | 商务与库存分别通过 SQLite backup API 包含 WAL 读取源卷；源卷、备份及恢复库指纹匹配 |

以上为停写、合成业务、小数据量验收；恢复时间不是生产 RTO，不能替代持续写入的 PITR、跨区域和真实支付供应商验收。测试数据库不纳入仓库，只归档报告和 Actions artifact；历史实验保持原样。

## 已完成的功能与依赖验收基线

提交 [`7d720b6`](https://github.com/scallion7/xc_sx_agent/commit/7d720b6cba0a86663524aeae3a5934dd7d9a3b4c) 的 [Actions 运行 40175352228](https://github.com/scallion7/xc_sx_agent/actions/runs/40175352228) 已完成，**7/7 作业通过**。

| 项目 | 已验证范围 | 证据 |
|---|---|---|
| 本地完整回归 | 345 项通过，53.113 秒，无跳过，Python 3.11 | [unittest.log](../evidence/ci-20261025/unittest.log) |
| 语句覆盖率 | app 范围 81%，不含脚本，也未合并独立服务子进程 | [coverage.log](../evidence/ci-20261025/coverage.log) |
| Python 兼容性 | 3.10 / 3.11 / 3.12 的 CI 作业通过 | 上述 Actions 运行 |
| SQL 迁移与契约 | SQLite/PostgreSQL 空库升级、drift、业务/平台 smoke、降级重建 | 上述 migrations 两个作业 |
| 真实 HTTP/MCP/浏览器 | 独立服务、确认、SSE、人工接管及数据库断言 | [浏览器测试](../../tests/test_platform_http.py)、platform-browser 作业 |
| 依赖安全 | 58 个锁定依赖与 53 个运行依赖解析结果审计无已知漏洞 | [锁文件审计](../evidence/ci-20261025/pip-audit-lock.json)、[范围审计](../evidence/ci-20261025/pip-audit-ranges.json) |
| 镜像交付 | SBOM、Docker build、Trivy 与报告门禁通过 | supply-chain 作业 |

安全审计表示在该日期与扫描配置下未发现相应问题，不承诺未来无漏洞。Chroma 因上游未修复公告暂停，处理经过见 [CI 修复归档](../history/CI-第27-34期上传修复.md)。

## 保留的早期实验与演练

| 证据 | 结果与解释 |
|---|---|
| [第 27–34 期本地验收](../evidence/platform/validation.md) | 342 项测试是修复前的历史结果，不改成 345 |
| [受控模型评测](../evidence/platform/evaluation.json) | 60 条合成两轮任务 × 3 策略 × 3 次，共 540 次任务；非真实模型能力评分 |
| [原始逐题记录](../evidence/platform/evaluation.raw.json.gz) | 保存模型响应、工具轨迹、usage 和数据库断言；模拟 usage 不是付费账单 |
| [容量与恢复](../evidence/platform/capacity-and-restore.json) | SQLite、单 Worker、受控模型的 1/10/50 并发；61 请求成功；50 并发约 17.67 次/秒、P95 2620ms；非持续压测或生产 SLA |
| [故障演练](../evidence/platform/failure-drills.json) | Worker 被终止后的租约接管、SQLite 短暂阻断后的恢复 |
| [计划治理场景](../evidence/platform/plan-quality.log) | 40 条固定确定性场景 |

检索数据包含模板变体，现有 holdout 曾用于诊断，不是未触碰的最终测试集。真实语义验收需另建独立人工复核集。重复运行间存在相关性，报告中的统计区间不用于泛化结论。

## 仍未完成的验收

- 生产持续写入下的 PostgreSQL PITR、跨区域和真实供应商一致恢复点演练。
- 真实模型效果、人工金标准复核、语义 embedding 质量及真实费用验证。
- 真实组织身份系统、订单/支付供应商与生产资金联调。
- 持续高负载、饱和点、跨机数据库和生产 SLA；新增平台表作用域也不证明整个系统已完成 SaaS 多租户改造。

## 文档与展示整理的检查

第一轮整理当前说明、历史归档、页面/CLI/API 展示名称及 CI 产物名称；本地 30 项相关回归通过（11.580 秒，含 Chromium），README 的 smoke 命令与迁移/drift/smoke 命令已实际执行，81 路由 OpenAPI 校验、Ruff 和编译检查通过。[本轮检查记录](../evidence/docs-round1/checks.json)与[测试日志](../evidence/docs-round1/targeted-tests.log)单独保存。对应提交继续执行相同质量门禁；通过 [当前分支 Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml) 查看该提交结果，不沿用旧提交的成功状态。

当前展示截图见 [客户服务](../assets/workspace.png) 与 [客服工作台](../assets/workbench.png)；历史截图与录像继续保留在 `evidence/platform/`，不覆盖原始哈希证据。

### 本轮 CI 时序问题与补充检查

文档提交 `6fb6658` 的 [运行 40178221025](https://github.com/scallion7/xc_sx_agent/actions/runs/40178221025) 有 6 个作业通过，Python 3.12 在既有退款 HTTP 测试失败：正常请求共用 150ms 超时，返回 `unknown` 而非预期成功。使用 300ms 提交后延迟[复现了原失败](../evidence/docs-round1/timeout-reproduction.log)。正常路径改用 5 秒测试超时；故障用例单独设置 0.5 秒超时与 2 秒响应延迟，成功、单次退款和对账断言保持不变。补充的 [13 项 HTTP 回归全部通过](../evidence/docs-round1/http-regression.log)，21.259 秒。本次仅调整测试时序，不改变生产超时配置；后续修复提交 `4ce5160` 的 [Actions 运行 40178664536](https://github.com/scallion7/xc_sx_agent/actions/runs/40178664536) 已完成，7/7 作业通过。

## 第二轮代码与模块命名整理

以 `4ce5160` 为基线，将余弦后端、客服接管、MCP 调用器和测试套件改为职责命名，保留兼容入口。新增 9 项兼容回归，完整本地测试 **354 项通过、无跳过、53.262 秒、覆盖率 81%**，Python 3.11，启用 Chromium；旧测试模块在发现模式中不重复计数。

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| 全量业务与兼容回归 | 354 项通过，包含真实 HTTP/MCP、浏览器和旧接口兼容 | [测试日志](../evidence/naming-round2/unittest.log) |
| app 语句覆盖率 | 81%，不包含脚本或独立服务子进程；与历史口径一致 | [覆盖率报告](../evidence/naming-round2/coverage.log) |
| API 与历史契约 | 81 个路径；除 API 总说明外 OpenAPI 完全一致，已发布迁移和历史记录字节不变 | [兼容审计](../evidence/naming-round2/compatibility-audit.json) |
| 计划评测映射 | 40 条固定场景通过，已引用新的职责模块 | [计划门禁](../evidence/naming-round2/plan-quality.log) |
| 计划性能门禁 | 现有阈值检查通过；不作为真实模型或生产性能结果 | [计划基准](../evidence/naming-round2/plan-benchmark.log) |
| SQLite 迁移与平台 smoke | 升级至 `20261025_0006`、无 schema drift、知识/预算/所有权检查通过 | [迁移日志](../evidence/naming-round2/migration.log) |

Ruff、格式、编译、文档链接及旧模块显式运行检查均已通过。命名兼容策略和保留的历史标识见[说明](../engineering/naming-and-compatibility.md)。本轮远端 Python 多版本、PostgreSQL、浏览器、镜像与安全扫描结果，按本次提交查看 [GitHub Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml)，不以旧 CI 结果代替。

### 本轮首轮 CI 诊断

[运行 41479809711](https://github.com/scallion7/xc_sx_agent/actions/runs/41479809711) 的浏览器/容量/故障演练和两种数据库迁移成功；新增部署作业因 Ubuntu 默认 apt 源缺少 PostgreSQL 17 客户端停止，尝试 PGDG 源后遇到上游下载连接中断，最终改为同一 PostgreSQL 17 镜像内的真实客户端适配器。锁定依赖审计发现 pypdf 6.21.2 的 7 条漏洞公告，按扫描给出的修复版本升级到 6.21.4，并提高运行依赖下限；未添加忽略漏洞规则。最终结果以修复提交的 CI 为准。

部署调试还发现并修复了两个验收缺口：旧库夹具需要显式关闭自动建表，以免绕过迁移后产生重复表；库存 Compose 服务需要真实 MCP 握手健康检查，不能沿用 API 的 `/ready` 或仅禁用检查。最终完整部署作业已覆盖这些路径。
