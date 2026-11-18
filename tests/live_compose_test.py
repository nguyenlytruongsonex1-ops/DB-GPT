"""HTTP proof against an already running docker compose stack.

Start the stack yourself (`docker compose up -d postgres_db prometheus alertmanager pg_exporter python_app`).
Exit 2 when nothing is listening. This file does not start or stop Docker.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = os.path.join(ROOT, "tests", "evidence", "live_lock_docker.json")


def http_json(url, timeout=10, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def metric_value():
    body = http_json("http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": "pg_demo_lock_waits"}))
    result = (body.get("data") or {}).get("result") or []
    return None if not result else float(result[0]["value"][1])


def alert_states():
    body = http_json("http://127.0.0.1:9090/api/v1/alerts")
    return [item.get("state") for item in (body.get("data") or {}).get("alerts") or []
            if (item.get("labels") or {}).get("alertname") == "DemoLockContention"]


def status():
    return http_json("http://127.0.0.1:8023/live/status", timeout=20)


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
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    try:
        urllib.request.urlopen("http://127.0.0.1:8023/test", timeout=3)
    except Exception as exc:
        print(json.dumps({"ok": False, "skipped": True, "error": exc.__class__.__name__}))
        return 2
    evidence = {"docker": True, "phases": {}, "started_at": time.time()}
    try:
        wait_until(lambda: status().get("ready") is True, 90, 2)
        evidence["phases"]["ready"] = time.time()
        wait_until(lambda: metric_value() == 0, 30, 1)
        evidence["baseline_metric"] = metric_value()
        if alert_states():
            raise AssertionError("alerts present at baseline")
        evidence["phases"]["baseline"] = time.time()
        http_json("http://127.0.0.1:8023/live/start", data=b"")
        wait_until(lambda: (status().get("workload") or {}).get("phase") == "holding"
                   or (status().get("workload") or {}).get("error"), 20, 0.5)
        workload = status()["workload"]
        if workload.get("error"):
            raise AssertionError(workload["error"])
        evidence["phases"]["holding"] = time.time()
        seen = []

        def observe():
            for state in alert_states():
                if state not in seen:
                    seen.append(state)
            return "pending" in seen and "firing" in seen

        wait_until(observe, 40, 2)
        evidence["alert_states"] = list(seen)
        evidence["metric_while_holding"] = metric_value()
        if evidence["metric_while_holding"] is None or evidence["metric_while_holding"] < 1:
            raise AssertionError("metric did not rise")
        raw = urllib.request.urlopen("http://127.0.0.1:9187/metrics", timeout=3).read().decode(errors="replace")
        evidence["exporter_while_holding"] = [line for line in raw.splitlines() if line.startswith("pg_demo_lock_waits{")][:3]

        def fired():
            return any(item.get("status") == "firing" and not item.get("duplicate") for item in status().get("events") or [])

        wait_until(fired, 20, 1)
        evidence["phases"]["webhook_firing"] = time.time()
        firing = json.load(open(os.path.join(ROOT, "alert_results", "live", "firing.json")))
        samples = firing["metric"]["samples"]
        if firing.get("replay") or firing["diagnosis"]["called"] or firing["diagnosis"]["text"]:
            raise AssertionError("diagnosis was not honest")
        if not samples or max(point["value"] for point in samples) < 1 or min(point["ts"] for point in samples) < 1700000000:
            raise AssertionError("firing samples are not a live window")
        if not any(row.get("wait_event_type") == "Lock" and "demo_lock.ticket" in (row.get("query") or "")
                   for row in firing.get("lock_waits") or []):
            raise AssertionError("lock wait SQL missing")
        evidence["firing"] = {
            "alertname": firing["alertname"], "sample_count": len(samples),
            "max_value": max(point["value"] for point in samples),
            "diagnosis_mode": firing["diagnosis"]["mode"], "diagnosis_called": False,
        }
        http_json("http://127.0.0.1:8023/live/stop", data=b"")
        wait_until(lambda: metric_value() == 0, 25, 1)

        def resolved():
            return any(item.get("status") == "resolved" for item in status().get("events") or [])

        wait_until(resolved, 45, 2)
        evidence["phases"]["webhook_resolved"] = time.time()
        evidence["ok"] = True
        evidence["note"] = "docker compose services on this amd64 sandbox. Mac M4 arm64 was not executed here."
        json.dump(evidence, open(EVIDENCE, "w"), indent=1)
        print(json.dumps({"ok": True, "states": evidence["alert_states"], "metric": evidence["metric_while_holding"]}))
        return 0
    except Exception as exc:
        evidence["ok"] = False
        evidence["error"] = exc.__class__.__name__ + ": " + str(exc)[:400]
        json.dump(evidence, open(EVIDENCE, "w"), indent=1)
        print(json.dumps({"ok": False, "error": evidence["error"]}), file=sys.stderr)
        return 1
    finally:
        try:
            http_json("http://127.0.0.1:8023/live/stop", data=b"")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
