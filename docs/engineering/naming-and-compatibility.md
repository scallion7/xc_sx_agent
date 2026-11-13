# 模块命名与兼容约定

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

代码入口按业务职责命名，旧名称通过薄兼容入口转发到同一实现。下述模块改名保留原接口与持久化格式；身份安全、任务状态和浏览器恢复另有升级要求，不能用“旧环境完全不变”概括全部修订。

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

## 命名升级保留的契约

- `RAG_BACKEND=numpy` 继续可用，无需仅因后端改名修改旧配置；新配置推荐 `cosine`。键名、索引路径和 embedding 模型校验保持不变。
- 旧 JSON 索引可以直接加载，不需要因改名重建。切换 embedding 模型仍必须重建索引，模型不匹配会拒绝检索。
- 知识工具返回的 `backend` 反映显式配置：旧配置返回 `numpy`，新默认返回 `cosine`，统计系统可将二者归为同一后端。
- `COLLABORATION_ENABLED`、数据库所有权字段和任务快照键保留，服务类改名不改变旧 Worker 读取的数据。
- 旧测试模块仍可显式运行；全量 `discover` 只运行职责命名的模块一次，避免测试数量虚增。兼容别名暂无移除计划，移除前应单独发布迁移说明。

已发布 Alembic 文件、revision/down_revision、`STAGE26_DOWNGRADE_BLOCKED` 错误码及历史证据名称保留。仓库 `xc_sx_agent`、默认数据库、Compose 用户及库名、已有 API 路径不因职责改名而迁移。`.stage-deps/` 继续忽略，防止本地依赖被纳入提交。当前运行代码和 CI 临时数据使用职责名称，文档链接检查维护主题导航。

## 身份升级：稳定主体与显式旧账号映射

OIDC 默认用户标识为 `oidc_` 加上精确 `[issuer, sub]` 的 SHA-256。签名、issuer、audience、算法、有效期和所需 scope 仍由认证层核验；`preferred_username` 只作为显示名，不参与数据归属。显示名同名、改名、超长或带路径字符均不会改变稳定主体身份。

旧版按用户名产生的账号不会自动迁移。需要保留其数据访问时，管理员必须先从身份提供方核实主体与旧账号的对应关系，再配置 `OIDC_IDENTITY_MAP_PATH`。文件格式为版本 1 的 `bindings`，每条明确给出 `issuer`、`subject`、`user_id`；重复主体、重复目标、非法路径字符或占用 `oidc_` 命名空间均拒绝启动。

这张映射直接让已核实主体继续使用旧 `user_id`，无需批量改写订单、会话或 action 引用。未映射的数据仍保留，但新生成身份不能自动访问。静态认证禁止占用 `oidc_` 和已映射目标，防止跨认证方式碰撞；纯 OIDC 模式不会因为未启用的静态演示账号而拒绝合法映射。所有实例必须使用一致映射，切回静态认证时也应保留对应限制。历史上已混入同一旧账号的数据需要人工核对，不可按显示名自动拆分。配置步骤见[配置指南](../guides/configuration.md)。

## 状态及协议的增量变化

| 使用方 | 当前要求 |
|---|---|
| 普通提案调用方 | 拒绝、失效或过期后可生成新 action；旧 ID 及确认不恢复。提案去重不替代执行中或结果未知业务的核对 |
| run 状态消费者 | 识别终态 `resolved` 和事件 `run.resolved`，显示为已核对结案；它不等于业务成功，也不触发自动重放 |
| run 管理调用方 | 以当前版本和所有写调用的证据哈希结案；旧日志缺少可绑定回执时保持阻断，不能只按 `call.completed` 放行 |
| 工具结果消费者 | 数值、布尔类型与合法 action 控制引用在结构化脱敏后保留；自由文本仍可能被脱敏 |
| 同步 chat 调用方 | `/v1/chat`、`/v1/chat/stream` 继续存在；需要持久化队列、恢复与 SSE 游标时使用 `/v1/runs` |

数据库迁移和扩展说明见[对话与索引扩展](improvement-loop.md)，run 恢复依据见[可信执行](reliable-execution.md)。

## 浏览器请求、身份与重试

浏览器先通过 `GET /v1/auth/session` 使用当前令牌认证。服务端按部署租户、身份提供方、OIDC issuer、主体和内部用户 ID 生成存储标识，返回值设置 `Cache-Control: no-store`。同一主体续签或更改显示名后，仍能找到原标签页保存的请求键、请求体和任务游标。其他租户和其他主体使用独立的存储标识。业务写请求仍独立验证当前凭据和权限。

升级后的页面在旧凭据仍有效时，将该凭据对应的旧槽位移入新作用域。若升级前凭据已失效且仅有旧格式回执，运维应先核对服务端任务或消息记录，再决定是否提交。历史键中缺少可验证的主体信息。

同槽位、同内容的并发写共享进行中的请求；内容不同则提示先核对原请求。成功响应及明确的参数拒绝会清除待确认项。之前结果未知的写入遇到 401 或 403 时继续保留，方便同主体在恢复授权后核对。409 只对明确表示 CAS 未提交的 `INTAKE_VERSION_CONFLICT` 清除；未知 409、5xx 和传输中断保留原键及原内容。售后页面的“重试上次提交”直接重放保存的 payload，包括原修订，服务端先检查回执再检查版本。

workspace 提交 run 时固定 token、会话和消息；异步返回后只更新仍属于当前范围的界面。页面刷新后重新提交相同内容可复用原请求键；切换 token 或会话会停止旧 SSE 监听。`run_stream.js` 按经验证的主体、会话及 run 保存事件游标，重连读取进度，不重新执行业务。客服工作台用全局 `busy` 覆盖操作及刷新全过程，详见[人工接管](human-handoff.md)。

这些缓存只属于当前浏览器会话存储，不提供跨设备恢复、清除浏览器数据后的请求找回或跨标签页互斥。服务端回执、任务和业务状态始终是核对依据。

## 职责拆分与验证入口

`app.api` 保留应用工厂、ASGI `app` 和旧路由注册函数导出；ASGI 默认应用在访问时初始化，导入工厂不触发数据库连接。路由按职责位于 `app.web`，公共响应、分页和 OpenAPI 契约位于 `contracts`。

`KnowledgePlatform` 与 `KnowledgeWorker` 仍从 `app.platform.knowledge` 导入。解析与切块在 `knowledge_documents`，导入任务在 `knowledge_ingestion`，发布及索引生命周期在 `knowledge_releases`；原公共类型和函数继续导出。

回归入口包括[命名兼容](../../tests/test_naming_compatibility.py)、[OIDC 身份](../../tests/test_oidc_identity.py)、[可信提案](../../tests/test_trusted_actions.py)、[run 结案](../../tests/test_run_reconciliation.py)、[浏览器重试](../../tests/test_browser_retries.py)和[售后页面](../../tests/test_intake_browser.py)。已运行结果、环境与限制统一见[当前验收](../evaluation/acceptance.md)。
