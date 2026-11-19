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
import re
import signal
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://python_app:8023").rstrip("/")


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


def active_alert(alertname: str) -> dict | None:
    url = PROM_URL.rstrip("/") + "/api/v1/alerts"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    if body.get("status") != "success":
        raise RuntimeError(body.get("error") or "prometheus alerts query failed")
    for item in (body.get("data") or {}).get("alerts") or []:
        if (item.get("labels") or {}).get("alertname") == alertname:
            return item
    return None


def wait_for_firing(alertname: str, timeout_s: int, stop: threading.Event) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop.is_set():
        try:
            item = active_alert(alertname)
            if item and item.get("state") == "firing":
                return item
        except Exception:
            pass
        time.sleep(2)
    return None


def wait_for_webhook(alertname: str, started_at: float, timeout_s: int, stop: threading.Event) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop.is_set():
        try:
            url = WEBHOOK_URL + "/live/status"
            with urllib.request.urlopen(url, timeout=10) as resp:
                status = json.loads(resp.read().decode())
            for event in reversed(status.get("events") or []):
                if (event.get("alertname") == alertname and event.get("status") == "firing"
                        and float(event.get("received_at") or 0) >= started_at):
                    return event
        except Exception:
            pass
        time.sleep(2)
    return None


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


def build_anomaly(scenario, window_start: float, window_end: float, firing_alert: dict) -> dict:
    """Anomaly document in the shape run_diagnose.py consumes.

    alerts/labels mirror the Alertmanager payload layout. Ground-truth labels are
    the scenario's own definition and are recorded as such in the trace.
    """
    stamps = firing_alert.get("activeAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start))
    end_stamps = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end))
    labels = firing_alert.get("labels") or {}
    annotations = firing_alert.get("annotations") or {}
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
                        "labels": labels,
                        "annotations": annotations,
                        "startsAt": stamps,
                        "endsAt": end_stamps,
                    }
                ],
            }
        ],
        "workload": {scenario.work_sql: 1},
        "slow_queries": [],
        "workload_statistics": [],
        # The full metric-analysis tool fetches live values from Prometheus;
        # keep the historical-fixture field present but empty for this live run.
        "exceptions": {},
    }


def compact_consultation(record: dict) -> dict:
    """Keep only messages explicitly exchanged in the round-table phase."""
    messages = []
    participants = []
    seen = set()
    raw_messages = (record.get("brainstorming") or {}).get("messages") or []
    for index, message in enumerate(raw_messages[-50:], start=1):
        sender = str(message.get("sender") or "D-Bot").strip()
        text = str(message.get("data") or "")
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()[:1600]
        if not text:
            continue
        if sender not in seen:
            seen.add(sender)
            participants.append(sender)
        messages.append({
            "id": f"message-{index}",
            "sender": sender,
            "time": str(message.get("time") or ""),
            "text": text,
        })
    return {"participants": participants, "messages": messages}


def run_diagnosis(anomaly_path: Path, allow_llm: bool) -> dict:
    cmd = [sys.executable, "run_diagnose.py", "--anomaly_file", str(anomaly_path), "--enable_prometheus", "true"]
    env = dict(os.environ)
    # The live workload and exporters use demo_live, not the historical sysbench database.
    env["PG_DB"] = os.environ.get("LIVE_PG_DB", "demo_live")
    if not allow_llm:
        return {"returncode": 2, "seconds": 0, "llm_calls": 0,
                "model": os.environ.get("DEMO_LLM_MODEL"),
                "error": "Chẩn đoán LIVE yêu cầu --allow-llm; pipeline chưa được gọi"}
    enabled = os.environ.get("DEMO_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    api_key = os.environ.get("DEMO_LLM_API_KEY", "").strip()
    if not enabled or not api_key:
        return {"returncode": 2, "seconds": 0, "llm_calls": 0,
                "model": os.environ.get("DEMO_LLM_MODEL"),
                "error": "DEMO_LLM_ENABLED cần bật và DEMO_LLM_API_KEY cần được cấu hình; pipeline chưa được gọi"}
    # Keep the LIVE credential separate in the parent/service environment. Only
    # this explicitly authorized child process receives D-Bot's OpenAI-compatible
    # variable names, so offline runs and the webhook's simplified path are untouched.
    env["OPENAI_API_KEY"] = api_key
    env["LLM_MODEL"] = os.environ.get("DEMO_LLM_MODEL", "qwen/qwen3-30b-a3b-instruct-2507")
    env["OPENAI_BASE_URL"] = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    started = time.time()
    output_tail = []
    llm_calls = [0]
    try:
        proc = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent.parent), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace")
        def stream_output():
            for line in proc.stdout:
                if "chat/completions" in line:
                    llm_calls[0] += 1
                safe = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[redacted]", line.rstrip())
                safe = re.sub(r"(?i)sk-[A-Za-z0-9._-]{8,}", "sk-[redacted]", safe)[:400]
                output_tail.append(safe)
                del output_tail[:-25]
                print(safe, flush=True)
        reader = threading.Thread(target=stream_output, daemon=True)
        reader.start()
        proc.wait(timeout=1800)
        reader.join(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        if "reader" in locals():
            reader.join(timeout=5)
        return {"returncode": 124, "seconds": round(time.time() - started, 1),
                "llm_calls": llm_calls[0], "model": env.get("LLM_MODEL"),
                "error": "D-Bot vượt quá timeout 30 phút", "tail": output_tail}
    result_files = list((Path(__file__).resolve().parent.parent / "alert_results").glob("*/*.jsonl"))
    result_files = [p for p in result_files if p.stat().st_mtime >= started]
    result_file = max(result_files, key=lambda p: p.stat().st_mtime) if result_files else None
    report = None
    consultation = {"participants": [], "messages": []}
    if result_file:
        try:
            record = json.loads(result_file.read_text(encoding="utf-8"))
            report = record.get("report")
            consultation = compact_consultation(record)
        except Exception:
            report = None
    return {
        "returncode": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "llm_calls": llm_calls[0],
        "model": env.get("LLM_MODEL"),
        "result_file": str(result_file.relative_to(Path(__file__).resolve().parent.parent)) if result_file else None,
        "report": report,
        "consultation": consultation,
        "tail": output_tail,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--hold-seconds", type=int, default=60)
    ap.add_argument("--signal-timeout", type=int, default=120)
    ap.add_argument("--out-dir", default=os.environ.get("ARTIFACT_DIR", "alert_results/live_scenarios"))
    ap.add_argument("--diagnose", action="store_true", help="invoke run_diagnose.py after the signal is observed")
    ap.add_argument("--allow-llm", action="store_true", help="allow the full D-Bot pipeline to call the configured model")
    args = ap.parse_args()

    scenario = SCENARIOS[args.scenario]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"{scenario.key}.json"
    stop = threading.Event()
    trace = {"scenario": scenario.key, "title": scenario.title, "metric": scenario.metric,
             "threshold": scenario.threshold, "ground_truth": scenario.labels,
             "started_at": time.time(), "phases": {}}
    thread = None
    return_code = 1
    def request_stop(_signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, request_stop)
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
            firing = wait_for_firing(scenario.alert, args.signal_timeout, stop)
            if not firing:
                trace["error"] = "metric crossed threshold but Prometheus alert did not reach firing"
                return_code = 3
                return return_code
            trace["prometheus_alert"] = {"state": firing.get("state"), "activeAt": firing.get("activeAt"),
                                          "labels": firing.get("labels")}
            hook = wait_for_webhook(scenario.alert, trace["started_at"], 45, stop)
            if not hook:
                trace["error"] = "Prometheus fired but Alertmanager webhook was not observed"
                return_code = 4
                return return_code
            trace["webhook"] = hook
            anomaly = build_anomaly(scenario, window_start, time.time(), firing)
            anomaly_path = out_dir / f"{scenario.key}_anomaly.json"
            anomaly_path.write_text(json.dumps(anomaly, indent=2))
            trace["anomaly_file"] = str(anomaly_path)
            trace["phases"]["signal_observed"] = time.time()
            if args.diagnose:
                trace["diagnosis"] = run_diagnosis(anomaly_path, args.allow_llm)
                diagnosis = trace["diagnosis"]
                if diagnosis.get("returncode") != 0 or not diagnosis.get("report"):
                    trace["error"] = diagnosis.get("error") or "D-Bot không tạo được báo cáo"
                    return_code = 5
                elif args.allow_llm and diagnosis.get("llm_calls", 0) < 1:
                    trace["error"] = "Không xác nhận được lời gọi LLM thật trong log D-Bot"
                    return_code = 6
                else:
                    return_code = 0
            else:
                return_code = 0

        time.sleep(max(0, args.hold_seconds - int(time.time() - window_start)))
    except Exception as exc:
        trace["error"] = f"{exc.__class__.__name__}: {str(exc)[:300]}"
        return_code = 1
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
        if trace.get("cleanup") != "ok" and return_code == 0:
            trace["error"] = "LIVE workload cleanup failed"
            return_code = 7
        trace["finished_at"] = time.time()
        trace_path.write_text(json.dumps(trace, indent=2))
        print(json.dumps({"scenario": scenario.key, "signal": trace.get("signal", {}).get("observed"),
                          "trace": str(trace_path)}))
    return locals().get("return_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
