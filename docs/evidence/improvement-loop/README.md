# 多轮办理与评测闭环验收证据

[当前验收](../../evaluation/acceptance.md) · [证据总索引](../README.md)

2026-11-09，本地工作树，基线提交 `eccd6eb`。先看 [checks.json](checks.json)，原始命令与重跑说明见 [commands.json](commands.json)。本次未提交或推送，新增 GitHub Actions 配置未远端运行。

| 产物 | 口径 |
|---|---|
| [完整回归](unittest.log)、[覆盖率](coverage.log) | 428 项、无跳过；Python 3.11，包含真实 HTTP/MCP 和 Chromium；82% app 语句覆盖率，不含独立进程和脚本 |
| [源码清单](tested-source.json)、[隔离说明](isolation.json) | 验收副本与本地 app/tests Python 哈希相同；前端哈希另在 checks.json，旧默认数据库保留 |
| [持续负载摘要](postgres-load.json)、[原始记录](postgres-load.raw.json.gz) | 临时 PostgreSQL 17.11、3 worker、180 秒 20 次/秒；包含排队、资源采样与四类故障演练 |
| [索引规模](index-scale.json) | 1000/10000 个合成 chunk，每档 12 对查询；输出一致，冷/热读、构建和增量分配分开记录 |
| [双数据库迁移](migrations.json)、[日志](migrations.log) | SQLite/PostgreSQL 无 drift；业务、知识、数据版本 smoke 和升降级重建 |
| [版本数据闭环](dataset-loop.json)、[CLI 日志](dataset-loop.log) | 候选去重、人工整理协议、冻结、导出、4 组开发集比较；0 条真实人工批准 |
| [开发集比较](version-comparison.json)、[压缩轨迹](version-comparison.raw.json.gz) | 1 条合成协议演示题，不用于质量提升或发布结论 |
| [计划质量](plan-quality.log)、[计划基准](plan-benchmark.log) | 40 项确定性计划协议，模拟执行，不是真实模型成功率 |
| [OpenAPI](openapi.log)、[lint](lint.log)、[格式](format.log) | 101 个路径、operation ID 唯一；静态检查通过 |
| [申请界面](conversation-intake.png)、[版本复核界面](review-versioned.png) | 独立演示数据库、合成数据，截图已视觉检查 |

完整回归日志尾部的 1/10/50 错误批次来自**故意构造失败的容量门禁单测**，不是本次真实负载报告。真实持续负载结果以 postgres-load.json 为准。commands.json 保留临时启动器与旧默认数据库造成的诊断失败；相应检查已在隔离环境重跑通过，未修改业务数据库来消除失败。

原报告中的 `source.python_files` 和 tested-source.json 标识具体输入；[sha256.json](sha256.json) 校验本目录产物。压缩文件保留完整原始记录，公开摘要仅移出长请求列表与采样数组。人工金标准、原冻结保留集、付费模型、真实支付与生产 SLA 均不包含在验收中。
