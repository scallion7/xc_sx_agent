"""确定性风险路由。

高风险判断不能只依赖模型：消息级规则负责强制转人工，工单级规则负责决定
自动处理、人工审核或高优先级人工升级。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.agent.after_sale.models import CaseType, RiskLevel


@dataclass
class RiskDecision:
    level: str
    route: str
    reasons: list[str]

    @property
    def requires_human(self) -> bool:
        return self.route in {"manual_review", "human_escalation"}


class RiskRouter:
    CRITICAL_SIGNALS = {
        "消协",
        "起诉",
        "报警",
        "律师",
        "人身伤害",
        "受伤",
        "触电",
        "爆炸",
        "泄露隐私",
        "盗刷",
        "媒体曝光",
        "监管投诉",
    }
    COMPLAINT_SIGNALS = {"投诉", "欺骗", "假货", "质量太差", "非常不满", "赔偿"}

    def __init__(
        self,
        auto_approve_max_amount: float = 200.0,
        high_risk_amount: float = 2000.0,
        rules_path: str | Path | None = None,
    ):
        self.auto_approve_max_amount = auto_approve_max_amount
        self.high_risk_amount = high_risk_amount
        self.rule_version = "builtin"
        self.critical_signals = set(self.CRITICAL_SIGNALS)
        self.complaint_signals = set(self.COMPLAINT_SIGNALS)
        self.evidence_types = {
            CaseType.QUALITY_ISSUE.value,
            CaseType.MISSING_ITEM.value,
            CaseType.LOGISTICS_DAMAGE.value,
        }
        self.manual_review_score = 1
        self.human_escalation_score = 4
        self.score_weights = {
            "amount_over_auto": 1,
            "amount_high": 4,
            "evidence_missing": 2,
            "other_case": 1,
            "existing_refund": 3,
            "complaint": 2,
        }
        if rules_path:
            self._load_rules(Path(rules_path))

    def _load_rules(self, path: Path) -> None:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.rule_version = str(data.get("version", "unversioned"))
        self.critical_signals = set(data.get("critical_signals", self.critical_signals))
        self.complaint_signals = set(
            data.get("complaint_signals", self.complaint_signals)
        )
        self.evidence_types = set(
            data.get("evidence_required_case_types", self.evidence_types)
        )
        thresholds = data.get("score_thresholds", {})
        self.manual_review_score = int(
            thresholds.get("manual_review", self.manual_review_score)
        )
        self.human_escalation_score = int(
            thresholds.get(
                "human_escalation",
                self.human_escalation_score,
            )
        )
        self.score_weights.update(
            {
                key: int(value)
                for key, value in data.get(
                    "score_weights",
                    {},
                ).items()
                if key in self.score_weights
            }
        )

    def assess_message(self, text: str) -> RiskDecision:
        critical = sorted(signal for signal in self.critical_signals if signal in text)
        if critical:
            return RiskDecision(
                level=RiskLevel.CRITICAL.value,
                route="human_escalation",
                reasons=[f"命中高风险信号：{signal}" for signal in critical],
            )
        complaints = sorted(
            signal for signal in self.complaint_signals if signal in text
        )
        if complaints:
            return RiskDecision(
                level=RiskLevel.HIGH.value,
                route="manual_review",
                reasons=[f"命中投诉信号：{signal}" for signal in complaints],
            )
        return RiskDecision(RiskLevel.LOW.value, "agent", [])

    def assess_case(
        self,
        *,
        amount: float,
        case_type: str,
        reason: str,
        order_status: str,
        evidence_provided: bool,
    ) -> RiskDecision:
        message_risk = self.assess_message(reason)
        if message_risk.level == RiskLevel.CRITICAL.value:
            return message_risk

        score = 0
        reasons = list(message_risk.reasons)
        if amount >= self.high_risk_amount:
            score += self.score_weights["amount_high"]
            reasons.append(f"金额 {amount:.2f} 元达到高风险阈值")
        elif amount > self.auto_approve_max_amount:
            score += self.score_weights["amount_over_auto"]
            reasons.append(f"金额 {amount:.2f} 元超过自动审批阈值")

        if case_type in self.evidence_types and not evidence_provided:
            score += self.score_weights["evidence_missing"]
            reasons.append("争议类售后尚未提供图片或视频证据")
        if case_type == CaseType.OTHER.value:
            score += self.score_weights["other_case"]
            reasons.append("售后类型无法标准化识别")
        if order_status == "refund_processing":
            score += self.score_weights["existing_refund"]
            reasons.append("订单已存在退款流程")
        if message_risk.level == RiskLevel.HIGH.value:
            score += self.score_weights["complaint"]

        if score >= self.human_escalation_score:
            return RiskDecision(RiskLevel.HIGH.value, "human_escalation", reasons)
        if score >= self.manual_review_score:
            return RiskDecision(RiskLevel.MEDIUM.value, "manual_review", reasons)
        return RiskDecision(
            RiskLevel.LOW.value,
            "auto_process",
            reasons or ["金额及售后类型满足自动处理规则"],
        )
