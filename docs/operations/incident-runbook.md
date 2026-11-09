# 告警与恢复手册

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

当前实现是只读检测器、本地状态去重和标准输出事件，不自动发消息、不自动重放业务。
`GET /v1/admin/alerts` 要求 `maintenance:run`；核心队列按部署聚合，模型配额按当前 tenant 统计，不代表系统已完成 SaaS 多租户隔离。

```bash
python -m app.scripts.check_alerts
python -m app.scripts.check_alerts --watch --interval 15 --state-file app/sessions/alerts.json
```

单次检查正常退出 0，有告警或检测失败退出 2。watch 模式仅在 firing/resolved 变化时输出 JSON，供部署平台日志采集；每个状态文件只运行一个监控进程。默认待处理年龄 60 秒、租约宽限 5 秒、模型预算使用率 90%；监控部署需要给状态文件配置持久卷。

<a id="queue-backlog"></a>
## 队列积压

`AGENT/OUTBOX/RECONCILIATION/KNOWLEDGE/CONNECTOR_BACKLOG`：确认相应 Worker 是否存活、数据库可达、模型/供应商延迟和限流。先恢复依赖，再观察最老待处理年龄下降；不要通过删除任务清除告警。重试退避中的任务也参与年龄统计，告警提示检查，不表示可以无条件重放。

<a id="worker-recovery"></a>
## 租约过期

`*_LEASE_EXPIRED`：检查进程、时钟和租约配置。恢复 Worker 后由队列 CAS 与 fencing 接管；旧 owner 不能提交结果。记录原任务编号、接管次数、最终状态，确认告警 resolved。执行 `python -m app.scripts.drill_platform --output test-results/drills.json` 可在隔离 SQLite + 真实进程环境复现终止、接管和告警解除。

<a id="dead-letters"></a>
## 死信

`*_DEAD`：查看 error_code、请求摘要与审计，定位确定性失败或重试耗尽。修复根因后，使用相应管理接口按原业务键重试；先核对外部结果。`needs_reconciliation` 不是可直接重放的死信。清除记录不能证明恢复成功。

<a id="refund-unknown"></a>
## 退款结果未知

`REFUND_UNKNOWN`：暂停人工补发，按原幂等键查询供应商状态和 Outbox/Inbox、对账记录。回调缺失由查询对账收敛；供应商仍未知则人工核对。不要换键重发退款。恢复完成的证据必须包含供应商退款次数和本地最终状态。

<a id="model-budget"></a>
## 模型预算

`MODEL_BUDGET_HIGH`：核对 spent + reserved、使用范围和配额上限。`MODEL_USAGE_UNKNOWN` 表示存在可能已计费但未得到确定 usage 的调用；先核对供应商账单，不把不确定调用自动置零或释放预留。当前离线受控模型的模拟 usage 不是实际费用。

<a id="monitor-unavailable"></a>
## 检测器不可用

`MONITOR_UNAVAILABLE`：检查数据库连接、迁移与权限；输出只保留稳定错误码，不输出凭据。恢复依赖后重新采样，确认该告警解除，同时检查新出现的业务告警。仅 `/health` 正常不能证明数据库可用。

## 恢复验收边界

[Compose 验收](compose-acceptance.md) 在停写窗口分别验证应用 PostgreSQL、退款沙箱和库存库的恢复指纹。线上仍需补充备份加密、保留周期、跨区域与时间点恢复、真实供应商核对和通知接收端。这里的恢复耗时不等于生产 RTO；单个快照一致不等于持续 RPO 保证。
