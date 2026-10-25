"""Compatibility imports for the former, NumPy-free backend name.

New code uses cosine_backend.CosineBackend and similarity.cosine_similarity.
The aliases preserve constructor, identity and persisted JSON compatibility.
"""

from app.agent.rag.backends.cosine_backend import CosineBackend as NumpyBackend
from app.agent.rag.similarity import cosine_similarity as _cosine

__all__ = ["NumpyBackend", "_cosine"]
