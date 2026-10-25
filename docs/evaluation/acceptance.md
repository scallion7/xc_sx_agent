# 验收状态与证据

更新日期：2026-10-25。此页是当前状态汇总；历史报告保留其原提交、样本和测量值。文档、界面名称整理不把历史报告重新标成新业务实验。

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

- 完整 Compose 服务集在干净环境中的端到端部署、启动顺序和运行联调。
- PostgreSQL 的 pg_dump/pg_restore 备份恢复与跨服务一致恢复点演练；已通过的 SQL smoke 不能替代它。
- 真实模型效果、人工金标准复核、语义 embedding 质量及真实费用验证。
- 真实组织身份系统、订单/支付供应商与生产资金联调。
- 持续高负载、饱和点、跨机数据库和生产 SLA；新增平台表作用域也不证明整个系统已完成 SaaS 多租户改造。

## 文档与展示整理的检查

第一轮整理当前说明、历史归档、页面/CLI/API 展示名称及 CI 产物名称；本地 30 项相关回归通过（11.580 秒，含 Chromium），README 的 smoke 命令与迁移/drift/smoke 命令已实际执行，81 路由 OpenAPI 校验、Ruff 和编译检查通过。[本轮检查记录](../evidence/docs-round1/checks.json)与[测试日志](../evidence/docs-round1/targeted-tests.log)单独保存。对应提交继续执行相同质量门禁；通过 [当前分支 Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml) 查看该提交结果，不沿用旧提交的成功状态。

当前展示截图见 [客户服务](../assets/workspace.png) 与 [客服工作台](../assets/workbench.png)；历史截图与录像继续保留在 `evidence/platform/`，不覆盖原始哈希证据。
