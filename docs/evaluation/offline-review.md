# 离线评测与人工复核

当前要求：先完成离线比较和人工复核材料，暂不调用付费模型。本轮所有评测使用本地 lexical projection / 受控 HTTP 模型，不能据此声称真实语义能力或线上费用。

## 冻结材料

[review 数据目录](../../app/evaluation/review/) 包含 18 份合成政策、32 条题目、文件 SHA256 manifest 和空白 review-template。16 条 development 与 16 条 holdout 按 16 个 family 隔离。所有题目和初始标签均由开发过程生成，**尚未人工复核**；本轮没有运行新 holdout。旧 platform 数据的 holdout 曾参与诊断，不再作为最终独立测试集。

```bash
python -m app.scripts.review_evaluation --output test-results/offline-development.json
python -m app.scripts.review_evaluation --review-template --output test-results/review.json
```

第一条只比较 development 上的 vector、BM25、hybrid、rerank；其中 vector 仍为本地哈希投影。查看[本轮原始结果](../evidence/reliability/offline-development.json)，包括逐题结果、Recall/MRR/排序指标和拒答状态。不要仅摘录最好模式或将其叫作真实模型准确率。第二条创建待填写材料，拒绝覆盖已有文件。

## 本次开发集诊断结果

16 题使用初始待复核标签；Recall 仅对有 gold 的题求平均，MRR 按现有脚本口径包含无证据题。

| 模式 | Recall@K | MRR | 状态判断准确率 | 引用正确率 |
|---|---:|---:|---:|---:|
| 本地哈希向量 | 42.9% | 0.375 | 50.0% | 50.0% |
| BM25 | 42.9% | 0.375 | 50.0% | 50.0% |
| Hybrid | 42.9% | 0.375 | 50.0% | 50.0% |
| Hybrid + rerank | 42.9% | 0.375 | 50.0% | 50.0% |

此数据未显示混合检索增益，也不支持“检索质量已达标”的结论。后续先复核 gold、适用性条件和拒答原因，再只使用 development 诊断分词、过滤与排序；保留集维持冻结。真实语义模型效果仍未验证。

## 人工复核步骤

1. 将 cases.json 的题目与 documents.json 对照，独立判断用户约束（地域、渠道、商品、时效）是否明确。先判断再看原始建议标签，避免锚定。
2. 为每题确定 `gold_documents` 和 `outcome`：answerable（证据足够）、no_evidence（无支持）、needs_clarification（缺少决定性条件）、policy_conflict（适用规则互相冲突）。明确冲突或拒答理由。
3. 在 review.json 填写真实 `reviewer`、`reviewed_at`、`notes`，纠正标签后将 `approved` 改为 true。不要把执行脚本或模型自评当作人工审核。
4. 建议第二位复核者检查所有冲突、拒答及分歧题。分歧未解决时保持未批准；保留复核人与判定依据。不修改冻结题目文件；如发现题目缺陷，另建版本并重新分组/冻结。
5. 全部 32 条审核通过并保存原文件哈希后，才能执行下面的 holdout 命令。当前不代填审核身份、不生成批准状态。

```bash
python -m app.scripts.review_evaluation --split holdout \
  --review test-results/review.json --output test-results/holdout.json
```

门禁校验数据哈希、题目覆盖和审核字段。该校验不能验证签名者真实身份，组织环境还需评审权限和签名审计。初次 holdout 结果应完整保存，不能反复调参后仍称“未触碰测试集”。真实 embedding、付费模型和账单验证另行授权后进行。

## 发布评测门禁

新发布清单需要 `evaluation_plan`（case_ids、strategies、repeats），报告 schema_version=2，完整覆盖该笛卡尔积。评测和部署均复核 complete、逐题通过、数据库断言、零不安全动作、token 汇总及重复/缺失行。预算截断的结果保存为诊断，不能部署。

历史清单和报告仍可读取；缺少计划或仅部分结果的历史版本不能再通过新部署门禁，需要绑定计划重新完整评测。旧已分配会话读取语义保留。本次未改历史评测文件以伪装它们通过新协议。
