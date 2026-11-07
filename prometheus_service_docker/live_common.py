"""Live alert path: real Prometheus samples, no historical JSON, paid LLM off by default."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
METRIC = "pg_demo_lock_waits"
_lock = threading.Lock()
_seen = None
_workload = {"running": False, "stop": False, "phase": "idle", "error": None, "started_at": None, "stopped_at": None}


def parse_rfc3339(value):
    """Parse RFC3339, including nanosecond fractions. Year 0001 means unset."""
    if value is None:
        raise ValueError("missing timestamp")
    text = str(value).strip()
    if not text:
        raise ValueError("missing timestamp")
    if text.startswith("0001-01-01"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        digits, i = [], 0
        while i < len(tail) and tail[i].isdigit():
            digits.append(tail[i])
            i += 1
        text = head + "." + (("".join(digits) + "000000")[:6]) + tail[i:]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_prometheus_config():
    cfg = {
        "api_url": "http://prometheus:9090/",
        "postgresql_exporter_instance": "pg_exporter:9187",
        "node_exporter_instance": "node_exporter:9100",
    }
    path = os.environ.get("PROMETHEUS_CONFIG_YAML", os.path.join(os.path.dirname(__file__), "config.yaml"))
    if os.path.isfile(path):
        try:
            import yaml
            with open(path, encoding="utf-8") as fh:
                block = (yaml.safe_load(fh) or {}).get("PROMETHEUS") or {}
            for key in cfg:
                if block.get(key):
                    cfg[key] = str(block[key])
        except Exception as exc:
            sys.stderr.write(f"prometheus config skipped: {exc}\n")
    if os.environ.get("PROMETHEUS_URL"):
        cfg["api_url"] = os.environ["PROMETHEUS_URL"]
    if os.environ.get("PG_EXPORTER_INSTANCE"):
        cfg["postgresql_exporter_instance"] = os.environ["PG_EXPORTER_INSTANCE"]
    if os.environ.get("NODE_EXPORTER_INSTANCE"):
        cfg["node_exporter_instance"] = os.environ["NODE_EXPORTER_INSTANCE"]
    cfg["api_url"] = cfg["api_url"].rstrip("/") + "/"
    return cfg


def artifact_dir():
    path = os.environ.get("ARTIFACT_DIR", os.path.join(os.getcwd(), "alert_results", "live"))
    os.makedirs(path, exist_ok=True)
    return path


def _int_env(name, default, low, high):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _http_json(url, timeout):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def query_range(query, start, end, step=5):
    base = load_prometheus_config()["api_url"]
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    body = _http_json(base + "api/v1/query_range?" + params, timeout=10)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error") or "prometheus query failed")
    points = []
    for serie in (body.get("data") or {}).get("result") or []:
        for ts, val in serie.get("values") or []:
            points.append({"ts": float(ts), "value": float(val)})
    points.sort(key=lambda item: item["ts"])
    return points[:300]


def _pg_kwargs():
    kw = {
        "host": os.environ.get("PGHOST", "postgres_db"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "test"),
        "dbname": os.environ.get("PGDATABASE", "demo_live"),
        "connect_timeout": 3,
        "application_name": "demo_live_lock",
    }
    password = os.environ.get("PGPASSWORD")
    if password:
        kw["password"] = password
    return kw


def _connect():
    import psycopg2
    return psycopg2.connect(**_pg_kwargs())


def ensure_demo_schema(conn):
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS demo_lock")
    cur.execute("CREATE TABLE IF NOT EXISTS demo_lock.ticket (id integer PRIMARY KEY, n integer NOT NULL)")
    cur.execute("INSERT INTO demo_lock.ticket (id, n) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    cur.close()


def collect_lock_waits():
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pid, COALESCE(wait_event_type, ''), COALESCE(wait_event, ''),
                   COALESCE(state, ''), LEFT(COALESCE(query, ''), 300)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND application_name = 'demo_live_lock'
              AND pid <> pg_backend_pid()
            ORDER BY pid
            """
        )
        rows = [
            {"pid": row[0], "wait_event_type": row[1], "wait_event": row[2], "state": row[3], "query": row[4]}
            for row in cur.fetchall()
        ]
        cur.close()
        return rows
    finally:
        conn.close()


def _terminate_our_backends():
    conn = _connect()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND application_name = 'demo_live_lock'
              AND pid <> pg_backend_pid()
            """
        )
        cur.close()
    finally:
        conn.close()


def _count_lock_waits():
    return sum(1 for row in collect_lock_waits() if row["wait_event_type"] == "Lock")


def _hold_lock(hold_s):
    holder = waiter = None
    try:
        holder = _connect()
        ensure_demo_schema(holder)
        holder.autocommit = False
        hcur = holder.cursor()
        hcur.execute("UPDATE demo_lock.ticket SET n = n + 1 WHERE id = 1")
        waiter = _connect()
        waiter.autocommit = False
        waiter_done = threading.Event()

        def _wait():
            try:
                wcur = waiter.cursor()
                wcur.execute("SET lock_timeout = '120s'")
                wcur.execute("UPDATE demo_lock.ticket SET n = n + 1 WHERE id = 1")
            except Exception:
                pass
            finally:
                waiter_done.set()

        threading.Thread(target=_wait, daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline and not _workload["stop"]:
            if _count_lock_waits() >= 1:
                _workload["phase"] = "holding"
                break
            time.sleep(0.2)
        else:
            if not _workload["stop"]:
                _workload["error"] = "waiter did not block on demo_lock.ticket"
        end = time.time() + hold_s
        while time.time() < end and not _workload["stop"]:
            time.sleep(0.2)
    except Exception as exc:
        _workload["error"] = exc.__class__.__name__
    finally:
        _workload["stop"] = True
        try:
            _terminate_our_backends()
        except Exception:
            pass
        if "waiter_done" in locals():
            waiter_done.wait(2)
        for conn in (waiter, holder):
            if conn is None:
                continue
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        _workload["running"] = False
        _workload["phase"] = "stopped"
        _workload["stopped_at"] = time.time()


def start_workload(hold_s=None):
    hold_s = _int_env("LOCK_HOLD_SECONDS", 50, 5, 120) if hold_s is None else max(5, min(int(hold_s), 120))
    with _lock:
        if _workload["running"]:
            return {"ok": False, "error": "workload already running"}
        _workload.update(running=True, stop=False, phase="starting", error=None, started_at=time.time(), stopped_at=None, hold_s=hold_s)
    try:
        _terminate_our_backends()
    except Exception as exc:
        with _lock:
            _workload.update(running=False, phase="idle", error=str(exc.__class__.__name__))
        return {"ok": False, "error": _workload["error"]}
    threading.Thread(target=_hold_lock, args=(hold_s,), daemon=True).start()
    return {"ok": True, "hold_s": hold_s, "database": os.environ.get("PGDATABASE", "demo_live"), "schema": "demo_lock"}


def stop_workload():
    _workload["stop"] = True
    try:
        _terminate_our_backends()
    except Exception as exc:
        _workload["error"] = exc.__class__.__name__
    deadline = time.time() + 5
    while _workload["running"] and time.time() < deadline:
        time.sleep(0.1)
    return {"ok": True, "running": _workload["running"], "phase": _workload["phase"]}


def _load_seen():
    global _seen
    if _seen is None:
        path = os.path.join(artifact_dir(), "seen.json")
        try:
            _seen = set(json.load(open(path)))
        except Exception:
            _seen = set()
    return _seen


def _save_seen():
    with open(os.path.join(artifact_dir(), "seen.json"), "w", encoding="utf-8") as fh:
        json.dump(sorted(_load_seen()), fh)


def _append_event(event):
    with open(os.path.join(artifact_dir(), "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)


def _write_doc(doc):
    path = artifact_dir()
    _write_json(os.path.join(path, "current.json"), doc)
    name = "firing.json" if doc.get("status") == "firing" else "resolved.json" if doc.get("status") == "resolved" else "other.json"
    target = os.path.join(path, name)
    if not (doc.get("status") == "firing" and os.path.exists(target)):
        _write_json(target, doc)
    diag_dir = os.environ.get("GEN_DIAG_DIR", os.path.join(os.getcwd(), "gen_diagnostic_files"))
    os.makedirs(diag_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(doc["received_at"], timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    _write_json(os.path.join(diag_dir, f"alert_time_{stamp}_{doc.get('status')}.json"), doc)


def maybe_diagnose(samples, waits, labels):
    model = os.environ.get("DEMO_LLM_MODEL", MODEL)
    max_tokens = _int_env("DEMO_LLM_MAX_TOKENS", 400, 1, 800)
    timeout_s = _int_env("DEMO_LLM_TIMEOUT_S", 25, 1, 45)
    result = {"called": False, "attempted": False, "mode": "disabled", "model": model, "text": None,
              "max_tokens": max_tokens, "timeout_s": timeout_s}
    enabled = os.environ.get("DEMO_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    if not enabled:
        result["reason"] = "DEMO_LLM_ENABLED is not true; no model was called"
        return result
    key = os.environ.get("DEMO_LLM_API_KEY", "").strip()
    if not key:
        result["mode"] = "blocked_no_key"
        result["reason"] = "DEMO_LLM_API_KEY is empty. OPENAI_API_KEY is not read."
        return result
    evidence = json.dumps({"labels": labels, "samples": samples[-30:], "lock_waits": waits}, ensure_ascii=False)[:4000]
    prompt = (
        "Use only this evidence from a live PostgreSQL alert. "
        "If it is not enough, say the evidence is insufficient. Do not invent metrics or causes.\n"
        + evidence
    )
    url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    result["attempted"] = True
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode())
        text = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            result["mode"] = "error"
            result["reason"] = "model response had no text"
            return result
        result.update(called=True, mode="openrouter", text=text[:4000])
        if isinstance(payload.get("usage"), dict):
            result["usage"] = {k: payload["usage"].get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return result
    except Exception as exc:
        detail = exc.read().decode()[:300] if hasattr(exc, "read") else exc.__class__.__name__
        if key:
            detail = detail.replace(key, "[redacted]")
        result["mode"] = "error"
        result["reason"] = detail[:300]
        return result


def process_alert(payload, now=None):
    now = time.time() if now is None else now
    alerts = payload.get("alerts") or []
    if not alerts:
        raise ValueError("webhook has no alerts")
    alert = alerts[0]
    status = payload.get("status") or alert.get("status") or ""
    labels = alert.get("labels") or {}
    fingerprint = alert.get("fingerprint") or labels.get("alertname") or "none"
    starts_raw = alert.get("startsAt")
    key = f"{fingerprint}|{status}|{starts_raw}"
    received = time.time()
    with _lock:
        seen = _load_seen()
        duplicate = key in seen
        _append_event({"received_at": received, "status": status, "fingerprint": fingerprint,
                       "alertname": labels.get("alertname"), "duplicate": duplicate})
        with open(os.path.join(artifact_dir(), "alert_history.txt"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        if duplicate:
            return {"ok": True, "duplicate": True, "status": status, "fingerprint": fingerprint}
        seen.add(key)
        _save_seen()
    start_dt = parse_rfc3339(starts_raw)
    if start_dt is None:
        raise ValueError("startsAt is unset")
    end_dt = None
    if alert.get("endsAt"):
        end_dt = parse_rfc3339(alert.get("endsAt"))
    start_ts = start_dt.timestamp() - 30
    end_ts = now if end_dt is None else min(now, end_dt.timestamp())
    if end_ts <= start_ts:
        end_ts = now
    metric = labels.get("metric") or METRIC
    errors, samples, waits = [], [], []
    try:
        samples = query_range(metric, start_ts, end_ts, step=5)
    except Exception as exc:
        errors.append("prometheus: " + exc.__class__.__name__)
    try:
        waits = collect_lock_waits()
    except Exception as exc:
        errors.append("postgres: " + exc.__class__.__name__)
    if status == "firing":
        diagnosis = maybe_diagnose(samples, waits, labels)
    else:
        diagnosis = {"called": False, "attempted": False, "mode": "not_applicable", "model": os.environ.get("DEMO_LLM_MODEL", MODEL),
                     "text": None, "reason": "resolved notifications do not call the model"}
    doc = {
        "kind": "live", "source": "prometheus", "replay": False, "scenario": labels.get("scenario") or "lock_contention",
        "alertname": labels.get("alertname"), "status": status, "fingerprint": fingerprint,
        "starts_at": starts_raw, "ends_at": None if end_dt is None else alert.get("endsAt"),
        "window": {"start": start_ts, "end": end_ts},
        "metric": {"name": metric, "samples": samples},
        "lock_waits": waits, "diagnosis": diagnosis, "errors": errors, "received_at": received,
    }
    with _lock:
        _write_doc(doc)
    return doc


def readiness():
    checks = {}
    prom = load_prometheus_config()["api_url"]
    try:
        req = urllib.request.Request(prom + "-/ready")
        with urllib.request.urlopen(req, timeout=3) as resp:
            checks["prometheus"] = resp.status == 200
    except Exception:
        checks["prometheus"] = False
    try:
        body = _http_json(prom + "api/v1/rules", timeout=3)
        groups = (body.get("data") or {}).get("groups") or []
        names = [rule.get("name") for group in groups for rule in group.get("rules") or []]
        checks["rule_demo_lock"] = "DemoLockContention" in names
    except Exception:
        checks["rule_demo_lock"] = False
    exporter = os.environ.get("EXPORTER_URL", "http://pg_exporter:9187/metrics")
    try:
        req = urllib.request.Request(exporter)
        with urllib.request.urlopen(req, timeout=3) as resp:
            checks["exporter_metric"] = METRIC in resp.read().decode(errors="replace")
    except Exception:
        checks["exporter_metric"] = False
    am = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093").rstrip("/") + "/-/ready"
    try:
        req = urllib.request.Request(am)
        with urllib.request.urlopen(req, timeout=3) as resp:
            checks["alertmanager"] = resp.status == 200
    except Exception:
        checks["alertmanager"] = False
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        checks["postgres"] = True
    except Exception:
        checks["postgres"] = False
    return {"ready": all(checks.values()), "checks": checks, "metric": METRIC}


def live_status():
    ready = readiness()
    current = None
    path = os.path.join(artifact_dir(), "current.json")
    if os.path.isfile(path):
        try:
            current = json.load(open(path))
        except Exception:
            current = None
    events = []
    epath = os.path.join(artifact_dir(), "events.jsonl")
    if os.path.isfile(epath):
        lines = open(epath, encoding="utf-8").read().splitlines()[-20:]
        events = [json.loads(line) for line in lines if line.strip()]
    return {
        "ready": ready["ready"], "checks": ready["checks"], "metric": METRIC,
        "scenario": "lock_contention", "label": "Tranh chấp khóa (LIVE)",
        "workload": {k: v for k, v in _workload.items()},
        "current": current, "events": events,
        "llm_enabled": os.environ.get("DEMO_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "y", "on"},
        "model": os.environ.get("DEMO_LLM_MODEL", MODEL),
    }
