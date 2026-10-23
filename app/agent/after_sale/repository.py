"""Case repository contract and process-safe JSON implementation."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from app.agent.after_sale.creation import (
    TERMINAL_CASE_STATUSES,
    CaseConflictError,
    CaseCreationResult,
    case_creation_hash,
    check_case_identity,
    check_request_hash,
)
from app.agent.after_sale.models import AfterSaleCase, CaseStatus
from app.infrastructure.file_lock import exclusive_file_lock


class AfterSaleRepository(Protocol):
    def get(self, case_id: str, user_id: str | None = None) -> AfterSaleCase | None: ...
    def save(self, case: AfterSaleCase) -> None: ...
    def create_or_get(
        self, case: AfterSaleCase, *, idempotency_key: str = ""
    ) -> CaseCreationResult: ...
    def get_creation(
        self, user_id: str, key: str, request_hash: str
    ) -> CaseCreationResult | None: ...
    def find_active_by_order(
        self, order_id: str, user_id: str = "default"
    ) -> AfterSaleCase | None: ...
    def list_pending(self, user_id: str | None = None) -> list[AfterSaleCase]: ...


class JsonAfterSaleRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def _read(self):
        if not self.path.exists():
            return {}, {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cases = {
                item["case_id"]: AfterSaleCase.from_dict(item) for item in data["cases"]
            }
            if len(cases) != len(data["cases"]):
                raise ValueError("duplicate case IDs")
            requests = data.get("creation_requests", {})
            if not isinstance(requests, dict):
                raise ValueError("invalid creation receipts")
            active = set()
            for case in cases.values():
                identity = (case.user_id, case.order_id)
                if case.status not in TERMINAL_CASE_STATUSES:
                    if identity in active:
                        raise ValueError("duplicate active cases")
                    active.add(identity)
            return cases, requests
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise CaseConflictError(
                "CASE_STORAGE_CORRUPT",
                "工单文件损坏或存在重复活跃工单，请人工核对；未覆盖原文件",
            ) from exc

    def _write(self, cases, requests):
        payload = {
            "version": 2,
            "cases": [case.to_dict() for case in cases.values()],
            "creation_requests": requests,
        }
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _request(cases, requests, user_id, key, fingerprint):
        receipt = requests.get(json.dumps([user_id, key])) if key else None
        if receipt is None:
            return None
        check_request_hash(receipt["request_hash"], fingerprint)
        case = cases.get(receipt["case_id"])
        if case is None or case.user_id != user_id:
            raise CaseConflictError(
                "CASE_RECEIPT_INVALID", "幂等记录关联的工单不可用，禁止重新创建"
            )
        return CaseCreationResult(case, False, "request_key")

    @staticmethod
    def _active(cases, order_id, user_id):
        return next(
            (
                case
                for case in cases.values()
                if case.order_id == order_id
                and case.user_id == user_id
                and case.status not in TERMINAL_CASE_STATUSES
            ),
            None,
        )

    def get_creation(self, user_id: str, key: str, request_hash: str):
        with exclusive_file_lock(self.path):
            cases, requests = self._read()
            return self._request(cases, requests, user_id, key, request_hash)

    def create_or_get(
        self, case: AfterSaleCase, *, idempotency_key: str = ""
    ) -> CaseCreationResult:
        if case.status in TERMINAL_CASE_STATUSES or case.version != 0:
            raise CaseConflictError(
                "CASE_INVALID_CREATE", "新工单必须处于活跃状态且版本为零"
            )
        with exclusive_file_lock(self.path):
            cases, requests = self._read()
            fingerprint = case_creation_hash(case)
            replay = self._request(
                cases, requests, case.user_id, idempotency_key, fingerprint
            )
            if replay:
                return replay
            active = self._active(cases, case.order_id, case.user_id)
            selected = active or replace(case, version=1)
            if active is None:
                if case.case_id in cases:
                    raise CaseConflictError("CASE_ID_COLLISION", "工单编号已存在")
                cases[selected.case_id] = selected
            if idempotency_key:
                requests[json.dumps([case.user_id, idempotency_key])] = {
                    "case_id": selected.case_id,
                    "request_hash": fingerprint,
                    "created_at": case.created_at,
                }
            if active is None or idempotency_key:
                self._write(cases, requests)
            return CaseCreationResult(selected, active is None)

    def get(self, case_id: str, user_id: str | None = None) -> AfterSaleCase | None:
        with exclusive_file_lock(self.path):
            cases, _ = self._read()
            case = cases.get(case_id)
            return (
                case
                if case is not None and (user_id is None or case.user_id == user_id)
                else None
            )

    def save(self, case: AfterSaleCase) -> None:
        with exclusive_file_lock(self.path):
            cases, requests = self._read()
            current = cases.get(case.case_id)
            if current is not None:
                check_case_identity(current, case)
            elif case.version != 0:
                raise CaseConflictError(
                    "CASE_VERSION_CONFLICT", "工单不存在，不能以旧版本重新写入"
                )
            if case.status not in TERMINAL_CASE_STATUSES:
                active = self._active(cases, case.order_id, case.user_id)
                if active and active.case_id != case.case_id:
                    raise CaseConflictError(
                        "CASE_ACTIVE_CONFLICT", "该用户订单已有其他活跃工单"
                    )
            saved = replace(case, version=case.version + 1)
            cases[case.case_id] = saved
            self._write(cases, requests)
            case.version = saved.version

    def find_active_by_order(
        self, order_id: str, user_id: str = "default"
    ) -> AfterSaleCase | None:
        with exclusive_file_lock(self.path):
            cases, _ = self._read()
            return self._active(cases, order_id, user_id)

    def list_pending(self, user_id: str | None = None) -> list[AfterSaleCase]:
        with exclusive_file_lock(self.path):
            cases, _ = self._read()
            return [
                case
                for case in cases.values()
                if case.status == CaseStatus.PENDING_REVIEW.value
                and (user_id is None or case.user_id == user_id)
            ]
