"""运行诊断：迁移、认证、Provider、规则和计划租约。"""

from __future__ import annotations

import json
from pathlib import Path

from app.bootstrap import check_runtime
from app.config.settings import Settings
from app.infrastructure.migrations.runtime import migration_status
from app.repositories.plan_runs import build_plan_run_repository


def diagnose(config: Settings | None = None) -> dict:
    settings = config or Settings()
    checks = [item.__dict__ for item in check_runtime(settings)]
    try:
        migration = migration_status(settings.database_url)
    except Exception as exc:  # noqa: BLE001
        migration = {
            "up_to_date": False,
            "error_code": f"MIGRATION_CHECK_{type(exc).__name__.upper()}",
        }
    try:
        active_runs = build_plan_run_repository(
            settings,
            session_path=None,
            user_id=None,
        ).list(status="running", limit=20)
        plan_runs = {"ok": True, "error_code": ""}
    except Exception as exc:  # noqa: BLE001
        active_runs = []
        plan_runs = {
            "ok": False,
            "error_code": f"PLAN_RUN_CHECK_{type(exc).__name__.upper()}",
        }
    return {
        "config": settings.safe_summary(),
        "runtime": checks,
        "migration": migration,
        "auth": {
            "mode": settings.auth_mode,
            "jwks_available": settings.auth_mode == "static"
            or Path(settings.oidc_jwks_path).is_file(),
        },
        "provider": {
            "mode": settings.provider_mode,
            "webhook_verifier_configured": bool(settings.provider_webhook_secret),
        },
        "rules": {"available": Path(settings.risk_rules_path).is_file()},
        "plan_runs": plan_runs,
        "active_plan_runs": [item.model_dump(mode="json") for item in active_runs],
    }


if __name__ == "__main__":
    print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
