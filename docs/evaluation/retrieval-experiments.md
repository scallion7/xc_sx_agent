# 检索诊断与配置对比

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

面向开发者定位召回、排序和证据过滤损失。只使用 development 与本地哈希投影；真实审核流程见[复核工作台](../guides/review-workbench.md)，当前验证范围见[验收状态](acceptance.md)。

## 查看单次检索诊断

```bash
python -m app.scripts.review_evaluation --diagnostics \
  --output test-results/retrieval-development.json
```

页面选择 development 题目后也可运行本题诊断。诊断调用与公开检索相同的 `_search`，绕过缓存，额外保存：查询分词、日期和条件、删除/时效/适用范围原因、BM25 与向量分数和名次、RRF/重排前后分数、最终名次、词法重叠门槛、被拒候选及结果状态。公开检索响应不增加内部诊断字段。

“缺少 region/channel/product”表示需要澄清，未必被过滤；`lexical_support_rejected` 表示候选已有正分，但没有通过本地哈希投影的词法支持门槛。BM25 模式没有计算向量分数，因此诊断中的该列为 0，不能当作独立向量实验结果。

## 政策冲突与对话澄清

`top_k` 和候选池只控制展示与召回，不决定政策是否冲突。检索命中一个 `policy_key` 后，会检查同一快照内该键的全部有效决策；排在 K 之外、没有词法匹配的同键相反决策也会阻止自动回答。检查排除已删除、过期、尚未生效或明确不适用的政策，不混入其他政策键。相关政策的地域、渠道或商品条件仍未知时返回 `needs_clarification`，补齐后才判断冲突或可回答。

Agent 的 `search_knowledge` 工具支持 `region`、`channel`、`product` 和 `order_id`。首次缺少条件时追问，下一轮将用户明确补充的原文值放入相应字段；不能只把条件拼进 query。服务端只接受当前用户消息中出现的值或该会话已经核实的值，记录消息哈希，并沿用现有会话保存/恢复流程。此检查验证文字来源，不等同于现实事实核验，也不从模型摘要猜测地域或商品类别。

用户选择具体订单时，服务端通过 `CommerceService.get_order(order_id, user_id)` 检查归属，优先采用订单 `policy_context` 中的 `region/channel/product`（兼容订单同名顶层字段）。供应商没有明确字段时仍需用户澄清，不从收货地址推断购买地域，也不从商品名推断类别。模型参数与订单事实冲突会返回 `KNOWLEDGE_CONTEXT_CONFLICT`；切换订单清除旧订单的澄清条件。作用域按会话及用户保存，不跨用户复用。兼容旧知识后端的行为不变，这条流程用于版本化知识检索。

用户明确否定旧条件时，即使模型漏传更新字段也不会沿用被否定值。用户提及另一订单却未传入选择参数时，清除旧订单上下文并要求澄清；订单条件冲突后，同轮删去冲突参数重试也不会恢复旧条件。阻断绑定持久任务 ID 或单次 chat 的标识，新一轮用户可以重复相同文字完成澄清。

回归：[政策条件与多轮对话](../../tests/test_policy_context.py)，覆盖两轮补齐、实例重建、订单事实优先、跨用户/订单隔离，以及冲突判断不受 K 限制。工具结果在执行边界按结构化值脱敏；构建模型消息和恢复会话时保留 JSON 数字类型及合法 action 控制引用，敏感自由文本仍脱敏。

## 配置配对实验

```bash
python -m app.scripts.compare_retrieval \
  --baseline app/evaluation/retrieval_profiles/legacy-v1.json \
  --candidate app/evaluation/retrieval_profiles/candidate-v2.json \
  --output test-results/retrieval-comparison.json
```

`RetrievalProfile` 是不可变、有校验的实验配置，包含 tokenizer 版本、Unicode NFKC 规范化、候选池大小、RRF 参数/权重、词法重排权重和证据支持门槛。完整配置参与 SHA256 与缓存键；同名配置改参数也不会共享旧缓存。支持的 tokenizer 当前仍只有既有版本，不能填写任意名称伪装切换分词器。

默认配置保留原检索行为。候选 v2 仅增加 NFKC 规范化和 20 条候选池：先召回候选，再按原证据门槛过滤，最后截取 top K，从而允许候选池内的合格证据递补。没有自动降低门槛或上线候选配置。测试以受控排序复现“第一候选被拒、后续合格证据应递补”的机制。

对比使用同一知识快照、同一批 development 题目与四种模式，独立统计候选召回、最终证据召回、结果状态、错误放行、无依据引用及不必要拒答。缺失证据归因分为：索引缺失、适用性过滤、无正分、候选池之外、证据筛选拒绝、最终 top K 之外。逐题报告标识变化、改善与退化，并保存两侧完整诊断轨迹、配置哈希、原始文件哈希与代码树哈希。

本次 16 题 × 4 模式 = **64 组配对比较**，使用尚未人工审核的初始标签：

| 观测 | 基线与候选结果 |
|---|---|
| BM25 / hybrid 候选召回 | 均为 100%，只在有预期证据的题上计算 |
| 本地哈希向量候选召回 | 均为 92.9% |
| 最终证据召回 / 状态准确率 | 四模式均为 42.9% / 50.0% |
| 输出变化、改善、退化 | 64 组均无变化 |
| BM25 / hybrid 缺失证据归因 | 9 次文档实例被证据门槛拒绝；不是 9 个独立业务家族 |

这批数据的主要损失位于证据判断阶段，扩大候选池没有改善其最终表现。**这些结果不支持语义质量提升结论。** 原始标签仍需真实审核，再决定如何调整证据策略。[摘要](../evidence/retrieval-handoff-round2/comparison.json)关联完整压缩轨迹。实验入口只接受 development，报告始终 `release_eligible=false`；`--review` 可应用全量审核通过的标签，但不会解锁本脚本对 holdout 的运行。

实现：[配置定义](../../app/platform/retrieval_profile.py)、[比较器](../../app/platform/retrieval_experiment.py)、[CLI](../../app/scripts/compare_retrieval.py)。
