"""Trang demo D-Bot: chọn kịch bản -> chạy run_diagnose.py -> xem tiến độ + kết quả."""
import ast, glob, json, os, re, shlex, signal, subprocess, sys, threading, time, uuid
import urllib.error
import urllib.request
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVE = os.path.join(ROOT, "demo_results")
EST = 200  # giây, thời gian chạy ước lượng
CMD = os.environ.get("DEMO_CMD", "python run_diagnose.py --anomaly_file {f}")  # DEMO_CMD: lệnh giả để test
LOGN = 300
CASES = {
    1: ("Đọc dữ liệu quá lớn", "Ứng dụng kéo về khối dữ liệu khổng lồ bằng truy vấn lồng nhau, máy chủ bị quá tải."),
    2: ("Xoá dữ liệu hàng loạt", "Một tác vụ xoá rất nhiều dòng cùng lúc, CPU máy chủ lên gần 100%."),
    3: ("Quá nhiều chỉ mục (index)", "Bảng có quá nhiều index, mỗi lần ghi dữ liệu đều chậm và nặng máy."),
    4: ("Ghi dữ liệu đồng thời ồ ạt", "Rất nhiều người dùng cùng ghi/commit một lúc, hệ thống bị nghẽn."),
    5: ("Join chậm + tranh chấp CPU", "Truy vấn nối bảng kém hiệu quả, các tiến trình giành nhau CPU."),
}
STAGES = {  # flow title -> (tên tiếng Việt, % tiến độ)
    "初始化专家角色": ("Khởi tạo đội chuyên gia AI", 5),
    "初始化诊断报告": ("Khởi tạo báo cáo", 10),
    "根据异常分配诊断专家": ("Đọc cảnh báo, phân công chuyên gia", 15),
    "专家诊断": ("Các chuyên gia đang điều tra", 25),
    "圆桌讨论": ("Thảo luận bàn tròn giữa chuyên gia", 70),
    "报告生成": ("Viết báo cáo chẩn đoán", 85),
}
TOOLS = {"whether_is_abnormal_metric": "kiểm tra chỉ số bất thường",
         "match_diagnose_knowledge": "tra cứu kho tri thức chẩn đoán",
         "obtain_slow_queries": "lấy các truy vấn chậm"}

app = FastAPI()
lock = threading.Lock()
state = {"running": False}


def case_file(n): return os.path.join(ROOT, "test_cases", f"testing_cases_{n}.json")


def case_info(n):
    d = json.load(open(case_file(n)))
    a = d["alerts"][0]["alerts"][0]
    return {"id": n, "title": CASES[n][0], "desc": CASES[n][1], "alert": a["labels"]["alertname"],
            "alert_desc": a["annotations"]["description"].strip(), "labels": d["labels"],
            "has_replay": os.path.exists(os.path.join(SAVE, f"case_{n}.json")),
            "live_workload": False,
            "note": "Phát lại JSON lịch sử. Ca này chưa sinh tải SQL sống."}


def enrich(res):
    """Thêm metrics (parse từ chuỗi repr của topMetrics) để frontend vẽ."""
    tm = res["record"].get("topMetrics") or []
    try: tm = ast.literal_eval(tm) if isinstance(tm, str) else tm
    except Exception: tm = []
    seen, out = set(), []
    for m in tm:
        if m.get("title") not in seen and m.get("values"): seen.add(m["title"]); out.append(m)
    return dict(res, metrics=out[:12], knowledge=res.get("knowledge") or kb_from_msgs(res["record"]))


def kb_from_msgs(rec):
    """Bản ghi cũ: lấy tri thức từ câu 'The matched knowledge ... is:' trong tin nhắn chuyên gia."""
    out, seen = [], set()
    for ex, v in (rec.get("anomalyAnalysis") or {}).items():
        for m in v.get("messages", []):
            for blk in re.findall(r"matched knowledge[^:]{0,60}?is:(.*?)(?:<br><br>(?=The )|\\n\\n|$)", str(m.get("data", "")), re.S):
                for it in re.split(r"(?:^|<br>)\s*\d+\.\s", blk.strip()):
                    it = re.sub(r"<[^>]+>", " ", it).strip()
                    if len(it) > 15 and it[:80] not in seen:
                        seen.add(it[:80]); out.append({"expert": ex, "desc": it[:600]})
    return out


def step(text):
    state["steps"].append({"t": round(time.time() - state["start"]), "text": text})


def worker(n, model=None, out=None):
    t0 = state["start"]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if model: env["LLM_MODEL"] = model
    p = subprocess.Popen(shlex.split(CMD.format(f=case_file(n))), cwd=ROOT, env=env, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    state["proc"] = p
    expert = None
    tail = []
    for line in p.stdout:
        tail = (tail + [line.rstrip()[:300]])[-15:]
        if "sk-" not in line: state["log"].append(line.rstrip()[:400]); del state["log"][:-LOGN]; state["log_n"] += 1
        if line.startswith("[KBDOC] "):
            try:
                d = json.loads(line[8:])
                state["knowledge"].append({"expert": expert, "kb": d.get("kb"), "cause": d.get("cause_name"), "score": round(d.get("score", 0), 3),
                                           "desc": str(d.get("desc", ""))[:600]})
            except Exception: pass
        if "chat/completions" in line:
            state["llm_calls"] += 1
            if " 401 " in line or "Unauthorized" in line:
                state["error"] = "OpenRouter trả 401: key sai/hết hạn"; p.kill()
        m = re.search(r"知识库采用其中的(\d+)条", line)
        if m: state["kb_searches"] += 1; state["kb_docs"] += int(m.group(1))
        m = re.search(r'<flow>\{"title": "([^"]+)".*"isCompleted": 0', line)
        if m and m.group(1) in STAGES:
            name, pct = STAGES[m.group(1)]
            if state["stage"] != name: state["stage"], state["stage_pct"] = name, pct; step(name)
        m = re.search(r"Assigned Experts:\s+(\[.*\])", line)
        if m:
            state["experts"] = ast.literal_eval(m.group(1))
            step("Đã chọn chuyên gia: " + ", ".join(state["experts"]))
        m = re.search(r"\[(\w+Expert)\] 开始诊断", line)
        if m: expert = m.group(1); step(f"{expert} bắt đầu điều tra")
        m = re.search(r"^\s+Name: (\w+)", line)
        if m: step(f"{expert or 'Chuyên gia'} dùng công cụ: {TOOLS.get(m.group(1), m.group(1))}")
    p.wait()
    state.pop("proc", None)
    files = [f for f in glob.glob(os.path.join(ROOT, "alert_results", "*", "*.jsonl")) if os.path.getmtime(f) >= t0]
    if p.returncode == 0 and files:
        rec = json.load(open(max(files, key=os.path.getmtime)))
        res = {"case": n, "elapsed": round(time.time() - t0), "llm_calls": state["llm_calls"], "kb_searches": state["kb_searches"], "kb_docs": state["kb_docs"],
               "experts": state["experts"], "steps": state["steps"], "record": rec,
               "model": env.get("LLM_MODEL", ""), "finished_at": int(time.time()), "knowledge": state["knowledge"]}
        step("Hoàn tất")
        d = os.path.join(SAVE, out) if out else SAVE  # out: thư mục phụ để so sánh, không ghi đè bản xem lại
        os.makedirs(d, exist_ok=True)
        json.dump(res, open(os.path.join(d, f"case_{n}.json"), "w"), ensure_ascii=False, indent=1)
        state["result"] = enrich(res)
    elif state.get("stopped"): step("Đã dừng theo yêu cầu")
    elif not state.get("error"):
        state["error"] = f"Chẩn đoán lỗi (mã {p.returncode}), xem log container"
        state["log_tail"] = [l for l in tail if "sk-" not in l]
    state["running"] = False


@app.get("/")
def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/assets/bootstrap.min.css")
def bootstrap_css():
    return FileResponse(os.path.join(os.path.dirname(__file__), "bootstrap.min.css"), media_type="text/css")


@app.get("/api/cases")
def cases(): return [case_info(n) for n in CASES]


@app.post("/api/run/{n}")
def run(n: int, model: str = "", out: str = ""):
    if out and not re.fullmatch(r"[\w.-]+", out): raise HTTPException(400)
    if n not in CASES: raise HTTPException(404)
    with lock:
        if state.get("running") or live.get("running"): raise HTTPException(409, "Đang có một lượt chạy, vui lòng chờ")
        state.clear(); state.update(running=True, case=n, start=time.time(), steps=[], llm_calls=0, kb_searches=0, kb_docs=0,
                                    stage="", stage_pct=0, experts=[], result=None, error=None, log=[], log_n=0, knowledge=[], stopped=False)
    threading.Thread(target=worker, args=(n, model or None, out or None), daemon=True).start()
    return {"ok": True}


@app.get("/api/status")
def status():
    s = {k: v for k, v in state.items() if k not in ("proc", "log")}
    if "start" in s:
        el = time.time() - s["start"]
        s["elapsed"] = round(el)
        s["progress"] = 100 if s.get("result") else min(97, max(s["stage_pct"], round(el / EST * 95)))
    return s


@app.get("/api/replay/{n}")
def replay(n: int):
    f = os.path.join(SAVE, f"case_{n}.json")
    if not os.path.exists(f): raise HTTPException(404, "Chưa có kết quả lưu sẵn cho kịch bản này")
    return enrich(json.load(open(f)))


def _rel(f): return os.path.relpath(f, ROOT)


def history_records():
    """Build normalized history rows without exposing report payloads."""
    out = []
    live_result_ids = set()
    for trace_file in glob.glob(os.path.join(ROOT, "alert_results", "live_scenarios", "*", "*.json")):
        if trace_file.endswith("_anomaly.json"):
            continue
        try:
            trace = json.load(open(trace_file))
            result_file = ((trace.get("diagnosis") or {}).get("result_file") or "").strip()
            if result_file:
                live_result_ids.add(os.path.normpath(result_file))
        except Exception:
            continue
    for f in glob.glob(os.path.join(SAVE, "**", "case_*.json"), recursive=True):
        try: r = json.load(open(f))
        except Exception: continue
        sub = os.path.relpath(os.path.dirname(f), SAVE)
        out.append({"id": _rel(f), "case": r.get("case"), "model": r.get("model") or "", "set": "" if sub == "." else sub,
                    "time": r.get("finished_at"), "elapsed": r.get("elapsed"), "severity": r["record"].get("severity"),
                    "title": r["record"].get("title", ""), "source": "OFFLINE", "status": "Hoàn tất"})
    for f in glob.glob(os.path.join(ROOT, "alert_results", "*", "*.jsonl")):
        try: r = json.load(open(f)); t = int(r.get("time") or 0)
        except Exception: continue
        result_id = os.path.normpath(_rel(f))
        out.append({"id": _rel(f), "case": None, "model": os.path.basename(os.path.dirname(f)), "set": "alert_results",
                    "time": t, "elapsed": None, "severity": r.get("severity"), "title": r.get("title", "")})
        out[-1].update(source="LIVE" if result_id in live_result_ids else "OFFLINE", status="Hoàn tất")
    return sorted(out, key=lambda x: -(x["time"] or 0))


@app.get("/api/history")
def history(page: int = 1, page_size: int = 25, q: str = ""):
    """Paginate saved results; history is loaded only when its tab opens."""
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    rows = history_records()
    query = q.strip().lower()
    if query:
        rows = [row for row in rows if query in " ".join(str(row.get(key) or "") for key in
                ("title", "model", "set", "source", "status")).lower()]
    total = len(rows)
    start = (page - 1) * page_size
    return {"items": rows[start:start + page_size], "total": total, "page": page,
            "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size)}


@app.get("/api/result")
def result(id: str):
    f = os.path.realpath(os.path.join(ROOT, id))
    ok = any(f.startswith(os.path.join(ROOT, d) + os.sep) for d in ("demo_results", "alert_results"))
    if not ok or not os.path.isfile(f) or not f.endswith((".json", ".jsonl")): raise HTTPException(404)
    r = json.load(open(f))
    if "record" not in r:  # bản ghi gốc alert_results
        r = {"case": None, "elapsed": None, "llm_calls": None, "experts": [k for k in r.get("anomalyAnalysis", {}) if k.endswith("Expert")],
             "record": r, "model": os.path.basename(os.path.dirname(f)), "finished_at": int(r.get("time") or 0)}
    return enrich(r)


@app.post("/api/stop")
def stop():
    p = state.get("proc")
    if not state.get("running") or not p: raise HTTPException(409, "Không có lượt chạy nào")
    state["stopped"] = True
    try:
        os.killpg(p.pid, signal.SIGTERM); p.wait(5)
    except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    return {"ok": True}


def _proxy(method, path, timeout=20):
    url = os.environ.get("WEBHOOK_URL", "http://python_app:8023").rstrip("/") + path
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw)
        except Exception:
            body = {"ok": False, "error": raw[:200]}
        return body, exc.code
    except Exception as exc:
        return {"ready": False, "ok": False, "error": exc.__class__.__name__}, 503


@app.get("/api/live/status")
def live_status():
    body, code = _proxy("GET", "/live/status")
    return JSONResponse(body, status_code=code)


@app.post("/api/live/start")
def live_start():
    body, code = _proxy("POST", "/live/start")
    return JSONResponse(body, status_code=code)


@app.post("/api/live/stop")
def live_stop():
    body, code = _proxy("POST", "/live/stop")
    return JSONResponse(body, status_code=code)


# Scenario keys for the five demo cases; used by the live workload runner.
LIVE_CASES = {
    1: "large_read",
    2: "bulk_delete",
    3: "many_indexes",
    4: "concurrent_writes",
    5: "slow_join",
}
live = {"running": False, "case": None, "scenario": None, "run_id": None, "proc": None,
        "log": [], "trace": None, "error": None, "llm_expected": True}


def _redact_live_log(line: str) -> str:
    line = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[redacted]", line)
    line = re.sub(r"(?i)sk-[A-Za-z0-9._-]{8,}", "sk-[redacted]", line)
    return line[:400]


def live_worker(n: int, scenario: str, diagnose: bool, run_id: str):
    out_dir = os.path.join(ROOT, "alert_results", "live_scenarios", run_id)
    live.update(running=True, case=n, scenario=scenario, run_id=run_id, error=None,
                trace=None, log=[], llm_expected=diagnose)
    args = [sys.executable, "-m", "live_scenarios.runner", "--scenario", scenario,
            "--hold-seconds", "45", "--out-dir", out_dir]
    if diagnose:
        args.extend(["--diagnose", "--allow-llm"])
    p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    live["proc"] = p
    for line in p.stdout:
        clean_line = _redact_live_log(line.rstrip())
        live["log"].append(clean_line)
        del live["log"][:-LOGN]
        if " 401 Unauthorized" in clean_line or "HTTP/1.1\" 401" in clean_line:
            live["error"] = "LLM provider từ chối xác thực (HTTP 401); kiểm tra DEMO_LLM_API_KEY trong .env."
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    p.wait()
    live["proc"] = None
    live["running"] = False
    trace_path = os.path.join(out_dir, f"{scenario}.json")
    if os.path.exists(trace_path):
        live["trace"] = json.load(open(trace_path))
    if p.returncode != 0 and not live.get("error"):
        live["error"] = f"live run failed (exit {p.returncode})"


@app.get("/api/live/scenarios")
def live_scenarios():
    return [{"case": n, "scenario": key} for n, key in LIVE_CASES.items()]


@app.get("/api/live/run/config")
def live_run_config():
    enabled = os.environ.get("DEMO_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
    ready = enabled and bool(os.environ.get("DEMO_LLM_API_KEY", "").strip())
    scenarios = []
    for n, key in LIVE_CASES.items():
        info = case_info(n)
        scenarios.append({"case": n, "scenario": key, "title": info["title"], "desc": info["desc"],
                          "alert": info["alert"], "alert_desc": info["alert_desc"]})
    return {"llm_ready": ready,
            "llm_enabled": enabled,
            "model": os.environ.get("DEMO_LLM_MODEL", "configured D-Bot model"),
            "scenarios": scenarios}


@app.post("/api/live/run/stop")
def live_run_stop():
    p = live.get("proc")
    if not live.get("running") or not p:
        raise HTTPException(409, "Không có lượt LIVE nào đang chạy")
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return {"ok": True, "run_id": live.get("run_id")}


@app.post("/api/live/run/{n}")
def live_run(n: int, diagnose: bool = True):
    if n not in LIVE_CASES:
        raise HTTPException(404)
    with lock:
        if live["running"] or state.get("running"):
            raise HTTPException(409, "Một lượt chạy live đang diễn ra")
        llm_enabled = os.environ.get("DEMO_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
        if diagnose and (not llm_enabled or not os.environ.get("DEMO_LLM_API_KEY", "").strip()):
            raise HTTPException(503, "LIVE + D-Bot chưa sẵn sàng: bật DEMO_LLM_ENABLED và cấu hình DEMO_LLM_API_KEY trong .env")
        run_id = uuid.uuid4().hex
        live.update(running=True, case=n, scenario=LIVE_CASES[n], run_id=run_id,
                    error=None, trace=None, log=[], llm_expected=diagnose)
    threading.Thread(target=live_worker, args=(n, LIVE_CASES[n], diagnose, run_id), daemon=True).start()
    return {"ok": True, "case": n, "scenario": LIVE_CASES[n], "run_id": run_id, "mode": "live"}


@app.get("/api/live/run/state")
def live_run_state():
    return {k: v for k, v in live.items() if k != "proc"}


@app.get("/api/log")
def log(since: int = 0):
    """Trả các dòng log mới (since = số dòng client đã có)."""
    L, n = state.get("log", []), state.get("log_n", 0)
    return {"n": n, "lines": L[max(0, len(L) - (n - since)):] if since < n else []}


@app.get("/api/alert/{n}")
def alert(n: int):
    if n not in CASES: raise HTTPException(404)
    return json.load(open(case_file(n)))
