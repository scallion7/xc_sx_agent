# 完整部署与恢复验收

此流程仅用于隔离验收，使用受控 HTTP 模型和合成订单，不调用付费模型。需要 Docker Compose v2、Python 3.11、锁定依赖、`pg_dump`/`pg_restore` 17，以及本机空闲端口 8000、9124、9125、55432。

```bash
python -m pip install -r requirements-lock.txt
python -m app.scripts.accept_deployment --output test-results/deployment.json
```

脚本创建随机 `ecom-acceptance-*` 项目，拒绝复用已有容器；所有 Compose 命令固定项目名。结束时仅删除该项目的容器和卷。输出目录应每次使用新目录，已有备份不会被覆盖。它不是对已有部署进行升级或删除的运维命令。

验收流程：

1. 启动 PostgreSQL 17，先迁移至 `20261023_0005`，写入真实 action 提案，再升级至 head，逐表验证旧数据哈希不变。
2. 构建并启动 API、受控模型、HTTP 商务沙箱、库存 MCP；模型与商务服务就绪后启动 API。
3. 单独完成知识种子发布后启动两个 Agent Worker 和独立退款 Worker，避免种子发布与消费争抢。
4. 20 次并发提交复用 10 个幂等键，断言 10 个唯一任务、每个一次尝试；验证确认接口、提交后断连与回调丢失的退款对账，以及 MCP 库存确认执行。
5. 重启有状态服务，验证幂等记录、外部退款次数和新任务执行。
6. 停止全部写入者，`pg_dump` 到新备份，再 `pg_restore` 至单独空库，对比所有表的记录数和 SHA256。使用恢复库启动应用并读取旧任务；密码通过权限 0600 的临时 passfile 传给 libpq，不放进进程 argv。
7. 停止服务后使用 SQLite backup API 读取独立卷中的数据库（包含已提交 WAL），再分别备份和恢复，对比源卷与恢复库的全部表指纹；不把应用数据库备份解释成供应商数据备份。

产物包含 `deployment.json`、命令日志、服务日志和三类隔离测试数据库备份。GitHub Actions 的 `compose-acceptance` 作业始终上传产物，失败使该作业失败；最终状态以[验收汇总](../evaluation/acceptance.md)关联的当前提交为准。

这覆盖停写后的逻辑快照恢复，不覆盖生产持续写入、跨机时钟/网络、真实支付系统、时间点恢复或灾难恢复 SLA。恢复应用备份后仍需按原业务键核对外部效果；不得通过回滚本地账本再次发起已发生的退款。

CI 显式配置 [PostgreSQL 官方 PGDG 源](https://www.postgresql.org/download/linux/ubuntu/) 安装 17 客户端，避免使用 Ubuntu 默认仓库中的不同主版本。
