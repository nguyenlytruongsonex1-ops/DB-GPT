"""Drive one live scenario: create the fault, observe the real signal, run D-Bot.

Usage (inside the python_app / dbot container network):
    python -m live_scenarios.runner --scenario concurrent_writes --hold-seconds 60

Steps: setup -> start workload -> wait for the live metric to cross its threshold
-> build a *live* anomaly JSON with the current window -> optionally invoke
run_diagnose.py with --enable_prometheus true -> write a trace -> clean up.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_scenarios.scenarios import SCENARIOS, cleanup_schema, connect, terminate_scenario_backends  # noqa: E402

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")


def prom_query(metric: str) -> list[float]:
    url = PROM_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": metric})
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    if body.get("status") != "success":
        raise RuntimeError(body.get("error") or "prometheus query failed")
    out = []
    for serie in (body.get("data") or {}).get("result") or []:
        value = serie.get("value") or []
        if len(value) == 2:
            out.append(float(value[1]))
    return out


def wait_for_signal(metric: str, threshold: float, timeout_s: int, stop: threading.Event) -> dict:
    deadline = time.time() + timeout_s
    samples: list[dict] = []
    while time.time() < deadline and not stop.is_set():
        try:
            values = prom_query(metric)
        except Exception as exc:  # keep observing; a transient scrape error is not proof
            values = []
            samples.append({"t": time.time(), "error": exc.__class__.__name__})
        if values:
            samples.append({"t": time.time(), "values": values})
            if max(values) >= threshold:
                return {"observed": True, "samples": samples[-40:]}
        time.sleep(3)
    return {"observed": False, "samples": samples[-40:]}


def build_anomaly(scenario, window_start: float, window_end: float) -> dict:
    """Anomaly document in the shape run_diagnose.py consumes.

    alerts/labels mirror the Alertmanager payload layout. Ground-truth labels are
    the scenario's own definition and are recorded as such in the trace.
    """
    stamps = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start))
    end_stamps = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end))
    return {
        "start_time": str(int(window_start)),
        "end_time": str(int(window_end)),
        "source": "live_workload",
        "scenario": scenario.key,
        "alerts": [
            {
                "receiver": "db-gpt",
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": scenario.alert,
                            "scenario": scenario.key,
                            "metric": scenario.metric,
                            "severity": "warning",
                        },
                        "annotations": {
                            "summary": scenario.title,
                            "description": f"{scenario.metric} crossed {scenario.threshold} during a live workload",
                        },
                        "startsAt": stamps,
                        "endsAt": end_stamps,
                    }
                ],
                "labels": scenario.labels,
            }
        ],
        "labels": scenario.labels,
        "labels_source": "scenario ground truth (not an observation)",
        "workload": {scenario.work_sql: 1},
        "slow_queries": [],
    }


def run_diagnosis(anomaly_path: Path, allow_llm: bool) -> dict:
    cmd = [sys.executable, "run_diagnose.py", "--anomaly_file", str(anomaly_path), "--enable_prometheus", "true"]
    env = dict(os.environ)
    if not allow_llm:
        env["DEMO_LLM_ENABLED"] = "false"
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent), env=env,
                          capture_output=True, text=True, timeout=1800)
    return {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "tail": (proc.stdout or "").strip().splitlines()[-25:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--hold-seconds", type=int, default=60)
    ap.add_argument("--signal-timeout", type=int, default=120)
    ap.add_argument("--out-dir", default=os.environ.get("ARTIFACT_DIR", "alert_results/live_scenarios"))
    ap.add_argument("--diagnose", action="store_true", help="invoke run_diagnose.py after the signal is observed")
    ap.add_argument("--allow-llm", action="store_true", help="allow a paid model call during diagnosis")
    args = ap.parse_args()

    scenario = SCENARIOS[args.scenario]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"{scenario.key}.json"
    stop = threading.Event()
    trace = {"scenario": scenario.key, "title": scenario.title, "metric": scenario.metric,
             "threshold": scenario.threshold, "started_at": time.time(), "phases": {}}
    thread = None
    try:
        scenario.setup()
        trace["phases"]["setup"] = time.time()
        thread = threading.Thread(target=scenario.workload, args=(stop,), daemon=True)
        thread.start()
        window_start = time.time()
        trace["phases"]["workload_running"] = window_start

        observed = wait_for_signal(scenario.metric, scenario.threshold, args.signal_timeout, stop)
        trace["signal"] = observed
        if not observed["observed"]:
            trace["error"] = "live signal threshold was never reached; scenario did not fire"
            return_code = 2
        else:
            anomaly = build_anomaly(scenario, window_start, time.time())
            anomaly_path = out_dir / f"{scenario.key}_anomaly.json"
            anomaly_path.write_text(json.dumps(anomaly, indent=2))
            trace["anomaly_file"] = str(anomaly_path)
            trace["phases"]["signal_observed"] = time.time()
            if args.diagnose:
                trace["diagnosis"] = run_diagnosis(anomaly_path, args.allow_llm)
            return_code = 0

        time.sleep(max(0, args.hold_seconds - int(time.time() - window_start)))
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=30)
        try:
            trace["terminated_backends"] = terminate_scenario_backends()
            scenario.cleanup()
            cleanup_schema()
            trace["cleanup"] = "ok"
        except Exception as exc:
            trace["cleanup"] = f"failed: {exc.__class__.__name__}"
        trace["finished_at"] = time.time()
        trace_path.write_text(json.dumps(trace, indent=2))
        print(json.dumps({"scenario": scenario.key, "signal": trace.get("signal", {}).get("observed"),
                          "trace": str(trace_path)}))
    return locals().get("return_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
