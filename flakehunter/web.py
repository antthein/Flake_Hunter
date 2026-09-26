"""
flakehunter/web.py -- web UI for FlakeHunter (FastAPI + Server-Sent Events).

Modes (env FLAKEHUNTER_MODE):
  demo  (default) -- Bob's fixes are replayed from demo_data/bob_run.json, a
                     recording of a real IBM Bob 2.0 run. Detection and
                     verification run live. No Bob API key needed.
  live            -- calls IBM Bob for real (needs `bob` on PATH and BOB_API_KEY).
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from flakehunter.pipeline import run_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_PROJECT = REPO_ROOT / "demo_originals"   # pristine flaky copy; never modified
REPLAY_FILE = REPO_ROOT / "demo_data" / "bob_run.json"
STATIC = Path(__file__).resolve().parent / "static"
RUNS_DIR = Path(tempfile.gettempdir()) / "flakehunter_runs"
MAX_CONCURRENT = 2
KEEP_RUNS = 20

MODE = os.environ.get("FLAKEHUNTER_MODE", "demo").lower()
RUNS = int(os.environ.get("FLAKEHUNTER_RUNS", "15"))
VERIFY_RUNS = int(os.environ.get("FLAKEHUNTER_VERIFY_RUNS", "20"))

app = FastAPI(title="FlakeHunter")
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/info")
def info() -> dict:
    return {"mode": MODE, "runs": RUNS, "verify_runs": VERIFY_RUNS}


@app.get("/api/run")
async def run() -> StreamingResponse:
    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/runs/{run_id}/report.html")
def report(run_id: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise HTTPException(404)
    path = RUNS_DIR / run_id / "report.html"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream():
    if not _slots.acquire(blocking=False):
        yield _sse("busy", {"message": "Two runs are already in progress. Please try again in a minute."})
        return

    run_id = uuid.uuid4().hex
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_runs()
    events: queue.Queue = queue.Queue()

    def emit(event: str, data: dict) -> None:
        events.put((event, data))

    def work() -> None:
        try:
            run_pipeline(
                DEMO_PROJECT, run_dir, emit,
                runs=RUNS, verify_runs=VERIFY_RUNS,
                replay_path=REPLAY_FILE if MODE != "live" else None,
            )
            emit("done", {"report_url": f"/runs/{run_id}/report.html"})
        except Exception as exc:  # surface failures to the page instead of hanging
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            _slots.release()
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    yield _sse("start", {"run_id": run_id, "mode": MODE, "runs": RUNS, "verify_runs": VERIFY_RUNS})

    while True:
        try:
            item = await asyncio.to_thread(events.get, True, 15)
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if item is None:
            return
        yield _sse(*item)


def _cleanup_old_runs() -> None:
    runs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in runs[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
