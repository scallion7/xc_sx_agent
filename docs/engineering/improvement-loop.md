# 对话办理、证据约束与索引扩展

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

这次扩展围绕同一条主线：用户分轮提供售后资料，系统保存可追溯草稿；检索与回答保留证据边界；失败任务经过脱敏整理和人工复核，成为后续版本的评测材料。运行负载由 SQL admission 控制，新增能力默认关闭，便于分步启用和兼容既有行为。

## 多轮售后与可信执行

启用 `CONVERSATION_INTAKE_ENABLED=true` 后，Agent 获得 `get_after_sale_intake` 与 `update_after_sale_intake`。前者查当前会话，后者支持补充资料、生成提案、撤回和生成执行提案。工具直接复用原有申请状态机，不能通过模型输出直接创建工单或退款。

例如依次输入“售后申请”“补充订单 ORD-20240125-006”“补充原因：尺寸不合适”“生成提案”。每轮先读取修订，再提交 CAS 命令。字段必须能在本轮真实用户消息中找到来源；订单归属仍由业务服务验证。缺订单或原因时，最终回复由确定性代码提示缺失项。`new_task=true` 要求用户明确提出新申请，且新草稿使用修订 0。

- 字段来源记录 run ID、消息哈希和修订，不复制原始整段消息；后续修改替换该字段来源。
- 草稿创建与修改的提交事务校验 run owner、fence、有效租约、取消标记，以及会话所有权版本。接管再释放也不能复用旧 run 写入。
- 草稿工具在调用账本中标记为 `draft`；命令回执保证进程恢复后重放不重复修改。计划执行器串行执行草稿操作，售后与投诉子 Agent 具备相同工具边界。
- 生成提案、修改资料使旧提案失效、记录命令回执保持同事务。确认与执行继续通过原可信 action API；已经执行的业务不能被“撤回申请”抵消。
- API 固定本轮功能开关及草稿参数 schema 哈希，worker 使用任务快照，避免进程配置不同导致重试时工具集变化。旧配置 manifest 的哈希协议保留兼容。

客户页面展示申请资料，客服工作台可查看会话申请及字段来源。完整流程与入口见[售后指南](../guides/after-sale.md)。受控 HTTP 模型只覆盖这些确定性测试用语；真实模型在开放式对话中的抽取、澄清和规划质量尚未验证。

## 逐结论证据检查

`ANSWER_EVIDENCE_CHECK_ENABLED=true` 在最终回答发布前检查 `answerable` 检索结果。每个结论必须对应适用政策的完整原文分句；检查日期、地域、渠道、商品条件，保留逗号前的限制条件，并要求相邻的显式限制句同时出现。报告包含分句、命中 chunk、原文及 chunk 内字符位置，输出 `answer.support.checked` 事件。

不支持、数字冲突、删去否定/条件、适用范围不明时，将 `evidence_state` 置为 `unsupported_claim`，输出人工核实提示；失效引用仍由既有 release/chunk 校验拦截。

这是**保守的原文支持规则**，不是自然语言蕴含模型。它会拒绝正确的同义改写，也不能理解所有跨段限定或领域语义；事件明确标记 `semantic_entailment_verified=false`。默认关闭，不把规则测试通过表述成真实回答准确率提升。下一步真实语义评测仍需独立人工标签和模型预算授权。

## 预编译索引

`COMPILED_RETRIEVAL_ENABLED=true` 时，发布构建把规范化文档 token 计数持久化到 `compiled_knowledge_indexes`；已有 ready 版本首次读取时也可补建。索引键包含 tenant、release、profile、分词器与格式版本，内容带校验哈希。构建重试复用相同产物；同键不同源数据拒绝。

检索先按日期、适用范围和删除标记过滤，再在实际候选子集重算 BM25 的文档频率与平均长度，保证与原路径相同的分数语义。常驻缓存最多 8 个索引，按序列化体积预算 32 MiB；Python 对象实际内存会更大。ready 检查、删除屏蔽、活动版本切换、保留期与 GC 继续生效；延迟构建通过 release 行锁与 GC 串行化。

原快照保留，关闭开关即可使用原路径；派生索引不构成唯一数据源。当前仍是 Python 精确扫描，向量分支没有升级为 ANN 或外部检索引擎。规模实验使用合成 ready 快照与本地哈希向量，测量 SQL 读入、过滤、词法打分，排除公共查询缓存；**不包含导入耗时，也不能证明语义检索质量**。

复现：

```bash
python -m app.scripts.benchmark_indexes --sizes 1000,10000 --repeats 12 --output test-results/index-scale.json
```

## 迁移与兼容

执行 `python -m app.scripts.manage_migrations upgrade` 到 `20261109_0009`。新增数据集、候选、admission 锁、预编译索引四张表；旧表、旧迁移、历史证据与原接口保留。候选或数据集非空时禁止降级；派生索引可随空业务迁移回滚后重建。SQLite 的 DDL 不保证跨多版本降级原子性，应先检查每级保护，再在备份副本演练。

源码：[草稿工具](../../app/agent/tools/intake.py)、[事务 fencing](../../app/services/run_context.py)、[证据规则](../../app/platform/evidence_support.py)、[索引读取](../../app/platform/compiled_index.py)。数据闭环见[版本化评测](../evaluation/versioned-datasets.md)，容量及故障边界见[持续负载验收](../operations/sustained-load.md)。
