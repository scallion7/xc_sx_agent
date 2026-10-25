# 配置与运行

所有命令从仓库根目录执行。推荐 Python 3.11；CI 另覆盖 Python 3.10–3.12。当前项目展示名为 Ecom Service Agent，目录和数据库仍沿用 `xc_sx_agent` 以保持兼容。

## 三种使用方式

| 方式 | 用途 | 入口 |
|---|---|---|
| 隔离演示 | 面试、业务流程展示，无真实模型费用 | `python -m app.scripts.demo_stack` |
| 自动化测试 | 协议、业务、迁移、浏览器回归 | `python -m app.scripts.quality_platform --browser` |
| 自行配置服务 | CLI/API/Worker、真实兼容模型或外部业务联调 | `.env.example` 与部署清单 |

演示启动器注入自己的端口、数据库、令牌与受控模型 URL。自行配置服务时需要明确准备相应凭据和数据库，不能直接套用演示令牌。

## 安装与测试

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip check
python -m playwright install chromium
RUN_BROWSER_E2E=1 python -m coverage run -m unittest discover -s tests
python -m coverage report --fail-under=65
python -m app.scripts.validate_openapi
```

测试应在干净检出中运行，让各项测试自行配置隔离数据库。不要给整个测试进程设置全局 `DATABASE_URL`；它可能覆盖迁移测试创建的数据库目标。独立 OpenAPI 校验如需绕开旧默认数据库，可仅对该命令设置 `DATABASE_URL=sqlite:///:memory:`。

`requirements-lock.txt` 是已验收的 Python 3.11 完整依赖闭包；`requirements.txt` 为运行依赖范围。两者均通过 CI 审计。手动安装旧 Chroma 会偏离支持范围。

## 主要配置

完整字段与默认值以 [.env.example](../../.env.example) 和 [Settings](../../app/config/settings.py) 为准。

| 配置 | 作用与注意事项 |
|---|---|
| `DATABASE_URL` / `DATABASE_AUTO_CREATE` | 正式服务先迁移；旧数据库不靠自动建表升级 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 自行接模型时设置；勿提交密钥 |
| `API_USER_TOKENS` / `API_ADMIN_TOKENS` | 本机演示之外需使用自己的身份配置 |
| `KNOWLEDGE_VERSIONED_ENABLED` | 启用 SQL 版本化知识，不代表已部署分布式向量库 |
| `MODEL_BUDGET_ENABLED` | 启用共享累计 token 配额与调用账本 |
| `COLLABORATION_ENABLED` | 启用会话所有权和人工协作 |
| `TELEMETRY_ENABLED` | W3C Trace 与持久化指标；业务审计独立存在 |
| `MCP_IDENTITY_SECRET` | 连接器身份签名，真实部署使用独立密钥 |
| `RAG_BACKEND` | 默认 `cosine`；旧 `numpy` 别名兼容；`chroma` 已安全暂停 |

## 迁移与部署

只在明确指定的测试数据库上执行迁移演练：

```bash
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m app.scripts.manage_migrations upgrade
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m alembic check
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m app.scripts.platform_smoke
```

旧库先做一致性备份，再执行迁移并核对业务行；非空新账本拒绝破坏性降级。Docker 构建与扫描已通过 CI，但完整 Compose 服务部署仍须单独验证。操作细节见[部署清单](../operations/deployment-checklist.md)，沙箱参数见[历史 HTTP 沙箱说明](../history/第26期-HTTP业务沙箱与退款对账.md)。

代码入口和配置别名的升级说明见[命名与兼容约定](../engineering/naming-and-compatibility.md)。旧索引、数据库及 `COLLABORATION_ENABLED` 配置键无需因本轮改名迁移。
