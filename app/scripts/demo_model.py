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
    try:
        envelope = json.loads(query)
        if isinstance(envelope, dict) and "request" in envelope:
            query = envelope["request"]
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
    schema = body.get("response_format", {}).get("json_schema", {}).get("name", "")
    reply = "您好，这是受控离线模型的演示回复。"
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
    if not schema and tool in names and not already:
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
