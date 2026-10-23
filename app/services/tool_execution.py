"""Single execution boundary for ReAct, plans and authenticated action APIs."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from time import monotonic

from pydantic import ValidationError

from app.services.actions import ActionError, digest


class ToolExecutionService:
    def __init__(self, registry, *, event_bus, redactor=lambda value: value):
        self.registry = registry
        self.actions = registry.action_service
        self.event_bus = event_bus
        self.redactor = redactor

    def _arguments(self, name, arguments):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError) as exc:
                raise ActionError(
                    "INVALID_ARGUMENT", "工具参数必须是有效 JSON 对象"
                ) from exc
        if not isinstance(arguments, dict):
            raise ActionError("INVALID_ARGUMENT", "工具参数必须是对象")
        try:
            parsed = self.registry.validate_arguments(name, arguments)
        except ValidationError as exc:
            raise ActionError(
                "INVALID_ARGUMENT", "工具参数不符合 Schema，请修正后重试"
            ) from exc
        if name == "create_after_sale_case":
            # Legacy booleans are accepted for compatibility but never trusted.
            parsed["user_confirmed"] = False
            parsed["evidence_provided"] = False
            parsed["evidence_ids"] = sorted(set(parsed["evidence_ids"]))
        return parsed

    def execute(self, name, arguments, context, **kwargs):
        from app.services.run_context import current_run

        run = current_run()
        if run is None:
            return self._execute_logged(name, arguments, context, **kwargs)
        return run.call(
            "read" if self.registry.is_read_only(name) else "write",
            name,
            {
                "arguments": arguments,
                "action_id": kwargs.get("action_id"),
                "prepare_only": kwargs.get("prepare_only", False),
            },
            lambda: self._execute_logged(name, arguments, context, **kwargs),
        )

    def _execute_logged(
        self,
        name,
        arguments,
        context,
        *,
        allowed_tools,
        remote=None,
        deadline=None,
        prepare_only=False,
        action_id=None,
    ):
        started = monotonic()
        try:
            audit_arguments = (
                json.loads(arguments) if isinstance(arguments, str) else arguments
            )
        except (ValueError, TypeError):
            audit_arguments = {}
        if not isinstance(audit_arguments, dict):
            audit_arguments = {}
        audit_arguments = self._redact_value(audit_arguments)
        audit = {
            "name": name,
            "arguments": audit_arguments,
            "tool_source": "mcp" if remote is not None else "local",
            "actor": context.user_id,
            "session_id": context.session_id,
        }
        self.event_bus.emit("tool.started", source="tool_execution", data=audit)
        try:
            result = self._execute(
                name,
                arguments,
                context,
                allowed_tools=allowed_tools,
                remote=remote,
                deadline=deadline,
                prepare_only=prepare_only,
                action_id=action_id,
            )
        except ActionError as exc:
            result = {"success": False, "error_code": exc.code, "error": str(exc)}
        except Exception:
            result = {
                "success": False,
                "error_code": "TOOL_EXECUTION_FAILED",
                "error": "工具执行异常",
            }
        sanitized = self._redact_value(result)
        # Server-generated action references are execution control data. A random
        # hexadecimal ID/hash can contain a phone/card-like digit sequence; never
        # redact it into an ID that cannot be confirmed or resumed. Free text and
        # arguments still pass through the normal redactor.
        action = result.get("action")
        if isinstance(action, dict):
            for key, length in (
                ("action_id", 32),
                ("arguments_hash", 64),
                ("business_version", 64),
            ):
                value = action.get(key)
                if isinstance(value, str) and re.fullmatch(
                    rf"[a-f0-9]{{{length}}}", value
                ):
                    sanitized["action"][key] = value
        encoded = json.dumps(sanitized, ensure_ascii=False)
        self.event_bus.emit(
            "tool.failed"
            if result.get("success") is False or result.get("error")
            else "tool.completed",
            source="tool_execution",
            data={
                **audit,
                "result": encoded,
                "latency_ms": (monotonic() - started) * 1000,
                "action_id": result.get("action", {}).get("action_id", action_id or ""),
            },
        )
        return encoded

    def _redact_value(self, value):
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    @staticmethod
    def _check_deadline(deadline):
        from app.services.run_context import current_run

        if current_run():
            current_run().check()
        if deadline is not None and monotonic() >= deadline:
            raise ActionError(
                "TOOL_DEADLINE_EXCEEDED", "调用截止时间已到，未开始新操作"
            )

    def _execute(
        self,
        name,
        arguments,
        context,
        *,
        allowed_tools,
        remote,
        deadline,
        prepare_only,
        action_id,
    ):
        self._check_deadline(deadline)
        if name not in allowed_tools:
            raise ActionError("TOOL_NOT_ALLOWED", "工具不存在或不在当前权限范围")
        required = self.registry.required_permission(name)
        if required and required not in context.permissions:
            raise ActionError("PERMISSION_DENIED", f"无权执行工具 {name}")
        if remote is not None:
            if name not in self.registry.names or not self.registry.is_read_only(name):
                raise ActionError(
                    "REMOTE_TOOL_UNGOVERNED", "远端工具需注册本地契约与执行适配器"
                )
            parsed = self._arguments(name, arguments)
            output = json.loads(remote(name, parsed))
            if not isinstance(output, dict):
                raise ActionError("INVALID_TOOL_OUTPUT", "远端工具返回结果必须是对象")
            return output
        parsed = self._arguments(name, arguments)
        if self.registry.is_read_only(name):
            raw = self.registry.execute(name, parsed, context)
            return json.loads(raw)
        if self.actions is None or self.registry.business_snapshot is None:
            raise ActionError("ACTION_UNAVAILABLE", "可信确认服务未配置")
        if action_id:
            row = self.actions.get(action_id, context.user_id, context.session_id)
            if row["tool_name"] != name or row["arguments_hash"] != digest(parsed):
                raise ActionError(
                    "ACTION_ARGUMENTS_CHANGED", "确认不能用于不同工具或参数"
                )
            # Replay must remain available after the business operation changed the order.
            if row["status"] == "completed":
                return json.loads(row["result_json"]) | {
                    "idempotent_replay": True,
                    "action": self.actions.public(row),
                }
        snapshot = self.registry.business_snapshot(name, parsed, context)
        version = digest(snapshot)
        row = (
            self.actions.get(action_id, context.user_id, context.session_id)
            if action_id
            else self.actions.propose(
                context.user_id, context.session_id, name, parsed, version
            )
        )
        self.actions.validate(row, digest(parsed), version)
        if row["status"] == "completed":
            return json.loads(row["result_json"]) | {
                "idempotent_replay": True,
                "action": self.actions.public(row),
            }
        if row["status"] == "pending" or prepare_only:
            self.event_bus.emit(
                "action.proposed",
                source="tool_execution",
                data={
                    "action_id": row["action_id"],
                    "actor": context.user_id,
                    "session_id": context.session_id,
                    "arguments_hash": row["arguments_hash"],
                    "business_version": row["business_version"],
                },
            )
            return {
                "success": False,
                "confirmation_required": True,
                "error_code": "CONFIRMATION_REQUIRED",
                "error": "请用户确认下面的具体操作",
                "action": self.actions.public(row),
            }
        if row["status"] != "confirmed":
            raise ActionError(
                "ACTION_" + row["status"].upper(),
                "提案不可执行；已开始的操作请查询结果，勿重复提交",
            )
        self._check_deadline(deadline)
        if not self.actions.claim(row):
            raise ActionError(
                "ACTION_ALREADY_CLAIMED", "确认已被消费或过期，请查询执行结果"
            )
        try:
            # Re-read immediately before dispatch. The domain service still owns its
            # final business validation; external operations require provider-side CAS.
            latest = self.registry.business_snapshot(name, parsed, context)
            self.actions.validate(row, digest(parsed), digest(latest))
            self._check_deadline(deadline)
            if name == "create_after_sale_case":
                parsed["user_confirmed"] = True
                parsed["evidence_provided"] = any(
                    item["status"] == "verified" for item in snapshot["evidence"]
                )
            raw = self.registry.execute(
                name, parsed, replace(context, action_id=row["action_id"])
            )
            result = json.loads(raw)
            unknown = result.get("error_code") in {
                "TOOL_EXECUTION_FAILED",
                "PROVIDER_TIMEOUT",
                "PROVIDER_FAILURE",
            }
            self.actions.finish(row["action_id"], result, unknown=unknown)
        except ActionError as exc:
            result = {"success": False, "error_code": exc.code, "error": str(exc)}
            self.actions.finish(row["action_id"], result)
        except Exception:
            self.actions.finish(
                row["action_id"],
                {
                    "success": False,
                    "error_code": "ACTION_UNKNOWN",
                    "error": "执行结果未知，需要核对，禁止自动重放",
                },
                unknown=True,
            )
            raise
        row = self.actions.get(row["action_id"], context.user_id, context.session_id)
        return result | {"action": self.actions.public(row)}

    def confirm(self, action_id, context, arguments_hash, *, channel="api"):
        row = self.actions.get(action_id, context.user_id, context.session_id)
        version = row["business_version"]
        if row["status"] not in {"completed", "executing", "unknown"}:
            version = digest(
                self.registry.business_snapshot(
                    row["tool_name"], json.loads(row["arguments_json"]), context
                )
            )
        confirmed = self.actions.confirm(
            action_id,
            context.user_id,
            context.session_id,
            arguments_hash,
            version,
            channel=channel,
        )
        self.event_bus.emit(
            "action.confirmed",
            source="tool_execution",
            data={
                "action_id": action_id,
                "actor": context.user_id,
                "channel": channel,
                "status": confirmed["status"],
                "arguments_hash": arguments_hash,
            },
        )
        return confirmed
