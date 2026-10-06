# Red Team 3 — "Kill This As a Production System" (Scale Bottlenecks)

## 1. Executive Summary & Scaling Stress Test

We stress-test the proposed architecture at four orders of magnitude:
- **10 Concurrent Agents:** Single developer / small team
- **100 Concurrent Agents:** Enterprise engineering department
- **1,000 Concurrent Agents:** Scaled automation platform / multi-repo CI fleet
- **10,000 Concurrent Agents:** Cloud-scale multi-tenant orchestration service

---

## 2. Bottleneck Analysis Across Scales

```
+-----------------------------------------------------------------------------------+
|                        SCALING BOTTLENECK PROGRESSION                             |
+-----------------------------------------------------------------------------------+
| SCALE      | CRITICAL BOTTLENECK                | ARCHITECTURAL MITIGATION        |
+------------+------------------------------------+---------------------------------+
| 10 Agents  | Local file lock contention;        | Use SQLite WAL mode or          |
|            | disk I/O on git checkouts.         | Postgres; shallow git clones.   |
|                                                                                   |
| 100 Agents | Postgres connection exhaustion;    | PgBouncer connection pooling;   |
|            | heartbeat write amplification.     | Adaptive heartbeats (30s+);     |
|            |                                    | Redis lease cache (optional).   |
|                                                                                   |
| 1,000      | Outbox table bloat;                | Partitioned outbox table;       |
| Agents     | Scheduler polling overhead;        | Postgres LISTEN/NOTIFY or       |
|            | Model provider rate limits.        | distributed queue; token token- |
|            |                                    | bucket rate limiting per org.   |
|                                                                                   |
| 10,000     | Database write throughput limits   | Sharding by Tenant/Workspace ID;|
| Agents     | (IOPS on single Postgres WAL);     | Kafka event stream;             |
|            | Multi-tenant storage costs.        | Object store (S3) for artifacts.|
+-----------------------------------------------------------------------------------+
```

---

## 3. Deep Dive: Heartbeat Write Amplification

### The Bottleneck:
If 10,000 active agents send a heartbeat every 5 seconds, the database must process:
$$\frac{10,000 \text{ agents}}{5 \text{ seconds}} = 2,000 \text{ write transactions / second}$$
In Postgres, 2,000 writes/sec to the same `orchestrator_lease` table generates massive WAL write volume, table bloat, and vacuuming pressure, degrading performance for scheduling and state transitions.

### Architectural Solutions:
1. **Adaptive Heartbeat Windows:**
   - Instead of 5 seconds, scale heartbeat intervals based on task length:
     - Short tasks (<2 min): 15-second heartbeat, 45-second lease TTL.
     - Long tasks (15–60 min): 60-second heartbeat, 3-minute lease TTL.
2. **In-Memory / Redis Lease Extension Layer:**
   - At extreme scale (10,000+), route heartbeats to an in-memory Redis cluster with TTLs. The database is only touched when a lease is initially acquired or terminated.
3. **Optimistic Heartbeat Coalescing:**
   - If a worker is actively emitting tool call events, the event stream itself serves as an implicit heartbeat, eliminating redundant heartbeat writes.

---

## 4. Scheduling Fairness & Resource Contention

Under 10,000 queued tasks, naive FIFO queueing results in:
1. **Tenant Starvation:** A single runaway user queuing 5,000 tasks monopolizes all worker sandboxes.
2. **Priority Inversion:** High-priority bug fixes wait behind low-priority documentation sweeps.

### Architectural Solution:
**Two-Level Deterministic Fair-Share Scheduler:**
1. Group eligible tasks by `(tenant_id, priority)`.
2. Apply Deficit Round Robin (DRR) or Weighted Fair Queueing (WFQ) across active tenants.
3. Enforce strict concurrency quotas per tenant (`max_active_executions = N`).

---

## 5. Storage & Artifact Explosion

10,000 coding tasks generating full git diffs, build outputs, and logs can produce tens of gigabytes per hour.
- **Rule:** Never store binary artifacts, build caches, or raw model logs in the relational database.
- **Rule:** Store large artifacts in content-addressed object storage (S3 / GCS / local blob store). The database stores only the SHA-256 digest, URI, and byte length metadata.
- **Rule:** Implement configurable retention policies: prune ephemeral execution logs after 30 days while retaining terminal state transitions permanently.
