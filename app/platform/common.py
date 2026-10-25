import json
import time
from uuid import uuid4

from sqlalchemy import insert

from app.infrastructure.database import platform


class PlatformError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit(conn, tenant, actor, action, subject, detail=None):
    conn.execute(
        insert(platform["platform_audits"]).values(
            audit_id=uuid4().hex,
            tenant_id=tenant,
            actor=actor,
            action=action,
            subject=subject,
            detail_json=encode(detail or {}),
            created_at=time.time(),
        )
    )
