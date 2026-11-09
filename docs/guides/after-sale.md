# 售后申请：补资料、确认与撤回

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

适用于用户页面 `/after-sale` 及其 API 接入。自然语言客户服务演示见[五分钟演示](five-minute-demo.md)；底层执行保证见[可信执行与业务一致性](../engineering/reliable-execution.md)。

## 操作流程

启动现有离线演示，打开终端打印的 `/after-sale`：

```bash
python -m app.scripts.demo_stack --directory app/sessions/intake-demo
```

使用 `demo-user` 新建申请，分次填写订单与原因，生成创建提案，核对具体参数后“确认并执行”。需要人工审批的工单审批后，再生成执行提案并重新确认。材料通过已有 `/v1/evidence` 提交、由管理员审核；本版页面可关联已提交的材料编号，尚未实现图片存储或自动理解证明材料。

创建、读取或保存申请时，资料字段暂时禁用，待当前请求及刷新完成后恢复编辑，避免较晚返回的申请数据覆盖刚输入的内容。

![售后申请页面](../assets/after-sale.png)

## 接口与持久化契约

申请使用两张表：`after_sale_intakes` 保存当前资料和修订，`after_sale_intake_events` 保存命令哈希、完整回执和服务端时间。所有记录绑定配置 tenant 和认证用户；请求体不能传入审核身份、可信确认布尔值或退款金额。金额仍由订单和业务规则决定。

| 接口 | 作用 |
|---|---|
| `POST /v1/after-sale-intakes` | 使用 `session_id` 和 `Idempotency-Key` 创建/找回申请 |
| `GET /v1/after-sale-intakes/{id}` | 读取申请、当前提案、真实工单和退款操作状态 |
| `GET /v1/after-sale-intakes/{id}/history` | 读取本用户的修订与命令回执 |
| `POST /v1/after-sale-intakes/{id}/commands` | 提交 `update`、`propose`、`propose_execution`、`withdraw`，必须携带 `expected_revision` 与 `Idempotency-Key` |

`update` 的字段为 order_id、reason、case_type、evidence_ids。订单变化时默认清空旧证据引用；生成提案时核验材料归属及订单、规则、证据快照。提案创建与申请修订/回执同事务提交；修改或撤回也与旧提案失效同事务提交。响应丢失后重试原键返回原修订回执，不重复修改；同键不同内容拒绝。并发使用不同键修改旧修订时，失败方刷新后重新核对。

创建与执行沿用 `/v1/actions/{id}/confirm` 和 `/execute`，不会因保存资料或生成提案产生工单/退款。修改已确认但尚未执行的申请会使原确认失效；新提案须重新确认。已经执行或结果未知的创建操作禁止原地改写/撤回。已创建工单的诉求变更仍需业务审核处理，**本版不支持直接改写已审批工单或撤销已经发生的退款**。

`status` 描述申请资料和提案阶段；`business_state` 从工单、退款操作实时派生。特别是幂等重放可能返回原始“已受理”回执，最新退款结果必须查询业务状态，不能把该回执当作失败再退款。

这是结构化申请流程与业务执行契约，复用原有 Agent 工具的确认、快照和业务服务。没有新增未经评测的自然语言槽位提取能力；真实模型如何完成复杂多轮对话，留待后续真实模型评测验证。

## 迁移与验证

已有数据库启动新版本前执行 `python -m app.scripts.manage_migrations upgrade`，新增迁移 `20261105_0008`；旧迁移保持不变。已有售后申请或回执时拒绝降级，不能为降级删除真实历史。SQLite 空库升级、drift、专用租户 smoke、降级与重建已验证，[迁移日志](../evidence/complex-after-sale-round3/sqlite-migration.log)可复查；既有真实业务数据库未自动迁移。

[任务基线](../evaluation/task-baseline.md)说明补证审批、变更撤回、退款未知恢复三条 HTTP 场景；[验收状态](../evaluation/acceptance.md)统一列出本地与 CI 结果。
