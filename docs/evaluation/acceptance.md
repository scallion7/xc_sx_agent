# 当前验收状态

[文档中心](../README.md) · [证据索引](../evidence/README.md)

更新：2026-11-10。以下本地结果于 2026-11-09 在基于 `eccd6eb` 的工作树上生成；源码哈希、命令与限制见[本批检查清单](../evidence/improvement-loop/checks.json)。证据中的未提交状态保留生成时事实；提交后的远端 CI 需按具体提交核验，不能沿用旧提交的绿色状态。

## 当前本地验收

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

完整回归在不含本地旧数据库、私有 .env 的临时源码副本中运行，Python 与前端内容哈希和当前工作树一致；原默认演示数据库保留。PostgreSQL 使用随机口令、回环端口和临时目录，验收后停止清理，无业务库迁移。

## 历史结果仍可追溯

旧实现 `7565683` 的 [Actions 42054871319](https://github.com/scallion7/xc_sx_agent/actions/runs/42054871319) 曾通过 8/8 作业，包含 Python 3.10–3.12、双数据库、供应链、浏览器与 Compose。该结果不代表当前改动的 CI 状态。

18 项任务基线、三条复杂售后 HTTP 工作流和 64 组检索配置比较保留在[历史证据](../evidence/README.md)。本次没有重写旧日志、冻结数据或原第 1–34 期说明，也未重新宣称检索质量提升。

## 尚需外部条件的验收

| 工作 | 当前状态 | 必要条件 |
|---|---|---|
| 人工金标准 | 原 18 份政策、32 题仍未完成真实审核；新增版本工作流已实现 | 独立人员使用[复核工作台](../guides/review-workbench.md)，高风险题双人复核 |
| 原冻结保留集 | 16 题 holdout 未运行 | 完成全量真实审核，遵守[离线评测协议](offline-review.md) |
| 真实 embedding / 模型效果与费用 | 未验证；付费模型调用为 0 | 单独授权服务、预算、数据与评测方案 |
| 生产身份与供应商 | 未连接真实订单、支付或组织身份 | 凭据、幂等/查单契约与实际联调 |
| 生产 SLA 与灾备 | 本机持续负载通过；跨机、高可用、PITR、跨区域恢复未验收 | 目标环境、业务流量模型与恢复目标 |
| 当前 GitHub CI | 新增短时 PostgreSQL 负载作业已配置；本地证据不代表远端结论 | 在对应提交的 [Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml) 查看结果 |

证据支持检查是保守原文规则，不能称为语义蕴含验证；本地哈希向量、词法重排和合成标签不能证明真实语义效果。任务 completed 不等于业务答案正确率。对话草稿与索引缓存有明确事务和版本边界，整套旧业务仍不宣称共享库 SaaS 或真实支付 exactly-once。

复现入口：[多轮办理与索引](../engineering/improvement-loop.md) · [版本化评测](versioned-datasets.md) · [持续负载](../operations/sustained-load.md) · [配置与测试](../guides/configuration.md)。
