"""Reversible draft tools; only existing action APIs can authorize business writes."""

from types import SimpleNamespace
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import select

from app.agent.tools.contracts import EmptyArguments, ToolArguments
from app.platform.after_sale_intake import TABLE, AfterSaleIntake
from app.platform.common import PlatformError
from app.services.actions import ActionError, digest
from app.services.run_context import current_run


class IntakeUpdate(ToolArguments):
    command: Literal["update", "propose", "withdraw", "propose_execution"] = "update"
    expected_revision: int = Field(ge=0)
    fields: dict[str, str | list[str]] = Field(default_factory=dict)
    new_task: bool = False


def register_intake_tools(registry, config, commerce, after_sale):
    from app.agent.tools.registry import RegisteredTool

    services = SimpleNamespace(config=config, commerce=commerce, after_sale=after_sale)

    def latest(context):
        flow = AfterSaleIntake(services, context.user_id)
        session = context.session_id.removeprefix(context.user_id + ":")
        with flow.engine.connect() as conn:
            row = conn.execute(
                select(TABLE.c.intake_id)
                .where(
                    TABLE.c.tenant_id == config.tenant_id,
                    TABLE.c.user_id == context.user_id,
                    TABLE.c.session_id == session,
                )
                .order_by(TABLE.c.updated_at.desc(), TABLE.c.intake_id)
                .limit(1)
            ).first()
        return flow, flow.get(row[0]) if row else None

    def get(context):
        _, state = latest(context)
        return {
            "success": True,
            "intake": state,
            "missing": (state or {}).get("missing", ["order_id", "reason"]),
        }

    def update(command, expected_revision, fields, new_task, context):
        run = current_run()
        if not run:
            return {"success": False, "error_code": "INTAKE_DURABLE_RUN_REQUIRED"}
        row = run.check()
        if (
            row["user_id"] != context.user_id
            or row["session_key"] != context.session_id
        ):
            return {"success": False, "error_code": "INTAKE_RUN_SCOPE_MISMATCH"}
        message = row["payload"]["request"]["message"]
        flow, state = latest(context)
        try:
            if new_task and not any(
                x in message for x in ("新申请", "另一笔", "重新申请")
            ):
                raise PlatformError("INTAKE_NEW_TASK_NOT_REQUESTED", 422)
            if command == "withdraw" and not any(
                x in message for x in ("撤回", "取消申请", "不申请了")
            ):
                raise PlatformError("INTAKE_WITHDRAW_NOT_REQUESTED", 422)
            # No invented fields: every update must have a user-text source. Ambiguous
            # or unsupported extraction is rejected, so the Agent must ask a question.
            aliases = {
                "exchange": ("换货",),
                "return_refund": ("退货", "退款"),
                "quality_issue": ("质量",),
                "missing_item": ("缺少", "少件"),
                "logistics_damage": ("物流破损",),
                "other": ("其他",),
            }
            for field, value in fields.items():
                if field == "case_type":
                    supported = isinstance(value, str) and any(
                        word in message for word in aliases.get(value, ())
                    )
                elif field == "evidence_ids":
                    supported = isinstance(value, list) and all(
                        v and v in message for v in value
                    )
                else:
                    supported = (
                        isinstance(value, str) and bool(value) and value in message
                    )
                if not supported:
                    raise PlatformError("INTAKE_FIELD_SOURCE_REQUIRED", 422)
            if fields and command != "update":
                raise PlatformError("INTAKE_FIELDS_NOT_ALLOWED", 422)
            if not state or new_task:
                state = flow.create(
                    context.session_id.removeprefix(context.user_id + ":"),
                    "dialogue:" + (run.run_id if new_task else context.session_id),
                    durable=run,
                )
            from app.agent.tools.manager import ToolManager

            manager = ToolManager(
                registry=registry, execution_context=context, after_sale_enabled=True
            )
            source = {
                "run_id": run.run_id,
                "message_sha256": digest(message),
                "origin": "user_message",
                "session_key": context.session_id,
            }
            key = digest([run.run_id, command, expected_revision, fields, new_task])
            receipt = flow.command(
                state["intake_id"],
                command,
                expected_revision,
                key,
                fields if command == "update" else None,
                manager,
                source=source,
            )
            current = flow.get(receipt["intake_id"])
            output = {
                "success": True,
                "intake": current,
                "receipt_revision": receipt["revision"],
                "missing": current["missing"],
                "requires_clarification": bool(current["missing"]),
            }
            if command in {"propose", "propose_execution"}:
                output.update(
                    confirmation_required=True,
                    action=current.get("execution_action") or current.get("action"),
                )
            return output
        except ValidationError:
            return {"success": False, "error_code": "INVALID_ARGUMENT", "intake": state}
        except (PlatformError, ActionError) as exc:
            return {"success": False, "error_code": exc.code, "intake": state}

    for name, handler, model, draft, desc in [
        (
            "get_after_sale_intake",
            get,
            EmptyArguments,
            False,
            "读取当前会话售后申请、修订和缺失资料；每轮先读取再更新。",
        ),
        (
            "update_after_sale_intake",
            update,
            IntakeUpdate,
            True,
            "按当前用户原话更新售后申请。必须使用读取的修订号，字段不能猜测；缺资料应追问。propose仅生成待确认提案，不执行退款。",
        ),
    ]:
        registry.register(
            RegisteredTool(
                definition={
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": model.model_json_schema(),
                    },
                },
                handler=handler,
                arguments_model=model,
                read_only=not draft,
                draft_only=draft,
                context_aware=True,
            )
        )
