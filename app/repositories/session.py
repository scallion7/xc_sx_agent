"""会话仓储协议及 JSON / SQL 实现。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol

from sqlalchemy import delete, insert, select

from app.agent.storage import (
    delete_session,
    load_session,
    save_session,
)
from app.infrastructure.database import get_engine, sessions_table


class SessionRepository(Protocol):
    def load(self) -> dict | None: ...

    def save(
        self,
        messages: list[dict],
        summary: str | None,
        short_term_memory: dict | None,
    ) -> None: ...

    def delete(self) -> None: ...


class JsonSessionRepository:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict | None:
        return load_session(self.path)

    def save(
        self,
        messages: list[dict],
        summary: str | None,
        short_term_memory: dict | None,
    ) -> None:
        save_session(
            self.path,
            messages,
            summary,
            short_term_memory=short_term_memory,
        )

    def delete(self) -> None:
        delete_session(self.path)


class SqlSessionRepository:
    def __init__(
        self,
        database_url: str,
        session_key: str,
        user_id: str,
    ):
        self.engine = get_engine(database_url)
        self.session_key = session_key
        self.user_id = user_id

    def load(self) -> dict | None:
        statement = select(sessions_table).where(
            sessions_table.c.session_key == self.session_key,
            sessions_table.c.user_id == self.user_id,
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        if not row:
            return None
        return {
            "summary": row["summary"],
            "messages": json.loads(row["messages_json"]),
            "short_term_memory": (
                json.loads(row["short_term_memory_json"])
                if row["short_term_memory_json"]
                else None
            ),
        }

    def save(
        self,
        messages: list[dict],
        summary: str | None,
        short_term_memory: dict | None,
    ) -> None:
        payload = {
            "session_key": self.session_key,
            "user_id": self.user_id,
            "summary": summary,
            "messages_json": json.dumps(messages, ensure_ascii=False),
            "short_term_memory_json": (
                json.dumps(short_term_memory, ensure_ascii=False)
                if short_term_memory is not None
                else None
            ),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(sessions_table.c.session_key).where(
                    sessions_table.c.session_key == self.session_key,
                    sessions_table.c.user_id == self.user_id,
                )
            ).first()
            if existing:
                connection.execute(
                    sessions_table.update()
                    .where(
                        sessions_table.c.session_key == self.session_key,
                        sessions_table.c.user_id == self.user_id,
                    )
                    .values(**payload)
                )
            else:
                connection.execute(insert(sessions_table).values(**payload))

    def delete(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(sessions_table).where(
                    sessions_table.c.session_key == self.session_key,
                    sessions_table.c.user_id == self.user_id,
                )
            )


def build_session_repository(
    config,
    *,
    session_path: str | None,
    user_id: str,
) -> SessionRepository:
    resolved = session_path or config.session_path
    if config.persistence_backend == "json" or resolved.endswith(".json"):
        return JsonSessionRepository(resolved)
    return SqlSessionRepository(
        config.database_url,
        session_key=resolved,
        user_id=user_id,
    )
