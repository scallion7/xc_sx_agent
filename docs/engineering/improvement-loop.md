# 对话办理、证据约束与索引扩展

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

用户可以分轮提供售后资料，系统保存可追溯草稿；知识回答保留条件和证据边界；失败任务经过脱敏整理及人工复核后，才能成为后续版本的评测材料。运行负载由 SQL admission 控制。对话售后、逐结论证据检查、预编译索引分别由开关控制，默认关闭；身份隔离、已有政策检索的安全判断、结构化脱敏和请求恢复不依赖这三个可选开关。

## 多轮售后与可信执行

启用 `CONVERSATION_INTAKE_ENABLED=true` 后，Agent 获得 `get_after_sale_intake` 与 `update_after_sale_intake`。前者查当前会话，后者支持补充资料、生成提案、撤回和生成执行提案。工具复用申请状态机，不能通过模型输出直接创建工单或退款。

例如依次输入“售后申请”“补充订单 ORD-20240125-006”“补充原因：尺寸不合适”“生成提案”。每轮先读取修订，再提交 CAS 命令。字段必须能在本轮真实用户消息中找到来源，订单归属仍由业务服务验证。缺订单或原因时，最终回复由确定性代码提示缺失项。`new_task=true` 要求用户明确提出新申请，且新草稿使用修订 0。

- 字段来源记录 run ID、消息哈希和修订，不复制原始整段消息；后续修改替换该字段来源。
- 草稿创建与修改的提交事务校验 run owner、fence、有效租约、取消标记及会话所有权版本。接管再释放也不能复用旧 run 写入。
- 草稿工具在调用账本中标记为 `draft`，命令回执保证进程恢复后重放不重复修改。计划执行器串行执行草稿操作，售后与投诉子 Agent 使用相同工具边界。
- 生成提案、修改资料使旧提案失效、记录命令回执保持同事务。确认与执行继续通过可信 action API；已经执行的业务不能被“撤回申请”抵消。
- API 固定本轮功能开关及草稿参数 schema 哈希，Worker 使用任务快照，避免进程配置不同导致重试时工具集变化。旧配置 manifest 的哈希协议保留兼容。

客户页面展示申请资料，客服工作台可查看申请及字段来源。响应丢失后，页面提供“重试上次提交”，重放保存的请求体、修订和幂等键，而不是使用刷新后读到的新修订重造命令。明确的 `409 INTAKE_VERSION_CONFLICT` 表示 CAS 未提交，可释放该请求并刷新；未知 409、服务异常和网络中断继续保留原意图。详见[售后指南](../guides/after-sale.md)与[请求兼容约定](naming-and-compatibility.md)。

受控 HTTP 模型覆盖确定性测试用语；真实模型在开放式对话中的抽取、澄清和规划质量尚未由这些回归验证。

## 政策澄清上下文与草稿分离

政策工具保存的是地域、渠道、商品及所选订单的检索条件；售后 intake 保存的是待办理申请。二者独立，澄清政策条件不会自动创建申请，也不会授权退款。条件只接收用户消息中可追溯的值或所属订单的结构化权威字段。

政策上下文随用户会话恢复。换单清除前一订单条件；否定旧条件会撤销相应缓存值；权威条件冲突或模型漏传明确换单时，本轮进入澄清并阻止删参重试沿用旧条件。下一轮使用 run ID 或新 chat turn ID 重新核验。排序之外的同政策冲突检查与这一上下文共同决定检索 outcome，详细规则见[知识与治理](knowledge-and-governance.md)。

## 结构化工具输出与脱敏

工具返回 JSON 时，护栏递归处理字符串叶子，保留数字、布尔值和容器类型，再序列化为 JSON；恢复历史工具消息时使用相同路径。检索分数或金额中的连续数字因此不会被文本电话正则替换，导致 JSON 无法解析。非 JSON 文本继续按普通文本脱敏。

顶层 `action` 中格式合法的 `action_id`、`arguments_hash`、`business_version` 属于后续确认和执行所需的控制引用，按固定长度十六进制格式保留。自由文本里的电话、邮箱等仍按既有规则处理。保留引用不授予权限，服务端仍核验用户、会话和参数；这也不是对任意工具字段的全量隐私识别保证。

## 逐结论证据检查

`ANSWER_EVIDENCE_CHECK_ENABLED=true` 在最终回答发布前检查 `answerable` 检索结果。每个结论必须对应适用政策的完整原文分句；检查日期、地域、渠道、商品条件，保留逗号前的限制条件，并要求相邻的显式限制句同时出现。报告包含分句、命中 chunk、原文及 chunk 内字符位置，输出 `answer.support.checked` 事件。

不支持、数字冲突、删去否定或条件、适用范围不明时，将 `evidence_state` 置为 `unsupported_claim` 并提示人工核实；失效引用仍由 release/chunk 校验拦截。

这是**保守的原文支持规则**，不是自然语言蕴含模型。它会拒绝正确的同义改写，也不能理解所有跨段限定或领域语义；事件明确标记 `semantic_entailment_verified=false`。默认关闭，不把规则测试通过表述成真实回答准确率提升。真实语义评测仍需独立人工标签和模型预算授权。

## 预编译索引

`COMPILED_RETRIEVAL_ENABLED=true` 时，发布构建把规范化文档 token 计数持久化到 `compiled_knowledge_indexes`；已有 ready 版本首次读取时也可补建。索引键包含 tenant、release、profile、分词器与格式版本，内容带校验哈希。构建重试复用相同产物，同键不同源数据拒绝。

检索先按日期、适用范围和删除标记过滤，再在实际候选子集重算 BM25 的文档频率与平均长度，保持原路径的分数语义。常驻缓存最多 8 个索引，按序列化体积预算 32 MiB；Python 对象实际内存会更大。ready 检查、删除屏蔽、活动版本切换、保留期与 GC 继续生效；延迟构建通过 release 行锁与 GC 串行化。

原快照保留，关闭开关即可使用原路径；派生索引不是唯一数据源。当前仍是 Python 精确扫描，向量分支没有升级为 ANN 或外部检索引擎。规模实验使用合成 ready 快照与本地哈希向量，测量 SQL 读入、过滤、词法打分，排除公共查询缓存；**不包含导入耗时，也不能证明语义检索质量**。

复现命令：

```bash
python -m app.scripts.benchmark_indexes --sizes 1000,10000 --repeats 12 --output test-results/index-scale.json
```

## 数据闭环与迁移

失败候选与正式评测数据分开保存。脱敏候选不自动成为 gold，也不自动进入冻结集或发布门禁；来源、人工复核、版本哈希和分区流程见[版本化评测](../evaluation/versioned-datasets.md)。

数据库升级命令为 `python -m app.scripts.manage_migrations upgrade`，当前迁移头为 `20261109_0009`。该迁移新增数据集、候选、admission 锁和预编译索引四张表；本轮身份、政策上下文、结构化脱敏及 run 核对修订没有另加 schema revision。OIDC 身份升级仍需要独立的映射配置核验，不能把数据库迁移完成当作历史身份已完成迁移。

候选或数据集非空时禁止降级；派生索引可随空业务迁移回滚后重建。SQLite 的 DDL 不保证跨多版本降级原子性，应先检查每级保护，并在备份副本演练。

实现入口：[草稿工具](../../app/agent/tools/intake.py)、[事务 fencing](../../app/services/run_context.py)、[政策上下文](../../app/agent/tools/knowledge_context.py)、[脱敏护栏](../../app/security/guardrails.py)、[证据规则](../../app/platform/evidence_support.py)、[索引读取](../../app/platform/compiled_index.py)。专项回归见[政策与工具输出](../../tests/test_policy_context.py)、[对话申请](../../tests/test_dialogue_intake.py)、[申请页面](../../tests/test_intake_browser.py)。容量与故障边界见[持续负载验收](../operations/sustained-load.md)，实际通过结果统一见[当前验收](../evaluation/acceptance.md)。
