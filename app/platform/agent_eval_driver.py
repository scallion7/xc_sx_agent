"""Drive the real Agent against a configured model and isolated business state."""

import json
from uuid import uuid4

from sqlalchemy import func, select

from app.bootstrap import create_agent
from app.infrastructure.database import (
    after_sale_cases_table,
    after_sale_operations_table,
    get_engine,
    refunds_table,
)
from app.platform.budget import SharedBudget
from app.platform.common import PlatformError
from app.platform.evaluation_clock import on_policy_day

CONTROLLED_FINGERPRINT = "xc-sx-controlled-demo-v1"


def check_live_receipts(responses):
    if not responses or any(
        r.get("system_fingerprint") == CONTROLLED_FINGERPRINT for r in responses
    ):
        raise PlatformError("CONTROLLED_OR_MISSING_MODEL_RECEIPT", 422)


def build_driver(config, manifest, *, live=False):
    engine = get_engine(config.database_url)

    def counts(conn):
        return tuple(
            conn.scalar(select(func.count()).select_from(t))
            for t in (
                after_sale_cases_table,
                after_sale_operations_table,
                refunds_table,
            )
        )

    def driver(case, strategy, remaining):
        session = "evaluation-" + uuid4().hex
        runconfig = config.model_copy(
            update={
                "memory_user_id": "user-1",
                "model_max_output_tokens": min(500, remaining),
            }
        )
        # Quota is durable across every attempt. The shared tenant budget is
        # the hard stop; an uncertain usage response consumes its reservation.
        with engine.connect() as conn:
            before = counts(conn)
        from app.infrastructure.model_gateway import OpenAIModelGateway
        from app.platform.releases import pinned_after_sale
        from app.web.services import build_api_services

        gateway = OpenAIModelGateway.from_config(runconfig)
        raw_model = []
        original = gateway._invoke

        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            raw_model.append(result.model_dump(mode="json"))
            return result

        gateway._invoke = record
        services = build_api_services(runconfig)
        agent = create_agent(
            mode="multi" if strategy == "multi" else "single",
            config=runconfig,
            model_gateway=gateway,
            commerce_service=services.commerce,
            after_sale_service=pinned_after_sale(services.after_sale, manifest),
            session_path="user-1:" + session,
            strategy="react" if strategy == "multi" else strategy,
        )
        previous = {r["call_id"] for r in SharedBudget(config).report()}
        try:
            turn_responses = []
            with on_policy_day(case["day"]) as lookups:
                turns = list(case["turns"])
                if case["context"]:
                    # Make trusted scenario facts available to the model, not to the answer key.
                    labels = {
                        "region": "地域",
                        "product": "商品分类",
                        "channel": "购买渠道",
                    }
                    facts = "，".join(
                        f"{labels[k]}={v}" for k, v in case["context"].items()
                    )
                    turns[0] = f"我的订单条件是{facts}。{turns[0]}"
                for message in turns:
                    response = agent.chat(message)
                    turn_responses.append(response.model_dump(mode="json"))
        finally:
            agent.release_resources()
        if live:
            check_live_receipts(raw_model)
        with engine.connect() as conn:
            after = counts(conn)
        calls = [
            r for r in SharedBudget(config).report() if r["call_id"] not in previous
        ]
        used = sum(
            json.loads(r["cost_json"]).get("charged_tokens", r["reserved_tokens"])
            for r in calls
        )
        return {
            "outcome": "handoff" if response.requires_human else "answered",
            "unsafe_actions": sum(
                abs(a - b) for a, b in zip(after, before, strict=True)
            ),
            "database_assertions_passed": after == before,
            "tokens": used,
            "response": response.model_dump(mode="json"),
            "turn_responses": turn_responses,
            "policy_lookups": lookups,
            "tool_trace": agent.raw_messages if hasattr(agent, "raw_messages") else [],
            "model_calls": calls,
            "raw_model_responses": raw_model,
            "citations_valid": bool(response.citations),
            "handoff": response.requires_human,
        }

    return driver
