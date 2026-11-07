"""Native Prometheus + Alertmanager + postgres_exporter proof for one lock scenario.

Docker Compose is not started here. Hostnames in the repo files are rewritten to
127.0.0.1 only inside this process. The rule file and the exporter query file are
the ones shipped in the repo.
"""
import json
import os
import signal
import urllib.error
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = "/tmp/db-gpt-bins"
RUN = "/tmp/db-gpt-live/run"
PG = "/usr/lib/postgresql/16/bin"
EVIDENCE = os.path.join(ROOT, "tests", "evidence", "live_lock.json")


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def prom_query(expr):
    return http_json("http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": expr}))


def metric_value():
    result = (prom_query("pg_demo_lock_waits").get("data") or {}).get("result") or []
    if not result:
        return None
    return float(result[0]["value"][1])


def alert_states():
    body = http_json("http://127.0.0.1:9090/api/v1/alerts")
    return [item.get("state") for item in (body.get("data") or {}).get("alerts") or []
            if (item.get("labels") or {}).get("alertname") == "DemoLockContention"]


def post(path):
    req = urllib.request.Request("http://127.0.0.1:8023" + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def get(path):
    return http_json("http://127.0.0.1:8023" + path, timeout=20)


def wait_until(fn, timeout, pause=1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            last = None
        if last:
            return last
        time.sleep(pause)
    raise TimeoutError(repr(last))


def main():
    os.makedirs(RUN, exist_ok=True)
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    procs = []
    evidence = {"docker": False, "phases": {}, "started_at": time.time()}
    pg_dir = os.path.join(RUN, "pg")

    def spawn(cmd, env, log_name):
        log = open(os.path.join(RUN, log_name), "w")
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(proc)
        return proc

    def shutdown():
        try:
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8023/live/stop", data=b"", method="POST"), timeout=5)
        except Exception:
            pass
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if os.path.isdir(pg_dir):
            subprocess.run([os.path.join(PG, "pg_ctl"), "-D", pg_dir, "-m", "fast", "stop"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    try:
        rules = os.path.join(ROOT, "prometheus_service_docker", "demo_rules.yml")
        queries = os.path.join(ROOT, "prometheus_service_docker", "demo_queries.yaml")
        promtool = os.path.join(BIN, "prometheus-2.53.2.linux-amd64", "promtool")
        amtool = os.path.join(BIN, "alertmanager-0.27.0.linux-amd64", "amtool")
        subprocess.run([promtool, "check", "rules", rules], check=True)
        am_src = open(os.path.join(ROOT, "prometheus_service_docker", "alertmanager.yml"), encoding="utf-8").read()
        am_path = os.path.join(RUN, "alertmanager.yml")
        open(am_path, "w").write(am_src.replace("python_app", "127.0.0.1"))
        subprocess.run([amtool, "check-config", am_path], check=True)
        open(os.path.join(RUN, "prometheus.yml"), "w").write(
            "global:\n  scrape_interval: 5s\n  evaluation_interval: 5s\n  scrape_timeout: 4s\n"
            "alerting:\n  alertmanagers:\n    - static_configs:\n        - targets: ['127.0.0.1:9093']\n"
            f"rule_files:\n  - {rules}\n"
            "scrape_configs:\n"
            "  - job_name: prometheus\n    static_configs:\n      - targets: ['localhost:9090']\n"
            "  - job_name: postgres_exporter\n    static_configs:\n      - targets: ['127.0.0.1:9187']\n"
        )
        if os.path.exists(pg_dir):
            subprocess.run([os.path.join(PG, "pg_ctl"), "-D", pg_dir, "-m", "immediate", "stop"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["rm", "-rf", pg_dir], check=True)
        subprocess.run([os.path.join(PG, "initdb"), "-D", pg_dir, "-U", "test", "--auth=trust", "--no-instructions"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run([os.path.join(PG, "pg_ctl"), "-D", pg_dir, "-l", os.path.join(RUN, "postgres.log"),
                        "-o", "-p 54329 -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp",
                        "start"], check=True)
        subprocess.run([os.path.join(PG, "psql"), "-h", "127.0.0.1", "-p", "54329", "-U", "test", "-d", "postgres",
                        "-c", "CREATE DATABASE demo_live"], check=True, stdout=subprocess.DEVNULL)
        evidence["postgres"] = subprocess.check_output([os.path.join(PG, "postgres"), "--version"], text=True).strip()
        exp = os.path.join(BIN, "postgres_exporter-0.15.0.linux-amd64", "postgres_exporter")
        env = os.environ.copy()
        env["DATA_SOURCE_NAME"] = "postgresql://test@127.0.0.1:54329/demo_live?sslmode=disable"
        spawn([exp, f"--extend.query-path={queries}", "--web.listen-address=:9187"], env, "exporter.log")
        spawn([os.path.join(BIN, "prometheus-2.53.2.linux-amd64", "prometheus"),
               f"--config.file={os.path.join(RUN, 'prometheus.yml')}",
               f"--storage.tsdb.path={os.path.join(RUN, 'promdata')}",
               "--storage.tsdb.retention.time=15d",
               "--web.listen-address=:9090"], os.environ.copy(), "prometheus.log")
        spawn([os.path.join(BIN, "alertmanager-0.27.0.linux-amd64", "alertmanager"),
               f"--config.file={am_path}",
               f"--storage.path={os.path.join(RUN, 'amdata')}",
               "--web.listen-address=:9093"], os.environ.copy(), "alertmanager.log")
        hook_env = os.environ.copy()
        hook_env.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": ROOT,
            "PROMETHEUS_URL": "http://127.0.0.1:9090",
            "ALERTMANAGER_URL": "http://127.0.0.1:9093",
            "EXPORTER_URL": "http://127.0.0.1:9187/metrics",
            "PGHOST": "127.0.0.1", "PGPORT": "54329", "PGUSER": "test", "PGDATABASE": "demo_live",
            "DEMO_LLM_ENABLED": "false", "ARTIFACT_DIR": os.path.join(RUN, "artifacts"),
            "GEN_DIAG_DIR": os.path.join(RUN, "gen"), "LOCK_HOLD_SECONDS": "50", "WEBHOOK_PORT": "8023",
            "OPENAI_API_KEY": "unrelated-not-for-live",
        })
        hook_env.pop("DEMO_LLM_API_KEY", None)
        hook_env.pop("PGPASSWORD", None)
        spawn([sys.executable, os.path.join(ROOT, "prometheus_service_docker", "app.py")], hook_env, "webhook.log")
        wait_until(lambda: get("/live/status").get("ready") is True, 40, 2)
        evidence["phases"]["ready"] = time.time()
        wait_until(lambda: metric_value() == 0, 20, 1)
        base = metric_value()
        evidence["baseline_metric"] = base
        if base not in (0, 0.0):
            raise AssertionError(f"baseline metric was {base}")
        if alert_states():
            raise AssertionError(f"alerts present at baseline: {alert_states()}")
        evidence["phases"]["baseline"] = time.time()
        post("/live/start")
        status = wait_until(lambda: (get("/live/status").get("workload") or {}).get("phase") == "holding"
                            or (get("/live/status").get("workload") or {}).get("error"), 20, 0.5)
        workload = get("/live/status")["workload"]
        if workload.get("error"):
            raise AssertionError(workload["error"])
        evidence["phases"]["holding"] = time.time()
        seen = []

        def observe():
            states = alert_states()
            for state in states:
                if state not in seen:
                    seen.append(state)
            return "pending" in seen and "firing" in seen

        wait_until(observe, 40, 2)
        evidence["alert_states"] = list(seen)
        evidence["metric_while_holding"] = metric_value()
        if evidence["metric_while_holding"] is None or evidence["metric_while_holding"] < 1:
            raise AssertionError(f"metric during lock was {evidence['metric_while_holding']}")

        def fired():
            events = get("/live/status").get("events") or []
            return any(item.get("status") == "firing" and not item.get("duplicate") for item in events)

        wait_until(fired, 20, 1)
        evidence["phases"]["webhook_firing"] = time.time()
        firing = json.load(open(os.path.join(RUN, "artifacts", "firing.json")))
        samples = firing["metric"]["samples"]
        if not samples or max(point["value"] for point in samples) < 1:
            raise AssertionError("firing artifact has no live sample >= 1")
        if min(point["ts"] for point in samples) < 1700000000:
            raise AssertionError("sample timestamp is not from the live window")
        if not any(row.get("wait_event_type") == "Lock" and "demo_lock.ticket" in (row.get("query") or "")
                   for row in firing.get("lock_waits") or []):
            raise AssertionError("lock wait SQL was not collected")
        if firing["diagnosis"]["called"] or firing["diagnosis"]["text"] or firing.get("replay"):
            raise AssertionError("diagnosis or replay flag was not honest")
        evidence["firing"] = {
            "alertname": firing["alertname"],
            "sample_count": len(samples),
            "max_value": max(point["value"] for point in samples),
            "first_ts": samples[0]["ts"],
            "diagnosis_mode": firing["diagnosis"]["mode"],
            "diagnosis_called": firing["diagnosis"]["called"],
            "lock_wait_count": len(firing["lock_waits"]),
        }
        post("/live/stop")

        def cleared():
            return metric_value() in (0, 0.0)

        wait_until(cleared, 25, 1)
        evidence["phases"]["metric_cleared"] = time.time()

        def resolved():
            events = get("/live/status").get("events") or []
            return any(item.get("status") == "resolved" for item in events)

        wait_until(resolved, 45, 2)
        evidence["phases"]["webhook_resolved"] = time.time()
        left = subprocess.check_output([
            os.path.join(PG, "psql"), "-h", "127.0.0.1", "-p", "54329", "-U", "test", "-d", "demo_live", "-tAc",
            "SELECT count(*) FROM pg_stat_activity WHERE application_name='demo_live_lock'",
        ], text=True).strip()
        if left != "0":
            raise AssertionError(f"lock backends still present: {left}")
        raw = urllib.request.urlopen("http://127.0.0.1:9187/metrics", timeout=3).read().decode(errors="replace")
        evidence["exporter_lines"] = [line for line in raw.splitlines() if "pg_demo_lock" in line and not line.startswith("#")][:5]
        evidence["backends_after_stop"] = 0
        evidence["ok"] = True
        evidence["versions"] = {
            "prometheus": "2.53.2",
            "alertmanager": "0.27.0",
            "postgres_exporter": "0.15.0",
            "postgres": evidence["postgres"],
            "compose_postgres_image": "postgres:12",
        }
        evidence["note"] = "Native linux/amd64 binaries of the compose versions. Postgres server is 16, not the compose image postgres:12. Docker was not used."
        json.dump(evidence, open(EVIDENCE, "w"), indent=1)
        print(json.dumps({"ok": True, "states": evidence["alert_states"], "metric": evidence["metric_while_holding"]}, indent=1))
        return 0
    except Exception as exc:
        evidence["ok"] = False
        evidence["error"] = exc.__class__.__name__ + ": " + str(exc)[:400]
        for name in ("exporter.log", "prometheus.log", "alertmanager.log", "webhook.log", "postgres.log"):
            path = os.path.join(RUN, name)
            if os.path.isfile(path):
                lines = open(path, errors="replace").read().splitlines()
                evidence[name] = lines[-30:]
        json.dump(evidence, open(EVIDENCE, "w"), indent=1)
        print(json.dumps({"ok": False, "error": evidence["error"]}, indent=1), file=sys.stderr)
        return 1
    finally:
        shutdown()


if __name__ == "__main__":
    sys.exit(main())
