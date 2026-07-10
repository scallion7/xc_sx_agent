"""售后工单 JSON 仓储，使用原子替换避免半写入文件。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.agent.after_sale.models import AfterSaleCase, CaseStatus


class JsonAfterSaleRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load_all(self) -> dict[str, AfterSaleCase]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {
            item["case_id"]: AfterSaleCase.from_dict(item)
            for item in data.get("cases", [])
        }

    def _save_all(self, cases: dict[str, AfterSaleCase]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "cases": [case.to_dict() for case in cases.values()]}
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def get(self, case_id: str) -> AfterSaleCase | None:
        return self._load_all().get(case_id)

    def save(self, case: AfterSaleCase) -> None:
        cases = self._load_all()
        cases[case.case_id] = case
        self._save_all(cases)

    def find_active_by_order(self, order_id: str) -> AfterSaleCase | None:
        terminal = {CaseStatus.COMPLETED.value, CaseStatus.REJECTED.value}
        return next(
            (case for case in self._load_all().values()
             if case.order_id == order_id and case.status not in terminal),
            None,
        )

    def list_pending(self) -> list[AfterSaleCase]:
        return [
            case for case in self._load_all().values()
            if case.status == CaseStatus.PENDING_REVIEW.value
        ]
