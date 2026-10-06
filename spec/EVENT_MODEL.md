# Event Model Specification (CloudEvents 1.0 Compliant)

## 1. Scope & Objective

Every state change, scheduling decision, execution attempt, and verification in the orchestrator produces a versioned, immutable domain event.

All events conform strictly to the **CNCF CloudEvents 1.0 JSON Specification** to guarantee interoperability across different programming languages, message brokers, and event consumers.

---

## 2. Event Envelope Definition

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OrchestratorCloudEvent",
  "type": "object",
  "properties": {
    "specversion": { "type": "string", "const": "1.0" },
    "id": { "type": "string", "description": "Unique UUID v4 or ULID for this event" },
    "source": { "type": "string", "format": "uri-reference" },
    "type": { "type": "string", "description": "Namespaced event type identifier" },
    "time": { "type": "string", "format": "date-time" },
    "datacontenttype": { "type": "string", "const": "application/json" },
    "correlation_id": { "type": "string", "description": "Trace / Root causation ID" },
    "causation_id": { "type": "string", "description": "ID of the event that directly caused this event" },
    "sequence_number": { "type": "integer", "description": "Monotonically increasing sequence number per Work entity" },
    "data": { "type": "object", "description": "Event-specific typed payload" }
  },
  "required": ["specversion", "id", "source", "type", "time", "datacontenttype", "sequence_number", "data"]
}
```

---

## 3. Standard Event Taxonomy

| Event Type String | Emitted When | Primary Payload Fields |
| :--- | :--- | :--- |
| `io.orchestrator.work.created` | New work is enqueued. | `work_id`, `title`, `priority`, `dependencies` |
| `io.orchestrator.work.eligible` | Dependencies are satisfied. | `work_id` |
| `io.orchestrator.lease.claimed` | Worker claims work lease. | `work_id`, `worker_id`, `fencing_token`, `expires_at` |
| `io.orchestrator.lease.renewed` | Heartbeat renews lease. | `work_id`, `worker_id`, `fencing_token`, `expires_at` |
| `io.orchestrator.lease.expired` | Lease times out. | `work_id`, `stale_holder_id`, `stale_token` |
| `io.orchestrator.execution.started` | Agent process starts. | `work_id`, `execution_id`, `attempt`, `runtime_type` |
| `io.orchestrator.execution.tool_called` | Agent executes a tool. | `execution_id`, `tool_name`, `args_hash`, `duration_ms` |
| `io.orchestrator.execution.checkpointed`| Agent saves progress. | `execution_id`, `checkpoint_uri`, `step_index` |
| `io.orchestrator.execution.completed` | Agent finishes work. | `execution_id`, `exit_code`, `candidate_artifacts` |
| `io.orchestrator.verification.started` | Verification suite runs. | `execution_id`, `verifier_name`, `test_suite` |
| `io.orchestrator.verification.finished`| Verification completes. | `execution_id`, `passed`, `summary`, `details` |
| `io.orchestrator.approval.requested` | Human approval required.| `work_id`, `reason`, `action_summary` |
| `io.orchestrator.approval.granted` | Human grants approval. | `work_id`, `actor_id`, `decision_nonce` |
| `io.orchestrator.approval.rejected`| Human denies approval. | `work_id`, `actor_id`, `rejection_reason` |
| `io.orchestrator.work.succeeded` | Work successfully done. | `work_id`, `total_attempts`, `promoted_artifacts` |
| `io.orchestrator.work.failed` | Work permanently failed.| `work_id`, `failure_reason`, `attempts` |
| `io.orchestrator.work.cancelled` | Work was cancelled. | `work_id`, `cancellation_reason` |

---

## 4. Ordering, Deduplication & Replay Semantics

1. **Total Ordering Per Work:** Every event for a given `work_id` has a strictly monotonic `sequence_number`. Consumers can order events deterministically:
   $$\text{Event}_a < \text{Event}_b \iff \text{Event}_a.\text{sequence\_number} < \text{Event}_b.\text{sequence\_number}$$
2. **Deduplication:** Consumers track processed `event_id`s in a local set or table, discarding duplicate deliveries.
3. **Replayability:** The complete history of an execution can be reconstructed by querying:
   ```sql
   SELECT * FROM orchestrator_events WHERE work_id = :id ORDER BY sequence_number ASC;
   ```
