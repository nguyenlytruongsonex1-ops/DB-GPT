"""Trang demo D-Bot: chọn kịch bản -> chạy run_diagnose.py -> xem tiến độ + kết quả."""
import ast, glob, json, os, re, shlex, signal, subprocess, threading, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

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
            "has_replay": os.path.exists(os.path.join(SAVE, f"case_{n}.json"))}


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


@app.get("/api/cases")
def cases(): return [case_info(n) for n in CASES]


@app.post("/api/run/{n}")
def run(n: int, model: str = "", out: str = ""):
    if out and not re.fullmatch(r"[\w.-]+", out): raise HTTPException(400)
    if n not in CASES: raise HTTPException(404)
    with lock:
        if state.get("running"): raise HTTPException(409, "Đang có một lượt chạy, vui lòng chờ")
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


@app.get("/api/history")
def history():
    """Liệt kê mọi kết quả đã lưu: demo_results (có case/model) + alert_results gốc."""
    out = []
    for f in glob.glob(os.path.join(SAVE, "**", "case_*.json"), recursive=True):
        try: r = json.load(open(f))
        except Exception: continue
        sub = os.path.relpath(os.path.dirname(f), SAVE)
        out.append({"id": _rel(f), "case": r.get("case"), "model": r.get("model") or "", "set": "" if sub == "." else sub,
                    "time": r.get("finished_at"), "elapsed": r.get("elapsed"), "severity": r["record"].get("severity"),
                    "title": r["record"].get("title", "")})
    for f in glob.glob(os.path.join(ROOT, "alert_results", "*", "*.jsonl")):
        try: r = json.load(open(f)); t = int(r.get("time") or 0)
        except Exception: continue
        out.append({"id": _rel(f), "case": None, "model": os.path.basename(os.path.dirname(f)), "set": "alert_results",
                    "time": t, "elapsed": None, "severity": r.get("severity"), "title": r.get("title", "")})
    return sorted(out, key=lambda x: -(x["time"] or 0))


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


@app.get("/api/log")
def log(since: int = 0):
    """Trả các dòng log mới (since = số dòng client đã có)."""
    L, n = state.get("log", []), state.get("log_n", 0)
    return {"n": n, "lines": L[max(0, len(L) - (n - since)):] if since < n else []}


@app.get("/api/alert/{n}")
def alert(n: int):
    if n not in CASES: raise HTTPException(404)
    return json.load(open(case_file(n)))
