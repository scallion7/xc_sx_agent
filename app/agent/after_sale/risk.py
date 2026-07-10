"""确定性风险路由。

高风险判断不能只依赖模型：消息级规则负责强制转人工，工单级规则负责决定
自动处理、人工审核或高优先级人工升级。
"""

from __future__ import annotations

from dataclasses import dataclass

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
        "消协", "起诉", "报警", "律师", "人身伤害", "受伤", "触电",
        "爆炸", "泄露隐私", "盗刷", "媒体曝光", "监管投诉",
    }
    COMPLAINT_SIGNALS = {"投诉", "欺骗", "假货", "质量太差", "非常不满", "赔偿"}

    def __init__(self, auto_approve_max_amount: float = 200.0, high_risk_amount: float = 2000.0):
        self.auto_approve_max_amount = auto_approve_max_amount
        self.high_risk_amount = high_risk_amount

    def assess_message(self, text: str) -> RiskDecision:
        critical = sorted(signal for signal in self.CRITICAL_SIGNALS if signal in text)
        if critical:
            return RiskDecision(
                level=RiskLevel.CRITICAL.value,
                route="human_escalation",
                reasons=[f"命中高风险信号：{signal}" for signal in critical],
            )
        complaints = sorted(signal for signal in self.COMPLAINT_SIGNALS if signal in text)
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
            score += 4
            reasons.append(f"金额 {amount:.2f} 元达到高风险阈值")
        elif amount > self.auto_approve_max_amount:
            score += 1
            reasons.append(f"金额 {amount:.2f} 元超过自动审批阈值")

        evidence_types = {
            CaseType.QUALITY_ISSUE.value,
            CaseType.MISSING_ITEM.value,
            CaseType.LOGISTICS_DAMAGE.value,
        }
        if case_type in evidence_types and not evidence_provided:
            score += 2
            reasons.append("争议类售后尚未提供图片或视频证据")
        if case_type == CaseType.OTHER.value:
            score += 1
            reasons.append("售后类型无法标准化识别")
        if order_status == "refund_processing":
            score += 3
            reasons.append("订单已存在退款流程")
        if message_risk.level == RiskLevel.HIGH.value:
            score += 2

        if score >= 4:
            return RiskDecision(RiskLevel.HIGH.value, "human_escalation", reasons)
        if score >= 1:
            return RiskDecision(RiskLevel.MEDIUM.value, "manual_review", reasons)
        return RiskDecision(
            RiskLevel.LOW.value,
            "auto_process",
            reasons or ["金额及售后类型满足自动处理规则"],
        )
