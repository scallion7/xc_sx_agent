# 配置与运行

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md) · [部署清单](../operations/deployment-checklist.md)

以下命令从仓库根目录执行，Shell 示例使用 Bash/Zsh。推荐 Python 3.11；CI 另外验证 Python 3.10 和 3.12。展示名为 Ecom Service Agent，仓库、部分路径与数据库仍使用 `xc_sx_agent`。

## 选择运行方式

| 方式 | 命令入口 | 实际运行的内容 |
|---|---|---|
| 本机业务演示 | `python -m app.scripts.demo_stack --intake` | 隔离 SQLite、API、Agent Worker、受控 HTTP 模型、库存 MCP；商务 Provider 为 mock |
| 代码与协议检查 | `python -m app.scripts.quality_platform` | Ruff、编译、覆盖率回归、OpenAPI、文档链接与计划质量门禁 |
| 浏览器回归 | `python -m app.scripts.quality_platform --browser` | 上述检查，并启用需要 Chromium 的测试 |
| 完整部署与恢复 | `python -m app.scripts.accept_deployment --output test-results/deployment-01/deployment.json` | Docker Compose、PostgreSQL、双 Agent Worker、独立退款 Worker、HTTP 商务沙箱与恢复演练 |
| 自行配置服务 | `.env.example`、`run_api`、`run_worker` | 读取部署环境；真实模型、身份与业务适配器须自行接入 |

本机演示不调用付费模型，也不启动独立 HTTP 退款沙箱。`--intake` 只开启 `/workspace` 的多轮售后草稿；`/after-sale` 表单页面不依赖这个开关。演示与完整 Compose 的令牌、订单及端口不同，分别按各自指南使用。

## 安装与验证

Python 3.11 的可复现安装：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip check
python -m app.scripts.quality_platform
```

Windows PowerShell 创建与激活环境使用 `py -3.11 -m venv .venv` 和 `.venv\Scripts\Activate.ps1`；随后执行相同的 `python` 命令。Python 3.10/3.12 的兼容性检查按 CI 安装 `requirements-dev.txt`。该文件使用支持范围依赖；`requirements-lock.txt` 固定 Python 3.11 的完整闭包，包含测试与格式工具。

浏览器文件不包含在 Python 依赖中，首次另外安装：

```bash
python -m playwright install chromium
python -m app.scripts.quality_platform --browser
```

Linux CI 使用 `python -m playwright install --with-deps chromium` 同时安装系统依赖。未设置 `RUN_BROWSER_E2E=1` 且未传 `--browser` 时，相应浏览器测试会按配置跳过；不能把跳过记为浏览器验收通过。需要与 CI 对照时，以[当前验收](../evaluation/acceptance.md)的提交、作业及覆盖率口径为准。

在干净检出中运行测试，让用例自行创建临时数据库；不要给整个测试进程注入业务 `DATABASE_URL` 或沿用生产 `.env`。`quality_platform --database` 会在同一进程环境的完整检查后增加迁移、drift 和平台 smoke；它不是只读检查，也不是独立数据库选择器。推荐把数据库验收拆成下文的专用命令。

## 配置来源与主要开关

[Settings](../../app/config/settings.py) 从环境变量与仓库根目录 `.env` 读取配置；完整示例见 [.env.example](../../.env.example)。演示启动器覆盖端口、模型 URL、令牌和数据库等关键项，但仍继承进程环境；演示和测试应使用独立环境，不混入生产身份配置。

| 配置 | 当前行为 |
|---|---|
| `PERSISTENCE_BACKEND` | 默认 `sql`；持久化任务队列和平台功能要求 SQL |
| `DATABASE_URL` / `DATABASE_AUTO_CREATE` | 默认本地 SQLite / true；部署先迁移，再将自动建表关闭。自动建表不负责升级旧结构 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME` | 真实兼容模型连接参数；默认普通启动可能访问外部服务，离线演示请使用 `demo_stack` |
| `AUTH_MODE` | `static` 或 `oidc`；OIDC JWT 也通过 HTTP `X-API-Key` 头传入 |
| `API_USER_TOKENS` / `API_ADMIN_TOKENS` | static 模式的 `token:user_id` 列表；演示值不用于部署 |
| `PROVIDER_MODE` | `mock`、`sandbox` 或 `external`；external 只声明模式，不自动提供真实业务适配器 |
| `KNOWLEDGE_VERSIONED_ENABLED` / `KNOWLEDGE_EMBEDDING_MODE` | 默认 false / `local_hash`；版本化知识与本地确定性 embedding，不等同于分布式向量库或真实语义质量 |
| `CONVERSATION_INTAKE_ENABLED` | 默认 false；开启持久化对话草稿及字段来源检查 |
| `ANSWER_EVIDENCE_CHECK_ENABLED` | 默认 false；保守的原文分句支持检查，无法支持的结论转人工 |
| `COMPILED_RETRIEVAL_ENABLED` | 默认 false；按知识版本预编译 token 计数，保留原检索路径 |
| `RUN_MAX_PENDING` / `RUN_MAX_PENDING_PER_USER` | 默认 0，即不限制；统计 queued/running/retry_wait，所有 API 实例须使用一致值 |
| `RUN_LEASE_SECONDS` / `RUN_MAX_ATTEMPTS` / `RUN_POLL_SECONDS` | 默认 30 秒 / 3 次 / 1 秒；与计划步骤自身的租约配置分开 |
| `MODEL_BUDGET_ENABLED` | 默认 false；共享累计 token 配额和调用账本，不按日自动重置 |
| `COLLABORATION_ENABLED` | 默认 false；会话接管、人工消息及新自动调用阻断 |
| `TELEMETRY_ENABLED` / `OBSERVABILITY_ENABLED` | 前者启用平台 Trace/指标，后者控制本地事件日志；二者不是同一开关 |
| `MCP_IDENTITY_SECRET` | 非空时至少 16 字符，用于连接器身份签名 |
| `RAG_BACKEND` | 默认 `cosine`；旧 `numpy` 别名兼容，`chroma` 已暂停支持 |

多项平台功能要求 SQL；production 还要求关闭自动建表、关闭演示数据、提供非占位模型密钥与外部 Provider 配置。单独设置 `ENVIRONMENT=production` 不会完成外部接入，详见[部署清单](../operations/deployment-checklist.md)。

## OIDC 配置和旧账号迁移

默认适配器要求 `OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_JWKS_PATH`。`OIDC_REQUIRED_SCOPES` 和 `OIDC_ALGORITHMS` 使用空格分隔；默认分别为 `chat:use`、`RS256`，JWKS 缓存默认 300 秒。默认 JWKS 加载器读取本地 JSON 文件；若需要远程刷新，使用自定义 `OidcJwtProvider` 并通过 `create_app(auth_provider=...)` 注入。

内部账号 ID 来自经验证的完整 `iss` 与 `sub`：`oidc_` 加 SHA-256。`preferred_username` 只作为显示名，不授予历史账号权限。同名、改名、长用户名及主体中的路径字符都不会改变这条授权规则；issuer 按完整字符串精确校验，末尾 `/` 不自动删除。

**已有 OIDC 数据须先核验绑定再升级。** 旧实现以显示名截取到 80 字符作为账号 ID。未提供显式绑定时，新版会使用新的账号作用域，旧订单、会话和提案保留在旧 ID 下。系统不会根据用户名自动认领或合并旧数据。

完成一致性备份、暂停相关账号写入后，由运维结合身份提供方和原账号记录核验唯一主体，准备受权限保护的文件：

```json
{
  "version": 1,
  "bindings": [
    {
      "issuer": "https://identity.example",
      "subject": "verified-stable-subject",
      "user_id": "legacy-user-1"
    }
  ]
}
```

设置 `OIDC_IDENTITY_MAP_PATH=/etc/ecom/oidc-identities.json`，在所有实例加载同一份文件并重启，再核对迁移账户的历史订单、会话与权限。文件按精确 issuer/sub 继续使用原内部 ID，不改写历史业务引用；它属于长期身份配置，须纳入受控备份，不能在迁移后随意删除。

格式严格要求版本 1 和 `bindings` 数组；主体、目标旧 ID 均一对一。旧 ID 只允许 1–80 位字母、数字、`_`、`-`，不能占用 `oidc_` 前缀。重复主体/目标、路径字符、额外字段或不可读文件会阻止启动。无法核验的旧账号保持隔离。

static 账号也不能使用 `oidc_` 命名空间。OIDC 模式不启用未使用的静态演示令牌；切回 static 时仍保留绑定文件，静态账号若占用已绑定的 OIDC 旧 ID 会启动失败。自定义认证适配器须同样使用稳定、路径安全且来源不冲突的内部 ID。

## 数据库和独立进程

以下仅是新建的可丢弃 SQLite 示例；每条命令明确限定目标，不把该 URL 全局传给测试：

```bash
DATABASE_URL=sqlite:////tmp/ecom-migration-demo-01.db DATABASE_AUTO_CREATE=false python -m app.scripts.manage_migrations upgrade
DATABASE_URL=sqlite:////tmp/ecom-migration-demo-01.db DATABASE_AUTO_CREATE=false python -m alembic check
DATABASE_URL=sqlite:////tmp/ecom-migration-demo-01.db DATABASE_AUTO_CREATE=false python -m app.scripts.database_smoke
DATABASE_URL=sqlite:////tmp/ecom-migration-demo-01.db DATABASE_AUTO_CREATE=false python -m app.scripts.platform_smoke
```

`upgrade` 默认升至 head；`manage_migrations check` 判断修订是否到 head，`alembic check` 判断模型与迁移是否有结构漂移，两者用途不同。smoke 会写入合成记录，只在隔离数据库运行；平台 smoke 和任务核对 smoke 清理各自作用域的临时账本，不删除真实审计。

自行配置好同一数据库、模型与业务依赖后，在不同终端/进程运行：

```bash
python -m app.scripts.run_api
python -m app.scripts.run_worker
```

只有配置 `PROVIDER_MODE=sandbox` 并启动独立商务沙箱后，才增加 `python -m app.scripts.run_refund_worker --mode both`。Agent Worker 和退款 Worker 不是同一进程。真实 external Provider 需要在 API 与 Worker 的应用装配入口中一致注入，默认 CLI 不会自动发现第三方适配器。

旧库先备份再迁移到 head（当前 `20261109_0009`）。业务非空时的破坏性降级受保护；备份、恢复和人工核对操作见[部署清单](../operations/deployment-checklist.md)与[告警恢复手册](../operations/incident-runbook.md)。兼容别名见[命名约定](../engineering/naming-and-compatibility.md)。
