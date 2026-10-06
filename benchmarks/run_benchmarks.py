"""Performance benchmark suite measuring throughput and latency under load."""

import asyncio
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orchestrator.domain import (
    ActorContext,
    ActorRole,
    VerifierVerdict,
    VerificationResult,
)
from orchestrator.storage.memory import MemoryStorageBackend
from orchestrator.storage.sqlite import SQLiteStorageBackend
from orchestrator.api.service import OrchestratorService


async def benchmark_memory_throughput(n_tasks: int = 2000):
    print(f"\n[BENCHMARK] In-Memory Storage: {n_tasks} Work Units")
    storage = MemoryStorageBackend()
    service = OrchestratorService(storage=storage)

    # 1. Work creation
    t0 = time.perf_counter()
    for i in range(n_tasks):
        await service.create_work(f"BENCH-{i}", f"Task {i}", priority=i % 10)
    t_create = time.perf_counter() - t0
    rate_create = n_tasks / t_create
    print(f"  Work Creation:      {rate_create:10.1f} ops/sec  ({t_create*1000/n_tasks:.3f} ms/op)")

    # 2. Lease claims
    t0 = time.perf_counter()
    tokens = []
    for i in range(n_tasks):
        w, lease = await service.claim_work(f"BENCH-{i}", worker_id=f"worker-{i%50}")
        tokens.append(lease.fencing_token)
    t_claim = time.perf_counter() - t0
    rate_claim = n_tasks / t_claim
    print(f"  Lease Claims:       {rate_claim:10.1f} ops/sec  ({t_claim*1000/n_tasks:.3f} ms/op)")

    # 3. Submissions for verification
    t0 = time.perf_counter()
    actor = ActorContext(actor_id="worker", role=ActorRole.AGENT_WORKER)
    for i in range(n_tasks):
        await service.submit_for_verification(f"BENCH-{i}", tokens[i], actor)
    t_submit = time.perf_counter() - t0
    rate_submit = n_tasks / t_submit
    print(f"  Submit Verification:{rate_submit:10.1f} ops/sec  ({t_submit*1000/n_tasks:.3f} ms/op)")

    # 4. Verification completions
    t0 = time.perf_counter()
    verifier = ActorContext(actor_id="ci", role=ActorRole.AUTHORIZED_VERIFIER)
    for i in range(n_tasks):
        verdict = VerificationResult(
            verification_id=f"v-{i}",
            execution_id=f"exec-BENCH-{i}-1",
            verifier_name="pytest",
            verdict=VerifierVerdict.PASSED,
            passed=True,
            summary="Pass",
        )
        await service.record_verification_verdict(f"BENCH-{i}", verdict, verifier)
    t_complete = time.perf_counter() - t0
    rate_complete = n_tasks / t_complete
    print(f"  Record Verdicts:    {rate_complete:10.1f} ops/sec  ({t_complete*1000/n_tasks:.3f} ms/op)")


async def benchmark_sqlite_throughput(n_tasks: int = 500):
    with TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "bench.db"
        print(f"\n[BENCHMARK] SQLite WAL Storage: {n_tasks} Work Units (Disk I/O)")
        storage = SQLiteStorageBackend(db_file)
        service = OrchestratorService(storage=storage)

        # 1. Work creation
        t0 = time.perf_counter()
        for i in range(n_tasks):
            await service.create_work(f"SQLITE-{i}", f"Task {i}")
        t_create = time.perf_counter() - t0
        rate_create = n_tasks / t_create
        print(f"  Work Creation:      {rate_create:10.1f} ops/sec  ({t_create*1000/n_tasks:.3f} ms/op)")

        # 2. Lease claims
        t0 = time.perf_counter()
        tokens = []
        for i in range(n_tasks):
            w, lease = await service.claim_work(f"SQLITE-{i}", worker_id="worker-1")
            tokens.append(lease.fencing_token)
        t_claim = time.perf_counter() - t0
        rate_claim = n_tasks / t_claim
        print(f"  Lease Claims:       {rate_claim:10.1f} ops/sec  ({t_claim*1000/n_tasks:.3f} ms/op)")


def main():
    print("=" * 60)
    print("  ORCHESTRATOR PLATFORM PERFORMANCE BENCHMARKS")
    print("=" * 60)
    asyncio.run(benchmark_memory_throughput(2000))
    asyncio.run(benchmark_sqlite_throughput(500))
    print("=" * 60)


if __name__ == "__main__":
    main()
