# Live scenario reconstruction — plan and fidelity contract

Goal: every D-Bot demo scenario must be able to run **live end-to-end**:
a real PostgreSQL workload creates the fault, the signal is observed from the running
stack, D-Bot diagnoses that observation, and the run is traceable and repeatable.
Historical JSON stays only as a fixture for unit tests.

## 1. Fidelity contract

A run counts as live only when all of these hold:

1. The fault is produced by SQL/activity on the running PostgreSQL container right now.
2. The signal comes from the running exporters/Prometheus (or live catalog queries),
   not from a stored fixture.
3. The diagnosis consumes that observation; `enable_prometheus` is on and the alert
   window is inside the live window (`metric_analysis_results` requires a window within
   the last 2 days).
4. A trace file records: scenario id, workload SQL, observed samples, window, alert
   payload, diagnosis invocation, result path, cleanup outcome.
5. The scenario cleans up after itself, including on failure (terminate backends,
   drop the scenario schema).

Any scenario that cannot satisfy all five is reported as `partial`, never as live.

## 2. Scenarios

| # | Scenario | Fault created | Live signal | Alert |
|---|---|---|---|---|
| 1 | Large nested read | Repeated correlated subquery over a large table | `pg_demo_long_running_queries`, `pg_demo_max_query_seconds` | `DemoLargeRead` |
| 2 | Bulk delete | Large `DELETE` in committed batches | `pg_demo_dead_tuples` | `DemoBulkDelete` |
| 3 | Too many indexes | Many single-column indexes on one hot table | `pg_demo_index_count` | `DemoTooManyIndexes` |
| 4 | Concurrent writes/commits | Many overlapping `UPDATE`/`INSERT` on one row range | `pg_demo_lock_waits`, `pg_demo_active_writers` | `DemoConcurrentWrites` |
| 5 | Slow join + CPU contention | Unindexed join repeated across parallel sessions | `pg_demo_long_running_queries` + `node_procs_running`/`node_cpu` | `DemoSlowJoin` |

Existing `DemoLockContention` (row lock) stays as the reference live path.

## 3. Pipeline stages mapped to D-Bot (Zhou et al., arXiv:2312.01454)

The paper specifies architecture and method families, not an observability schema
or a prescribed action per scenario. Scenario probes below are reconstruction
choices and are labelled as such.

| Paper stage | Repo module | Live requirement | Status |
|---|---|---|---|
| Offline document learning (§4.1) | `knowledge_base/`, `server/knowledge_base/kb_doc_api.py` | knowledge chunks + summaries | exists, unchanged |
| Offline tool preparation (§4.2) | `multiagents/tools/**` | tools invocable with real results | exists; live tools partially missing |
| Prompt generation: knowledge + tool matching (§5) | `metric_monitor/api.py`, `anomaly_analysis.py` | case-specific retrieval | exists |
| LLM diagnosis with tree search (§6) | `multiagents/multiagents.py`, `agents/` | iterative evidence-driven search | exists |
| Collaborative multi-expert diagnosis (§7) | expert configs, round-table step | cross-review for multi-cause cases | exists |
| Alert/context intake (Fig. 1c) | `prometheus_service_docker/app.py` | real Alertmanager webhook | exists for lock only |
| Metric evidence | `anomaly_analysis.metric_analysis_results` | Prometheus range read for a live window | gated by 2-day freshness |
| Workload/slow-query evidence | `metrics.get_workload_sqls`, `get_slow_queries` | live `pg_stat_statements` deltas / slow log | **missing**; now read from anomaly payload |
| Anomaly detection | `metric_monitor/anomaly_detection.py` | unchanged | exists |

## 3b. Acceptance per scenario (inference, not paper text)

Each live run must show: the initial evidence, the investigation plan and tool
calls, returned evidence, any changed hypothesis, and the final cause/advice
report. "Large query" or "lock" alone is not accepted as a root cause.

## 4. Execution order

1. Vertical slice on scenario 4 (concurrent writes) — reuses the working lock plumbing.
2. Scenario 1 and 5 (query-duration signals), scenario 2 (dead tuples), scenario 3 (index count).
3. Wire `demo` UI: one "Run live" control per scenario, status `live|replay`, source shown.
4. Compose + Apple Silicon proof run, evidence committed under `tests/evidence/`.
