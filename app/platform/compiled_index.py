"""Version-scoped immutable index reader; tokenize documents once, preserve BM25."""

import hashlib
import json
import math
from collections import Counter, OrderedDict
from threading import RLock

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database import improvement
from app.platform.common import PlatformError, encode

TABLE = improvement["compiled_knowledge_indexes"]
_CACHE = OrderedDict()
_LOCK = RLock()
MAX_CACHE_BYTES = 32 * 1024 * 1024


def index_key(profile):
    from app.platform.retrieval import TOKENIZER_VERSION

    return hashlib.sha256(
        encode(["compiled-v1", TOKENIZER_VERSION, profile.fingerprint]).encode()
    ).hexdigest()


def compile_items(items, profile):
    from app.platform.retrieval import tokens

    return {
        "items": items,
        "counts": {
            x["chunk_id"]: dict(Counter(tokens(profile.normalize(x["text"]))))
            for x in items
        },
    }


def persist(conn, tenant, rid, items, profile):
    from app.platform.knowledge_tables import REL

    locked = conn.execute(
        update(REL)
        .where(
            REL.c.tenant_id == tenant,
            REL.c.release_id == rid,
            REL.c.status.in_(["building", "ready"]),
        )
        .values(status=REL.c.status)
    )
    if locked.rowcount != 1:
        raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
    previous = (
        conn.execute(
            select(TABLE).where(
                TABLE.c.tenant_id == tenant,
                TABLE.c.release_id == rid,
                TABLE.c.profile_hash == index_key(profile),
            )
        )
        .mappings()
        .first()
    )
    source_hash = hashlib.sha256(encode(items).encode()).hexdigest()
    if previous:
        if previous["source_hash"] != source_hash:
            raise PlatformError("COMPILED_INDEX_SOURCE_CHANGED")
        return json.loads(previous["payload_json"])
    payload = compile_items(items, profile)
    payload["checksum"] = hashlib.sha256(encode(payload).encode()).hexdigest()
    conn.execute(
        insert(TABLE).values(
            tenant_id=tenant,
            release_id=rid,
            profile_hash=index_key(profile),
            payload_json=encode(payload),
            source_hash=hashlib.sha256(encode(items).encode()).hexdigest(),
        )
    )
    return payload


class CompiledIndexReader:
    def __init__(self, service, profile):
        self.service, self.profile = service, profile

    def read(self, rid):
        key = (
            str(self.service.engine.url),
            self.service.tenant,
            rid,
            index_key(self.profile),
        )
        # Caller checks release status before this read, including warm hits.
        with _LOCK:
            if key in _CACHE:
                _CACHE.move_to_end(key)
                return _CACHE[key][0]
        with self.service.engine.connect() as conn:
            raw = conn.scalar(
                select(TABLE.c.payload_json).where(
                    TABLE.c.tenant_id == self.service.tenant,
                    TABLE.c.release_id == rid,
                    TABLE.c.profile_hash == index_key(self.profile),
                )
            )
        if raw is None:
            release = self.service.release(rid)
            if release["status"] != "ready":
                raise PlatformError("KNOWLEDGE_RELEASE_NOT_READY")
            items = json.loads(release["index_json"])
            try:
                with self.service.engine.begin() as conn:
                    payload = persist(
                        conn, self.service.tenant, rid, items, self.profile
                    )
                raw = encode(payload)
            except IntegrityError:
                return self.read(rid)
        else:
            payload = json.loads(raw)
        checksum = payload.get("checksum")
        if (
            checksum
            != hashlib.sha256(
                encode({k: v for k, v in payload.items() if k != "checksum"}).encode()
            ).hexdigest()
        ):
            raise PlatformError("COMPILED_INDEX_HASH_MISMATCH")
        size = len(raw.encode())
        with _LOCK:
            if size <= MAX_CACHE_BYTES:
                _CACHE[key] = (payload, size)
                _CACHE.move_to_end(key)
                while (
                    len(_CACHE) > 8
                    or sum(v[1] for v in _CACHE.values()) > MAX_CACHE_BYTES
                ):
                    _CACHE.popitem(last=False)
        return payload

    @staticmethod
    def lexical(query, items, compiled):
        from app.platform.retrieval import tokens

        terms = set(tokens(query))
        counts = [compiled["counts"][x["chunk_id"]] for x in items]
        lengths = [sum(c.values()) for c in counts]
        avg = sum(lengths) / max(len(items), 1) or 1
        # Scope/date/deletion filters change the corpus: recompute document
        # frequencies on the eligible subset, exactly matching the reference path.
        df = Counter(term for c in counts for term in c if term in terms)
        return [
            sum(
                math.log(1 + (len(items) - df[t] + 0.5) / (df[t] + 0.5))
                * c[t]
                * 2.2
                / (c[t] + 1.2 * (0.25 + 0.75 * length / avg))
                for t in terms
                if c.get(t)
            )
            for c, length in zip(counts, lengths, strict=True)
        ]


def clear_cache():
    with _LOCK:
        _CACHE.clear()
