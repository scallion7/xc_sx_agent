"""Review identity, evidence integrity, isolation, concurrency and offline gates."""

import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.api import create_app
from app.infrastructure.migrations.runtime import downgrade_database, upgrade_database
from app.platform.common import PlatformError
from app.platform.review_dataset import apply_review
from app.platform.reviews import ReviewWorkspace
from fastapi.testclient import TestClient

from tests.test_knowledge_platform import PlatformTestCase


class ReviewWorkspaceTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.review = ReviewWorkspace(self.config)
        self.project = self.review.create()["project_id"]
        self.case = self.review.cases[0]["id"]
        self.annotation = {
            "status": "draft",
            "outcome": "no_evidence",
            "gold_documents": [],
            "notes": "",
            "evidence": [],
        }

    def test_atomic_concurrent_save_has_one_winner_and_one_history(self):
        def save(_):
            try:
                self.review.save(self.project, self.case, self.annotation, 0, "human")
                return "ok"
            except PlatformError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("REVIEW_VERSION_CONFLICT"), 3)
        self.assertEqual(len(self.review.history(self.project, self.case)), 1)
        self.assertEqual(self.review.list_cases(self.project)["revision"], 1)

    def test_approval_requires_notes_and_exact_source_evidence(self):
        doc = self.review.documents[0]
        annotation = self.annotation | {
            "status": "approved",
            "outcome": "answerable",
            "gold_documents": [doc["document_id"]],
            "notes": "test-only manual fixture",
        }
        with self.assertRaisesRegex(PlatformError, "REVIEW_EVIDENCE_REQUIRED"):
            self.review.save(self.project, self.case, annotation, 0, "human")
        cite = {
            "document_id": doc["document_id"],
            "start": 0,
            "end": len(doc["text"]),
            "quote": doc["text"],
        }
        annotation["evidence"] = [cite | {"quote": "fabricated"}]
        with self.assertRaisesRegex(PlatformError, "REVIEW_EVIDENCE_INVALID"):
            self.review.save(self.project, self.case, annotation, 0, "human")
        annotation["evidence"] = [cite]
        result = self.review.save(self.project, self.case, annotation, 0, "human")
        self.assertEqual(result["actor"], "human")
        self.assertEqual(
            self.review.export(self.project)["status"], "pending_human_review"
        )
        with self.assertRaisesRegex(PlatformError, "REVIEW_NOTES_REQUIRED"):
            self.review.save(
                self.project, self.case, annotation | {"notes": ""}, 1, "human"
            )

    def test_import_is_atomic_draft_only_and_revision_bound(self):
        exported = self.review.export(self.project)
        for row in exported["reviews"]:
            row.update(
                approved=True,
                reviewer="forged",
                outcome="no_evidence",
                notes="imported",
            )
        bad = copy.deepcopy(exported)
        bad["reviews"][-1]["gold_documents"] = ["nonexistent"]
        with self.assertRaisesRegex(PlatformError, "REVIEW_GOLD_INVALID"):
            self.review.import_drafts(self.project, bad, 0, "real")
        self.assertEqual(self.review.list_cases(self.project)["revision"], 0)
        self.review.import_drafts(self.project, exported, 0, "real")
        rows = self.review.export(self.project)["reviews"]
        self.assertTrue(
            all(
                r["status"] == "draft" and not r["approved"] and r["reviewer"] == "real"
                for r in rows
            )
        )
        with self.assertRaisesRegex(PlatformError, "REVIEW_VERSION_CONFLICT"):
            self.review.import_drafts(self.project, exported, 0, "real")
        self.assertEqual(len(self.review.history(self.project, self.case)), 1)
        bad = exported | {"documents_sha256": "tampered"}
        with self.assertRaisesRegex(PlatformError, "REVIEW_DATASET_MISMATCH"):
            self.review.import_drafts(self.project, bad, 1, "real")

    def test_tenant_isolation_and_holdout_gate_before_any_retrieval(self):
        other = ReviewWorkspace(self.config.model_copy(update={"tenant_id": "other"}))
        with self.assertRaisesRegex(PlatformError, "REVIEW_PROJECT_NOT_FOUND"):
            other.detail(self.project, self.case)
        holdout = next(c["id"] for c in self.review.cases if c["split"] == "holdout")
        with patch("app.platform.reviews.offline_retriever") as retriever:
            with self.assertRaisesRegex(PlatformError, "HOLDOUT_REVIEW_REQUIRED"):
                self.review.diagnose(self.project, holdout)
            retriever.assert_not_called()
        self.assertEqual(other.create()["approved_count"], 0)

    def test_api_auth_actor_and_stale_revision(self):
        with TestClient(create_app(self.config)) as client:
            base = "/v1/admin/evaluation/reviews"
            self.assertEqual(client.post(base).status_code, 401)
            self.assertEqual(
                client.post(base, headers={"X-API-Key": "user"}).status_code, 403
            )
            client.headers["X-API-Key"] = "admin"
            path = base + "/" + self.project + "/cases/" + self.case
            body = {"annotation": self.annotation, "expected_revision": 0}
            self.assertEqual(
                client.put(path, json=body | {"reviewer": "forged"}).status_code, 422
            )
            response = client.put(path, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["actor"], "root")
            self.assertEqual(client.put(path, json=body).status_code, 409)
            self.assertEqual(
                client.get(path + "/history").json()["events"][0]["actor"], "root"
            )
            self.assertEqual(
                client.post(
                    base + "/" + self.project + "/import",
                    json={
                        "expected_revision": 1,
                        "document": self.review.export(self.project)
                        | {"reviews": [None] * 32},
                    },
                ).status_code,
                422,
            )

    def test_export_v2_validates_real_spans_and_timestamp(self):
        # Disposable fixture only. No production corpus annotation is generated.
        for case in self.review.cases:
            self.review.save(
                self.project,
                case["id"],
                self.annotation | {"status": "approved", "notes": "test fixture"},
                0,
                "test-reviewer",
            )
        export = self.review.export(self.project)
        self.assertEqual(
            len(apply_review(self.review.manifest, self.review.cases, export)), 32
        )
        bad = copy.deepcopy(export)
        bad["reviews"][0]["reviewed_at"] = "yesterday"
        with self.assertRaisesRegex(PlatformError, "REVIEW_TIMESTAMP_INVALID"):
            apply_review(self.review.manifest, self.review.cases, bad)
        doc = self.review.documents[0]
        bad = copy.deepcopy(export)
        bad["reviews"][0].update(
            outcome="answerable",
            gold_documents=[doc["document_id"]],
            evidence=[
                {
                    "document_id": doc["document_id"],
                    "start": 0,
                    "end": 1,
                    "quote": "fake",
                }
            ],
        )
        with self.assertRaisesRegex(PlatformError, "REVIEW_EVIDENCE_INVALID"):
            apply_review(self.review.manifest, self.review.cases, bad)

    def test_review_migration_preserves_history_and_blocks_destructive_downgrade(self):
        url = f"sqlite:///{self.temp.name}/migration.db"
        upgrade_database(url)
        workspace = ReviewWorkspace(
            self.config.model_copy(update={"database_url": url})
        )
        workspace.create()
        workspace.save(self.project, self.case, self.annotation, 0, "reviewer")
        with self.assertRaisesRegex(RuntimeError, "REVIEW_DOWNGRADE_BLOCKED"):
            downgrade_database(url, "20261025_0006")
        self.assertEqual(len(workspace.history(self.project, self.case)), 1)
