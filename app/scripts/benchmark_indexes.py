"""Synthetic compiled-index scale experiment: same candidates, filters and BM25 semantics."""

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from app.config.settings import Settings
from app.platform.common import encode
from app.platform.compiled_index import clear_cache, persist
from app.platform.evaluation import percentile
from app.platform.knowledge import KnowledgePlatform, LocalHashEmbedder
from app.platform.knowledge_tables import DOC, REL
from app.platform.retrieval import HybridRetrieval, tokens
from app.platform.retrieval_profile import RetrievalProfile
from sqlalchemy import insert


def measure(size, repeats):
    with tempfile.TemporaryDirectory(prefix="ecom-index-scale-") as folder:
        config = Settings(
            _env_file=None,
            environment="test",
            database_url="sqlite:///" + folder + "/scale.db",
            knowledge_versioned_enabled=True,
            knowledge_embedding_mode="local_hash",
        )
        embedder = LocalHashEmbedder()
        service = KnowledgePlatform(config, embedder=embedder)
        items = []
        for i in range(size):
            text = (
                f"商品{i}：未拆吊牌的服饰在签收七天内可申请退货。退款在审核通过后按原支付渠道退回。"
                if i % 2 == 0
                else f"商品{i}：上海门店购买的家电可预约安装，安装辅材费用需另行确认。"
            )
            items.append(
                dict(
                    chunk_id=f"c{i:06}",
                    document_id=f"d{i // 20}",
                    version_id=f"v{i // 20}",
                    text=text,
                    page=1,
                    start=i * 100,
                    end=i * 100 + len(text),
                    line=i + 1,
                    metadata={
                        "title": f"商品政策{i}",
                        "policy_type": "after_sale",
                        "effective_date": "2026-01-01",
                        "expires_at": None,
                        "regions": ["上海"] if i % 3 == 0 else [],
                        "channels": [],
                        "products": [],
                        "source": "synthetic-scale",
                        "version": "1",
                    },
                    vector=embedder.encode_one(text),
                )
            )
        raw = encode(items)
        with service.engine.begin() as conn:
            conn.execute(
                insert(DOC),
                [
                    dict(
                        tenant_id=config.tenant_id,
                        document_id=f"d{i}",
                        title="scale",
                        deleted=0,
                        version=1,
                        updated_at=time.time(),
                    )
                    for i in range((size + 19) // 20)
                ],
            )
            conn.execute(
                insert(REL).values(
                    tenant_id=config.tenant_id,
                    release_id="scale",
                    status="ready",
                    manifest_json="[]",
                    index_json=raw,
                    embedding_model=embedder.model,
                    error_code="",
                    created_at=time.time(),
                    retained_until=time.time() + 86400,
                )
            )
            start = time.perf_counter()
            payload = persist(
                conn, config.tenant_id, "scale", items, RetrievalProfile()
            )
            build_ms = (time.perf_counter() - start) * 1000
        readers = {
            "reference": HybridRetrieval(config, embedder=embedder),
            "compiled": HybridRetrieval(
                config.model_copy(update={"compiled_retrieval_enabled": True}),
                embedder=embedder,
            ),
        }
        queries = [
            ("服饰七天退货", {}),
            ("上海家电安装费用", {"region": "上海"}),
            ("家电安装", {"region": "北京"}),
        ]
        raw_samples = {key: [] for key in readers}
        equality = []
        # Initialize jieba before timing; compilation and cold SQL/deserialization are reported separately.
        tokens("预热分词器")
        clear_cache()
        start = time.perf_counter()
        readers["compiled"]._search(
            queries[0][0], context={}, release_id="scale", mode="bm25"
        )
        cold_ms = (time.perf_counter() - start) * 1000
        for iteration in range(repeats):
            query, context = queries[iteration % len(queries)]
            results = {}
            # Alternate order to reduce a fixed execution-order advantage.
            for name in (
                list(readers) if iteration % 2 == 0 else list(reversed(readers))
            ):
                start = time.perf_counter()
                result = readers[name]._search(
                    query, context=context, release_id="scale", mode="bm25"
                )
                raw_samples[name].append((time.perf_counter() - start) * 1000)
                results[name] = (
                    result["outcome"],
                    result["results"],
                    result["citations"],
                )
            equality.append(results["reference"] == results["compiled"])
        memory = {}
        for name, reader in readers.items():
            tracemalloc.start()
            reader._search(queries[0][0], context={}, release_id="scale", mode="bm25")
            memory[name] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        return {
            "chunks": size,
            "repeats": repeats,
            "build_ms": build_ms,
            "compiled_cold_read_ms": cold_ms,
            "reference_serialized_bytes": len(raw.encode()),
            "compiled_serialized_bytes": len(encode(payload).encode()),
            "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "identical_results": all(equality),
            "latency_ms": {
                name: {
                    "p50": statistics.median(values),
                    "p95": percentile(values, 0.95),
                    "raw": values,
                }
                for name, values in raw_samples.items()
            },
            "incremental_tracemalloc_peak_bytes": memory,
        }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sizes", default="1000,10000")
    p.add_argument("--repeats", type=int, default=9)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    sizes = [int(n) for n in a.sizes.split(",")]
    if not sizes or any(n < 20 or n > 20000 for n in sizes) or a.repeats < 3:
        p.error("sizes 20..20000, repeats >=3")
    from app.scripts.evidence_identity import identity

    report = {
        "source": identity(),
        "environment": platform.platform(),
        "python": platform.python_version(),
        "paid_calls": 0,
        "scope": "synthetic ready snapshots; uncached BM25 search including DB read/filter/scoring; excludes ingestion and network; vectors are local-hash, no semantic quality claim",
        "memory_note": "incremental allocations during one warm search; excludes retained index/cache and process baseline; cache budget is serialized bytes",
        "batches": [measure(n, a.repeats) for n in sizes],
    }
    report["passed"] = all(b["identical_results"] for b in report["batches"])
    path = Path(a.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "batches": [
                    {"chunks": b["chunks"], "latency_ms": b["latency_ms"]}
                    for b in report["batches"]
                ],
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
