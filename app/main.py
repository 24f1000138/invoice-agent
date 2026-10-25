import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from . import store
from .agent import process
from .a2a import router

app = FastAPI(title="Invoice agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/process")
async def process_ep(req: Request):
    pkg = await req.json()
    if not isinstance(pkg, dict):
        raise HTTPException(400, "expected a JSON object")
    hdr = req.headers.get("Idempotency-Key") or req.headers.get("X-Task-Id")
    if hdr and not any(pkg.get(k) for k in ("task_id", "taskId", "id")):
        pkg = dict(pkg, task_id=hdr)
    return process(pkg)


@app.get("/tasks/{task_id}")
def task(task_id: str):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "unknown task")
    return t


@app.get("/state")
def state():
    return {"tasks": store.list_tasks(), "payments": store.list_payments()}


@app.post("/reset")
def reset(req: Request):
    if not ADMIN_TOKEN or req.headers.get("Authorization") != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(403, "forbidden")
    store.reset()
    return {"ok": True}
