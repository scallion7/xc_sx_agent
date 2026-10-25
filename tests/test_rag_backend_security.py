"""Unsupported Chroma must fail before importing it or touching persisted data."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.rag.backends import create_backend
from app.agent.rag.backends.chroma_backend import ChromaBackend
from app.agent.rag.chunker import Chunk


class RagBackendSecurityTests(unittest.TestCase):
    def test_factory_rejects_chroma_without_importing_or_creating_data(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "chroma"
            with patch.dict("sys.modules", {"chromadb": None}):
                with self.assertRaisesRegex(RuntimeError, "CHROMA_BACKEND_DISABLED"):
                    create_backend("chroma", persist_dir=target)
            self.assertFalse(target.exists())

    def test_direct_constructor_cannot_bypass_guard_or_modify_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "chroma.sqlite3"
            existing.write_bytes(b"historical-index-must-be-preserved")
            with self.assertRaisesRegex(RuntimeError, "RAG_BACKEND=numpy"):
                ChromaBackend(Path(directory))
            self.assertEqual(
                existing.read_bytes(), b"historical-index-must-be-preserved"
            )

    def test_numpy_rebuild_and_reload_without_chroma(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "index.json"
            with patch.dict("sys.modules", {"chromadb": None}):
                backend = create_backend("numpy", index_path=target)
                backend.upsert(
                    [
                        Chunk("returns", "policy.md", "退款", "七天退货"),
                        Chunk("shipping", "shipping.md", "物流", "物流查询"),
                    ],
                    [[1.0, 0.0], [0.0, 1.0]],
                    "test-embedding-v1",
                )
                restored = create_backend("numpy", index_path=target)
                result = restored.search([1.0, 0.0], 1)
                self.assertEqual(result[0].chunk.chunk_id, "returns")
                self.assertAlmostEqual(result[0].score, 1.0)
                self.assertEqual(
                    restored.expected_embedding_model(), "test-embedding-v1"
                )
