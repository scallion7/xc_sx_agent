# PostgreSQL 持续负载与恢复验收

[文档中心](../README.md) · [配置与运行](../guides/configuration.md) · [当前验收](../evaluation/acceptance.md)

此脚本启动独立 API、多个 Agent Worker、受控 HTTP 模型和本机临时 PostgreSQL。任务使用合成订单与政策，不连接业务库、不调用付费模型。目标是验证排队、幂等、重试与恢复协议；受控模型延迟不能替代真实模型、跨机网络或生产 SLA。

## 运行方式与参数

先按[配置指南](../guides/configuration.md)安装依赖，再准备 PostgreSQL 服务端工具 `initdb`、`postgres`、`pg_ctl`。不用注册系统服务，也不传入已有数据库 DSN：

```bash
python -m app.scripts.benchmark_sustained --pg-bin /path/to/postgresql/bin --seconds 180 --rate 20 --workers 3 --max-pending 24 --output test-results/postgres-load-01.json
```

| 参数 | 默认值与约束 |
|---|---|
| `--pg-bin` | 可省略：先找 PATH 中的 initdb，再尝试 macOS Homebrew PostgreSQL 17 路径；明确传入最可复现 |
| `--seconds` | 到达持续时间默认 60 秒，至少 5 秒；不含初始化、排空和故障演练 |
| `--rate` | 每秒计划到达请求默认 15，必须大于 0 |
| `--workers` | 默认 3，至少 2 |
| `--max-pending` | 默认 24，至少 4；本脚本把全局与单用户上限都设为该值 |
| `--output` | 必填 JSON 路径，自动创建父目录；使用新文件名保留前次证据 |

临时集群使用私有目录、随机口令和动态回环端口，迁移、种子及故障注入仅作用于自建实例。正常退出或被捕获的异常会执行进程/目录清理；SIGKILL 或宿主中断不保证完成清理。需要手动恢复时，先核对本次临时目录及 postmaster PID，不触碰其他 PostgreSQL 服务。不要把整套测试环境与生产 `.env`、身份或数据库配置混用。

CI 的短时协议检查使用：

```bash
python -m app.scripts.benchmark_sustained --pg-bin "$(pg_config --bindir)" --seconds 15 --rate 10 --workers 2 --max-pending 12 --output test-results/postgres-load.json
python -m app.scripts.benchmark_indexes --sizes 1000 --repeats 3 --output test-results/index-scale.json
```

第二条验证检索编译索引一致性与规模变化，和队列负载是不同结果。完整作业见[工作流](../../.github/workflows/offline-quality.yml)；短时 CI 通过不能代替长时间稳态容量测量。

## 请求与测量口径

请求按设定速率独立调度，不等待上一任务完成才发下一个；报告调度迟延，用于判断发生器是否落后。两个用户以 3:1 到达，轮换短咨询、政策检索、订单查询和申请草稿；每个请求使用独立会话与幂等键。两个用户可能查询不属于自己的合成订单，归属拒绝可作为预期业务响应。

`accepted` 表示已被队列接受，`completed` 表示运行管线完成，不表示答案正确或业务请求获批。完成率以接受量为分母；429 单列为 rejected，不能在报告中隐藏。原始请求、每用户完成量和全部拒绝码保留供检查。

报告还包含源码身份、系统与 PostgreSQL 版本、offered、排队 p95、端到端 p95、停止到达后的排空耗时、队列样本、数据库连接数以及进程 RSS/CPU。延迟来自客户端；RSS 为 `ps` 采样的 KiB，轮询本身也消耗资源，采样可能漏过短峰值。

通过条件同时要求：接受量等于完成量且大于 0；原始记录数等于接受加拒绝；所有拒绝均为 `RUN_QUEUE_FULL`；两个用户都有完成任务；观测 pending 不超过 cap；最终 pending 为 0。故障演练中的断言也必须通过。采样峰值不构成任意瞬间上限的证明；并发容量约束还依赖 SQL guard 的原子性回归。

## Admission、公平性与会话状态

- `RUN_MAX_PENDING` 限制全库 queued/running/retry_wait 总量，`RUN_MAX_PENDING_PER_USER` 限制同类状态的单用户数量。默认 0 表示不限制；所有 API 实例须使用一致值。
- SQL guard 串行化“查重、计数、插入”，避免多进程同时占用最后一个名额。满载返回 HTTP 429 和 `Retry-After: 2`；已接受的同键同内容重试仍返回原 run，不能生成新键当作恢复。
- 独立会话按用户候选及最近服务时间排序，减少单个用户长期占用；这是尽力公平调度，不保证严格份额或租户 SLA。本脚本没有单独证明小于全局 cap 的单用户配额效果。
- dead_letter 与 needs_reconciliation 不占 pending 配额，但仍阻塞原会话。安全死信可按规则重放；未知写调用须核对可信账本后人工结案。`resolved` 释放原会话，不代表重新执行并完成旧任务。

HTTP 429 与前端资料修订 409 也不同：`INTAKE_VERSION_CONFLICT` 可刷新后重新编辑，网络/5xx 的未知提交须保持原请求内容。不要在通用压测客户端中把所有错误都变成换键重发。

## 故障演练

| 故障 | 注入方式 | 核验 |
|---|---|---|
| 队列饱和 | 暂停 Worker，接受到 cap | 429、Retry-After、满载取回原任务、恢复后排空 |
| Worker 崩溃 | 领取租约后进程退出 | 另一 Worker 接管、attempt 增加、任务完成 |
| PostgreSQL 中断 | 停止并重启自建实例 | API 暂时 5xx；同键重试只有一条任务；Worker 恢复消费 |
| 模型中断 | 停止受控 HTTP 模型，再恢复 | 观测 retry_wait、有限重试后完成 |

业务退款超时后查单、重复回调和未知结果由[HTTP 沙箱与完整部署验收](compose-acceptance.md)补充。本负载不执行真实支付，不能据此声明外部业务 exactly-once。演练耗时含本机启动成本，不是生产 RTO。

## 比较与保留证据

先核对报告的源码身份、参数、机器和模型条件，再比较相同口径。源码、模型延迟或 Worker 数变化时，不能把吞吐差异直接归因为某一项优化。性能数值作为测量结果保留，不设未经业务需求论证的生产吞吐目标。

最新提交的 CI 状态见[当前验收](../evaluation/acceptance.md)。[历史三分钟报告](../evidence/improvement-loop/postgres-load.json)、[原始记录压缩包](../evidence/improvement-loop/postgres-load.raw.json.gz)和[日志](../evidence/improvement-loop/postgres-load.log)保持当时源码与参数，不重标为本次文档修订的实测。

实现入口：[负载与演练](../../app/scripts/benchmark_sustained.py)、[临时 PostgreSQL](../../app/scripts/temporary_postgres.py)、[任务仓储](../../app/repositories/runs.py)。需要处理阻塞会话时按[恢复手册](incident-runbook.md)，不要用清空历史数据改善负载结果。
