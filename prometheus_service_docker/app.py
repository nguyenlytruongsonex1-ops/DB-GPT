import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from live_common import live_status, process_alert, readiness, start_workload, stop_workload

app = FastAPI()
logging.basicConfig(level=logging.INFO)


@app.get("/test")
def test():
    return {"code": 0, "msg": "success", "data": "prometheus service is running"}


@app.get("/ready")
def ready():
    body = readiness()
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


@app.get("/live/status")
def status():
    return live_status()


@app.post("/live/start")
def live_start(hold_s: int = 0):
    result = start_workload(hold_s or None)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.post("/live/stop")
def live_stop():
    return stop_workload()


@app.post("/alert")
async def alert(request: Request):
    payload = await request.json()
    try:
        doc = process_alert(payload)
    except Exception as exc:
        logging.exception("alert handling failed")
        fail = {"ok": False, "error": exc.__class__.__name__, "written": False}
        try:
            from live_common import artifact_dir
            import json
            import time
            path = artifact_dir()
            json.dump({"error": exc.__class__.__name__, "received_at": time.time(), "status": payload.get("status")},
                      open(f"{path}/current.json", "w"))
            fail["written"] = True
        except Exception:
            logging.exception("failed to persist alert error")
        return JSONResponse(fail, status_code=500)
    return {"ok": True, "duplicate": bool(doc.get("duplicate")), "status": doc.get("status"),
            "diagnosis_called": bool((doc.get("diagnosis") or {}).get("called"))}


if __name__ == "__main__":
    import os
    uvicorn.run(app=app, host="0.0.0.0", port=int(os.environ.get("WEBHOOK_PORT", "8023")))
