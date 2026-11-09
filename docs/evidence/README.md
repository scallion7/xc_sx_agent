# 验收证据索引

本目录保存原始结果，不是使用指南。当前结论见[验收状态](../evaluation/acceptance.md)，复现方法见[文档中心](../README.md)。目录中的 `round` 和日期是产物批次标识，保留以便追溯，不代表当前功能版本。

## 当前实现对应的本地证据

| 目录 | 建议先读 | 包含内容 |
|---|---|---|
| [retrieval-handoff-round2](retrieval-handoff-round2/) | [checks.json](retrieval-handoff-round2/checks.json) | 408 项回归日志、覆盖率、18 项任务摘要与压缩轨迹、64 组检索配置比较、接管回归 |
| [complex-after-sale-round3](complex-after-sale-round3/) | [未知退款恢复](complex-after-sale-round3/unknown-refund-recovery.json) | 补证审批、变更撤回、退款未知恢复的真实 HTTP 轨迹，浏览器与 SQLite 迁移日志 |

这两批证据在实现提交 `7565683` 前生成，`checks.json` 中未提交/未运行 CI 等字段保留当时事实。推送后的 [Actions 42054871319](https://github.com/scallion7/xc_sx_agent/actions/runs/42054871319) 已完成 8/8 作业；CI artifact 在该运行页面查看，不能把前一批部署报告冒充本次产物。

## 历史证据

| 目录 | 内容与用途 |
|---|---|
| [review-round1](review-round1/) | 385 项回归、人工复核页面、检索诊断及优化前的 2010 tokens 任务基线 |
| [reliability](reliability/) | 370 项回归、可靠性修复、容量/故障演练、冻结评测、当时的 CI 与部署恢复报告 |
| [naming-round2](naming-round2/) | 354 项回归、模块命名与导入/配置/索引兼容审计 |
| [docs-round1](docs-round1/) | 首次文档与展示整理、演示 smoke、HTTP 超时复现及修复回归 |
| [ci-20261025](ci-20261025/) | 345 项回归、锁文件与依赖范围审计、产物哈希 |
| [platform](platform/) | 阶段 27–34 的早期验收、540 次受控任务、容量演练、原始界面截图与录像 |

各批次准确环境、提交和结果解释见[历史验收台账](../history/2026-11-05-acceptance-ledger.md)。早期 platform 的 holdout 曾参与诊断，不是当前冻结 review 数据的独立保留集。

## 如何阅读与使用

- JSON 摘要通常链接 `.raw.json.gz` 原始轨迹和 SHA256；先核对代码树、输入、配置与数据集哈希，再解释差异。
- `.log` 保存命令输出。负向测试可能故意打印失败样例，应结合该测试断言和整体退出状态判断结果。
- 覆盖率不含脚本及独立进程；模型 usage 来自受控服务；容量与恢复时间属于对应环境的小规模测量。
- 历史结果不自动适用于当前代码，工程契约通过不等于真实模型或生产资金已验收。
- 不提交令牌、私有业务数据或真实数据库备份；已有报告与压缩轨迹不在文档整理时重写或重压缩。
