# 离线评测与人工复核

[文档中心](../README.md) · [当前验收](acceptance.md)

核验基线：2026-11-10，代码 `c5aee67`。本文的 CLI 使用本地哈希词法投影或受控 HTTP 模型，不发起付费模型调用。检索结果、业务执行契约、真实答案质量分别验收；不能将受控模型通过率当作真实模型效果或线上费用。

## 数据集和当前状态

| 数据 | 用途与边界 |
|---|---|
| [review-v1](../../app/evaluation/review/) | 18 份合成政策、32 题，16 条 development、16 条 holdout，按 16 个 family 隔离；SHA256 manifest 固定原文和题目 |
| [任务契约集](../../app/evaluation/tasks/) | 6 个合成开发场景 × 3 种策略，检查真实 HTTP/Worker/SQL 终态；见[任务基线](task-baseline.md) |
| [旧 platform 数据](../../app/evaluation/platform/) | 早期诊断与受控 Agent 演示；其 holdout 曾参与开发，不再作为最终独立测试集 |

review-v1 的初始标签尚未完成真实人工审核，其 16 题 holdout 未运行。原冻结 32 题没有 `requires_second_review` 标记；新增数据版本可以显式为高风险题设置该标记，启用强制双账号审核，不能把这项能力写成原材料已经完成双审。自动化测试中的批准记录不计入真实审核进度。

## 开发集与复核材料

从仓库根目录、安装项目依赖后的环境运行：

```bash
python -m app.scripts.review_evaluation --diagnostics \
  --output test-results/offline-development.json
python -m app.scripts.review_evaluation --review-template \
  --output test-results/review.json
```

第一条仅运行 development 的 vector、BM25、hybrid、hybrid_rerank，并附逐阶段诊断；vector 使用本地哈希投影，rerank 是词法规则。第二条生成兼容旧格式的待填模板，并拒绝覆盖已有文件。推荐使用[复核工作台](../guides/review-workbench.md)生成带身份、原文定位和双审记录的 schema_version=2 导出；CLI 模板本身不具有这些审核证据。

[历史冻结基线](../evidence/reliability/offline-development.json)的 16 题初始标签诊断为：

| 模式 | Recall@K | MRR | 状态准确率 | 文档引用正确率 |
|---|---:|---:|---:|---:|
| 本地哈希向量 | 42.9% | 0.375 | 50.0% | 50.0% |
| BM25 | 42.9% | 0.375 | 50.0% | 50.0% |
| Hybrid | 42.9% | 0.375 | 50.0% | 50.0% |
| Hybrid + 词法重排 | 42.9% | 0.375 | 50.0% | 50.0% |

这是旧代码与初始未复核标签的历史结果，不是 `c5aee67` 的新质量结论。Recall 只在有 gold 的题上取平均，MRR 包含无证据题；引用指标检查返回文档是否属于 gold，不检查生成回答的逐句蕴含。相同分数既未证明混合检索增益，也未证明不同方法的语义能力相同。

## 人工复核与保留集

1. 对照原始政策判断题目日期和地域、渠道、商品等条件。先形成结论，再展开初始建议标签，避免直接照抄。
2. 选择 `gold_documents` 与 `outcome`：`answerable`、`no_evidence`、`needs_clarification` 或 `policy_conflict`。无证据结论的 gold 必须为空；其他结论必须列出支持判断的政策。
3. 在工作台记录精确原文证据、理由并保存。批准必须有非空 notes；schema_version=2 要求 gold 中每份政策都有逐字匹配的 `[start,end)` 引用。分歧未解决时保留草稿或 disputed。
4. 对设置 `requires_second_review: true` 的题目，首位批准只记为草稿；不同认证账号对相同标注内容再次批准后才完成双审。结论、gold、证据或 notes 改变会重新开始。无该标记的题目可以增加组织复核流程，但应用不会自动强制双审。
5. 完成整个数据集的真实审核，导出并保存原文件与哈希。发现题目缺陷应创建新版本，不修改已冻结原文件；调整参数仅使用 development。

原 review-v1 全部 32 题审核通过后，运行其保留集：

```bash
python -m app.scripts.review_evaluation --split holdout \
  --review test-results/review-approved.json --output test-results/holdout.json
```

`review-approved.json` 应为真实完整的审核导出，不是未填写模板。CLI 只读取内置 review-v1，不接受自定义 `--dataset`；自定义版本的工作台诊断与开发集比较见[版本化评测](versioned-datasets.md)。

门禁检查题目哈希、全量题目覆盖、合法政策 ID、带时区的 ISO 时间、审核人及 notes；v2 另检查 documents 哈希与每段原文定位。旧格式兼容读取，但不具备完整的原文定位保证。外部文件不能证明填写人的真实身份，工作台导入一律降为草稿，必须由当前认证账号重新批准。初次保留集结果应完整保存，反复查看并调参后不能继续称作未触碰测试集。

## 发布报告的校验边界

配置发布使用独立的报告契约：manifest 必须有 `evaluation_plan`（case_ids、strategies、repeats），schema_version=2 报告必须完整覆盖其笛卡尔积；评测和部署均核对 manifest/dataset 哈希、complete、逐题通过、期望 outcome、数据库断言、零不安全动作、token 汇总及重复/缺失行。矩阵最多 10000 次，repeats 为 1–100；预算截断的结果只能用于诊断。

生产环境还要求报告字段为 `execution_mode=live`、`review_status=human_reviewed`。这些是报告完整性和一致性校验，不是对来源真实性或回答语义正确性的自动证明。当前 Agent runner 的 passed 主要检查 answered/handoff、无不安全动作及数据库断言；引用存在率单独统计。真实答案质量、人工金标准和费用仍需单独验收，不能只修改报告标签来取得发布资格。

旧清单和报告可以读取；缺少计划或结果不完整的历史版本不能重新通过部署门禁。`compare_retrieval` 和 `evaluate_tasks` 均输出 `release_eligible=false`，不能直接充作此处的发布证据。相关实现：[报告校验](../../app/platform/evaluation_contract.py)、[配置发布](../../app/platform/releases.py)。
