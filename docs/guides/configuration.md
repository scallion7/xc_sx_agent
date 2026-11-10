# 配置与运行

[文档中心](../README.md) · [当前验收](../evaluation/acceptance.md)

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
| `OIDC_IDENTITY_MAP_PATH` | 可选的已核验旧账号绑定文件；新账号默认按 issuer/sub 生成稳定内部 ID |
| `KNOWLEDGE_VERSIONED_ENABLED` | 启用 SQL 版本化知识，不代表已部署分布式向量库 |
| `CONVERSATION_INTAKE_ENABLED` | 默认 false；持久化多轮售后草稿与来源检查，演示用 `demo_stack --intake` |
| `ANSWER_EVIDENCE_CHECK_ENABLED` | 默认 false；保守原文分句支持规则，未支持结论转人工 |
| `COMPILED_RETRIEVAL_ENABLED` | 默认 false；按知识版本预编译 token 计数，原路径兼容 |
| `RUN_MAX_PENDING` / `RUN_MAX_PENDING_PER_USER` | 默认 0 不限制；生产需按测量设置，同步配置所有 API 实例 |
| `MODEL_BUDGET_ENABLED` | 启用共享累计 token 配额与调用账本 |
| `COLLABORATION_ENABLED` | 启用会话所有权和人工协作 |
| `TELEMETRY_ENABLED` | W3C Trace 与持久化指标；业务审计独立存在 |
| `MCP_IDENTITY_SECRET` | 连接器身份签名，真实部署使用独立密钥 |
| `RAG_BACKEND` | 默认 `cosine`；旧 `numpy` 别名兼容；`chroma` 已安全暂停 |

## 迁移与部署

### OIDC 身份升级与旧数据

OIDC 账号现在按已验证的完整 `iss` 和 `sub` 生成 `oidc_` 加 SHA-256 的内部 ID；`preferred_username` 只作为显示名。同名账号、长用户名或改名不会改变授权边界，签名主体中的路径字符也不会进入文件路径。issuer 按完整字符串精确校验，结尾的 `/` 不会自动删除。

**已有 OIDC 数据的部署应先核验账号绑定再升级。** 旧实现把显示名（最多 80 字符）作为账号 ID。未提供下面的显式映射时，升级后的账号会使用新的作用域，旧订单、会话和提案仍保留在旧 ID 下，不会自动转移或被同名账号认领。服务无法从旧用户名推断可信主体；历史上已经发生的同名混用需要人工核对，不能自动合并。

完成一致性备份并停止账号写入后，运维通过身份提供方及现有账号记录核验唯一的 issuer/sub，再准备受权限保护的文件，例如 `/etc/ecom/oidc-identities.json`：

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

设置 `OIDC_IDENTITY_MAP_PATH=/etc/ecom/oidc-identities.json` 并重启所有 API 实例。该映射按精确 issuer/sub 使用原内部 ID，因此现有订单、会话、确认提案及审计引用无需改写；用户改名仍访问原账户，其他同名主体不会继承它。对每个迁移账户验证历史会话、订单和权限，再恢复写入。映射是长期身份配置，应纳入受控备份并保持所有实例一致，不能在迁移后随意删除或修改。

文件必须使用上述严格格式，主体与旧 ID 均一对一；旧 ID 限于 1–80 位字母、数字、`_`、`-`，且不能占用 `oidc_` 前缀。重复主体、重复目标、路径字符或不可读文件会阻止服务启动，不会退回不安全的用户名映射。无法明确核验的旧账号继续保留隔离，待人工处理。

静态 Token 账号保留现有 ID，但不能使用 `oidc_` 命名空间。OIDC 模式不会启用或校验未使用的静态演示 Token；如切回 static，仍须保留绑定文件，静态账号与映射中的 OIDC 旧账号发生重叠时启动失败，需先移除冲突的静态凭据。自定义 AuthProvider 同样必须提供稳定、路径安全且不与其他身份来源混用的内部 ID。

只在明确指定的测试数据库上执行迁移演练：

```bash
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m app.scripts.manage_migrations upgrade
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m alembic check
DATABASE_URL=sqlite:////tmp/ecom-migration-demo.db python -m app.scripts.platform_smoke
```

旧库先做一致性备份，再执行迁移并核对业务行；非空新账本拒绝破坏性降级。完整 Compose 隔离部署与恢复已通过指定提交的 CI，范围见[当前验收](../evaluation/acceptance.md)；真实生产环境仍需独立验收。操作细节见[部署清单](../operations/deployment-checklist.md)，沙箱参数见[历史 HTTP 沙箱说明](../history/第26期-HTTP业务沙箱与退款对账.md)。

代码入口和配置别名的升级说明见[命名与兼容约定](../engineering/naming-and-compatibility.md)。旧索引、数据库及 `COLLABORATION_ENABLED` 配置键无需因展示或模块命名调整而迁移。
