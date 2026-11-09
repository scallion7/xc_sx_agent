# 版本化评测与失败案例回流

[文档中心](../README.md) · [当前验收](acceptance.md)

数据流程为：任务记录 → 脱敏候选 → 人工整理 → 不可变数据版本 → 原文复核 → 开发集比较。每个环节分开记录状态，收集失败样本不会自动产生金标准；冻结数据版本也不代表知识或模型已发布。

## 浏览器流程

在 `/review` 登录有 `maintenance:run` 权限的账号，打开“数据版本与失败案例”：

1. 输入来源任务 ID 与问题分类，收集候选。仅提取用户消息、失败分类、运行状态、来源哈希；不复制工具响应或完整运行 payload。自动脱敏电话、邮箱、订单 ID 和常见凭据形式。
2. 核对脱敏内容并填写整理说明，保存后得到 `curated` 状态和新修订。必须人工检查残余姓名、地址或其他业务隐私；规则脱敏不能覆盖所有自由文本。
3. 准备完整数据集 JSON，冻结新版本。候选来源题必须携带 `candidate_id`、`candidate_revision`、`source_hash`，query 必须等于该修订的脱敏问题；未整理、错来源、错修订都会被拒绝。
4. 打开该版本，按[人工复核指南](../guides/review-workbench.md)核对原文。`requires_second_review: true` 的题目首次批准只记录首位审核，另一账号对相同内容确认后才变为 approved；修改内容会重新开始双人确认。
5. 导出审核记录或数据集。导入外部审核文件仍全部变为草稿，不能绕过双人审批。原冻结的 32 题和其哈希保持不变，项目 ID 留空仍打开原数据。

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
    "split": "development", "query": "服饰退货需要什么条件？", "day": "2026-11-09", "context": {},
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
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 capture RUN_ID --classification retrieval
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 candidates
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 curate CANDIDATE_ID curated.json
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 freeze dataset.json
python -m app.scripts.manage_evaluation --url http://127.0.0.1:8000 --output dataset-export.json export PROJECT_ID
python -m app.scripts.compare_retrieval --dataset dataset-export.json --output test-results/version-comparison.json
```

`curated.json` 包含 `expected_revision`、脱敏后的 `query` 和 `notes`。版本接口位于 `/v1/admin/evaluation/datasets`，候选接口位于 `/v1/admin/evaluation/candidates`；均要求维护权限并按 tenant 查询。

比较脚本核对导出数据哈希，只比较 development，生成配置哈希、输入哈希、逐题轨迹与压缩原始报告，不自动推广配置。只有真实完整审核文件才可用 `--review`；仍需遵守[保留集协议](offline-review.md)。本次开发没有批准真实标签，也没有运行当前冻结 holdout 或调用付费模型。

源码：[corpora.py](../../app/platform/corpora.py)、[reviews.py](../../app/platform/reviews.py)、[管理 CLI](../../app/scripts/manage_evaluation.py)、[比较入口](../../app/scripts/compare_retrieval.py)。
