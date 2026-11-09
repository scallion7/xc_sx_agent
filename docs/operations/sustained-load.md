# PostgreSQL 持续负载与恢复验收

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

该验收使用独立 API、多个 worker、受控 HTTP 模型与本机临时 PostgreSQL；订单和政策为合成数据，不接业务库、不调用付费模型。它验证排队、重试与恢复机制，不能替代跨机网络、真实模型延迟或生产 SLA。

## 运行方式

安装 PostgreSQL 服务端工具（需要 `initdb`、`postgres`、`pg_ctl`），不必注册系统服务；将其 bin 目录传入：

```bash
python -m app.scripts.benchmark_sustained --pg-bin /path/to/postgresql/bin --seconds 180 --rate 20 --workers 3 --max-pending 24 --output test-results/postgres-load.json
```

脚本创建权限受限的临时目录、随机口令和回环监听端口；建库、迁移、种子初始化只作用于自建实例。成功或异常退出都会停止子进程并清理目录，命令不接受业务库 DSN。不要对该进程使用无法触发清理的强制终止；若操作系统直接中止进程，只核对输出关联的临时目录和 postmaster PID 后清理，不触碰其他 PostgreSQL 服务。

## 负载与测量

按照设定速率独立安排请求，而非等待上一个任务完成才发下一个；报告调度迟延，以识别负载发生器落后。两种身份按 3:1 到达，任务包含短咨询、政策检索、订单查询、申请草稿；任务类型与身份轮换，使用独立会话和幂等键。完成率以接受的任务为分母，429 单独计数，不隐藏在成功率中。completed 表示运行管线完成，不能当作业务答案正确率；不同身份查询他人演示订单时可能得到预期的归属拒绝。

报告包括 offered/accepted/completed/rejected、排队 p95、端到端 p95、停止到达后的排空耗时、每个身份的完成数量、队列深度、数据库连接数、应用进程 RSS/CPU 样本和原始请求记录。轮询与采样也消耗本机资源。延迟是客户端观测值；RSS 为 `ps` 采样的 KiB，采样可能漏过短峰值。

验收门槛：所有接受任务完成、无未知 HTTP 错误、拒绝必须是 `RUN_QUEUE_FULL`、每种身份均完成任务、观测队列不超过 cap、最终积压为 0。性能数值为测量结果，不设置一个未经业务需求论证的生产吞吐目标。

## Admission 与公平性

- `RUN_MAX_PENDING` 限制全库 queued/running/retry_wait 总数，`RUN_MAX_PENDING_PER_USER` 限制单个 user；0 保留旧版不限制行为。所有 API 实例须使用一致配置。
- SQL guard 行把“查重、计数、插入”串行化，避免多进程同时超卖队列容量。满载返回 HTTP 429 和 `Retry-After: 2`；已经接受的同键同内容请求仍能取回原 run。
- 独立会话间按用户候选和最近服务时间排序，减少大用户队列垄断；这是尽力公平调度，不保证严格配额或租户 SLA。
- 失败、dead_letter、待对账任务不占 pending 配额，但其既有会话阻塞、审核和重放规则仍有效。人为重放属于运维操作，需先确认队列与业务状态。

## 故障演练

| 故障 | 注入方式 | 核验 |
|---|---|---|
| 队列饱和 | 暂停 worker，接受到 cap | 429、Retry-After、满载重放原任务、恢复排空 |
| worker 崩溃 | 领取租约后进程直接退出 | 另一 worker 接管，attempt 增加，任务完成 |
| PostgreSQL 中断 | 停止并重启自建实例 | API 暂时 5xx，同键重试仅一条任务，worker 存活 |
| 模型服务中断 | 停止受控 HTTP 模型，再恢复 | retry_wait 被观测到，有限重试完成 |

退款供应商超时后查单、重复回调和未知结果的业务语义由既有真实 HTTP 沙箱回归覆盖；本负载没有执行真实支付，也不据此声明供应商 exactly-once。恢复耗时从注入操作计时，包含本机进程启动，与生产 RTO 无关。

CI 新增短时 `sustained-postgres` 作业作为协议回归；完整三分钟测量留在本地证据中。远端执行状态以对应提交的 [Actions](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml) 为准，本地三分钟报告与 CI 短时回归分别记录。源码：[负载与演练](../../app/scripts/benchmark_sustained.py)、[隔离实例](../../app/scripts/temporary_postgres.py)、[队列仓储](../../app/repositories/runs.py)。
