# 知识、模型与发布治理

## 知识生命周期

导入 MD/TXT/文本 PDF 后，任务持久化经历解析、切分、embedding 和 ready 阶段。内容哈希支持去重与增量向量缓存；发布先构建不可变快照，再通过 expected_version 切换活动版本。支持撤回、保留期内回滚、删除屏蔽和受引用保护的索引 GC。

检索组合中文 BM25、余弦相似度和 RRF，可启用词法重排；返回稳定 chunk/version/release 引用。默认本地哈希投影是词法表示，词法 reranker 不是 cross-encoder。未将合成数据上的结果视为真实语义效果。

默认旧知识检索后端仍叫 `NumpyBackend`，实际为标准库精确余弦计算；第一轮文档整理保留此代码名和配置别名。Chroma 已因安全公告暂停，不推荐绕过停用保护。

## 模型与配置

- 模型能力与路由由目录配置，SQL 账户控制跨进程 RPM、并发和累计 token 配额。
- 每次实际模型尝试单独记账；缺少 usage 或结果不确定时保守预留并进入待对账，不把估算冒充账单。
- 配置 manifest 固定模型、Prompt、工具 schema、知识版本、风险规则、数据集和策略等信息；新会话稳定分桶，旧会话保留既有版本。
- 配置发布经过评测门禁；生产模式要求真实模型与人工复核证据，当前合成报告不能满足这一门禁。
- 模型调用账本不包含真实 embedding 服务的全部外部费用，接入时需另行管理预算。

## MCP 与人工协作

连接器按 allowlist、契约/schema hash、权限、签名身份和输出限制管理；schema 漂移停用。库存写操作经可信确认与专用持久化适配器执行，重试前查单。客服工作台提供会话接管、草稿修订、工单审批和知识发布入口。

实现入口：[knowledge.py](../../app/platform/knowledge.py)、[retrieval.py](../../app/platform/retrieval.py)、[budget.py](../../app/platform/budget.py)、[releases.py](../../app/platform/releases.py)、[connectors.py](../../app/platform/connectors.py)。

详细参数与历史实现见[第 27–34 期归档](../history/第27-34期-知识平台与工程交付.md)，当前验证范围见[验收状态](../evaluation/acceptance.md)。
