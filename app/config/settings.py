from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """项目配置，从 .env 文件读取"""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.7
    model_timeout_seconds: float = 30.0
    model_max_retries: int = 2
    model_retry_base_seconds: float = 0.2
    model_requests_per_minute: int = 60

    action_confirmation_ttl_seconds: int = Field(default=900, ge=1, le=86400)

    # Agent 执行策略
    max_react_steps: int = 5
    agent_strategy: Literal["react", "plan_execute", "auto"] = "react"
    max_plan_steps: int = 6
    plan_max_concurrency: int = 3
    plan_ttl_seconds: int = 900
    plan_max_resume_count: int = 3
    plan_worker_id: str = "local"
    plan_run_lease_seconds: int = Field(default=30, ge=5, le=3600)
    plan_step_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    plan_total_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    plan_max_tool_calls: int = Field(default=20, ge=1, le=200)
    plan_max_retry_attempts: int = Field(default=5, ge=0, le=50)
    plan_max_output_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    plan_max_queued_steps: int = Field(default=20, ge=1, le=200)
    plan_read_max_attempts: int = Field(default=2, ge=1, le=5)

    # Durable Agent worker
    run_lease_seconds: int = Field(default=30, ge=3, le=3600)
    run_max_attempts: int = Field(default=3, ge=1, le=10)
    run_poll_seconds: float = Field(default=1.0, gt=0, le=30)

    # Stages 27–34: trusted deployment scope and opt-in platform integration.
    tenant_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    knowledge_versioned_enabled: bool = False
    knowledge_embedding_mode: Literal["local_hash", "openai"] = "local_hash"
    knowledge_release_id: str = ""
    knowledge_retention_days: int = Field(default=30, ge=1, le=3650)
    model_budget_enabled: bool = False
    model_catalog_snapshot: dict = Field(default_factory=dict)
    model_catalog_path: str = "app/config/model_catalog.json"
    model_shared_rpm: int = Field(default=60, ge=1)
    model_shared_concurrency: int = Field(default=4, ge=1)
    model_token_budget: int = Field(default=100000, ge=1)
    model_max_output_tokens: int = Field(default=1024, ge=1)
    configuration_releases_enabled: bool = False
    collaboration_enabled: bool = False
    telemetry_enabled: bool = False
    telemetry_sample_rate: float = Field(default=1.0, ge=0, le=1)
    mcp_governance_enabled: bool = True
    mcp_connector_id: str = "inventory"
    mcp_identity_secret: str = ""
    prompt_version: str = "builtin-v1"
    system_prompt_override: str = ""

    # MCP 配置
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"

    # RAG 配置
    embedding_model: str = "text-embedding-3-small"
    kb_dir: str = "app/agent/rag/knowledge"
    # 向量后端默认 cosine（兼容 numpy 别名）；历史 chroma 因未修复安全公告暂时停用
    rag_backend: str = "cosine"
    # CosineBackend 的 JSON 索引路径
    kb_index_path: str = "app/sessions/kb_index.json"
    # ChromaBackend 的持久化目录与 collection 名
    chroma_persist_dir: str = "app/sessions/chroma"
    chroma_collection: str = "ecom_kb"
    policy_rag_min_score: float = 0.2

    # Agent 深化：售后工单 / Human-in-the-Loop
    after_sale_enabled: bool = True
    after_sale_case_path: str = "app/sessions/after_sale_cases.json"
    after_sale_operation_path: str = ""
    after_sale_auto_approve_max_amount: float = 200.0
    after_sale_high_risk_amount: float = 2000.0
    after_sale_operation_lease_seconds: int = 300
    after_sale_operation_max_attempts: int = 3
    risk_rules_path: str = "app/config/risk_rules.json"
    risk_rule_versions_path: str = "app/sessions/risk_rule_versions.json"

    # Multi-Agent 配置
    multi_agent_enabled: bool = False

    # Memory 配置
    memory_enabled: bool = True
    memory_dir: str = "app/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50

    # Skill 配置
    skills_enabled: bool = True
    skills_dir: str = "app/agent/skills/definitions"

    # Evaluation 配置（离线评估工具，无聊天开关）
    eval_dataset_path: str = "app/evaluation/cases.json"
    eval_report_path: str = "app/sessions/eval_report.json"
    eval_use_judge: bool = True  # 是否启用 LLM-as-judge（质量/幻觉/过程合理性）
    eval_pass_threshold: float = 0.6  # 单维度通过阈值（judge 归一化到 0-1 后比较）

    # 多轮对话管理
    session_path: str = "app/sessions/session.json"
    history_threshold: int = 10  # 消息压缩策略通常为上下文达到一定的token数，例如claude code通常为达到最大上下文窗口的70%左右，此处简略为原始消息条数超过10轮
    history_keep_recent: int = 3  # 压缩时保留最近 3 条原始消息

    # 持久化与服务化
    persistence_backend: str = "sql"  # sql(SQLite/PostgreSQL) / json
    database_url: str = "sqlite:///app/sessions/xc_sx_agent.db"
    database_auto_create: bool = True
    commerce_seed_demo_data: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_user_tokens: str = "dev-user-token:user-1"
    api_admin_tokens: str = "dev-admin-token:admin"
    session_dir: str = "app/sessions/users"
    environment: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["static", "oidc"] = "static"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_path: str = ""
    oidc_required_scopes: str = "chat:use"
    oidc_algorithms: str = "RS256"
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=86400)
    provider_mode: Literal["mock", "external", "sandbox"] = "mock"
    provider_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    provider_read_max_attempts: int = Field(default=2, ge=1, le=5)
    sandbox_base_url: str = "http://127.0.0.1:9124"
    sandbox_api_token: str = ""
    provider_webhook_secret: str = ""
    provider_webhook_events_path: str = "app/sessions/provider_webhook_events.json"
    api_idempotency_path: str = "app/sessions/api_idempotency_keys.json"
    api_user_requests_per_minute: int = Field(default=120, ge=1, le=10000)
    api_admin_requests_per_minute: int = Field(default=60, ge=1, le=10000)
    api_expensive_requests_per_minute: int = Field(default=20, ge=1, le=1000)

    # 安全护栏
    guardrails_enabled: bool = True
    max_user_input_chars: int = 4000
    block_prompt_injection: bool = True

    # 可观测性
    observability_enabled: bool = True
    event_log_path: str = "app/sessions/events.jsonl"
    event_buffer_size: int = 1000

    # 数据生命周期（终态数据；活跃记录始终受保护）
    session_retention_days: int = Field(default=90, ge=1, le=3650)
    plan_retention_days: int = Field(default=90, ge=1, le=3650)
    event_retention_days: int = Field(default=30, ge=1, le=3650)
    operation_retention_days: int = Field(default=365, ge=1, le=3650)

    # Policy RAG
    rag_rerank_enabled: bool = True
    rag_filter_inactive_policies: bool = True
    rag_lexical_fallback_enabled: bool = True

    model_config = {"env_file": ".env"}

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if (
            any(
                (
                    self.knowledge_versioned_enabled,
                    self.model_budget_enabled,
                    self.configuration_releases_enabled,
                    self.collaboration_enabled,
                    self.telemetry_enabled,
                )
            )
            and self.persistence_backend != "sql"
        ):
            raise ValueError("Platform features require SQL persistence")
        if self.mcp_identity_secret and len(self.mcp_identity_secret) < 16:
            raise ValueError("MCP identity secret must contain at least 16 characters")
        if self.plan_step_timeout_seconds > self.plan_total_timeout_seconds:
            raise ValueError("单步骤超时不能大于计划总超时")
        if self.auth_mode == "oidc" and (
            not self.oidc_issuer or not self.oidc_audience or not self.oidc_jwks_path
        ):
            raise ValueError("OIDC 模式必须配置 issuer、audience 和 JWKS 路径")
        if not self.oidc_algorithms.split() or any(
            algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384"}
            for algorithm in self.oidc_algorithms.split()
        ):
            raise ValueError("OIDC 签名算法不在允许列表")
        if self.provider_mode == "sandbox":
            if (
                self.persistence_backend != "sql"
                or not self.sandbox_api_token
                or not self.provider_webhook_secret
            ):
                raise ValueError(
                    "Sandbox mode requires SQL, API token and webhook secret"
                )
        if self.environment == "production":
            if not self.openai_api_key.strip() or self.openai_api_key in {
                "sk-xxx",
                "sk-replace-me",
            }:
                raise ValueError("生产环境必须配置非占位模型密钥")
            if (
                "dev-user-token" in self.api_user_tokens
                or "dev-admin-token" in self.api_admin_tokens
            ):
                raise ValueError("生产环境禁止使用默认开发 Token")
            if self.commerce_seed_demo_data:
                raise ValueError("生产环境禁止写入演示业务数据")
            if self.database_auto_create:
                raise ValueError("生产环境必须通过迁移管理数据库结构")
            if self.provider_mode in {"mock", "sandbox"}:
                raise ValueError("生产环境禁止使用 Mock Provider")
            if self.provider_mode == "external" and not self.provider_webhook_secret:
                raise ValueError("外部 Provider 模式必须配置回调签名密钥")
        return self

    def safe_summary(self) -> dict[str, object]:
        """仅返回不含密钥、Token、URL 凭据的诊断摘要。"""
        return {
            "environment": self.environment,
            "persistence_backend": self.persistence_backend,
            "auth_mode": self.auth_mode,
            "provider_mode": self.provider_mode,
            "provider_timeout_seconds": self.provider_timeout_seconds,
            "provider_read_max_attempts": self.provider_read_max_attempts,
            "database_auto_create": self.database_auto_create,
            "plan_limits": {
                "max_steps": self.max_plan_steps,
                "max_concurrency": self.plan_max_concurrency,
                "step_timeout_seconds": self.plan_step_timeout_seconds,
                "total_timeout_seconds": self.plan_total_timeout_seconds,
                "max_tool_calls": self.plan_max_tool_calls,
            },
        }


settings = Settings()
