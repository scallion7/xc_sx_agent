# 第 25 期：可恢复的异步 Agent 任务执行

> 历史归档：记录当时的设计、实现与验收范围，不代表当前运行状态。当前入口见 [文档导航](../README.md) 与 [验收状态](../evaluation/acceptance.md)。原始证据保持原版本，不追改历史测量结果。

本期将原来绑定 HTTP 请求生命周期的 Agent 执行接入持久化队列。API 只提交任务，
独立 Worker 调用现有 Agent、模型网关和统一工具入口，任务结果与事件可跨进程查询。
第 23 期的可信确认、第 24 期的工单唯一性和创建幂等继续生效。

本地验收：**240 项测试通过（新增 32 项），app 语句覆盖率 82%**。测试使用 SQLite、
Fake 模型和演示业务数据；没有调用真实模型、OIDC、支付或退款供应商。PostgreSQL
共用 smoke 已加入 CI，但本机没有 PostgreSQL/Docker，未宣称本地通过 PostgreSQL 实测。

## 1. API 与执行入口

| 接口 | 权限 | 行为 |
|---|---|---|
| `POST /v1/runs` | `chat:use` | 必填 `Idempotency-Key`，返回 202、run_id 和 queued 状态 |
| `GET /v1/runs/{run_id}` | `session:read` | 仅任务所属用户可查询状态及最终结果 |
| `POST /v1/runs/{run_id}/cancel` | `plan:cancel` | 返回 requested、acknowledged 或 too_late |
| `GET /v1/runs/{run_id}/events` | `session:read` | SSE，支持 `Last-Event-ID` 补发 |
| `POST /v1/admin/runs/{run_id}/replay` | `after_sale:recover` | 管理员安全检查后重新入队，审计记录 actor |

提交体沿用 `ChatRequest`：`message`、`session_id`、`mode`、`strategy`；异步提交消息
最多 32000 字符。身份来自认证上下文，不能通过请求体指定任务所属用户或 Worker。
相同用户、相同幂等键、相同请求回读原任务；参数变化返回 409。

同一用户会话只允许一个未处理完毕的任务。数据库部分唯一索引覆盖 queued、running、
retry_wait、dead_letter、needs_reconciliation，第二个请求返回 `SESSION_RUN_ACTIVE`。
其他会话可以并发。本期使用显式拒绝策略，没有实现第 24 期原始大方案中的会话消息
序列和多租户体系；也不支持绕过 API 的 CLI 与 Worker 同时写同一个会话。

SQL 模式的 `/v1/chat` 和 `/v1/chat/stream` 是兼容适配层：仍保持同步响应格式，
但内部使用同一队列、调用记录和会话提交协议。若其他 Worker 已领取或同步执行不能
完成，返回 409 与 run_id，客户端转用任务查询。独立的 `/v1/runs` 始终由 Worker
处理，API 请求内不调用模型。JSON 模式保留原同步接口，异步接口明确返回 503。

## 2. 数据与领取协议

新增 Alembic revision：`20261023_0004`，前置 revision 为 `20261023_0003`。

- `agent_runs`：用户、会话、幂等键、请求摘要、状态、owner、fence、attempt、
  max_attempts、version、execution_version、lease_until、next_retry_at、取消状态、
 错误类别及 JSON 执行快照。
- `run_events`：`(run_id, sequence)` 联合主键；状态修改和对应事件在同一事务提交。
- JSON 快照：原始任务、稳定会话输入、初始计划、待确认提案、调用日志、计划检查点、
  最终结果。它是执行数据，不是面向普通用户开放的调试 API。

Worker 一次最多扫描 32 个候选任务并领取一个。通过 version 条件更新竞争所有权，
领取后递增 fence 和 attempt；任何并发失败者回读并尝试其他候选。模型与工具调用
期间不持有数据库事务或行锁。heartbeat 每个租约周期的三分之一续租。

旧 owner/fence、过期租约以及已转终态的执行者不能提交调用记录、会话输出或任务结果。
持久化错误不能被普通观测订阅器吞掉：调用开始与完成均通过直接检查点写入；EventBus
仅补充用量事件。安全中断使用独立控制异常，穿透原 Agent 的通用异常降级逻辑。

## 3. 调用记录与恢复

ReAct 和计划工具都经过统一执行入口，模型的 complete/parse 经过统一网关。每个调用
按调用种类、名称、参数摘要、重复序号标识；计划还绑定 plan_id/step_id，避免恢复时
跳过前序步骤导致同参工具调用串位。

| 崩溃位置 | 恢复处理 |
|---|---|
| 领取后、调用前 | 租约到期后重新领取 |
| 模型请求 in_flight，无法取回输出 | 可以重新请求；记录 dispatch 次数及 possible_duplicate_charge |
| 模型输出已保存 | 复用保存结果，不再次发出该调用 |
| 只读调用未完成 | 可以重做 |
| 工具结果已保存 | 复用已保存结果 |
| 业务写调用开始，尚无确定结果 | needs_reconciliation，禁止自动领取和管理员重放 |
| 计划步骤检查点已提交 | 复用权威计划及已完成步骤；保留原 plan_id，接管时创建新计划运行租约 |
| 会话输出生成、任务结果尚未提交 | 会话仍未落库；恢复原始输入和调用结果后重新原子提交 |

调用“完成”不等于业务成功。结构化超时、unknown/executing 或无法确认的写结果同样
标为不安全恢复；用户可查询任务状态，不能靠反复提交确认强行重放。任务 completed
表示 Agent 已返回可持久化响应，具体业务是否成功仍由响应、action 和工单状态决定。

计划层继续使用原有 Plan Repository、版本、租约与 fencing。新 Worker 不强占仍有效
的旧计划租约；短暂等待通过重试处理。查询已保存计划时取其权威版本，防止仅使用旧的
任务快照覆盖较新的计划状态。执行版本不匹配直接进入待核对。

用户确认场景会固定本轮开始时的待确认提案；即使确认操作已完成而进程在会话提交前
退出，恢复仍关联原提案和原结果。工具的确认和幂等校验仍由第 23/24 期服务负责。

## 4. 重试、死信、取消和会话

连接、超时、模型限流及模型服务内部错误属于可重试类别；使用 `min(60, 2**attempt)`
秒退避，默认最多三次领取。不可重试异常或尝试耗尽进入 dead_letter。管理员只能重放
相同执行版本且没有未决业务写调用的死信；保留原请求与调用记录，增加三次尝试额度，
不允许修改请求参数或强制跳过不安全检查。

- 未开始任务取消后直接 acknowledged，任务变为 cancelled。
- 正在执行时先记录 requested；模型/只读调用返回后的安全边界响应取消。
- 进入潜在业务写工具后采用保守策略，不把已提交效果报告为撤销；等待确定结果，
  成功落库后返回 too_late。不能硬中断已经发出的 Python 或外部请求。
- 未知业务结果继续 needs_reconciliation；取消不是退款冲正或业务补偿。

会话输入首次读取后写入任务快照，执行期间会话 save 仅暂存输出。任务完成结果、终态
事件和 SQL 会话输出在同一事务提交，并核对当前会话仍与输入快照一致；冲突进入待核对，
不会静默覆盖外部变更。活动任务阻止会话删除，现有维护清理也保留任务占用的会话。

## 5. SSE 断线续传

SSE 直接读取持久化 `run_events`，单次最多读取 100 条、按 sequence 升序发送；
`id` 就是该任务内的 sequence。客户端保存最后处理的 ID，重连时发送 `Last-Event-ID`，
并按 `(run_id, sequence)` 去重。负值、非数字及超过当前末尾的 cursor 返回 422。

连接关闭只终止订阅，不取消已提交任务。终态事件补齐后连接正常结束；死信重放后可用
同一 run_id 和上次 cursor 重新订阅。心跳注释不占事件序号。事件暴露调用标识、状态、
领取及模型用量信息，不输出完整模型提示词、工具参数和工具原始结果。

## 6. 本地运行

API 与 Worker 必须使用同一 SQL 数据库和一致的模型、业务、认证相关配置。先升级：

```bash
python -m app.scripts.manage_migrations upgrade
python -m app.scripts.run_api
```

另一个终端启动 Worker，可启动多个独立进程：

```bash
python -m app.scripts.run_worker --owner worker-1
# 单次轮询（运维检查或受控测试）
python -m app.scripts.run_worker --once
```

默认 `RUN_LEASE_SECONDS=30`、`RUN_MAX_ATTEMPTS=3`、`RUN_POLL_SECONDS=1`。SIGTERM/INT
停止继续领取，当前任务正常结束；强制退出则由租约恢复。多机需同步时钟；当前租约以
应用 UTC epoch 为准，没有引入独立时钟服务。Docker Compose 新增 worker 服务，等待
API 迁移及 readiness 完成后启动，支持 `docker compose up --build --scale worker=2`。

```bash
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H 'X-API-Key: dev-user-token' \
  -H 'Idempotency-Key: demo-stage25-001' \
  -H 'Content-Type: application/json' \
  -d '{"message":"查询订单 ORD-20240125-006","session_id":"stage25","mode":"single","strategy":"react"}'

# 将 <run_id> 替换为上一步响应中的值
curl http://127.0.0.1:8000/v1/runs/<run_id> -H 'X-API-Key: dev-user-token'
curl -N http://127.0.0.1:8000/v1/runs/<run_id>/events \
  -H 'X-API-Key: dev-user-token' -H 'Last-Event-ID: 0'
```

生产库应关闭自动建表并先备份迁移。降级到 `20261023_0003` 会删除新任务和事件表，
必须先停 Worker/API 写入，导出并处理未完成任务；降级不会撤销已发生的业务效果。
本次开发没有迁移原仓库中的运行数据库。

## 7. 验收证据与边界

新增 `tests/test_async_runs.py` 32 项测试，包括双线程/双进程抢占、持久化幂等、租约
失效与旧 owner 拒绝、heartbeat、退避与死信权限、取消竞态、迟到结果、未知写禁止重放、
模型缓存和潜在重复费用标记、会话/结果事务回滚、跨用户隔离、SSE cursor、JSON 模式拒绝。

测试还使用子进程 `os._exit` 验证三个真实进程退出窗口，使用现有 EcomAgent、模型网关和
工具入口验证 ReAct 执行、确认结果恢复及计划检查点复用。受控测试中，强制租约过期后
接管通过小于 30 秒的断言；这不是生产 SLA 或大规模压测结果。

本地完整检查：

- 240 项回归通过，coverage：8045 条语句，1484 条未覆盖，82%。
- Ruff、格式检查、compileall 通过；OpenAPI 49 条路径校验通过。
- SQLite 空库升级、从第 24 期升级、降级/重升及 Alembic drift 检查通过。
- 共用 smoke：8 个领取者仅 1 个成功，事件连续；第 24 期 50 请求工单 smoke 仍通过。
- 固定计划质量集 40 条，12 项确定性指标均为 1.0；不代表真实模型准确率。

本期不承诺外部 exactly-once，也没有自动解决 needs_reconciliation。真实 HTTP 业务
沙箱、Provider 按幂等键查询、Outbox/Inbox 与退款结果对账按原路线留给第 26 期。
多租户、完整会话顺序、生产队列容量/SLA、任务快照保留与清理策略、密钥平台和真实费用
统计仍需单独实施。任务与调用快照包含业务数据，部署环境须控制数据库访问与备份权限。

可用于简历的已实现表述：基于 SQL CAS、租约和 fencing 构建可恢复 Agent 任务队列，
通过调用检查点、原子会话提交与 SSE 续传处理进程中断；以并发、故障注入和数据库迁移
测试验证恢复协议及业务写操作的保守边界。
