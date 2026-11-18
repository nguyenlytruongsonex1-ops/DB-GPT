"""Unit checks for the live path. Mocks are not end-to-end proof."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prometheus_service_docker"))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

detect_anomalies = _load(
    "anomaly_detection",
    os.path.join(ROOT, "multiagents", "tools", "metric_monitor", "anomaly_detection.py"),
).detect_anomalies
import live_common


class Parser(unittest.TestCase):
    def test_fractions_and_unset(self):
        nano = live_common.parse_rfc3339("2023-09-19T15:28:49.467858611Z")
        self.assertEqual(nano.microsecond, 467858)
        short = live_common.parse_rfc3339("2024-01-02T03:04:05.123Z")
        self.assertEqual(short.microsecond, 123000)
        plain = live_common.parse_rfc3339("2024-01-02T03:04:05Z")
        self.assertEqual(plain.second, 5)
        offset = live_common.parse_rfc3339("2024-01-02T03:04:05.123456789+00:00")
        self.assertEqual(offset.microsecond, 123456)
        self.assertIsNone(live_common.parse_rfc3339("0001-01-01T00:00:00Z"))


class Detector(unittest.TestCase):
    def test_flat_ramp_spike(self):
        self.assertFalse(detect_anomalies([1] * 50)[1])
        self.assertFalse(detect_anomalies(list(range(50)))[1])
        self.assertFalse(detect_anomalies([1, 2, 3])[1])
        self.assertTrue(detect_anomalies([1] * 49 + [100])[1])


class Gate(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        os.environ["DEMO_LLM_ENABLED"] = "false"
        os.environ["OPENAI_API_KEY"] = "unrelated-key"
        os.environ.pop("DEMO_LLM_API_KEY", None)
        self.calls = 0
        real = urllib.request.urlopen

        def wrapped(req, timeout=None):
            self.calls += 1
            return real(req, timeout=timeout)

        self._real = real
        urllib.request.urlopen = wrapped

    def tearDown(self):
        urllib.request.urlopen = self._real
        os.environ.clear()
        os.environ.update(self._env)

    def test_disabled_does_not_call(self):
        result = live_common.maybe_diagnose([], [], {})
        self.assertFalse(result["called"])
        self.assertIsNone(result["text"])
        self.assertEqual(result["mode"], "disabled")
        self.assertEqual(self.calls, 0)
        self.assertNotIn("unrelated-key", json.dumps(result))

    def test_enabled_without_dedicated_key_does_not_call(self):
        os.environ["DEMO_LLM_ENABLED"] = "true"
        result = live_common.maybe_diagnose([], [], {})
        self.assertEqual(result["mode"], "blocked_no_key")
        self.assertFalse(result["called"])
        self.assertEqual(self.calls, 0)

    def test_mocked_request_shape_is_not_a_live_call(self):
        os.environ["DEMO_LLM_ENABLED"] = "true"
        os.environ["DEMO_LLM_API_KEY"] = "unit-test-key"
        captured = {}

        class Fake:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "mock text"}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["timeout"] = timeout
            return Fake()

        urllib.request.urlopen = fake_open
        result = live_common.maybe_diagnose([{"ts": 1, "value": 1}], [], {"alertname": "DemoLockContention"})
        self.assertTrue(result["called"])
        self.assertEqual(result["mode"], "openrouter")
        self.assertEqual(captured["body"]["model"], "qwen/qwen3-30b-a3b-instruct-2507")
        self.assertLessEqual(captured["body"]["max_tokens"], 800)
        self.assertNotIn("unit-test-key", json.dumps(result))


class Webhook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ARTIFACT_DIR"] = self.tmp.name
        os.environ["GEN_DIAG_DIR"] = os.path.join(self.tmp.name, "gen")
        os.environ["DEMO_LLM_ENABLED"] = "false"
        os.environ["OPENAI_API_KEY"] = "unrelated-key"
        os.environ.pop("DEMO_LLM_API_KEY", None)
        os.environ["PGHOST"] = "127.0.0.1"
        os.environ["PGPORT"] = "1"
        os.environ["PGUSER"] = "test"
        os.environ["PGDATABASE"] = "demo_live"
        os.environ.pop("PGPASSWORD", None)
        live_common._seen = None
        now = time.time()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"status": "success", "data": {"result": [{"values": [[now - 5, "1"], [now, "1"]]}]}}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        os.environ["PROMETHEUS_URL"] = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def payload(self, status="firing"):
        return {"status": status, "alerts": [{
            "status": status,
            "labels": {"alertname": "DemoLockContention", "metric": "pg_demo_lock_waits", "scenario": "lock_contention"},
            "startsAt": "2026-11-07T00:00:00.123456789Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "abc",
        }]}

    def test_samples_come_from_query_and_second_firing_is_deduped(self):
        doc = live_common.process_alert(self.payload(), now=time.time())
        self.assertFalse(doc["replay"])
        self.assertEqual(doc["metric"]["samples"][0]["value"], 1.0)
        self.assertGreater(doc["metric"]["samples"][0]["ts"], 1700000000)
        self.assertIsNone(doc["diagnosis"]["text"])
        self.assertFalse(doc["diagnosis"]["called"])
        self.assertTrue(any(item.startswith("postgres:") for item in doc["errors"]))
        again = live_common.process_alert(self.payload(), now=time.time())
        self.assertTrue(again["duplicate"])
        with open(os.path.join(self.tmp.name, "firing.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["metric"]["samples"][0]["value"], 1.0)


class Repo(unittest.TestCase):
    def test_compose_and_rules_match_exporter(self):
        def text(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        compose = text(os.path.join(ROOT, "docker-compose.yml"))
        prom = text(os.path.join(ROOT, "prometheus_service_docker/prometheus.yml"))
        rules = text(os.path.join(ROOT, "prometheus_service_docker/demo_rules.yml"))
        am = text(os.path.join(ROOT, "prometheus_service_docker/alertmanager.yml"))
        metric = text(os.path.join(ROOT, "prometheus_service_docker/prometheus_abnormal_metric.py"))
        self.assertIn("demo_rules.yml", compose)
        self.assertIn("demo_live", compose)
        self.assertIn("DEMO_LLM_ENABLED", compose)
        self.assertNotIn("pgsql_rules.yml", compose)
        self.assertNotIn("8.131.129.55", metric)
        self.assertNotIn("112.27.58.65", metric)
        self.assertIn("pg_demo_lock_waits > 0", rules)
        self.assertIn("send_resolved: true", am)
        self.assertIn("http://python_app:8023/alert", am)
        self.assertIn("demo_rules.yml", prom)
        self.assertIn("scrape_timeout: 4s", prom)


class ArgparseFlag(unittest.TestCase):
    def test_false_string_is_false(self):
        import subprocess
        code = (
            "import importlib.util, sys;"
            "sys.argv=['x','--anomaly_file','a','--enable_prometheus','false'];"
            "spec=importlib.util.spec_from_file_location('our_argparse','multiagents/our_argparse.py');"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "assert m.args.enable_prometheus is False;"
            "sys.argv=['x','--anomaly_file','a','--enable_prometheus','true'];"
            "assert m.main_parse_args().enable_prometheus is True"
        )
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
