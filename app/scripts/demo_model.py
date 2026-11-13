"""Deterministic OpenAI-compatible HTTP model for integration tests, NOT an LLM."""

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

app = FastAPI(title="Controlled offline model (no quality claim)")


@app.get("/health")
def health():
    return {"status": "ok", "kind": "controlled_offline_model"}


@app.post("/v1/chat/completions")
def complete(body: dict):
    time.sleep(min(float(os.environ.get("DEMO_MODEL_DELAY", "0")), 10))
    messages = body.get("messages", [])
    users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    query = users[-1] if users else ""
    plan_steps = []
    try:
        envelope = json.loads(query)
        if isinstance(envelope, dict) and "request" in envelope:
            query = envelope["request"]
            execution = envelope.get("execution")
            if isinstance(execution, dict):
                plan_steps = execution.get("steps", [])
    except (ValueError, TypeError):
        pass
    names = {t["function"]["name"] for t in body.get("tools", [])}
    order = re.search(r"ORD-[A-Za-z0-9-]+", query)
    tool = (
        "query_order"
        if order
        else "search_knowledge"
        if any(x in query for x in ["政策", "七天", "退货"])
        else "list_user_orders"
    )
    args = (
        {"order_id": order.group()}
        if order
        else {"query": query}
        if tool == "search_knowledge"
        else {}
    )
    if order and "创建售后" in query:
        tool = "create_after_sale_case"
        args = {
            "order_id": order.group(),
            "reason": "不合适退货退款",
            "case_type": "return_refund",
        }
    intake_stage = False
    if "update_after_sale_intake" in names and (
        "售后申请" in query
        or "补充" in query
        or "撤回" in query
        or "生成提案" in query
        or "改为" in query
        or "新申请" in query
        or "另一笔" in query
        or "重新申请" in query
    ):
        last = max(
            [i for i, m in enumerate(messages) if m.get("role") == "user"] or [-1]
        )
        results = [m for m in messages[last + 1 :] if m.get("role") == "tool"]
        if not results:
            tool, args, intake_stage = "get_after_sale_intake", {}, True
        elif len(results) == 1:
            state = json.loads(results[0]["content"]).get("intake") or {}
            fields = {}
            if order:
                fields["order_id"] = order.group()
            reason = re.search(r"原因[：:](.+)", query)
            if reason:
                fields["reason"] = reason.group(1).strip()
            if "换货" in query:
                fields["case_type"] = "exchange"
            command = (
                "withdraw"
                if "撤回" in query
                else "propose"
                if "生成提案" in query
                else "update"
            )
            tool, args, intake_stage = (
                "update_after_sale_intake",
                {
                    "command": command,
                    "expected_revision": 0
                    if any(x in query for x in ("新申请", "另一笔", "重新申请"))
                    else state.get("revision", 0),
                    "new_task": any(
                        x in query for x in ("新申请", "另一笔", "重新申请")
                    ),
                    "fields": fields,
                },
                True,
            )
    schema = body.get("response_format", {}).get("json_schema", {}).get("name", "")
    reply = "您好，这是受控离线模型的演示回复。"
    if os.environ.get("DEMO_MODEL_POLICY_QUOTES") == "1":
        # Offline evaluation fixture: copy the actual retrieved text; no gold labels.
        if schema == "CustomerServiceResponse":
            reply = query
        elif plan_steps:
            for step in reversed(plan_steps):
                lookup = step.get("output", {})
                if (
                    step.get("tool_name") == "search_knowledge"
                    and lookup.get("grounded")
                    and lookup.get("results")
                ):
                    reply = lookup["results"][0]["text"]
                    break
        else:
            for msg in reversed(messages):
                if msg.get("role") == "tool":
                    lookup = json.loads(msg["content"])
                    if lookup.get("grounded") and lookup.get("results"):
                        reply = lookup["results"][0]["text"]
                    break
    if schema == "ExecutionPlan":
        content = json.dumps(
            {
                "goal": "检索已授权的业务证据",
                "steps": [
                    {
                        "step_id": "lookup",
                        "description": "读取证据",
                        "tool_name": tool,
                        "arguments": args,
                        "depends_on": [],
                    }
                ],
            },
            ensure_ascii=False,
        )
    elif body.get("response_format"):
        content = json.dumps(
            {
                "intent": "greeting",
                "confidence": 1,
                "reply": reply,
                "requires_human": False,
                "follow_up_question": None,
                "citations": [],
                "evidence_state": "not_requested",
            },
            ensure_ascii=False,
        )
    elif body.get("max_tokens") == 10:
        content = "postsale"
    else:
        content = reply
    last_user = max(
        [i for i, m in enumerate(messages) if m.get("role") == "user"] or [-1]
    )
    already = any(m.get("role") == "tool" for m in messages[last_user + 1 :])
    message = {"role": "assistant", "content": content}
    if not schema and tool in names and (not already or intake_stage):
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": tool,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ],
        }
    result = {
        "id": "chatcmpl-" + uuid4().hex,
        "object": "chat.completion",
        "system_fingerprint": "xc-sx-controlled-demo-v1",
        "created": int(time.time()),
        "model": body.get("model", "demo-small"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }
    record = os.environ.get("DEMO_MODEL_RECORD_PATH")
    if record:
        with Path(record).open("a") as stream:
            stream.write(
                json.dumps({"request": body, "response": result}, ensure_ascii=False)
                + "\n"
            )
    return result
