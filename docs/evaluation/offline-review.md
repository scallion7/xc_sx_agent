# 离线评测与人工复核

[文档中心](../README.md) · [当前验收](acceptance.md)

2026-11-13 更新。默认检索诊断使用本地哈希投影，Agent 开发回归使用受控 HTTP 模型。另有显式启用并限额的真实聊天模型入口。当前已有的实测证据均为离线或受控运行。

## 数据集和当前状态

| 数据 | 用途与边界 |
|---|---|
| [review-v1](../../app/evaluation/review/) | 18 份合成政策、32 题，16 条 development、16 条 holdout，按 16 个 family 隔离；SHA256 manifest 固定原文和题目 |
| [任务契约集](../../app/evaluation/tasks/) | 6 个合成开发场景 × 3 种策略，检查真实 HTTP/Worker/SQL 终态；见[任务基线](task-baseline.md) |
| [Agent 政策质量开发集](../../app/evaluation/platform/agent-quality-v1.json) | 9 个不同问题、9 个业务家族。检验五类可回答政策以及无证据、地域/渠道缺失、政策冲突的处理 |
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

## Agent 回答的分批评测

默认 `evaluate_platform` 使用独立临时 SQLite、独立租户和模拟业务服务，运行结束后清理。指定 `--release-id` 时，从配置的源数据库按 tenant 只读复制该配置固定的知识快照及当前删除标记。评测中的会话、模型账本和工具调用只写入临时库。未指定配置时加载演示语料，报告标明 `diagnostic_only`。固定输入见[Agent 政策质量开发集](../../app/evaluation/platform/agent-quality-v1.json)。

可在一个终端启动按实际检索片段作答的受控服务，另一个终端运行矩阵。选择本机空闲端口，以下示例使用 9130。

```bash
DEMO_MODEL_POLICY_QUOTES=1 python -m uvicorn app.scripts.demo_model:app --host 127.0.0.1 --port 9130
```

```bash
export OPENAI_BASE_URL=http://127.0.0.1:9130/v1 OPENAI_API_KEY=controlled
python -m app.scripts.evaluate_platform --agents --max-runs 9 \
  --output test-results/policy-quality.json
python -m app.scripts.evaluate_platform --agents --max-runs 18 --resume \
  --output test-results/policy-quality.json
```

默认矩阵为 9 题 × ReAct、Plan-and-Execute、multi 三种策略，共 27 格。每次 `--max-runs` 限制本次新增格数，按 family 交错。每个已完成格先保存带指纹的压缩原件，再原子替换摘要。相同输出同时只允许一个进程写。续跑验证代码、模型端点与配置、知识快照、数据与审核哈希、原始响应和已有行，直接跳过已测格。源或参数变化时使用新文件。失败格保留在原始结果中。每批 token 上限独立结算，预算耗尽或矩阵缺项时不能发布。若进程在某格付费调用期间异常终止，最后一个尚未记入报告的格可能需要重测，先核对供应商账单。

可回答题逐格核对必需事实、所有回复分句、引用的文档/版本/位置/原文和冻结适用条件。实际 Agent 检索采用题目日期，题目给定的条件加入用户输入；缺失的条件仍由正常工具执行路径处理。应转人工或补信息的题，同时检查实际检索原因、结构化状态和用户收到的说明。原文规则对自由改写较严格，跨句蕴含与真实用户满意度仍需要人工判断。每格同时检查工单、退款操作及退款记录的数量变化。评分与源码见[质量评分](../../app/platform/agent_quality.py)、[Agent 执行器](../../app/platform/agent_eval_driver.py)。

## 从审核材料到配置发布

新版本数据由复核工作台冻结，`--dataset` 接受包含 manifest、documents、cases 的完整导出，`--review` 接受同一项目的 schema-v2 审核导出。CLI 按 tenant 与源库的实际审核记录逐项比对，批准的原文引用作为关键事实，带双审标记的题仍需两位账号确认。原 review-v1 的 32 题尚未完成人工审核，16 题原始 holdout 保持未运行。

使用已有知识发布 ID 和实际 Git 提交创建待测配置。以下命令在已配置的隔离验证库内工作，`KNOWLEDGE_RELEASE_ID`、`COMMIT` 对应该库已就绪的知识版本及当前 40 位提交。首条命令返回的 `release_id` 填入 `CONFIG_ID`。

```bash
python -m app.scripts.publish_configuration --prepare-draft \
  --knowledge-release-id "$KNOWLEDGE_RELEASE_ID" --code-commit "$COMMIT"
python -m app.scripts.evaluate_platform --agents --release-id "$CONFIG_ID" \
  --max-runs 27 --token-budget 100000 --output test-results/config-quality.json
python -m app.scripts.publish_configuration --report test-results/config-quality.json
```

审核通过后，如需真实模型测试，在准备草稿和评测时使用相同的 `--dataset version-export.json --review review-approved.json --split holdout`；评测时再加 `--live`。须准备对应该版本的目标模型凭据和模型目录，真实模式每次最多 10 个新格、20000 个聊天 tokens。可按上述方式 `--resume`。真实模式检查实际模型响应，收到项目受控服务指纹或缺少模型响应时拒绝生成真实验收结果。当前评测只支持本地哈希知识向量，外部 embedding 计费未纳入该预算，CLI 会在首次请求前拒绝相应配置。真实人工审核、真实模型调用及原 holdout 结果须以实际运行证据记录。

发布命令从摘要定位同目录原件，复算 SHA256、逐格评分、数据与知识指纹，通过后将原草稿标为已评估。可用 `--deploy --expected-version N` 指定预期部署修订。生产放行需要完整 v3 矩阵、真实模式以及仍有效的全量 holdout 审核；旧 v2 工程报告仅用于兼容的非生产验证。CLI、发布入口及独立临时库回归见[评测命令](../../app/scripts/evaluate_platform.py)、[发布命令](../../app/scripts/publish_configuration.py)、[端到端测试](../../tests/test_agent_quality_cli.py)。
