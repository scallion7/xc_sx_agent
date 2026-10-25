"""Regression checks for old imports, persisted indexes and test entry points."""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.rag.backends import CosineBackend, create_backend
from app.agent.rag.chunker import Chunk
from app.agent.rag.retriever import KnowledgeRetriever
from app.agent.tools.knowledge import KnowledgeService
from app.config.settings import Settings


class BackendCompatibilityTests(unittest.TestCase):
    def test_all_public_import_paths_resolve_to_same_backend(self):
        for module in (
            "app.agent.rag",
            "app.agent.rag.backends",
            "app.agent.rag.backends.numpy_backend",
        ):
            with self.subTest(module=module):
                self.assertIs(
                    importlib.import_module(module).NumpyBackend, CosineBackend
                )
        from app.agent.rag import CosineBackend as exported

        self.assertIs(exported, CosineBackend)

    def test_old_json_index_loads_without_rewriting_and_preserves_ranking(self):
        # Literal old-format data, independent of the new writer implementation.
        payload = {
            "embedding_model": "existing-embedding",
            "chunks": [
                {
                    "chunk_id": "return",
                    "doc": "policy.md",
                    "section": "退货",
                    "text": "七天退货",
                    "metadata": {"version": "1"},
                },
                {
                    "chunk_id": "shipping",
                    "doc": "shipping.md",
                    "section": "物流",
                    "text": "配送查询",
                    "metadata": {},
                },
            ],
            "vectors": [[1.0, 0.0], [0.0, 1.0]],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            original = path.read_bytes()
            for name in ("cosine", "numpy", "NUMPY", ""):
                with self.subTest(name=name):
                    backend = create_backend(name, index_path=path)
                    self.assertEqual(backend.size(), 2)
                    self.assertEqual(
                        backend.expected_embedding_model(), "existing-embedding"
                    )
                    hits = backend.search([1.0, 0.0], 2)
                    self.assertEqual(
                        [h.chunk.chunk_id for h in hits], ["return", "shipping"]
                    )
                    self.assertEqual([h.score for h in hits], [1.0, 0.0])
                    self.assertEqual(hits[0].chunk.metadata, {"version": "1"})
                    self.assertEqual(path.read_bytes(), original)

    def test_new_index_remains_readable_through_old_constructor(self):
        from app.agent.rag.backends.numpy_backend import NumpyBackend

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            backend = CosineBackend(path)
            backend.upsert(
                [Chunk("refund", "policy", "退款", "退款规则")],
                [[0.0, 1.0]],
                "embedding-v1",
            )
            restored = NumpyBackend(path)
            self.assertEqual(restored.search([0.0, 1.0], 1)[0].chunk.chunk_id, "refund")
            self.assertEqual(restored.expected_embedding_model(), "embedding-v1")

    def test_environment_alias_and_default_create_equivalent_services(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            default = Settings(_env_file=None)
            self.assertEqual(default.rag_backend, "cosine")
            for name in ("cosine", "numpy"):
                with (
                    self.subTest(name=name),
                    patch.dict(os.environ, {"RAG_BACKEND": name}),
                ):
                    config = Settings(
                        _env_file=None, kb_index_path=f"{directory}/index.json"
                    )
                    self.assertEqual(config.rag_backend, name)
                    backend = KnowledgeService(config)._create_backend()
                    self.assertIsInstance(backend, CosineBackend)

    def test_index_builder_accepts_both_names(self):
        from app.scripts.build_kb_index import _build_backend

        canonical, target = _build_backend("cosine")
        legacy, old_target = _build_backend("numpy")
        self.assertIsInstance(canonical, CosineBackend)
        self.assertIsInstance(legacy, CosineBackend)
        self.assertEqual(target, old_target)

    def test_embedding_model_mismatch_still_fails_closed(self):
        class DifferentEmbedder:
            model = "different-model"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            create_backend("numpy", index_path=path).upsert(
                [Chunk("refund", "policy", "退款", "退款规则")],
                [[1.0]],
                "original-model",
            )
            retriever = KnowledgeRetriever(
                DifferentEmbedder(), create_backend("cosine", index_path=path)
            )
            with self.assertRaisesRegex(ValueError, "不一致"):
                retriever.load()

    def test_old_similarity_import_keeps_zero_and_dimension_checks(self):
        from app.agent.rag.backends.numpy_backend import _cosine
        from app.agent.rag.similarity import cosine_similarity

        self.assertIs(_cosine, cosine_similarity)
        self.assertEqual(_cosine([0.0], [1.0]), 0.0)
        self.assertEqual(_cosine([-1.0], [1.0]), -1.0)
        with self.assertRaises(ValueError):
            _cosine([1.0], [1.0, 0.0])


class EntryPointCompatibilityTests(unittest.TestCase):
    def test_governed_service_aliases_preserve_class_identity(self):
        from app.platform.collaboration import Collaboration
        from app.platform.connectors import GovernedMCPToolCaller, GovernedRemote
        from app.platform.conversation_handoff import ConversationHandoffService

        self.assertIs(Collaboration, ConversationHandoffService)
        self.assertIs(GovernedRemote, GovernedMCPToolCaller)

    def test_old_test_modules_load_explicitly_without_duplicate_discovery(self):
        names = {
            "test_stage_22_quality_gate": "test_operations_quality",
            "test_secondary_development": "test_application_integration",
            "test_strategy_development": "test_plan_execution",
            "test_agent_deepening": "test_after_sale_policy",
        }
        loader = unittest.TestLoader()
        for old, new in names.items():
            with self.subTest(old=old):
                old_module = importlib.import_module(f"tests.{old}")
                new_module = importlib.import_module(f"tests.{new}")
                expected = loader.loadTestsFromModule(new_module).countTestCases()
                self.assertGreater(expected, 0)
                self.assertEqual(
                    loader.loadTestsFromName(f"tests.{old}").countTestCases(), expected
                )
                self.assertEqual(
                    loader.loadTestsFromModule(
                        old_module, pattern="test*.py"
                    ).countTestCases(),
                    0,
                )
        self.assertEqual(loader.errors, [])
        old_case = loader.loadTestsFromName(
            "tests.test_stage_22_quality_gate.DataLifecycleTests.test_cleanup_dry_run_protects_active_plan"
        )
        self.assertTrue(unittest.TextTestRunner().run(old_case).wasSuccessful())
