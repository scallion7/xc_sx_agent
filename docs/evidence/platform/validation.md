# 本地验收记录（2026-10-25）

基线：第26期 `2653a8fd36fe2a1760c97bd6ddb6f89dbbd1971a`。本次为未提交的后续开发工作区，评测报告另附源码树 hash，不冒充新的提交版本。

| 检查 | 实测结果 |
|---|---|
| 完整 unittest（含真实 HTTP/MCP、子进程、Chromium） | **342/342 通过**，51.508 秒，无跳过 |
| app 语句覆盖率 | **81%**；未合并独立服务子进程的 coverage |
| ruff check / format / compileall | 通过 |
| OpenAPI | 校验通过；快照见 openapi.json |
| SQLite 空库升级、drift、平台 smoke | 通过 |
| 第26期业务数据升级保留、空库降级/重建、非空账本拒绝降级 | 通过 |
| 原第22期 40 项治理质量场景 | 通过 |
| 配置发布 CLI | 实际完成 draft → evaluated → active，见 configuration-publish.log |
| 检索消融 | 100 条合成数据；holdout 50 条；4 组输出完整 Recall/MRR/引用/无答案/p95 |
| 三策略受控模型运行 | 540 次两轮任务、1080 个用户回合；每策略180次；错误业务动作 0；完整运行标记 True |
| 隔离恢复 | 新数据库逐表行数/内容哈希一致；备份 0.0030s，恢复 0.0026s |
| 原项目环境复核 | 锁定依赖安装、pip check、ruff 与隔离 OpenAPI（81 路由）通过；独立 HTTP/MCP/Worker smoke 完成 |
| Docker / PostgreSQL | 本机无可用运行服务；CI、Compose 和 PostgreSQL 恢复路径已提供，**尚未实跑** |
| 真实模型 / 人工金标准复核 / 真实支付 | **未运行或未进行**；不以受控模型成绩代替 |

容量环境：macOS-27.0-arm64-arm-64bit，arm64，Python 3.11.15，10 CPU，1 Worker，SQLite、本地受控 HTTP 模型、全采样 Trace。

| 并发 | 请求数 | 吞吐/秒 | P50 ms | P95 ms | 失败 |
|---|---:|---:|---:|---:|---:|
| 1 | 1 | 1.34 | 744.6 | 744.6 | 0 |
| 10 | 10 | 22.41 | 313.4 | 445.4 | 0 |
| 50 | 50 | 17.67 | 1723.3 | 2619.8 | 0 |

每档只有一次冷批次，首次初始化开销不相同，未覆盖饱和点、持续负载、跨机数据库或真实模型。因此这些数字仅适用于当前演示环境。

资源采样：独立容量运行每 200ms 读取 API/Worker/模型进程 RSS 与 CPU，共 17 组。观测 RSS 峰值分别约 119.8 / 162.3 / 51.8 MiB；短时峰值可能漏采，原始数据包含在容量 JSON 中。受控模型评测报告的 108000 token 是替身返回的 usage，不是付费服务账单。

故障证据：Worker 在领取租约后退出，恢复后 attempt=2 且完成；SQLite 排他锁期间 API 500，解除后同一幂等请求仅一条记录、Worker 存活。原第26期测试还覆盖超时后提交、丢回调、重复回调及乱序回调。SQL 配额方案不依赖 Redis。

修复了全量回归暴露的旧退款竞态：事务先读不到操作、随后读到已被另一请求改成 processing 的工单，会误报 CASE_NOT_APPROVED。现在重新开始事务读取持久化幂等回执，最终全量回归通过。

演示数据的检索 outcome 与受控 Agent 断言通过不能推出真实语义准确率。Agent 样例为两轮合成读操作流程，检索样例包含模板变体，尚未人工复核。首版诊断曾暴露通用词引起的无关召回，加入停用词/词法覆盖门槛后做了重新评测；现有 holdout 已用于诊断，真实效果验收必须另建独立人工复核集，不能将这份报告称为未触碰的最终测试集。

复现：在安装 requirements-lock.txt 后运行 `RUN_BROWSER_E2E=1 python -m unittest discover -s tests`；浏览器先 `python -m playwright install chromium`。容量、故障和备份脚本见主文档。日志、JSON、压缩逐题记录、客户/客服截图与 WebM 均保存在当前目录。

原目录遗留演示 SQLite 的迁移版本为空，直接使用该旧库会触发 `CASE_SCHEMA_UPGRADE_REQUIRED`。本轮未对其进行就地迁移；本地演示和验收均使用隔离新库。继续使用旧数据前先做一致性备份，再执行 `python -m app.scripts.manage_migrations upgrade` 并核对业务行；纯质量检查可显式设置 `DATABASE_URL=sqlite:///:memory:`，避免依赖遗留运行数据。
