# 完整部署与恢复验收

[文档中心](../README.md) · [配置与运行](../guides/configuration.md) · [当前验收](../evaluation/acceptance.md)

此流程创建一次性 Docker Compose 环境，验证应用 PostgreSQL、多个 Worker、独立 HTTP 商务沙箱、库存 MCP 和恢复闭环。模型为受控 HTTP 服务，订单为合成数据，不调用付费模型或真实支付。它与 `demo_stack` 的本机 mock 演示是两套入口。

## 准备与运行

需要 Docker Compose v2、Python 3.11 锁定环境、可用的 `pg_dump`/`pg_restore` 17，以及本机空闲端口 8000、9124、9125、55432。从仓库根目录运行：

```bash
python -m pip install -r requirements-lock.txt
python -m app.scripts.accept_deployment --output test-results/deployment-01/deployment.json
```

`--output` 必填。每次选择新目录，脚本会创建报告父目录，但不会覆盖已有数据库备份。失败后检查本次报告与日志，下一次用新目录保留原证据。

脚本加载 `docker-compose.yml`、`docker-compose.platform.yml`、`docker-compose.sandbox.yml`、`docker-compose.acceptance.yml` 并开启 integration profile，创建随机 `ecom-acceptance-*` 项目，拒绝复用已有容器。所有 Compose 命令固定该项目名，收尾只对该项目执行 `down --volumes --remove-orphans`。它不是升级现有服务的命令；不要改成现有部署的项目名。

| 端点 | 入口与用途 |
|---|---|
| 应用 API/UI | 本机 `http://127.0.0.1:8000`；Compose 默认将 8000 发布到所有宿主接口，外部访问控制由本机网络配置决定 |
| 商务沙箱 | `127.0.0.1:9124`，独立 SQLite 持久卷 |
| 库存 MCP | `127.0.0.1:9125`，独立数据库 |
| PostgreSQL | `127.0.0.1:55432`，应用数据库 |
| 受控模型 | Compose 内部 `model:9126`，无需额外宿主端口 |

脚本使用 `dev-user-token`/`dev-admin-token`，商务订单为 `ORD-SANDBOX-001`；不要混用 `demo_stack` 的 `demo-user`、`demo-admin` 和本机 mock 订单。验收完成即停栈，持续交互演示使用[五分钟入口](../guides/five-minute-demo.md)。

CI 用 [container_pg_client](../../app/scripts/container_pg_client.py) 适配器调用与数据库相同的 `postgres:17-alpine` 镜像中的客户端，并以只读挂载传入临时 passfile。本机可以直接把 PostgreSQL 17 客户端加入 PATH；CI 包装器的完整配置见[工作流](../../.github/workflows/offline-quality.yml)。仅有 Python 的 psycopg 驱动不能替代 `pg_dump`/`pg_restore` 程序。

## 自动执行的验收链路

1. 启动 PostgreSQL 17，先迁移至 `20261023_0005` 并写入真实 action 提案，再升级至 head，对比旧表数据哈希。这里的 0005 是升级测试起点；正常部署必须升至当前 head（`20261109_0009`）。
2. 构建 API、受控模型、HTTP 商务沙箱与库存 MCP，按依赖就绪顺序启动 API。API `/ready` 只检查配置与迁移；后续实际业务请求才验证跨服务链路。
3. 单独完成知识种子发布后启动两个 Agent Worker 和独立退款 Worker，避免初始化与消费争抢。
4. 20 次并发提交复用 10 个幂等键，断言 10 个唯一任务且各一次尝试。实际走确认接口、退款提交后断连/回调丢失的对账链路，以及库存 MCP 的确认执行。
5. 重启有状态服务，检查原请求仍返回原任务、外部退款次数仍为 1，并确认新任务能完成。
6. 停止 API、Agent/退款 Worker、商务沙箱和库存写入者。将应用 PostgreSQL 备份到新文件，恢复至独立空库，对比全部表的行数、SHA-256 和审计指纹；用恢复库启动应用并通过认证接口读取旧任务。
7. 对停止后的沙箱与库存卷调用 SQLite backup API，获取包含已提交 WAL 的一致性快照，再分别备份、恢复并比较源卷与恢复库的表指纹。仅复制 `.db` 主文件不能替代此步骤。

PostgreSQL 子进程通过权限 0600 的临时 passfile 使用 libpq 凭据，不将密码放进 `pg_dump`/`pg_restore` argv。此处描述的是受控脚本内部调用；手工调用恢复 CLI 时也应避免把真实密码直接写进命令参数或日志。

## 查看产物与失败定位

输出目录包括 `deployment.json`、`compose-commands.log`、`compose-services.log`、应用 `postgres.dump`，以及沙箱和库存的源快照、备份与恢复库。先查看报告的 `passed`、`failure_type` 和已完成的阶段，再对照服务日志；日志不是退款成功的凭证，仍以 operation、投递状态和供应商计数为准。

GitHub Actions 的 `compose-acceptance` 作业无论成功与否都会尝试上传 `test-results/`，任何验收失败使作业失败。最新运行及源码提交见[验收汇总](../evaluation/acceptance.md)。仓库中的[历史部署报告](../evidence/reliability/deployment.json)只保留其当时验证范围，不作为每次新代码的执行证明。

端口冲突、Docker 不可用、客户端缺失或目标备份已存在时，应修复环境后在新输出目录重跑。不要清理其他 Compose 项目或覆盖旧备份来让本次测试继续；进程被强制终止导致收尾未执行时，先按报告中的随机项目名核对，再仅清理该项目。

## 验证边界

该流程覆盖停写后的逻辑快照恢复。它没有验证生产持续写入、跨机网络/时钟、真实身份系统、支付供应商、PITR 或容灾 SLA。应用库备份不包含供应商库；供应商退款已发生时，回滚应用库也不会撤销外部效果，恢复后必须按原业务键核对，不能再次发起退款。

Agent 任务被判定 `needs_reconciliation` 时，退款核对完成后还需按[人工结案流程](incident-runbook.md#agent-任务人工核对结案)处理任务本身；本部署脚本的成功不授权强制解锁未知写入。
