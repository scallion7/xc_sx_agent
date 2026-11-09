# 当前验收状态

[文档中心](../README.md) · [证据索引](../evidence/README.md)

更新：2026-11-06。已验证的实现提交为 [`7565683`](https://github.com/scallion7/xc_sx_agent/commit/7565683d9ebda84dee43b43215e574b7ddeb2969)。本页集中维护当前结论；历史测试数与故障经过移至[历史验收台账](../history/2026-11-05-acceptance-ledger.md)，原始文件见[证据索引](../evidence/README.md)。本次文档整理不重新计作功能回归。

## 已验证

| 范围 | 结果 | 证据与口径 |
|---|---|---|
| 本地完整回归 | 408 项通过，无跳过；77.286 秒 | [日志](../evidence/retrieval-handoff-round2/unittest.log)，Python 3.11，含 Chromium 与 HTTP/MCP |
| app 语句覆盖率 | 82% | [覆盖率](../evidence/retrieval-handoff-round2/coverage.log)，不含脚本和独立进程 |
| GitHub Actions | 8/8 作业通过 | [对应运行](https://github.com/scallion7/xc_sx_agent/actions/runs/42054871319)：Python 3.10–3.12、双数据库迁移、浏览器/平台、供应链、Compose |
| 迁移与恢复 | SQLite/PostgreSQL SQL 契约、升级/降级重建、完整 Compose 隔离部署与恢复通过 | 同一 Actions 的 migrations 和 compose-acceptance；[复现方法](../operations/compose-acceptance.md) |
| 任务级离线基线 | 18/18 通过；模拟用量 2010 → 1710 tokens | [报告](../evidence/retrieval-handoff-round2/task-baseline.json)，受控模型；三种策略接管场景均无新调用 |
| 复杂售后 | 3 条真实 HTTP 工作流通过 | [补证审批](../evidence/complex-after-sale-round3/multiturn-evidence-approval.json)、[变更撤回](../evidence/complex-after-sale-round3/changed-request-withdrawal.json)、[未知退款恢复](../evidence/complex-after-sale-round3/unknown-refund-recovery.json) |
| 检索配置实验 | 16 题 × 4 模式 = 64 组比较；无最终输出改善或退化 | [报告](../evidence/retrieval-handoff-round2/comparison.json)，尚未人工审核的开发集标签 |

GitHub CI 的通过属于指定提交，不自动适用于后续代码。原始 `checks.json` 中的 `local_only`、未推送和未运行 CI 字段记录生成报告时的状态，不是当前仓库状态；不改写历史快照。

## 结论边界

- 本地哈希向量、词法重排与合成标签不能证明真实语义质量；本次检索配置实验未显示质量提升。
- 18 项任务和三条业务工作流分别计数；受控模型任务通过不等于真实模型成功率。模拟 tokens 不是真实账单。
- Compose 验证的是合成业务、小数据量、停写后的备份恢复；不构成生产 RTO、持续写入 PITR 或跨区域恢复验收。
- 供应商为独立 HTTP 沙箱；应用内的 tenant 作用域也不等于整个系统具备共享库 SaaS 隔离。
- 依赖与镜像扫描结论对应当时版本及扫描配置，不承诺未来无漏洞。

## 待完成

| 工作 | 当前状态 | 后续条件 |
|---|---|---|
| 人工金标准 | 18 份合成政策、32 题尚未完成真实复核 | 使用[复核工作台](../guides/review-workbench.md)保留身份、理由与原文证据 |
| 独立保留集 | 当前冻结 review 数据的 16 题 holdout 未运行 | 按[离线评测协议](offline-review.md)完成全量审核；旧 platform holdout 曾用于诊断，不能替代 |
| 真实 embedding / 模型效果与费用 | 尚未验证，继续保持不调用付费服务 | 单独授权服务、费用上限与评测方案 |
| 正式业务与身份接入 | 未连接真实支付、订单供应商或组织身份系统 | 生产凭据、供应商幂等/查单契约与联调 |
| 生产容量与灾备 | 未验证持续高负载、跨机数据库 SLA、PITR、跨区域恢复 | 单独设计负载与灾备验收 |

复现入口：[配置与测试](../guides/configuration.md) · [离线任务](task-baseline.md) · [检索实验](retrieval-experiments.md) · [部署恢复](../operations/compose-acceptance.md)。
