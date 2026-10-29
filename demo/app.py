"""Trang demo D-Bot: chọn kịch bản -> chạy run_diagnose.py -> xem tiến độ + kết quả."""
import ast, glob, json, os, re, subprocess, threading, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVE = os.path.join(ROOT, "demo_results")
EST = 200  # giây, thời gian chạy ước lượng
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
    return dict(res, metrics=out[:6])


def step(text):
    state["steps"].append({"t": round(time.time() - state["start"]), "text": text})


def worker(n):
    t0 = state["start"]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    p = subprocess.Popen(["python", "run_diagnose.py", "--anomaly_file", case_file(n)], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    state["pid"] = p.pid
    expert = None
    for line in p.stdout:
        if "chat/completions" in line:
            state["llm_calls"] += 1
            if " 401 " in line or "Unauthorized" in line:
                state["error"] = "OpenRouter trả 401: key sai/hết hạn"; p.kill()
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
    files = [f for f in glob.glob(os.path.join(ROOT, "alert_results", "*", "*.jsonl")) if os.path.getmtime(f) >= t0]
    if p.returncode == 0 and files:
        rec = json.load(open(max(files, key=os.path.getmtime)))
        res = {"case": n, "elapsed": round(time.time() - t0), "llm_calls": state["llm_calls"],
               "experts": state["experts"], "steps": state["steps"], "record": rec,
               "model": os.environ.get("LLM_MODEL", ""), "finished_at": int(time.time())}
        step("Hoàn tất")
        json.dump(res, open(os.path.join(SAVE, f"case_{n}.json"), "w"), ensure_ascii=False, indent=1)
        state["result"] = enrich(res)
    elif not state.get("error"):
        state["error"] = f"Chẩn đoán lỗi (mã {p.returncode}), xem log container"
    state["running"] = False


@app.get("/")
def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/cases")
def cases(): return [case_info(n) for n in CASES]


@app.post("/api/run/{n}")
def run(n: int):
    if n not in CASES: raise HTTPException(404)
    with lock:
        if state.get("running"): raise HTTPException(409, "Đang có một lượt chạy, vui lòng chờ")
        state.clear(); state.update(running=True, case=n, start=time.time(), steps=[], llm_calls=0,
                                    stage="", stage_pct=0, experts=[], result=None, error=None)
    threading.Thread(target=worker, args=(n,), daemon=True).start()
    return {"ok": True}


@app.get("/api/status")
def status():
    s = {k: v for k, v in state.items() if k != "pid"}
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
