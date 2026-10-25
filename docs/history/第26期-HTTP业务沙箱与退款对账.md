# 第 26 期：真实 HTTP 业务沙箱与退款对账

> 历史归档：记录当时的设计、实现与验收范围，不代表当前运行状态。当前入口见 [文档导航](../README.md) 与 [验收状态](../evaluation/acceptance.md)。原始证据保持原版本，不追改历史测量结果。

本期把第 22 期的 Provider 契约、第 23 期可信确认和第 24–25 期持久化执行基础连接到独立 HTTP 服务。沙箱运行真实网络协议、独立进程和数据库，模拟退款业务，不连接支付账户或真实资金。

## 1. 实现与保证边界

| 路径 | 本期实现 |
|---|---|
| 提交 | 工单改为 processing、创建原有 AfterSaleOperation、插入 Outbox 和对账任务，在同一 SQL 事务提交 |
| 发送 | 独立退款 Worker 抢占 Outbox；HTTP 请求在事务外；固定幂等键、参数指纹、租约及 fencing |
| 供应商 | 独立数据库保存订单与退款；退款和回调 Outbox 同事务；幂等键、operation_id、订单退款唯一约束 |
| 回调 | HMAC 验签、严格 schema_version、请求身份/金额/退款编号校验；Inbox 去重与业务状态更新同事务 |
| 对账 | 只按原幂等键查询；超时进入 unknown，有限查询后仍不确定进入 needs_reconciliation |
| 补偿 | 独立补偿记录和权限；先查询能力；默认不支持冲正，进入 needs_manual；可显式开启沙箱模拟冲正 |
| 运维 | 有界轮询、重试退避、死信、查询重排、迁移保护和账本保留 |

采用“至少一次发送 + 供应商稳定幂等 + 本地 Inbox/CAS”实现最终一致性。单次外部业务效果依赖供应商的持久化幂等和结果查询能力，不能泛化为任意供应商的 exactly-once。HTTP 适配器本身不自动重试写请求，也不跟随重定向。

`PROVIDER_MODE=sandbox` 仅允许 SQL，必须配置 API Token 和回调密钥，production 模式禁止启用。现有 mock/external 路径继续兼容；本期 HTTP 工作流只接入有上述能力的 commerce-sandbox。新供应商接入前须重新验证其能力，不能直接复用安全结论。

## 2. 数据与状态流转

```text
可信提案 → 用户确认 → 执行工单
  → 应用 DB 事务：[工单 processing + operation pending + Outbox + reconciliation job]
  → dispatcher → HTTP → 沙箱 DB 事务：[一笔退款 + callback Outbox]
                         │
             同步回执 / 签名回调 / 按幂等键查询
                         ↓
  应用 DB 事务：[Inbox + operation/CAS + 工单/CAS + 终止发送/查询任务]
```

继续以 `after_sale_operations` 为退款事实来源，不另建第二份退款状态表。新增 provider、provider_version、provider_reference 和不可变 request 存入现有 payload_json，原 SQL status/version 保留查询与 CAS 作用。

| operation 状态 | 含义与后续 |
|---|---|
| pending | 本地已入队，尚无供应商确认 |
| running | Worker 尝试发送，或供应商明确返回 processing |
| unknown | 发送超时、连接中断或响应无法验证；不能宣称失败或成功 |
| succeeded | 供应商已确认成功，后续迟到失败不能覆盖 |
| failed | 供应商明确的失败回执；停止发送与自动对账 |
| needs_reconciliation | 有界重试/查询耗尽；停止新提交，交人工核对，可请求再次查询 |

只接纳递增的供应商 version；旧版本/同版本消息记录为 ignored。已成功状态保持不变，即使收到更高版本失败。相同 event_id 不同内容返回冲突，同内容重投返回 duplicate。校验用户、订单、operation、幂等键、原因、金额与退款编号后才更新业务。

初次执行返回 `accepted=true` 与 operation.pending，表示排队受理，**不是退款成功**。确认界面也明确提示查询最终结果。之后用 `GET /v1/after-sales/{case_id}/operation` 查看最终状态。可信 Action 的历史执行回执仍可能是 accepted，应以 operation 查询为准。

队列的领取与确认使用 owner/fence/version；旧租约不能确认新 Worker 的任务，也不能将迟到的超时写回新状态。Outbox 默认最多 8 次发送，失败退避 `min(60, 2^attempt)` 秒；对账任务默认最多 12 次查询，未终态查询间隔 2 秒，网络错误采用退避。领取批量上限 32。达到上限进入人工核对；例如延迟成功晚于自动查询窗口且回调丢失时，需要管理员再次查询。

第 25 期 Agent run 的历史 `needs_reconciliation` 不会在本期自动改为 completed 或重新运行工具。新退款已有明确 operation 回执，可独立查询；历史不确定写仍需按原恢复规则处理。

## 3. 本地启动

先安装 `requirements-dev.txt`。以下环境变量必须在 API 和退款 Worker 的终端保持一致；沙箱读取 shell 环境变量，独立服务配置不从应用 `.env` 读取。建议使用全新演示数据库，不接已有业务数据。

```bash
export DATABASE_URL=sqlite:///app/sessions/stage26-demo.db
export DATABASE_AUTO_CREATE=false
export PERSISTENCE_BACKEND=sql
export PROVIDER_MODE=sandbox
export COMMERCE_SEED_DEMO_DATA=false
export SANDBOX_BASE_URL=http://127.0.0.1:9124
export SANDBOX_API_TOKEN=sandbox-dev-key
export SANDBOX_ADMIN_TOKEN=sandbox-dev-admin
export PROVIDER_WEBHOOK_SECRET=sandbox-dev-webhook-secret
export SANDBOX_DATABASE_URL=sqlite:///app/sessions/stage26-commerce.db
export SANDBOX_CALLBACK_URL=http://127.0.0.1:8000/v1/provider/webhooks/commerce-sandbox
export API_USER_TOKENS=dev-user-token:user-1
export API_ADMIN_TOKENS=dev-admin-token:admin
mkdir -p app/sessions
python -m app.scripts.manage_migrations upgrade
```

分别启动三个进程（每个终端沿用上面的环境变量）：

```bash
# 终端 1：独立沙箱，默认仅监听 127.0.0.1:9124
python -m app.scripts.run_commerce_sandbox
# 终端 2：应用 API
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
# 终端 3：发送 + 对账；可拆成 --mode dispatch / --mode reconcile 两个进程
python -m app.scripts.run_refund_worker
```

使用异步 Agent `/v1/runs` 时另启动第 25 期 `python -m app.scripts.run_worker`，并配置有效模型凭据。退款 Worker 本身不调用模型。

沙箱种子订单 `ORD-SANDBOX-001` 属于 `user-1`，金额 99 元。通过原有 Agent/可信提案确认流程创建并执行工单即可入队；本期没有新增绕过确认的公开退款执行入口。沙箱订单单号查询走 HTTP；商品目录及订单列表仍沿用原本地 CommerceRepository，演示时请明确提供上述订单号。

也提供本地 Compose 覆盖配置：

```bash
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml config
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up --build
```

该组合启动 PostgreSQL、API、Agent Worker、退款 Worker 和 commerce-sandbox；应用数据库与沙箱 SQLite 卷分离。配置中的 Token 仅是本地演示值。当前机器未运行 Docker Compose 或 PostgreSQL，不能把配置及 CI 定义视为这些环境已经实测通过。

## 4. 故障注入与观察

故障控制需要独立 `X-Sandbox-Admin-Key`，业务 Token 无此权限。故障在首次受理时保存，同键重试读取既有结果。每个演示订单只保留一笔退款；重复实验用新的临时数据库，修改故障不会清除已有退款。

```bash
curl -X PUT http://127.0.0.1:9124/admin/faults/ORD-SANDBOX-001 \
  -H 'X-Sandbox-Admin-Key: sandbox-dev-admin' \
  -H 'Content-Type: application/json' \
  -d '{"mode":"timeout_after_commit","delay_seconds":10,"drop_callback":true}'
```

在默认 5 秒客户端超时下，沙箱已经提交，客户端先得到 unknown。对账器查询后应得到 succeeded，管理计数仍只有一笔退款：

```bash
curl http://127.0.0.1:9124/admin/stats -H 'X-Sandbox-Admin-Key: sandbox-dev-admin'
curl http://127.0.0.1:8000/v1/after-sales/CASE_ID/operation -H 'X-API-Key: dev-user-token'
```

| 配置 | 可观察行为 |
|---|---|
| `mode=disconnect_after_commit` | 提交后截断响应体，客户端按结果未知处理 |
| `mode=delayed_success, delay_seconds=2` | processing v1，随后 succeeded v2 |
| `callback_copies=10` | 同一 event_id 重投 10 次，业务只应用一次 |
| `mode=delayed_success, callback_delay=3, delay_seconds=1` | v2 成功回调先到，v1 处理中回调迟到 |
| `drop_callback=true` | 回调留作 dropped，靠查询收敛 |
| `mode=reject` | 明确业务拒绝，operation.failed |
| 重启沙箱，保持原 DB | 退款幂等与回调发送进度继续保留 |

无需模型的可重复全链路验收（自动临时建库、启动本机 HTTP 子进程并清理）：

```bash
python -m unittest tests.test_http_commerce_sandbox -v
```

该组还覆盖发送进程在外部接受后、保存本地回执前退出，以及回调在 HTTP 投递成功后、确认 Outbox 前退出。测试重启/接管后验证一笔外部退款与一次本地事件推进。测试需要允许监听本机回环端口。

## 5. 人工核对与补偿

管理员通过 `GET /v1/admin/after-sales/operations` 查阅记录；HTTP operation 不再进入旧的自动重试列表。需要再次对账时：

```bash
curl -X POST http://127.0.0.1:8000/v1/admin/after-sales/CASE_ID/recover \
  -H 'X-API-Key: dev-admin-token'
```

该入口只重排已有对账任务、增加 12 次查询预算并记录操作者；不新建退款或 Outbox，终态成功/明确失败不重排。普通用户返回 403。若供应商查不到且无法证明未执行，应保留人工核对，不换幂等键强行退款。

补偿沿用 `POST /v1/admin/compensations`（需 `Idempotency-Key`）和 `POST /v1/admin/compensations/{idempotency_key}/execute`。请求的 source_operation_id/case_id/user_id 必须匹配已成功操作；同源同动作得到同一补偿记录，过期执行者不能覆盖新结果。

默认 `SANDBOX_SUPPORTS_REVERSAL=false`，能力查询未声明 `reverse_refund` 时转 needs_manual，不发送补偿写请求。设为 true 并重启仅开启**模拟冲正**，沙箱持久化独立补偿结果，原退款仍是 succeeded。它不等价于真实退款一定可以撤销。人工处理应保留外部凭证，再按原有补偿登记流程审计。

## 6. 迁移、保留与运维

新 revision `20261023_0005`，父 revision `_0004`，新增 `outbox_events`、`inbox_events`、`reconciliation_jobs`。先备份，再停止写入并升级，随后启动新版 API/Worker。`DATABASE_AUTO_CREATE` 不能替代旧库迁移。新队列表的 helper 是冻结的 stage26 schema，后续字段变更应新增 migration，不能修改历史 helper。

```bash
python -m app.scripts.manage_migrations upgrade
alembic check
```

没有 HTTP 退款账本且没有 needs_manual 补偿时，支持 downgrade 到 `_0004`，同时去掉旧代码不认识的 operation JSON 默认字段；升降级已测。已有任何 HTTP operation（含成功记录）时，降级会报 `STAGE26_DOWNGRADE_BLOCKED`，防止丢失发送、对账和供应商身份依据。需要旧版本时应隔离新库并恢复升级前备份，不能直接删除账本来绕过保护；备份恢复也必须核对备份后已接受的外部操作。

数据清理暂时保留所有具有 Outbox 的退款 operation、对应 Inbox/Outbox/对账记录及需人工处理补偿的来源记录。尚未实现联合归档/压缩；长期运行需设计与供应商幂等保留期限一致的归档协议。应用备份不包含独立沙箱数据库，演示恢复时须分别备份两个库。

SIGTERM 后退款 Worker 停止领取新任务，等待当前有限超时的 HTTP 调用结束；强制退出依靠租约恢复。没有请求级取消外部已提交退款的能力。当前没有生产监控告警面板，运维可用 operation API 和队列表的 status/attempt/error_code/next_attempt_at 诊断。

## 7. 验收记录

本地 Python 3.11 / SQLite，2026-10-24：

| 检查 | 实测结果 |
|---|---|
| 全量 unittest | 280 项通过（第 25 期 240 项，本期新增 40 项） |
| 本期专项 | 25 项事务/状态/迁移/保留 + 13 项真实 HTTP + 2 项确认控制字段回归通过 |
| app 语句覆盖率 | 80%，高于仓库 65% 门禁；未聚合 HTTP 子进程 |
| Ruff / format / compileall | 通过 |
| OpenAPI | 校验通过 |
| 固定计划质量门禁 | 40 条案例、12 项指标均为 1.0（确定性测试指标，非生产 SLA） |
| SQLite 迁移 | 空库升级、旧版升级、drift、受保护降级、空账本全链回退再升级通过 |
| portable database smoke | 50 请求创建、8 Worker 抢占、16 入队/10 回调去重通过 |
| Compose / CI YAML | 语法解析通过；未执行 Docker Compose |

可复验命令：

```bash
ruff check .
ruff format --check .
python -m compileall -q app tests
coverage run -m unittest discover -s tests -v
coverage report --fail-under=65
python -m app.scripts.validate_openapi
python -m app.scripts.validate_plan_quality
# 以下会写数据，只能指向迁移完成的隔离测试库
DATABASE_URL=sqlite:////tmp/stage26-isolated.db python -m app.scripts.manage_migrations upgrade
DATABASE_URL=sqlite:////tmp/stage26-isolated.db python -m app.scripts.database_smoke
```

真实 HTTP 测试使用独立应用 API、沙箱服务进程和独立数据库；不调用外部模型、OIDC 或支付服务。覆盖率统计当前测试进程的 app 语句，未聚合 HTTP 子进程覆盖率。

- 事务回滚、并发提交、Inbox 重复/冲突/乱序、身份和金额校验、过期租约、人工查询、迁移保护、保留策略。
- 真实 HTTP 超时/断流、延迟成功/丢失回调、10 次重复回调、30 并发供应商请求、重启、两个崩溃窗口、补偿能力、可信确认到最终查询。
- CI Python 3.10/3.11/3.12 矩阵运行完整测试；SQLite/PostgreSQL 矩阵增加 16 并发入队、8 抢占、10 回调的 portable smoke。PostgreSQL 和 CI 结果须待实际运行核验。

回归期间额外修复了第 23 期确认恢复的偶发问题：随机 action_id/hash 中包含连续数字时，被文本脱敏规则误识别为手机号或银行卡，导致 ACTION_NOT_FOUND。现在保留严格匹配长度与十六进制格式的可信 action 控制字段，理由、消息和其他文本继续脱敏；新增两个确定性测试覆盖确认成功和文本隐私保护。

## 8. 简历与面试表述

可描述为：实现真实 HTTP 电商沙箱与退款最终一致性工作流，采用事务 Outbox/Inbox、持久化幂等、租约 fencing 与查询对账，在供应商已受理后超时、进程崩溃、回调重复和乱序场景下恢复退款状态，并明确人工核对及补偿边界。

面试重点是解释“两边数据库不能用一个本地事务一起提交”、为什么超时不等于失败、为何需要稳定幂等键与结果查询、为何 Inbox 必须与业务更新同事务，以及为什么未知结果不能直接重放。吞吐量、生产可用性、真实资金效果均未测量，不应包装成项目成果。下一期按原计划推进知识导入、版本发布与索引生命周期。
