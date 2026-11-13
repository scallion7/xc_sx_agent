"""End-to-end release evaluation in disposable databases, with a controlled HTTP model."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from app.config.settings import Settings
from app.platform.agent_dataset import material, prepare
from app.platform.agent_quality import grade
from app.platform.common import PlatformError
from app.platform.evaluation_artifacts import fingerprint, load_report
from app.platform.knowledge import KnowledgePlatform
from app.platform.releases import ConfigurationReleases
from app.scripts.demo_stack import port
from app.scripts.evaluate_platform import run
from app.scripts.seed_platform import seed


class QualityTests(unittest.TestCase):
    def test_policy_assertions_require_every_claim_and_fact_from_the_right_document(
        self,
    ):
        item = dict(
            text="退款仅限七天内申请",
            chunk_id="c",
            version_id="v",
            document_id="returns",
            metadata={},
            page=1,
            start=0,
            end=9,
        )
        ref = {
            k: item[k] for k in ("chunk_id", "version_id", "page", "start", "end")
        } | {"quote": item["text"], "release_id": "r"}
        case = {
            "knowledge_release_id": "r",
            "context": {},
            "day": "2026-11-13",
            "outcome": "answerable",
            "gold_documents": ["returns"],
            "facts": [{"document_id": "returns", "quote": item["text"]}],
        }
        response = {
            "reply": item["text"],
            "requires_human": False,
            "evidence_state": "answerable",
            "citations": [ref],
        }
        lookup = [
            {
                "release_id": "r",
                "day": "2026-11-13",
                "outcome": "answerable",
                "context": {},
            }
        ]
        self.assertTrue(grade(case, response, [item], lookup)["passed"])
        self.assertFalse(
            grade(case | {"context": {"region": "上海"}}, response, [item], lookup)[
                "passed"
            ]
        )
        for change in (
            {"reply": "任意时间都能退款"},
            {"citations": []},
            {"reply": item["text"] + "。另赠一万元。"},
            {"citations": [ref | {"quote": "一万元"}]},
            {"requires_human": True},
        ):
            self.assertFalse(grade(case, response | change, [item], lookup)["passed"])
        self.assertFalse(
            grade(case | {"outcome": "policy_conflict"}, response, [item], lookup)[
                "passed"
            ]
        )

    def test_review_and_split_guards_apply_before_execution(self):
        dataset = material()
        with self.assertRaisesRegex(PlatformError, "HOLDOUT_REVIEW_REQUIRED"):
            prepare(dataset, "holdout")
        duplicate = copy.deepcopy(dataset)
        duplicate["cases"][1]["query"] = duplicate["cases"][0]["query"]
        duplicate["manifest"]["files"]["cases.json"] = fingerprint(duplicate["cases"])
        with self.assertRaisesRegex(PlatformError, "DUPLICATE_AGENT_CONVERSATION"):
            prepare(duplicate, "development")
        with self.assertRaisesRegex(PlatformError, "REVIEW_DATASET_MISMATCH"):
            prepare(
                dataset, "holdout", {"schema_version": 2, "status": "human_reviewed"}
            )

    def test_review_receipt_is_bound_to_the_actual_project_and_all_gold_facts(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from app.platform.corpora import CorpusRegistry
        from app.platform.evaluation_contract import require_production_evidence
        from app.platform.reviews import ReviewWorkspace

        with tempfile.TemporaryDirectory() as base:
            source = Settings(
                _env_file=None,
                environment="test",
                database_url="sqlite:///" + str(Path(base) / "fixture.sqlite"),
            )
            # Disposable test approvals only. Do not use or open the original review-v1 holdout.
            d = material()
            case = d["cases"][0] | {"split": "holdout", "requires_second_review": True}
            registry = CorpusRegistry(source)
            pid = registry.publish(
                {
                    "name": "synthetic-unit-fixture",
                    "version": "1",
                    "cases": [case],
                    "documents": [d["documents"][0]],
                },
                "test-account",
            )["project_id"]
            workspace = ReviewWorkspace(source, project_id=pid)
            text = d["documents"][0]["text"]
            annotation = {
                "status": "approved",
                "outcome": "answerable",
                "gold_documents": ["returns"],
                "notes": "Temporary, synthetic regression fixture",
                "evidence": [
                    {
                        "document_id": "returns",
                        "start": 0,
                        "end": len(text),
                        "quote": text,
                    }
                ],
            }
            workspace.save(pid, case["id"], annotation, 0, "fixture-one")
            data = registry.get(pid)
            with self.assertRaises(PlatformError):
                prepare(data, "holdout", workspace.export(pid))
            workspace.save(pid, case["id"], annotation, 1, "fixture-two")
            review = workspace.export(pid)
            tasks = prepare(data, "holdout", review)
            report = {
                "schema_version": 3,
                "execution_mode": "live",
                "review_status": "human_reviewed",
                "review_reference": {
                    "project_id": pid,
                    "export_sha256": fingerprint(review),
                },
                "task_specifications": tasks,
            }
            # Synthetic receipt exercises code paths only; no live provider called.
            report["cases"] = [
                {
                    "raw_model_responses": [
                        {"system_fingerprint": "synthetic-unit-fixture"}
                    ]
                }
            ]
            require_production_evidence(report, source)
            with self.assertRaisesRegex(
                PlatformError, "CONTROLLED_OR_MISSING_MODEL_RECEIPT"
            ):
                require_production_evidence(
                    report
                    | {
                        "cases": [
                            {
                                "raw_model_responses": [
                                    {"system_fingerprint": "xc-sx-controlled-demo-v1"}
                                ]
                            }
                        ]
                    },
                    source,
                )
            workspace.save(
                pid, case["id"], annotation | {"status": "draft"}, 2, "fixture-two"
            )
            with self.assertRaisesRegex(PlatformError, "LIVE_REVIEW_REQUIRED"):
                require_production_evidence(report, source)
            with (
                patch("app.scripts.evaluate_platform.build_driver") as driver,
                self.assertRaisesRegex(
                    PlatformError, "UNMETERED_EMBEDDING_NOT_SUPPORTED"
                ),
            ):
                run(
                    SimpleNamespace(live=True),
                    source.model_copy(update={"knowledge_embedding_mode": "openai"}),
                    base,
                )
            driver.assert_not_called()

    def test_two_batches_publish_the_pinned_release_without_touching_knowledge_head(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = Settings(
                _env_file=None,
                environment="test",
                tenant_id="source-owner",
                openai_api_key="offline",
                database_url="sqlite:///" + str(base / "source.sqlite"),
            )
            knowledge = KnowledgePlatform(source)
            rid = seed(source)["release_id"]
            data = material()
            data["cases"] = [
                dict(c) for c in data["cases"] if c["family"] in {"returns", "expiry"}
            ]
            # The old campaign was valid in January 2020; the Agent must use this frozen date.
            historical = data["cases"][1]
            historical.update(
                day="2020-01-15",
                outcome="answerable",
                gold_documents=["expiry"],
                facts=[{"document_id": "expiry", "quote": "过期活动已停止"}],
            )
            data["manifest"].update(case_count=2)
            data["manifest"]["files"]["cases.json"] = fingerprint(data["cases"])
            dataset = base / "data.json"
            dataset.write_text(json.dumps(data, ensure_ascii=False))
            configs = ConfigurationReleases(source)
            original = knowledge.head()
            model_port = port()
            env = dict(
                os.environ,
                OPENAI_BASE_URL=f"http://127.0.0.1:{model_port}/v1",
                OPENAI_API_KEY="controlled",
                DEMO_MODEL_POLICY_QUOTES="1",
                DATABASE_URL=source.database_url,
                TENANT_ID=source.tenant_id,
                ENVIRONMENT="test",
                PYTHONDONTWRITEBYTECODE="1",
            )
            model = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.scripts.demo_model:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(model_port),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def cli(module, args, ok=True):
                out = subprocess.run(
                    [sys.executable, "-m", "app.scripts." + module, *map(str, args)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(out.returncode == 0, ok, out.stdout + out.stderr)
                return out

            try:
                for _ in range(100):
                    try:
                        with urlopen(
                            f"http://127.0.0.1:{model_port}/health", timeout=1
                        ):
                            break
                    except OSError:
                        time.sleep(0.05)
                drafted = cli(
                    "publish_configuration",
                    [
                        "--prepare-draft",
                        "--knowledge-release-id",
                        rid,
                        "--code-commit",
                        "a" * 40,
                        "--dataset",
                        dataset,
                        "--strategies",
                        "react",
                    ],
                )
                row = json.loads(drafted.stdout)
                report = base / "report.json"
                args = [
                    "--dataset",
                    dataset,
                    "--output",
                    report,
                    "--agents",
                    "--max-runs",
                    "1",
                    "--release-id",
                ]
                cli("evaluate_platform", args + ["missing"], False)
                self.assertEqual(knowledge.head(), original)
                self.assertFalse(report.exists())
                cli("evaluate_platform", args + [row["release_id"]])
                first = load_report(report)
                self.assertEqual(first["agents"]["sample_count"], 1)
                self.assertFalse(first["agents"]["complete"])
                cli("publish_configuration", ["--report", report], False)
                self.assertEqual(configs.get(row["release_id"])["status"], "draft")
                cli("evaluate_platform", args + [row["release_id"], "--resume"])
                final = load_report(report)
                self.assertEqual(
                    final["agents"]["cases"][0], first["agents"]["cases"][0]
                )
                self.assertEqual(final["agents"]["sample_count"], 2)
                self.assertTrue(final["agents"]["passed"], final["agents"]["cases"])
                cli(
                    "publish_configuration",
                    ["--report", report, "--deploy", "--expected-version", "0"],
                )
                self.assertEqual(configs.head()["active_id"], row["release_id"])
                self.assertEqual(knowledge.head(), original)
                metadata = json.loads(report.read_text())
                metadata["raw_artifact"]["sha256"] = "0" * 64
                report.write_text(json.dumps(metadata))
                with self.assertRaisesRegex(
                    PlatformError, "EVALUATION_ARTIFACT_INVALID"
                ):
                    load_report(report)
            finally:
                model.terminate()
                model.wait(timeout=15)
