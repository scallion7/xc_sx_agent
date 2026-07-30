from typing import Literal

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

    # Agent 执行策略
    max_react_steps: int = 5
    agent_strategy: Literal["react", "plan_execute", "auto"] = "react"
    max_plan_steps: int = 6

    # MCP 配置
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"

    # RAG 配置（第5期）
    embedding_model: str = "text-embedding-3-small"
    kb_dir: str = "app/agent/rag/knowledge"
    # 向量后端：numpy（手写余弦，教学透明，零依赖，默认）/ chroma（向量数据库，生产代表，需 pip install chromadb）
    rag_backend: str = "numpy"
    # NumpyBackend 的 JSON 索引路径
    kb_index_path: str = "app/sessions/kb_index.json"
    # ChromaBackend 的持久化目录与 collection 名
    chroma_persist_dir: str = "app/sessions/chroma"
    chroma_collection: str = "ecom_kb"
    policy_rag_min_score: float = 0.2

    # Agent 深化：售后工单 / Human-in-the-Loop
    after_sale_enabled: bool = True
    after_sale_case_path: str = "app/sessions/after_sale_cases.json"
    after_sale_auto_approve_max_amount: float = 200.0
    after_sale_high_risk_amount: float = 2000.0
    risk_rules_path: str = "app/config/risk_rules.json"

    # Multi-Agent 配置（第6期）
    multi_agent_enabled: bool = False

    # Memory 配置（第7期）
    memory_enabled: bool = True
    memory_dir: str = "app/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50

    # Skill 配置（第8期）
    skills_enabled: bool = True
    skills_dir: str = "app/agent/skills/definitions"

    # Evaluation 配置（第9期，离线评估工具，无聊天开关）
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
    commerce_seed_demo_data: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_user_tokens: str = "dev-user-token:user-1"
    api_admin_tokens: str = "dev-admin-token:admin"
    session_dir: str = "app/sessions/users"

    # 安全护栏
    guardrails_enabled: bool = True
    max_user_input_chars: int = 4000
    block_prompt_injection: bool = True

    # 可观测性
    observability_enabled: bool = True
    event_log_path: str = "app/sessions/events.jsonl"
    event_buffer_size: int = 1000

    # Policy RAG
    rag_rerank_enabled: bool = True
    rag_filter_inactive_policies: bool = True
    rag_lexical_fallback_enabled: bool = True

    model_config = {"env_file": ".env"}


settings = Settings()
