# Live scenario results (Mac mini, Apple Silicon)

Run date: 2026-11-17. Host: macOS arm64 (Mac mini), Docker Desktop, Compose stack
started from this checkout.

## Command

```bash
cp .env.example .env
docker compose up -d postgres_db node_exporter pg_exporter prometheus alertmanager
PGHOST=127.0.0.1 PGUSER=test PGPASSWORD=test PGDATABASE=demo_live \
PROMETHEUS_URL=http://127.0.0.1:9090 SCENARIO_ROWS=150000 HOT_ROWS=15000 INDEX_COUNT=20 \
  python -m live_scenarios.runner --scenario <key> --out-dir alert_results/live_scenarios
python live_scenarios/summarize.py
```

## Results

| Scenario | Live signal | Peak | Threshold | Cleanup | Diagnosis |
|---|---|---:|---:|---|---|
| large_read | yes | 1 | 1 | ok | not run |
| bulk_delete | yes | 150000 | 100000 | ok | not run |
| many_indexes | yes | 24 | 24 | ok | not run |
| concurrent_writes | yes | 6 | 6 | ok | not run |
| slow_join | yes | 4 | 1 | ok | not run |

Evidence artefacts: one `<scenario>.json` trace and one `<scenario>_anomaly.json`
live payload per scenario under `alert_results/live_scenarios/`.

Alert-path check (concurrent_writes): `DemoConcurrentWrites` observed in Prometheus
as `pending`, then `firing` after the 15s `for` window, and Alertmanager reported one
receiving alert. Alerts resolve once the workload stops and the metric returns to 0,
which is the intended behaviour.

## Fixes needed to reach this point

1. `metrics.py` raised at import when an anomaly file had no `exceptions`; a live
   payload has none. Now it warns and defers to Prometheus, and reads
   `slow_queries`/`workload` with defaults.
2. `run_diagnose.py` wrote empty `slow_queries`, `workload_statistics` and
   `workload_sqls` into `workload_info.json`, discarding the payload's own evidence.
3. `postgres_exporter` composes metric names as `<query_name>_<column>`; the first
   rule pass used names that never existed in Prometheus.
4. Scenario queries had to be made genuinely slow: `EXISTS`/`IN` were decorrelated
   into a hash semi-join (~3 ms), and an equality join was hash-joined in
   milliseconds. Both now use non-equi correlated predicates.
5. Cleanup could hang on `DROP TABLE` while a long query was still running; the
   runner now terminates its own tagged backends first.
6. `pg_demo_concurrent_writes` counted sessions by query text, which fluctuated
   below the alert threshold; it now counts active sessions by `application_name`.

## Not yet done

- The D-Bot diagnosis step has not been executed here: it needs the heavy `dbot`
  image build plus model credentials. Runs above stop at "signal observed + live
  anomaly payload written".
- Workload/slow-query evidence inside the diagnosis window still comes from the
  payload, not from a live `pg_stat_statements` delta collector.
- The demo UI endpoints exist (`/api/live/run/{n}`, `/api/live/run/state`) but the
  page still needs a live control wired to them.
