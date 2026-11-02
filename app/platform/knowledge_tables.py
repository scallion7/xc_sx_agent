"""Stable table handles shared by knowledge ingestion and release services."""

from app.infrastructure.database import platform

DOC = platform["documents"]
VER = platform["document_versions"]
CHUNK = platform["chunks"]
EMBED = platform["embedding_cache"]
JOB = platform["ingestion_jobs"]
REL = platform["knowledge_releases"]
HEAD = platform["knowledge_heads"]
CACHE = platform["knowledge_cache"]
