# 部署与维护清单

[文档中心](../README.md) · [配置与运行](../guides/configuration.md) · [当前验收](../evaluation/acceptance.md)

本清单用于把本机演示接到独立部署环境。当前提交的测试、镜像扫描、PostgreSQL 与 Compose 结果集中在[验收状态](../evaluation/acceptance.md)；这些结果不代表已完成真实身份、支付供应商或生产流量联调。

## 1. 装配身份、模型和业务依赖

依赖安装使用[配置指南](../guides/configuration.md)中的 Python 3.11 锁定环境。以下值是配置模板，密钥、Token 和数据库凭据由部署平台注入；不要将实际值提交到仓库、写入镜像或记录到命令日志。

```dotenv
ENVIRONMENT=production
OPENAI_API_KEY=<secret-manager-injected>
PERSISTENCE_BACKEND=sql
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<database>
DATABASE_AUTO_CREATE=false
COMMERCE_SEED_DEMO_DATA=false

AUTH_MODE=oidc
OIDC_ISSUER=https://identity.example/
OIDC_AUDIENCE=ecom-api
OIDC_JWKS_PATH=/run/secrets/oidc-jwks.json
OIDC_REQUIRED_SCOPES=chat:use
OIDC_ALGORITHMS=RS256

PROVIDER_MODE=external
PROVIDER_TIMEOUT_SECONDS=5
PROVIDER_READ_MAX_ATTEMPTS=2
PROVIDER_WEBHOOK_SECRET=<secret-manager-injected>
```

production 会拒绝占位模型密钥、默认开发 Token、mock/sandbox Provider、自动建表和演示业务数据。通过配置检查不等于业务适配器已接入：`PROVIDER_MODE=external` 不会自动发现第三方 SDK。

真实订单、物流、退款和补偿适配器分别实现 `OrderProvider`、`LogisticsProvider`、`RefundProvider`、`CompensationProvider`，通过 `create_runtime(...)` 装配，再传给 `create_app(runtime=...)`。API 与 Worker 都须使用相同的装配逻辑；直接运行默认 CLI 不会获得自定义 Provider。供应商须持久化识别幂等键并支持结果查询，副作用调用不能套用通用读请求重试。

OIDC 的内部身份来自经过验证的完整 issuer/sub，`preferred_username` 仅供显示；JWT 通过 `X-API-Key` 头传入。已有显示名账号必须按[OIDC 迁移步骤](../guides/configuration.md#oidc-配置和旧账号迁移)核验 `OIDC_IDENTITY_MAP_PATH`，所有实例长期使用同一映射文件。不要仅凭同名认领旧订单或删除映射来切换身份。

默认 JWKS 加载器读取本地文件；远程刷新需自定义 `OidcJwtProvider` 并注入 `create_app(auth_provider=...)`。上线前实际验证 issuer、audience、scope、签名、过期 Token 和密钥轮换；`/ready` 不替代这些验证。

## 2. 备份、迁移与启动

旧库先备份，暂停受影响的写入，再升至当前 head（`20261109_0009`）。迁移发现重复活跃工单等历史冲突时会拒绝升级，应核对关联审批、退款和审计后修复根因；不要删除账本或修改旧迁移文件绕过保护。自动建表不能升级旧库。

```bash
python -m app.scripts.manage_migrations upgrade
python -m app.scripts.manage_migrations check
python -m alembic check
python -m app.scripts.doctor
```

`upgrade` 默认目标为 head；第二条检查迁移版本，第三条检查 ORM 与迁移的结构漂移。`doctor` 输出诊断报告，不能仅凭进程退出成功判断报告中的每项正常。降级前另行核对[迁移说明](../../app/infrastructure/migrations/README)和业务数据保护条件。

在不同进程启动 API 与 Agent Worker：

```bash
python -m app.scripts.run_api
python -m app.scripts.run_worker
```

它们使用相同数据库、身份与业务配置。API 地址由 `API_HOST`/`API_PORT` 控制，默认 `0.0.0.0:8000`；反向代理、TLS、网络访问控制和进程管理由部署环境配置。自定义 external Provider 使用前述装配入口启动。

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

`/health` 只表示进程存活。`/ready` 检查迁移、认证配置存在性、规则文件和 Provider 装配/回调密钥配置，**不会实际请求身份提供方、模型或商务供应商**；OIDC 路径非空也不证明 JWKS 可读或 Token 有效。开发配置允许自动建表时，迁移不在 head 可能返回 HTTP 200 的 `degraded`，部署必须检查响应内容并关闭自动建表。随后用部署身份执行真实的受控读请求和授权负向用例。

## 3. 发布与操作边界验收

先在干净检出、临时数据库中执行质量门禁，不把业务 `DATABASE_URL` 注入全套测试：

```bash
python -m app.scripts.quality_platform
python -m playwright install chromium
python -m app.scripts.quality_platform --browser
```

CI 的 Python 兼容矩阵、覆盖率、迁移与 smoke、依赖审计、SBOM、容器扫描、持续负载和 Compose 均应对应待发布提交。浏览器是否启用及通过数量以[当前验收](../evaluation/acceptance.md)为准，不把配置跳过算作通过。

部署环境还需验证以下行为：

- 普通用户不能读取他人订单、会话、材料或提案，管理接口需要相应权限；旧 OIDC 账号仅由核验后的 issuer/sub 绑定访问。
- 在隔离库运行 `case_creation_smoke`，确认并发创建收敛与幂等记录；该命令会写合成工单，不能对业务库执行。
- 回调正确/错误签名、重复/乱序事件，以及供应商同键同内容只产生一次效果、同键改内容被拒绝。
- 表单 `INTAKE_VERSION_CONFLICT` 表示本次修订未提交；网络/5xx 和其他未知响应保留原键原内容核对，不能统一清空后重发。
- 工作台接管、审批、人工消息及完整刷新期间保持 busy；刷新结束后再确认当前用户/会话。按钮禁用不能替代服务端权限、版本与幂等约束。
- 任务取消、租约接管和旧 Worker fencing；业务已经开始或未知时禁止用新授权重复执行。

真实模型质量与费用、组织政策、供应商能力、峰值流量和灾备目标须在相应环境另行验收。

## 4. SQLite 备份、恢复与维护

以下命令针对配置中的 SQLite 源库。备份与平台恢复共用 SQLite backup API，包含已提交而尚未 checkpoint 的 WAL；不能只复制主 `.db` 文件。源库以只读模式打开，不存在即拒绝，不会创建一个空库假装备份成功。

```bash
python -m app.scripts.maintain_data backup --destination /safe/backup/agent-01.db
python -m app.scripts.maintain_data verify --source /safe/backup/agent-01.db
python -m app.scripts.maintain_data restore --source /safe/backup/agent-01.db --destination /safe/restore/agent-01.db
```

备份目标必须为新文件。备份、恢复库、JSON 报告与源库分别使用独立路径，检查同时涵盖链接别名及 SQLite 的 WAL、SHM、回滚日志。生成和校验在各目标目录中的私有临时文件完成，校验成功后排他发布。恢复已有库须显式指定 `--replace`，先停写并正常关闭目标应用与 Worker。若目标还有 SQLite 日志文件，先用 SQLite 正常完成 checkpoint 和关闭，再重试。替换失败时原目标保留。`verify` 只执行 SQLite 完整性检查，不证明业务行数、状态或外部退款结果一致。

需要可审查的恢复指纹时，先停写并使用独立新目标：

```bash
mkdir -p test-results/recovery-01
python -m app.scripts.backup_restore --backup test-results/recovery-01/agent.db --restore-url sqlite:///test-results/recovery-01/restored.db --report test-results/recovery-01/report.json
```

源连接来自 `DATABASE_URL`，输出目录每次使用新名称。报告路径同样要求新文件，普通发布失败会撤销本次已发布的本地文件。若进程在多个文件发布中遭强制终止，保留已有产物并选择新路径重跑。PostgreSQL 的恢复命令使用单个事务，恢复后的指纹校验或本地报告发布失败时保留已恢复的目标库供核查，重跑须另备空库。该脚本将备份和恢复后的所有表行数、哈希及审计指纹比较并记录耗时。PostgreSQL 使用匹配版本的 `pg_dump`/`pg_restore` 与独立空目标库，指纹比对期间源库必须停写；完整受控用例见[Compose 验收](compose-acceptance.md)。脚本不提供持续备份、PITR 或跨区域策略。

```bash
python -m app.scripts.maintain_data cleanup
python -m app.scripts.maintain_data cleanup --apply
python -m app.scripts.maintain_data export --destination /safe/export/plans-redacted.json
```

省略 `--destination` 时，备份写入 `app/sessions/backup.db`，恢复写入 `app/sessions/restored.db`，导出写入 `app/sessions/plans-redacted.json`。已有导出文件受保护，重复导出应指定新路径。

API、Worker 的 JSONL 订阅器与事件清理共用日志旁的 `.lock` 文件。部署进程需要共享同一日志目录并能获取操作系统文件锁。轮换在持锁期间保留未过期及畸形旧记录，发布后，等待中的写入者重新打开当前日志追加。

`cleanup` 默认预览候选，仍写维护审计；`--apply` 才执行受状态与保留期保护的清理。当前不自动清理 Agent 任务/事件或 HTTP 退款账本。`export` 仅导出计划 ID、用户哈希、状态和更新时间等脱敏元数据，不能作为完整数据库备份。

## 5. Worker、会话阻塞与人工结案

配置 `RUN_LEASE_SECONDS`、`RUN_MAX_ATTEMPTS`、`RUN_POLL_SECONDS` 并保持多机时钟同步；监控 queued/retry_wait、dead_letter、needs_reconciliation 和接管次数。验证 SIGTERM、崩溃接管、旧 owner 拒绝写入、SSE `Last-Event-ID` 补发，以及模型可能重复计费的账本处理。

`SESSION_RUN_ACTIVE` 时查询原任务；不要不断生成新键。管理员可按规则重放 dead_letter，但 `needs_reconciliation` 必须先核对可信业务回执和在途投递，再经带版本与凭证哈希的 `/reconciliation` → `/resolve` 流程结案。`resolved` 释放原会话，不重放旧任务，也不把原调用改成成功。接口需要 `after_sale:recover`，工作台没有强制解锁按钮；完整步骤见[恢复手册](incident-runbook.md#agent-任务人工核对结案)。

## 6. HTTP 退款沙箱与可选功能

独立 HTTP 沙箱仅用于隔离演示，production 禁止 `PROVIDER_MODE=sandbox`。应用、沙箱和库存服务各用独立数据库；全部先按各自入口初始化，应用迁移至当前 head，再启动 API、Agent Worker 与 `python -m app.scripts.run_refund_worker --mode both`。退款 Worker 与 Agent Worker 不是同一进程。固定供应商地址、Token 和回调密钥须在相关进程保持一致；故障控制使用单独管理员 Token。

unknown/needs_reconciliation 不代表退款失败。沙箱恢复接口只查询原操作，不能换键补发；Outbox、查询对账与退款终态都需要收敛。供应商未声明冲正能力时补偿进入 needs_manual，不能将原退款改成“未发生”。应用快照不包含独立供应商库，恢复后仍需按原业务键核对外部效果。已有 HTTP operation 或 needs_manual 补偿时，不得绕过降级保护删除账本。

对话草稿、答案证据检查、预编译检索默认关闭，升级后逐项开启并验收。队列总量和单用户 admission 默认 0，即不限制；参照[持续负载验收](sustained-load.md)设置所有 API 实例的一致上限，验证 429 与原键重试。受控模型的模拟预算和本机吞吐不能作为真实费用或生产容量承诺。
