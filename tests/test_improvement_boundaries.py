"""Boundary tests that reject forged sources, bypasses and lifecycle races."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.agent.base import BaseConversationAgent
from app.agent.tools.intake import register_intake_tools
from app.agent.tools.registry import ToolExecutionContext, ToolRegistry
from app.api import create_app
from app.platform.common import PlatformError
from app.platform.compiled_index import TABLE, CompiledIndexReader, clear_cache
from app.platform.corpora import CorpusRegistry, validate_corpus
from app.platform.evidence_support import check_claims
from app.platform.retrieval import HybridRetrieval
from app.platform.retrieval_profile import RetrievalProfile
from app.platform.review_dataset import apply_review, load_frozen
from app.platform.reviews import ReviewWorkspace
from app.repositories.runs import RunRepository
from app.schemas.response import CustomerServiceResponse
from app.services.run_context import RunContext
from app.web.services import build_api_services
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.test_knowledge_platform import PlatformTestCase


class ImprovementBoundaryTests(PlatformTestCase):
    def test_invalid_dataset_returns_validation_error_without_500(self):
        _, docs, cases = load_frozen()
        data = {"name": "edge", "version": "1", "documents": docs, "cases": cases}
        for mutation in (
            lambda x: x["documents"][0].pop("metadata"),
            lambda x: x["cases"][0].update(day="bad"),
            lambda x: x["cases"][0].update(context=None),
            lambda x: x["cases"][0].update(gold_documents=[{}]),
            lambda x: x["cases"][0].update(requires_second_review="true"),
        ):
            value = copy.deepcopy(data)
            mutation(value)
            with self.subTest(value=value["cases"][0]["id"]):
                with self.assertRaises(PlatformError) as error:
                    validate_corpus(value)
                self.assertEqual(error.exception.status, 422)
        with TestClient(create_app(self.config)) as client:
            bad = copy.deepcopy(data)
            bad["documents"][0].pop("metadata")
            self.assertEqual(
                client.post(
                    "/v1/admin/evaluation/datasets",
                    json=bad,
                    headers={"X-API-Key": "admin"},
                ).status_code,
                422,
            )
            self.assertEqual(
                client.get(
                    "/v1/admin/evaluation/candidates", headers={"X-API-Key": "user"}
                ).status_code,
                403,
            )

    def test_custom_dataset_review_uses_its_own_documents(self):
        docs = [
            {
                "document_id": "new-policy",
                "text": "测试政策全文。",
                "metadata": {
                    "title": "测试",
                    "effective_date": "2020-01-01",
                    "source": "synthetic",
                    "version": "1",
                },
            }
        ]
        cases = [
            {
                "id": "new-case",
                "family": "new",
                "query": "新政策",
                "day": "2026-11-09",
                "split": "development",
                "context": {},
                "outcome": "answerable",
                "gold_documents": ["new-policy"],
                "requires_second_review": True,
            }
        ]
        pid = CorpusRegistry(self.config).publish(
            {"name": "new", "version": "1", "documents": docs, "cases": cases}, "admin"
        )["project_id"]
        w = ReviewWorkspace(self.config, project_id=pid)
        a = {
            "status": "approved",
            "outcome": "answerable",
            "gold_documents": ["new-policy"],
            "notes": "test fixture only",
            "evidence": [
                {
                    "document_id": "new-policy",
                    "start": 0,
                    "end": len(docs[0]["text"]),
                    "quote": docs[0]["text"],
                }
            ],
        }
        w.save(pid, "new-case", a, 0, "test-a")
        w.save(pid, "new-case", a, 1, "test-b")
        exported = w.export(pid)
        self.assertEqual(
            apply_review(w.manifest, w.cases, exported, documents=w.documents)[0][
                "review_status"
            ],
            "human_reviewed",
        )
        exported["reviews"][0]["second_reviewer"] = "test-a"
        with self.assertRaisesRegex(PlatformError, "SECOND_REVIEW"):
            apply_review(w.manifest, w.cases, exported, documents=w.documents)

    def test_draft_tools_require_current_user_source_and_cannot_invent_fields(self):
        services = build_api_services(self.config)
        registry = ToolRegistry()
        register_intake_tools(
            registry, self.config, services.commerce, services.after_sale
        )
        ctx = ToolExecutionContext(user_id="user-1", session_id="user-1:s")
        args = {
            "command": "update",
            "expected_revision": 0,
            "fields": {"order_id": "ORD-OTHER"},
        }

        def call(args, context=ctx):
            return json.loads(
                registry.execute("update_after_sale_intake", args, context)
            )

        self.assertEqual(call(args)["error_code"], "INTAKE_DURABLE_RUN_REQUIRED")
        repo = RunRepository(self.config.database_url)
        row = repo.submit(
            {"message": "补充订单 ORD-20240125-006"}, "user-1", "user-1:s", "test"
        )
        row = repo.claim("test-worker", run_id=row["run_id"])
        with RunContext(repo, row):
            self.assertEqual(call(args)["error_code"], "INTAKE_FIELD_SOURCE_REQUIRED")
            self.assertEqual(
                call(
                    args, ToolExecutionContext(user_id="user-2", session_id="user-2:s")
                )["error_code"],
                "INTAKE_RUN_SCOPE_MISMATCH",
            )
            self.assertEqual(
                call({**args, "command": "withdraw", "fields": {}})["error_code"],
                "INTAKE_WITHDRAW_NOT_REQUESTED",
            )
            good = {**args, "fields": {"order_id": "ORD-20240125-006"}}
            first = call(good)
            second = call(good)
            self.assertTrue(first["success"], first)
            self.assertEqual(first["receipt_revision"], second["receipt_revision"])
            self.assertEqual(
                first["intake"]["field_sources"]["order_id"]["run_id"], row["run_id"]
            )
            self.assertEqual(
                call({**good, "new_task": True})["error_code"],
                "INTAKE_NEW_TASK_NOT_REQUESTED",
            )
        self.assertTrue(registry.is_draft_only("update_after_sale_intake"))
        from app.agent.strategies import ExecutionPlan, PlanExecutor, PlanStep
        from app.agent.tools.manager import ToolManager

        manager = ToolManager(
            registry=registry, execution_context=ctx, after_sale_enabled=True
        )
        with RunContext(repo, row):
            result = PlanExecutor(manager, event_bus=manager.event_bus).execute(
                ExecutionPlan(
                    goal="保存草稿",
                    steps=[
                        PlanStep(
                            step_id="draft",
                            description="按用户原文更新草稿",
                            tool_name="update_after_sale_intake",
                            arguments=good,
                        )
                    ],
                )
            )
        self.assertEqual(result.steps[0].status, "completed", result)
        self.assertFalse(manager.is_read_only("update_after_sale_intake"))

    def test_compiled_integrity_gc_and_rollback(self):
        rid = self.publish([self.ingest()])
        reader = CompiledIndexReader(self.k, RetrievalProfile())
        reader.read(rid)
        clear_cache()
        with self.k.engine.begin() as conn:
            conn.execute(update(TABLE).values(payload_json='{"items":[],"counts":{}}'))
        with self.assertRaisesRegex(PlatformError, "HASH_MISMATCH"):
            reader.read(rid)
        self.k.activate("", self.k.head()["version"], "admin")
        self.k.collect("admin", now=9999999999)
        with self.k.engine.connect() as conn:
            self.assertIsNone(conn.scalar(select(TABLE.c.release_id)))
        with self.assertRaisesRegex(PlatformError, "NOT_READY"):
            reader.read(rid)
        with self.assertRaisesRegex(PlatformError, "NOT_READY"):
            self.k.activate(rid, self.k.head()["version"], "admin")

    def test_adjacent_condition_cannot_be_omitted(self):
        evidence = [{"chunk_id": "x", "text": "仅限上海门店购买。可以退货。"}]
        self.assertFalse(check_claims(["可以退货"], evidence)["supported"])
        self.assertTrue(
            check_claims(["仅限上海门店购买", "可以退货"], evidence)["supported"]
        )

    def test_agent_finalizer_blocks_unsupported_claims_with_valid_citation(self):
        rid = self.publish([self.ingest(text="未拆吊牌的服饰在签收七天内可以退货。")])
        evidence = HybridRetrieval(self.config, embedder=self.embedding).search(
            "服饰七天退货", release_id=rid
        )
        agent = Mock(spec=BaseConversationAgent)
        agent.config = self.config.model_copy(
            update={"answer_evidence_check_enabled": True}
        )
        agent.guardrails = SimpleNamespace(redact=lambda x: x)
        agent._extract_structured_response = lambda _: CustomerServiceResponse(
            intent="after_sale",
            confidence=1,
            reply="所有商品在30天内可以退货。",
            requires_human=False,
        )
        agent.tool_registry = SimpleNamespace(
            knowledge_service=SimpleNamespace(last_result=evidence), action_service=None
        )
        agent.memory_manager = Mock()
        agent.raw_messages = []
        agent.history_threshold = 50
        agent.save = Mock()
        agent.event_bus = Mock()
        result = BaseConversationAgent._finalize_turn(agent, "test")
        self.assertTrue(result.requires_human)
        self.assertEqual(result.evidence_state, "unsupported_claim")
        self.assertTrue(agent.event_bus.emit.called)

    def test_per_user_limit_preserves_other_user_capacity(self):
        repo = RunRepository(self.config.database_url)
        repo.submit(
            {"message": "x"}, "a", "a:1", "1", max_pending=3, max_pending_per_user=1
        )
        from app.repositories.runs import RunError

        with self.assertRaises(RunError):
            repo.submit(
                {"message": "x"}, "a", "a:2", "2", max_pending=3, max_pending_per_user=1
            )
        self.assertEqual(
            repo.submit(
                {"message": "x"}, "b", "b:1", "1", max_pending=3, max_pending_per_user=1
            )["status"],
            "queued",
        )

    def test_draft_commit_rejects_lease_transfer_and_handoff_aba(self):
        from app.infrastructure.database import agent_runs_table, platform
        from app.platform.after_sale_intake import AfterSaleIntake
        from app.platform.conversation_handoff import ConversationHandoffService
        from app.repositories.runs import RunInterrupted

        services = build_api_services(self.config)
        flow = AfterSaleIntake(services, "user-1")
        own = ConversationHandoffService(self.config).ensure("user-1", "user-1:s")
        repo = RunRepository(self.config.database_url)
        run = repo.submit(
            {"message": "补充原因"},
            "user-1",
            "user-1:s",
            "fence",
            runtime_snapshot={
                "tenant_id": self.config.tenant_id,
                "collaboration": True,
                "ownership_version": own["version"],
            },
        )
        row = repo.claim("first", run_id=run["run_id"])
        draft = flow.create("s", "fence")
        source = {"run_id": run["run_id"], "session_key": "user-1:s"}
        with RunContext(repo, row):
            with repo.engine.begin() as conn:
                conn.execute(
                    update(agent_runs_table)
                    .where(agent_runs_table.c.run_id == run["run_id"])
                    .values(fence=row["fence"] + 1)
                )
            with self.assertRaisesRegex(RunInterrupted, "RUN_FENCED"):
                flow.command(
                    draft["intake_id"],
                    "update",
                    0,
                    "fenced",
                    {"reason": "尺寸不合适"},
                    source=source,
                )
            self.assertEqual(flow.get(draft["intake_id"])["revision"], 0)
            with repo.engine.begin() as conn:
                conn.execute(
                    update(agent_runs_table)
                    .where(agent_runs_table.c.run_id == run["run_id"])
                    .values(fence=row["fence"])
                )
                table = platform["conversation_ownership"]
                conn.execute(update(table).values(version=table.c.version + 2))
            with self.assertRaisesRegex(RunInterrupted, "RUN_HANDOFF"):
                flow.command(
                    draft["intake_id"],
                    "update",
                    0,
                    "aba",
                    {"reason": "尺寸不合适"},
                    source=source,
                )
            self.assertEqual(flow.get(draft["intake_id"])["revision"], 0)

    def test_migration_preserves_nonempty_corpus_on_downgrade(self):
        from app.infrastructure.migrations.runtime import (
            downgrade_database,
            migration_status,
            upgrade_database,
        )

        url = f"sqlite:///{self.temp.name}/migration-corpus.db"
        upgrade_database(url)
        registry = CorpusRegistry(self.config.model_copy(update={"database_url": url}))
        _, docs, cases = load_frozen()
        pid = registry.publish(
            {"name": "migration", "version": "1", "documents": docs, "cases": cases},
            "test",
        )["project_id"]
        with self.assertRaisesRegex(RuntimeError, "IMPROVEMENT_DOWNGRADE_BLOCKED"):
            downgrade_database(url, "20261105_0008")
        self.assertEqual(len(registry.get(pid)["cases"]), len(cases))
        self.assertTrue(migration_status(url)["up_to_date"])

    def test_authenticated_form_edit_replaces_old_model_field_source(self):
        from app.platform.after_sale_intake import TABLE as INTAKE
        from app.platform.after_sale_intake import AfterSaleIntake
        from app.platform.common import encode

        services = build_api_services(self.config)
        flow = AfterSaleIntake(services, "user-1")
        row = flow.create("source", "source")
        payload = {
            k: v
            for k, v in row.items()
            if k not in {"intake_id", "session_id", "revision", "updated_at"}
        }
        payload["field_sources"] = {
            "reason": {"origin": "user_message", "run_id": "prior"},
            "evidence_ids": {"origin": "user_message", "run_id": "prior"},
        }
        with flow.engine.begin() as conn:
            conn.execute(
                update(INTAKE)
                .where(INTAKE.c.intake_id == row["intake_id"])
                .values(payload_json=encode(payload))
            )
        changed = flow.command(
            row["intake_id"],
            "update",
            0,
            "edit",
            {"reason": "手动更正", "order_id": "ORD-20240125-006"},
        )
        self.assertEqual(
            changed["field_sources"]["reason"]["origin"], "authenticated_api"
        )
        self.assertNotIn("run_id", changed["field_sources"]["reason"])
        self.assertNotIn("evidence_ids", changed["field_sources"])
