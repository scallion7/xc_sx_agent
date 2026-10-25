# 模块命名与兼容约定

本轮按业务职责统一代码入口。配置键、HTTP 路径、业务表、会话与索引文件格式保持兼容；旧名称通过薄兼容入口转发到同一实现，不维护两套业务逻辑。

## 当前入口与旧名称

| 旧入口 | 当前推荐入口 | 兼容方式 |
|---|---|---|
| `app.agent.rag.backends.numpy_backend.NumpyBackend` | [cosine_backend.CosineBackend](../../app/agent/rag/backends/cosine_backend.py) | 同一个类的别名，构造参数和 JSON 索引格式不变；包级导出也保留 |
| `RAG_BACKEND=numpy`、`--backend numpy` | `RAG_BACKEND=cosine`、`--backend cosine` | 仍接受旧值；默认值和示例改为 `cosine` |
| `numpy_backend._cosine` | [similarity.cosine_similarity](../../app/agent/rag/similarity.py) | 同一个函数的别名；JSON 检索与 SQL 混合检索共用实现 |
| `app.platform.collaboration.Collaboration` | [conversation_handoff.ConversationHandoffService](../../app/platform/conversation_handoff.py) | 旧模块转发，旧类名为同一类的别名 |
| `app.platform.connectors.GovernedRemote` | [connectors.GovernedMCPToolCaller](../../app/platform/connectors.py) | 同一类的别名，保留可调用接口 |
| `tests.test_stage_22_quality_gate` | [tests.test_operations_quality](../../tests/test_operations_quality.py) | 旧模块显式运行和测试类导入兼容 |
| `tests.test_secondary_development` | [tests.test_application_integration](../../tests/test_application_integration.py) | 同上 |
| `tests.test_strategy_development` | [tests.test_plan_execution](../../tests/test_plan_execution.py) | 同上；计划评测数据集引用已更新 |
| `tests.test_agent_deepening` | [tests.test_after_sale_policy](../../tests/test_after_sale_policy.py) | 同上 |

`CosineBackend` 使用 Python 标准库进行精确余弦计算，不依赖 NumPy，不是近似向量索引。`ConversationHandoffService` 管理客服接管、所有权 CAS 与人工消息，区别于 `app/multi_agent/` 中的模型路由。`GovernedMCPToolCaller` 校验 MCP 连接器契约、权限和返回数据；远端写操作仍必须走持久化执行通道。

## 已有环境如何升级

- 旧 `.env` 无需立即修改，`RAG_BACKEND=numpy` 继续可用；新配置推荐 `cosine`。`RAG_BACKEND` 键名、索引路径和 embedding 模型校验保持不变。
- 旧 JSON 索引可以直接加载，不需要因改名重建。切换 embedding 模型仍必须重新构建索引；模型不匹配会继续拒绝检索。
- 知识工具返回的 `backend` 字段仍反映显式配置：旧配置返回 `numpy`，新默认配置返回 `cosine`，二者含义相同。消费该字段的统计系统应将两者归为同一后端。
- `COLLABORATION_ENABLED`、数据库中的所有权字段和任务快照键保留。服务类改名不改变旧 Worker 读取的数据。
- 旧测试模块可通过 `python -m unittest tests.test_stage_22_quality_gate` 或具体类/方法路径调用。全量 `discover` 只运行职责命名的模块一次，避免测试数量虚增。
- 兼容别名暂无移除计划。后续若移除，应先发布迁移说明并单独进行版本化变更。

## 特意保留的历史名称

- 已发布的 Alembic 迁移文件（包括 `20260823_0001_stage22_baseline.py`）、revision/down_revision、`STAGE26_DOWNGRADE_BLOCKED` 错误码保持原样，避免破坏数据库升级与运维自动化。
- 仓库 `xc_sx_agent`、默认数据库、Compose 用户/库名、API 路径、协议字段保持原样，已有部署无需搬迁数据。
- 历史分期文档、旧链接兼容页和原始证据保留当时命名与测试结果。
- `.stage-deps/` 的忽略配置保留，避免已有本地依赖目录被意外纳入代码检查或提交。

当前运行代码、说明性注释、测试数据与 CI 临时数据库使用职责名称。CI 使用文档链接检查维护主题导航，不再通过检查分期文档是否存在作为质量门禁。

## 验证方式

```bash
python -m unittest tests.test_naming_compatibility -v
python -m app.scripts.quality_platform --browser
python -m app.scripts.validate_plan_quality
```

兼容回归覆盖独立构造的旧格式索引、新写旧读、配置别名、embedding 模型不匹配、旧导入以及旧测试入口。完整业务回归继续验证人工接管、可信确认、MCP 契约、HTTP 退款和恢复；数据库升级/漂移检查用于确认命名修改未改变数据库契约。实际结果见[验收状态](../evaluation/acceptance.md)。
