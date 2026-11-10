# Ecom Service Agent｜电商售后智能体平台

[![Offline Quality](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml/badge.svg)](https://github.com/scallion7/xc_sx_agent/actions/workflows/offline-quality.yml)

面向订单咨询与售后办理的 Agent 应用。用户可以查询订单、获取政策证据、分轮补充申请并确认具体操作；客服可以接管会话、审批工单、管理知识版本与核对异常任务。

项目将模型推理与确定性业务执行分开：LLM 负责理解、规划和解释，服务端负责身份校验、操作确认、权限、幂等、状态转换与审计。演示使用合成业务数据和受控模型，可在本地复现 API、Worker、数据库、MCP 与浏览器之间的完整交互。

[快速演示](docs/guides/five-minute-demo.md) · [系统架构](docs/architecture/overview.md) · [验收证据](docs/evaluation/acceptance.md) · [文档中心](docs/README.md) · [维护者与工程演进](docs/CONTRIBUTIONS.md)

![客户服务界面](docs/assets/workspace.png)

*本地受控模型演示截图，页面由真实 API 和 Worker 驱动；不代表真实模型服务或线上业务。*

## 业务流程

```mermaid
flowchart LR
    A[咨询与补充申请] --> B[订单查询与政策证据]
    B --> C[具体操作提案]
    C --> D[用户确认参数]
    D --> E[权限与业务规则]
    E --> F[工单与持久化任务]
    F --> G[执行与对账]
    E --> H[人工审批与接管]
    G --> I[结果与审计]
    H --> I
```

修改或撤回申请会使关联的旧确认失效。政策上下文不足或证据冲突时，系统要求澄清或转人工；退款结果未知时进入对账，不通过换幂等键重复执行。

## 主要能力

| 能力 | 当前实现 | 阅读入口 |
|---|---|---|
| 身份与可信确认 | OIDC 身份绑定 issuer/sub；提案绑定用户、会话、参数与有效期，旧授权失效后重新提案 | [配置](docs/guides/configuration.md)、[可信执行](docs/engineering/reliable-execution.md) |
| 多轮售后办理 | 持久化申请草稿、字段来源、证据关联、修订冲突、撤回与同请求重试 | [售后申请](docs/guides/after-sale.md) |
| 可恢复执行 | SQL 幂等、租约 fencing、Outbox/Inbox、退款查询与有凭证的人工结案 | [执行与一致性](docs/engineering/reliable-execution.md)、[故障手册](docs/operations/incident-runbook.md) |
| 政策与知识治理 | 适用范围过滤、多轮上下文隔离、展示截断前的冲突检查、不可变知识快照与 CAS 发布 | [知识治理](docs/engineering/knowledge-and-governance.md) |
| 人工协作 | 所有权版本检查、接管后阻止新的自动调用、客服操作串行化与防重复回复 | [人工接管](docs/engineering/human-handoff.md) |
| 模型与配置治理 | 共享 token 额度、调用账本、发布门禁、会话版本固定 | [系统架构](docs/architecture/overview.md) |
| 评测与扩展 | 逐阶段检索诊断、审核审计、版本化数据、失败回流、预编译索引和队列准入 | [评测协议](docs/evaluation/offline-review.md)、[工程机制](docs/engineering/improvement-loop.md) |

技术栈：Python 3.10–3.12、FastAPI、Pydantic、SQLAlchemy/Alembic、SQLite/PostgreSQL、MCP Streamable HTTP、OpenTelemetry、Playwright、GitHub Actions。支持 ReAct、Plan-and-Execute 与多 Agent 路由。

## 快速启动

推荐 Python 3.11。在仓库根目录执行：

```bash
git clone https://github.com/scallion7/xc_sx_agent.git
cd xc_sx_agent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m app.scripts.demo_stack --intake --directory app/sessions/readme-demo
```

Windows PowerShell 将环境创建和激活两步替换为 `py -3.11 -m venv .venv` 与 `.venv\Scripts\Activate.ps1`。

启动器打印动态分配的本机地址：

| 页面 | 用途 | 演示令牌 |
|---|---|---|
| `/workspace` | 客户对话、任务进度、操作确认 | `demo-user` |
| `/after-sale` | 售后申请、资料修订与证据 | `demo-user` |
| `/workbench` | 人工接管、回复、审批与知识管理 | `demo-admin` |
| `/review` | 检索复核与数据集管理 | `demo-admin` |

启动器创建隔离数据库、执行迁移与种子导入，并启动 API、Worker、受控 HTTP 模型和库存 MCP。`--intake` 启用对话申请草稿，商务 Provider 使用本地 mock；独立 HTTP 退款沙箱见[配置指南](docs/guides/configuration.md)。退出时按 `Ctrl+C`，指定目录中的数据和日志保留。

演示无需真实模型密钥或 Docker，首次安装依赖需要网络。上述令牌仅用于本机演示。逐步操作与预期结果见[五分钟演示](docs/guides/five-minute-demo.md)。只验证启动和任务完成可运行：

```bash
python -m app.scripts.demo_stack --smoke --directory app/sessions/smoke-demo
```

## 验证与范围

已归档的功能修复基线 [`c5aee67`](https://github.com/scallion7/xc_sx_agent/commit/c5aee6716723ae5a4243497db919e094d3b238a5) 的 [GitHub Actions](https://github.com/scallion7/xc_sx_agent/actions/runs/42813941711) 已通过全部 9 项作业，覆盖三个 Python 版本、SQLite/PostgreSQL 迁移、真实浏览器、供应链检查、持续负载及完整 Compose 部署恢复。

后续提交的运行状态见页首 CI 标记。测试数量、跳过原因、覆盖率、原始证据和复现入口统一维护在[当前验收](docs/evaluation/acceptance.md)。受控模型下的工程验证与真实模型质量分别记录：人工金标准、原冻结 holdout、付费模型、真实支付和生产容量尚未验收。

- 默认余弦检索与版本化 SQL 知识平台可用；Chroma 后端因未修复安全公告停用。
- 新平台表有租户作用域，但不能据此认为所有历史业务表已经支持共享数据库的 SaaS 多租户。
- 恢复验收覆盖隔离部署、停写 PostgreSQL 与独立沙箱；持续写入 PITR、跨区域恢复和生产 SLA 尚无验收证据。

## 代码与文档入口

| 位置 | 内容 |
|---|---|
| `app/agent/` | Agent 策略、工具契约、检索、记忆与业务技能 |
| `app/services/`、`app/repositories/` | 业务服务、可信执行与持久化 |
| `app/platform/` | 知识、模型预算、协作与评测平台 |
| `app/web/` | HTTP 接口及客户、客服、复核页面 |
| `app/scripts/` | 启动、迁移、评测、备份与恢复命令 |
| `tests/`、`.github/workflows/` | 回归测试与 CI |
| `docs/` | 当前指南、历史开发记录与原始验收证据 |

开发与部署分别见[配置与运行](docs/guides/configuration.md)、[部署清单](docs/operations/deployment-checklist.md)。阶段 1–34 和后续交付记录见[历史目录](docs/history/README.md)，历史命令和测试数保留其原始时点。

## 维护者

项目维护者与仓库贡献者：[scallion7](https://github.com/scallion7)。历史提交别名通过 `.mailmap` 统一归并，工程演进及提交依据见[贡献说明](docs/CONTRIBUTIONS.md)。
