"""Live fault scenarios for the D-Bot demo.

Each scenario creates a real fault on the running PostgreSQL server, exposes a
signal that the live exporters/Prometheus can observe, and cleans up after
itself. Nothing here reads historical fixtures: the point is that the fault and
its signal exist now, in the running stack.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import psycopg2

SCHEMA = "demo_live_scenario"
APP_NAME = "demo_live_scenario"
ROWS = int(os.environ.get("SCENARIO_ROWS", "400000"))
HOT_ROWS = int(os.environ.get("SCENARIO_HOT_ROWS", "40000"))
INDEX_COUNT = int(os.environ.get("SCENARIO_INDEX_COUNT", "24"))
WRITERS = int(os.environ.get("SCENARIO_WRITERS", "12"))
OUTER_LIMIT = int(os.environ.get("SCENARIO_OUTER_LIMIT", "2000"))


def pg_kwargs() -> dict:
    kw = {
        "host": os.environ.get("PGHOST", "postgres_db"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "test"),
        "dbname": os.environ.get("PGDATABASE", "demo_live"),
        "connect_timeout": 5,
        "application_name": APP_NAME,
    }
    if os.environ.get("PGPASSWORD"):
        kw["password"] = os.environ["PGPASSWORD"]
    return kw


def connect(autocommit: bool = True):
    conn = psycopg2.connect(**pg_kwargs())
    conn.autocommit = autocommit
    return conn


@dataclass
class Scenario:
    key: str
    title: str
    alert: str
    metric: str
    threshold: float
    labels: list[str]
    setup: Callable[[], None]
    workload: Callable[[threading.Event], None]
    cleanup: Callable[[], None]
    work_sql: str


# ---------------------------------------------------------------- scenario 1
def setup_large_read() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.big_fact")
    cur.execute(
        f"CREATE TABLE {SCHEMA}.big_fact AS "
        f"SELECT g AS id, (g % 997) AS bucket, md5(g::text) AS payload "
        f"FROM generate_series(1, {ROWS}) AS g"
    )
    cur.close()
    conn.close()


def workload_large_read(stop: threading.Event) -> None:
    # Non-equi correlated scan. Plain EXISTS/IN gets decorrelated into a hash
    # semi-join (measured ~3 ms), which is not a "large nested read" at all, so
    # the predicate forces a nested loop with a per-row LIKE.
    sql = (
        f"SELECT count(*) FROM {SCHEMA}.big_fact a "
        f"JOIN {SCHEMA}.big_fact b ON b.payload LIKE '%' || left(a.payload, 6) || '%' "
        f"WHERE a.id <= {OUTER_LIMIT}"
    )
    while not stop.is_set():
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cur.fetchall()
            cur.close()
        except Exception:
            pass
        finally:
            conn.close()


def cleanup_large_read() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.big_fact")
    cur.close()
    conn.close()


# ---------------------------------------------------------------- scenario 2
def setup_bulk_delete() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.purge_target")
    cur.execute(
        f"CREATE TABLE {SCHEMA}.purge_target AS "
        f"SELECT g AS id, md5(g::text) AS payload FROM generate_series(1, {ROWS}) AS g"
    )
    # Keep dead-tuple growth observable instead of being reclaimed mid-scenario.
    cur.execute(f"ALTER TABLE {SCHEMA}.purge_target SET (autovacuum_enabled = false)")
    cur.close()
    conn.close()


def workload_bulk_delete(stop: threading.Event) -> None:
    conn = connect(autocommit=False)
    try:
        cur = conn.cursor()
        while not stop.is_set():
            try:
                cur.execute(
                    f"DELETE FROM {SCHEMA}.purge_target WHERE id IN "
                    f"(SELECT id FROM {SCHEMA}.purge_target LIMIT {HOT_ROWS})"
                )
                conn.commit()
            except Exception:
                conn.rollback()
            time.sleep(0.5)
    finally:
        conn.close()


def cleanup_bulk_delete() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.purge_target")
    cur.close()
    conn.close()


# ---------------------------------------------------------------- scenario 3
def setup_many_indexes() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.index_hot")
    cur.execute(
        f"CREATE TABLE {SCHEMA}.index_hot AS "
        f"SELECT g AS id, md5(g::text) AS payload FROM generate_series(1, {HOT_ROWS}) AS g"
    )
    cur.close()
    conn.close()


def workload_many_indexes(stop: threading.Event) -> None:
    conn = connect()
    cur = conn.cursor()
    prefix = f"idx_{SCHEMA}_hot"
    created = 0
    while not stop.is_set() and created < INDEX_COUNT:
        try:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {prefix}_{created} ON {SCHEMA}.index_hot ((id + {created}))")
            created += 1
        except Exception:
            stop.set()
            break
        time.sleep(0.2)
    while not stop.is_set():
        time.sleep(0.2)
    cur.close()
    conn.close()


def cleanup_many_indexes() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.index_hot")
    cur.close()
    conn.close()


# ---------------------------------------------------------------- scenario 4
def setup_concurrent_writes() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.write_hot")
    cur.execute(
        f"CREATE TABLE {SCHEMA}.write_hot AS "
        f"SELECT g AS id, 0 AS counter FROM generate_series(1, {HOT_ROWS}) AS g"
    )
    cur.close()
    conn.close()


def workload_concurrent_writes(stop: threading.Event) -> None:
    def worker(offset: int) -> None:
        conn = connect(autocommit=False)
        cur = conn.cursor()
        i = offset
        while not stop.is_set():
            try:
                cur.execute(f"UPDATE {SCHEMA}.write_hot SET counter = counter + 1 WHERE id = %s", (i,))
                conn.commit()
            except Exception:
                conn.rollback()
            i = (i + 1) % HOT_ROWS
        conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WRITERS)]
    for t in threads:
        t.start()
    while not stop.is_set():
        time.sleep(0.2)
    for t in threads:
        t.join(timeout=5)


def cleanup_concurrent_writes() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.write_hot")
    cur.close()
    conn.close()


# ---------------------------------------------------------------- scenario 5
def setup_slow_join() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.join_left")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.join_right")
    cur.execute(
        f"CREATE TABLE {SCHEMA}.join_left AS "
        f"SELECT g AS id, (g % 5000) AS k, md5(g::text) AS payload FROM generate_series(1, {ROWS // 4}) AS g"
    )
    cur.execute(
        f"CREATE TABLE {SCHEMA}.join_right AS "
        f"SELECT g AS id, (g % 5000) AS k, md5(g::text) AS payload FROM generate_series(1, {ROWS // 4}) AS g"
    )
    cur.close()
    conn.close()


def workload_slow_join(stop: threading.Event) -> None:
    # Equality on k lets the planner hash-join this in milliseconds, which is
    # not the intended slow join. A non-equi predicate over a bounded outer
    # range forces a nested loop with a per-row LIKE.
    sql = (
        f"SELECT count(*) FROM {SCHEMA}.join_left l "
        f"JOIN {SCHEMA}.join_right r ON r.payload LIKE '%' || left(l.payload, 6) || '%' "
        f"WHERE l.id <= {OUTER_LIMIT}"
    )

    def worker() -> None:
        while not stop.is_set():
            conn = connect()
            try:
                cur = conn.cursor()
                cur.execute(sql)
                cur.fetchall()
                cur.close()
            except Exception:
                pass
            finally:
                conn.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    while not stop.is_set():
        time.sleep(0.2)
    for t in threads:
        t.join(timeout=5)


def cleanup_slow_join() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.join_left")
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.join_right")
    cur.close()
    conn.close()


def cleanup_schema() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.close()
    conn.close()


def terminate_scenario_backends() -> int:
    """Kill background sessions started by this scenario before cleanup.

    Without this, DROP TABLE waits on a still-running query and cleanup hangs.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE application_name = %s
              AND pid <> pg_backend_pid()
            """,
            (APP_NAME,),
        )
        killed = len(cur.fetchall())
        cur.close()
        return killed
    finally:
        conn.close()


SCENARIOS = {
    "large_read": Scenario(
        key="large_read",
        title="Đọc dữ liệu quá lớn (subquery lồng)",
        alert="DemoLargeRead",
        metric="pg_demo_large_read_queries",
        threshold=1,
        labels=["Đọc dữ liệu lớn, subquery lồng"],
        setup=setup_large_read,
        workload=workload_large_read,
        cleanup=cleanup_large_read,
        work_sql=f"SELECT count(*) FROM {SCHEMA}.big_fact WHERE bucket IN (SELECT bucket FROM {SCHEMA}.big_fact WHERE payload LIKE ...)",
    ),
    "bulk_delete": Scenario(
        key="bulk_delete",
        title="Xoá dữ liệu hàng loạt",
        alert="DemoBulkDelete",
        metric="pg_demo_bulk_delete_dead_tuples",
        threshold=100000,
        labels=["Xoá dữ liệu hàng loạt"],
        setup=setup_bulk_delete,
        workload=workload_bulk_delete,
        cleanup=cleanup_bulk_delete,
        work_sql=f"DELETE FROM {SCHEMA}.purge_target WHERE id IN (SELECT id ... LIMIT {HOT_ROWS})",
    ),
    "many_indexes": Scenario(
        key="many_indexes",
        title="Quá nhiều chỉ mục (index)",
        alert="DemoTooManyIndexes",
        metric="pg_demo_many_indexes_count",
        threshold=INDEX_COUNT,
        labels=["Quá nhiều index"],
        setup=setup_many_indexes,
        workload=workload_many_indexes,
        cleanup=cleanup_many_indexes,
        work_sql=f"CREATE INDEX idx_{SCHEMA}_hot_N ON {SCHEMA}.index_hot ((id + N)) -- x{INDEX_COUNT}",
    ),
    "concurrent_writes": Scenario(
        key="concurrent_writes",
        title="Ghi dữ liệu đồng thời ồ ạt",
        alert="DemoConcurrentWrites",
        metric="pg_demo_concurrent_writes_writers",
        threshold=WRITERS / 2,
        labels=["Commit hoặc insert đồng thời nhiều"],
        setup=setup_concurrent_writes,
        workload=workload_concurrent_writes,
        cleanup=cleanup_concurrent_writes,
        work_sql=f"UPDATE {SCHEMA}.write_hot SET counter = counter + 1 WHERE id = N -- x{WRITERS} sessions",
    ),
    "slow_join": Scenario(
        key="slow_join",
        title="Join chậm + tranh chấp CPU",
        alert="DemoSlowJoin",
        metric="pg_demo_slow_join_queries",
        threshold=1,
        labels=["Join kém, tranh chấp CPU"],
        setup=setup_slow_join,
        workload=workload_slow_join,
        cleanup=cleanup_slow_join,
        work_sql=f"SELECT count(*) FROM {SCHEMA}.join_left l JOIN {SCHEMA}.join_right r ON l.k = r.k WHERE l.payload <> r.payload",
    ),
}
