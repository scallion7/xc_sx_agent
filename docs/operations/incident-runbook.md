# 告警与恢复手册

[文档中心](../README.md) · [部署清单](deployment-checklist.md) · [当前验收](../evaluation/acceptance.md)

先区分三种状态：Agent 任务是否还能继续、业务操作是否已有确定结果、队列中是否仍有可投递消息。业务已退款不代表 Agent 任务已解锁；一次 API 超时也不能证明退款未发生。处理时记录 run、action、工单、operation ID 和原业务键，以各自账本核对。

## 启动检测与保留证据

检测器只读业务状态，在本地文件中去重并输出标准 JSON 事件，不自动发消息或重放业务。`GET /v1/admin/alerts` 需要 `maintenance:run`。核心队列按部署聚合，模型配额按当前 tenant 统计；这不代表系统已经完成 SaaS 多租户隔离。

```bash
python -m app.scripts.check_alerts
python -m app.scripts.check_alerts --watch --interval 15 --state-file app/sessions/alerts.json
```

单次正常退出 0，有告警或检测失败退出 2。watch 只在 firing/resolved 变化时输出，最小间隔 1 秒；同一状态文件只运行一个监控进程，并配置持久卷和部署平台日志采集。默认阈值为待处理年龄 60 秒、租约宽限 5 秒、预算使用率 90%，不是 `.env` 中可直接修改的三个配置键。

处置前保留任务详情、错误码、调用/队列状态、最近事件和时间线，不在日志中复制 Token、回调密钥或含凭据的数据库 URL。记录已执行的恢复动作及结果，最后确认业务状态和告警都收敛。

<a id="queue-backlog"></a>
## 队列积压

`AGENT/OUTBOX/RECONCILIATION/KNOWLEDGE/CONNECTOR_BACKLOG`：检查对应 Worker、数据库、模型/供应商延迟和限流。Agent Worker 与退款 Worker 独立运行；API 健康并不代表队列正在消费。先恢复依赖，再观察最老待处理年龄下降。退避等待也参与年龄统计，不应通过删除任务或跳过投递清除告警。

`RUN_QUEUE_FULL` 返回 429 和 `Retry-After: 2`，客户端等待后用原键原内容重试。已经接受的同键同内容请求即使满载也可取回原任务。`SESSION_RUN_ACTIVE` 是原会话仍被任务占用，应查询原 run，不能换键绕过。[持续负载验收](sustained-load.md)说明 admission 统计口径。

<a id="worker-recovery"></a>
## 租约过期

`*_LEASE_EXPIRED`：核对进程、时钟、数据库连接和租约配置。恢复 Worker 后由 CAS 与 fencing 接管；旧 owner 不应提交结果。记录原任务编号、接管次数和最终状态，核验在途副作用后确认告警解除。

```bash
python -m app.scripts.drill_platform --output test-results/drills.json
```

该命令在隔离 SQLite 与真实子进程环境复现终止、接管和告警解除。PostgreSQL 重启与多 Worker 接管另由[持续负载脚本](sustained-load.md)覆盖；本机通过不等同于多机时钟和网络故障验收。

<a id="dead-letters"></a>
## 死信与安全重放

`*_DEAD`：根据 error_code、请求摘要与账本区分确定性失败、重试耗尽和结果未知。修复依赖后，也不能把所有死信批量改回 pending。

Agent 重放接口是 `POST /v1/admin/runs/{run_id}/replay`，要求 `after_sale:recover`，只接受执行协议版本兼容且没有不确定写调用的 dead_letter。服务端保留检查点、递增 fence/attempt，拒绝时返回 `RUN_REPLAY_UNSAFE`。不要通过改数据库状态规避检查。

`needs_reconciliation` 不能直接重放或取消以释放会话；先按下面的可信凭证流程处理。退款队列中的 dead/unknown 则先查询原业务操作，不能套用 Agent 重放接口。

### Agent 任务人工核对结案

此流程只处置 `needs_reconciliation` 的 Agent run。使用有 `after_sale:recover` 权限的管理员身份读取：

```text
GET /v1/admin/runs/{run_id}/reconciliation
```

响应包含 `status`、`expected_version`、总 `ready`、每条写调用的 `call_id`/`ready`/`evidence`/`evidence_hash`，以及已存在的 `resolution`。凭证由服务端从绑定的 action 和业务账本生成，人工备注、模型回复或旧日志中的“成功”都不能替代。

| 凭证来源 | 可结案条件 |
|---|---|
| 新协议已记录、尚未派发的写调用 | 可信调用日志表明尚未绑定/执行业务动作，证明无业务效果 |
| 未领取的 action | 与任务用户、会话、工具、参数哈希和业务版本匹配；pending/confirmed 授权会在结案事务内撤销 |
| 创建工单 | 存在 `action:<action_id>` 创建回执，关联用户、订单和工单一致 |
| 执行退款 | 原 operation 已是 succeeded/failed；沙箱 Outbox 和查询对账记录均存在且全部 sent，不能还有在途投递 |
| 已完成动作的无效果失败 | 只接受服务端明确允许的确定性拒绝回执；供应商超时、unknown、accepted 或通用执行失败不能证明未执行 |

无 action 绑定的历史写调用继续阻断，即使旧日志标为 completed。不能直接写入一个“成功”回执补齐证据，不能重新退款来验证此前是否成功。多个调用引用同一 action 时，仍提交每个调用自己的凭证条目；服务端统一核验后才修改该 action。

仅在响应总 `ready=true`、操作者已核对全部业务结论后，将刚读取的版本和**完整**凭证集合提交：

```text
POST /v1/admin/runs/{run_id}/resolve
```

```json
{
  "expected_version": 12,
  "evidence": [
    {"call_id": "从核对响应复制", "evidence_hash": "从核对响应复制的64位小写十六进制哈希"}
  ],
  "note": "已核对原业务回执及全部投递终态，记录核对依据"
}
```

示例中的版本和哈希必须替换成实际响应，不能照抄。`note` 为 1–2000 字符；不添加未声明字段，不遗漏或重复 call。服务端在同一事务内重新锁定并校验凭证、完成版本 CAS、撤销尚可执行的授权、递增 fence，写入 `run.resolved` 事件与平台审计。

成功后 run 为 `resolved`，原调用日志保留，任务不重放；原会话可以继续提交新消息，原幂等键仍返回已结案任务。该流程只确认已经可证明的结果，不补发业务、不执行退款冲正。

此接口以 `expected_version` 和凭证哈希提供重试保护，**不要求 `Idempotency-Key`**。并发或重复 POST 返回 409；如果提交响应丢失，先重新 GET，检查 `status=resolved` 和 `resolution` 中操作者、备注、凭证。不要自动换成新版本继续 POST。

| 错误码 | 处理 |
|---|---|
| `RUN_BUSINESS_NOT_SETTLED` | 先让原业务操作通过供应商查询/回调收敛，未知结果不重试写入 |
| `RUN_DELIVERY_NOT_SETTLED` | 核对 Outbox 与对账队列，确认没有可投递消息和未收敛任务 |
| `RUN_RECEIPT_MISSING` / `RUN_RECEIPT_SCOPE_MISMATCH` | 缺少可信凭证或绑定不一致，保持阻断并审查原始账本 |
| `RUN_RECEIPT_SET_MISMATCH` | 重新读取并提交全部写调用凭证，不合并或去重 call 条目 |
| `RUN_RECEIPT_CHANGED` / `RUN_VERSION_CONFLICT` | 重新读取详情，先判断是否已结案；若未结案，人工核对新状态后再决定 |

当前工作台没有强制解锁按钮，也没有面向无凭证旧调用的强制结案接口。合法处置须保留原账本并形成可验证的业务关联；无法证明时保持阻断。

<a id="refund-unknown"></a>
## 退款结果未知

`REFUND_UNKNOWN`：暂停人工补发，按原业务键核对供应商、operation、Outbox、Inbox 和查询任务。用户可读 `GET /v1/after-sales/{case_id}/operation`；管理员用 `/v1/admin/after-sales/operations/recoverable` 定位待恢复操作，必要时从 `/v1/admin/after-sales/operations` 核对所有状态。

HTTP 商务沙箱的 `POST /v1/admin/after-sales/{case_id}/recover` 需要 `after_sale:recover`，沿原 operation 查询结果，不换键补发。独立退款 Worker 的 `--mode dispatch` 负责投递，`--mode reconcile` 负责查询，默认 `--mode both`。供应商仍未知时保留未知；不要把查询超时改成失败或直接再次退款。

成功恢复的证据至少包括供应商退款次数、原幂等键、本地 operation 终态和投递/对账终态。外部 Provider 的适配与恢复语义必须单独验证；不能把沙箱的查询能力推定为所有真实供应商都具备。若 Agent run 仍阻塞，再执行上方人工结案。

## 前端提交不确定与工作台忙碌

`/after-sale` 的 `INTAKE_VERSION_CONFLICT` 是确定未提交，可刷新后编辑；其他 409、网络中断和 5xx 不能统一认定失败。用同一标签页保存的原键、原修订、原内容“重试上次提交”；刷新得到的新修订不能替换旧意图。确认/执行动作则围绕原 action 查询，不生成新授权反复执行，详见[售后指南](../guides/after-sale.md)。

工作台接管、审批、人工回复及后续刷新期间，按钮和目标字段保持禁用。等待完整刷新完成后核对当前用户与会话；刷新报错先解决连接/权限问题，不要急于切换目标并重发。人工接管阻断新自动写入，不能撤销已经进入供应商的请求。

<a id="model-budget"></a>
## 模型预算

`MODEL_BUDGET_HIGH`：核对 spent + reserved、使用范围与配额上限。配额是累计值，不按日自动重置。`MODEL_USAGE_UNKNOWN` 表示可能已经计费但 usage 未确认；先核对供应商账单，不能自动置零或释放预留。受控模型的模拟 usage/零价配置不是实际模型费用。

<a id="monitor-unavailable"></a>
## 检测器不可用

`MONITOR_UNAVAILABLE`：检查数据库连接、迁移与权限。恢复后重新采样，确认该告警解除并检查新出现的业务告警。`/health` 正常不能证明数据库可用；`/ready` 的配置检查也不能证明 JWKS、模型和供应商的远程请求可用。

## 恢复验收边界

[SQLite 维护](deployment-checklist.md#4-sqlite-备份恢复与维护)使用一致性快照包含已提交 WAL，恢复新目标并核对指纹。[Compose 验收](compose-acceptance.md)在停写窗口分别恢复应用 PostgreSQL、退款沙箱与库存库。生产的备份加密、保留、PITR、跨区域恢复、真实供应商核对与通知接收端仍需部署验证；本机恢复耗时不能充当生产 RTO，单次快照一致也不能充当持续 RPO 保证。
