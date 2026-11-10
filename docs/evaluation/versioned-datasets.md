# 版本化评测与失败案例回流

[文档中心](../README.md) · [当前验收](acceptance.md)

核验基线：2026-11-10，代码 `c5aee67`。数据流程为：任务记录 → 脱敏候选 → 人工整理 → 不可变数据版本 → 原文复核 → 开发集比较。每个环节分开记录状态，收集失败样本不会自动产生金标准；冻结数据版本也不代表知识或模型已发布。

## 浏览器流程

在 `/review` 登录有 `maintenance:run` 权限的账号，打开“数据版本与失败案例”：

1. 输入来源任务 ID 与分类，收集候选。分类支持 `retrieval`、`clarification`、`tool_selection`、`business_state`、`execution`、`other`。来源必须属于当前 tenant；保存脱敏后的用户消息、分类、run 状态、错误码、来源 run ID 与 payload 哈希，不复制工具响应或完整 payload。电话、邮箱、订单 ID 和常见凭据形式经过规则脱敏。
2. 核对脱敏内容并填写整理说明，保存后得到 `curated` 状态和新修订。必须人工检查残余姓名、地址或其他业务隐私；规则脱敏不能覆盖所有自由文本。
3. 准备完整数据集 JSON，冻结新版本。候选来源题必须携带 `candidate_id`、`candidate_revision`、`source_hash`，query 必须等于该修订的脱敏问题；未整理、错来源、错修订都会被拒绝。
4. 打开该版本，按[人工复核指南](../guides/review-workbench.md)核对原文。`requires_second_review: true` 的题目首次批准只记录首位审核，另一账号对相同内容确认后才变为 approved；修改内容会重新开始双人确认。
5. 导出审核记录或数据集。导入外部审核文件仍全部变为草稿，不能绕过双人审批。原冻结的 32 题和其哈希保持不变，项目 ID 留空仍打开原数据。原 review-v1 未设置双审标记，新建版本需自行明确哪些题要求双审。

候选捕获按 tenant、run ID、分类与来源 payload 哈希去重。同一来源快照重放返回已有候选；run 内容改变会产生新的来源哈希，不能把它当成旧候选的同一修订。列表最多返回最近 200 条候选。收集入口不要求 run 必须为 failed：业务回答有误但执行 completed 的任务也可以人工归类；运行状态不是质量标签。

候选整理采用 `expected_revision` 乐观并发。遇到 `CANDIDATE_VERSION_CONFLICT` 应重新读取并合并，不应仅递增本地版本号。`curated` 表示完成候选整理，发布后所有题目的 `review_status` 仍重置为 `pending_human_review`。

## 数据格式与隔离

```json
{
  "name": "after-sale-policy",
  "version": "v1",
  "documents": [{
    "document_id": "return-policy",
    "text": "未拆吊牌的服饰在签收七天内可申请退货。",
    "metadata": {"title": "演示政策", "source": "synthetic", "version": "1", "effective_date": "2026-01-01"}
  }],
  "cases": [{
    "id": "dev-return-1", "family": "dev-return", "order_family": "dev-order-1",
    "split": "development", "query": "服饰退货需要什么条件？", "day": "2026-11-10", "context": {},
    "outcome": "answerable", "gold_documents": ["return-policy"], "requires_second_review": true
  }],
  "candidate_ids": []
}
```

同一 tenant 的 name/version 唯一；同内容重放返回同一项目，不同内容不能覆盖旧版本。manifest 固定文档、题目与候选来源，读取时复验哈希。修改材料须创建新 version，审核历史绑定各自项目。

family、order_family 不能横跨 development/holdout；NFKC 规范化并去除标点后的相同问题也不能跨组。此规则检测显式泄漏，不能自动识别所有语义近重复，仍需要人工分组审查。题目上限 2000、文档上限 1000；服务端将序列化 JSON 限制为 400 万字符，浏览器上传文件另外限制为 4 MB。

## CLI 与开发集比较

令牌通过 `EVALUATION_ADMIN_TOKEN` 环境变量注入，不放在命令行参数或提交文件中。下列 URL 替换为演示启动器打印的实际地址，ID 替换为服务返回值：

```bash
mkdir -p test-results
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 datasets
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 capture RUN_ID --classification retrieval
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 candidates
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 curate CANDIDATE_ID curated.json
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 freeze dataset.json
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 --output test-results/dataset-export.json export PROJECT_ID
python -m app.scripts.compare_retrieval --dataset test-results/dataset-export.json --output test-results/version-comparison.json
```

`curated.json` 包含 `expected_revision`、脱敏后的 `query` 和非空 `notes`。`--url` 与 `--output` 是全局参数，应置于子命令之前；输出到目录时需先创建该目录。版本接口位于 `/v1/admin/evaluation/datasets`，候选接口位于 `/v1/admin/evaluation/candidates`；均要求维护权限并按 tenant 查询。

`manage_evaluation export` 调用 `/v1/admin/evaluation/datasets/{project_id}`，导出 manifest/documents/cases，**不包含审核记录**。审核导出从工作台“导出 JSON”或 `/v1/admin/evaluation/reviews/{project_id}/export` 获取，是另一个文件；导入审核也不会覆盖冻结的数据集。

比较脚本核对文档与题目哈希，只选择 development，保存配置/输入/源码哈希、逐题轨迹和压缩报告，始终 `release_eligible=false`。需要应用真实标签时传入匹配的全量审核文件：

```bash
python -m app.scripts.compare_retrieval \
  --dataset test-results/dataset-export.json \
  --review test-results/review-approved.json \
  --output test-results/version-comparison-reviewed.json
```

`--review` 不解锁保留集，也不把比较结果自动发布到知识库。内置 `review_evaluation --split holdout` 只读取原 review-v1；自定义版本在全量审核通过后可通过工作台逐题诊断 holdout，首次查看后应记录测试集已被使用。仓库当前没有真实人工批准材料或付费模型质量结果，详见[离线评测协议](offline-review.md)。

源码：[corpora.py](../../app/platform/corpora.py)、[reviews.py](../../app/platform/reviews.py)、[管理 CLI](../../app/scripts/manage_evaluation.py)、[比较入口](../../app/scripts/compare_retrieval.py)。
