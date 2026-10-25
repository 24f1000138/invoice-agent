"""A2A front door. Serves the v1.0 HTTP+JSON binding (POST message:send, GET tasks/{id},
POST tasks/{id}:cancel, GET tasks) plus JSON-RPC (v1.0 and v0.3 method names), under /a2a and /."""
import base64, datetime, hashlib, io, json, os, uuid
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from . import store
from .agent import process, cancel
from .decide import ESCALATE

router = APIRouter()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
MEDIA = "application/a2a+json"
SUPPORTED = ("0.3", "1.0")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def public_url(req=None):
    if os.environ.get("PUBLIC_URL") or req is None:
        return PUBLIC_URL
    proto = req.headers.get("x-forwarded-proto", req.url.scheme).split(",")[0].strip()
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or req.url.netloc
    return f"{'https' if proto == 'https' or not host.startswith(('localhost', '127.')) else proto}://{host}"


def card(req=None):
    root = public_url(req)
    base = f"{root}/a2a"
    return {
        "name": "Invoice Agent",
        "description": "Reads invoice packages, chooses pay / hold / escalate / reject / reject_duplicate with cited "
                       "evidence, executes only approved payments exactly once, and persists task state for safe retries.",
        "supportedInterfaces": [
            {"url": base, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
            {"url": base, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        ],
        "url": base, "preferredTransport": "HTTP+JSON", "protocolVersion": "1.0",
        "provider": {"organization": "TDS student", "url": root},
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["application/json", "text/plain", "application/pdf"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [{"id": "invoice-decision", "name": "Invoice decision",
                    "description": "Decide and safely execute the business action for each invoice package, citing evidence.",
                    "tags": ["invoice", "accounts-payable", "payments", "approvals"],
                    "examples": ["Process this invoice package"],
                    "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json"]}],
    }


def agent_card(req: Request):
    return JSONResponse(card(req))


for path in ("/.well-known/agent-card.json", "/.well-known/agent.json",
             "/a2a/.well-known/agent-card.json", "/a2a/.well-known/agent.json", "/a2a/v1/card", "/a2a/card"):
    router.add_api_route(path, agent_card, methods=["GET"])


# ---------------- inbound message -> package(s) ----------------

def file_text(raw=None, url=None, media=""):
    data = base64.b64decode(raw) if raw else httpx.get(url, timeout=30, follow_redirects=True).content if url else None
    if data is None:
        return ""
    if data[:4] == b"%PDF" or "pdf" in str(media):
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", "replace")


def part_payload(p):
    """Normalise v1.0 parts ({text}|{data}|{raw}|{url}) and v0.3 parts ({kind, text|data|file})."""
    md = p.get("metadata") or {}
    if "text" in p:
        return "text", p["text"], md, p.get("filename")
    if "data" in p:
        return "data", p["data"], md, p.get("filename")
    if "raw" in p or "url" in p:
        return "text", file_text(p.get("raw"), p.get("url"), p.get("mediaType", "")), md, p.get("filename")
    if "file" in p:
        f = p["file"]
        return "text", file_text(f.get("bytes"), f.get("uri"), f.get("mimeType", "")), md, f.get("name")
    return None, None, md, None


PKG_KEYS = ("documents", "docs", "task_id", "invoice", "package_id", "packages", "invoices", "files")


def packages_from_message(msg):
    pkg, docs = {}, []
    for i, p in enumerate(msg.get("parts") or []):
        kind, val, md, fname = part_payload(p)
        did = str(md.get("id") or md.get("doc_id") or md.get("name") or fname or f"part{i+1}")
        if kind == "text":
            try:
                j = json.loads(val)
            except (ValueError, TypeError):
                j = None
            if isinstance(j, (dict, list)):
                kind, val = "data", j
            elif val and val.strip():
                docs.append({"id": did, "content": val})
        if kind == "data":
            if isinstance(val, dict) and any(k in val for k in PKG_KEYS):
                pkg.update(val)
            elif isinstance(val, list):
                pkg.setdefault("packages", []).extend(val) if all(isinstance(x, dict) and any(k in x for k in PKG_KEYS) for x in val) \
                    else docs.extend({"id": f"{did}.{j}", "content": x} for j, x in enumerate(val))
            else:
                docs.append({"id": did, "content": val})
    if docs:
        pkg["documents"] = list(pkg.get("documents") or pkg.pop("docs", None) or []) + docs
    many = pkg.pop("packages", None) or pkg.pop("invoices", None)
    if many and isinstance(many, list):
        shared = pkg.get("documents", [])  # e.g. policy / payment history sent once for all packages
        out = []
        for j, sub in enumerate(many):
            sub = dict(sub)
            if isinstance(sub.get("documents"), dict) or "documents" not in sub and ("files" in sub or "docs" in sub):
                sub["documents"] = sub.pop("files", None) or sub.pop("docs", None) or sub.get("documents")
            if "documents" not in sub:
                sub = {"package_id": sub.get("package_id") or sub.get("id") or f"pkg{j+1}",
                       "documents": [{"id": "invoice", "type": "invoice", "content": sub}]}
            sub["documents"] = list(sub["documents"]) + shared
            out.append(sub)
        return out, True
    if isinstance(pkg.get("invoice"), (dict, str)) and not pkg.get("documents"):
        pkg["documents"] = [{"id": "invoice", "type": "invoice", "content": pkg["invoice"]}]
    return [pkg], False


def task_id_for(msg, pkgs):
    md = msg.get("metadata") or {}
    tid = msg.get("taskId") or msg.get("task_id") or md.get("task_id") or md.get("taskId")
    if not tid and len(pkgs) == 1:
        tid = pkgs[0].get("task_id") or pkgs[0].get("taskId")
    if tid:
        return str(tid)
    # no id from the client: identical content => same task, so a blind retry replays instead of paying again
    h = hashlib.sha256(json.dumps([msg.get("contextId"), pkgs], sort_keys=True, default=str).encode()).hexdigest()
    return "task-" + h[:24]


def handle_send(params):
    msg = params.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("parts"), list):
        return None, ("INVALID_ARGUMENT", "message with parts is required")
    pkgs, multi = packages_from_message(msg)
    tid = task_id_for(msg, pkgs)
    ctx = msg.get("contextId") or tid
    results = []
    for j, p in enumerate(pkgs):
        sub_id = tid if not multi else f"{tid}:{p.get('package_id') or p.get('id') or j+1}"
        p = dict(p, task_id=sub_id, context_id=ctx)
        r = process(p)
        r["package_id"] = p.get("package_id") or p.get("id") or sub_id
        results.append(r)
    with store.tx() as c:  # remember the A2A-level task so tasks/get can rebuild the full answer
        c.execute("INSERT OR REPLACE INTO a2a_tasks(task_id, context_id, sub_ids, history, updated_at) VALUES(?,?,?,?,?)",
                  (tid, ctx, json.dumps([r["task_id"] for r in results]), json.dumps([msg]), store.time.time()))
    return build_task(tid, ctx, results, msg), None


def decision_view(r):
    return {"package_id": r.get("package_id"), "task_id": r["task_id"],
            "invoice_id": (r.get("invoice") or {}).get("invoice_number"),
            "action": r["action"], "decision": r["action"], "status": r.get("status"),
            "reasons": r.get("reasons"), "evidence": r.get("evidence"), "citations": r.get("evidence"),
            "invoice": r.get("invoice"), "receipt": r.get("receipt"), "prior_receipt": r.get("prior_receipt"),
            "replayed": bool(r.get("replayed")), "error": r.get("error")}


def state_of(results):
    sts = [r.get("status") for r in results]
    if any(s == "canceled" for s in sts) and all(s == "canceled" for s in sts):
        return "TASK_STATE_CANCELED"
    if any(s == "payment_failed" for s in sts):
        return "TASK_STATE_FAILED"
    if any(r["action"] == ESCALATE and r.get("status") != "canceled" for r in results):
        return "TASK_STATE_INPUT_REQUIRED"
    return "TASK_STATE_COMPLETED"


def build_task(tid, ctx, results, msg=None):
    decisions = [decision_view(r) for r in results]
    summary = "\n".join(f"{d['package_id']}: {d['action']} - {' '.join(d['reasons'] or [])}"
                        + (f" (receipt {d['receipt'].get('receipt_id')})" if d.get("receipt") else "") for d in decisions)
    data = decisions[0] if len(decisions) == 1 else {"decisions": decisions}
    if len(decisions) == 1:
        data = dict(data, decisions=decisions)
    return {
        "id": tid, "contextId": ctx,
        "status": {"state": state_of(results), "timestamp": now_iso(),
                   "message": {"messageId": str(uuid.uuid5(uuid.NAMESPACE_URL, tid + summary)), "contextId": ctx,
                               "taskId": tid, "role": "ROLE_AGENT", "parts": [{"text": summary}]}},
        "artifacts": [{"artifactId": f"{tid}-decision", "name": "invoice-decision",
                       "parts": [{"data": data, "mediaType": "application/json"}, {"text": summary, "mediaType": "text/plain"}]}],
        "history": [msg] if msg else [],
        "metadata": {"decisions": [{"package_id": d["package_id"], "action": d["action"]} for d in decisions]},
    }


def load_task(tid):
    r = store.conn().execute("SELECT * FROM a2a_tasks WHERE task_id=?", (tid,)).fetchone()
    if not r:
        t = store.get_task(tid)  # allow fetching a single package sub-task directly
        if not t or not t["result"]:
            return None
        return build_task(tid, t["result"].get("context_id") or tid, [t["result"]])
    results = [(store.get_task(s) or {}).get("result") for s in json.loads(r["sub_ids"])]
    if any(x is None for x in results):
        return {"id": tid, "contextId": r["context_id"], "status": {"state": "TASK_STATE_WORKING", "timestamp": now_iso()}}
    return build_task(tid, r["context_id"], results, (json.loads(r["history"] or "[]") or [None])[0])


def handle_cancel(tid):
    r = store.conn().execute("SELECT * FROM a2a_tasks WHERE task_id=?", (tid,)).fetchone()
    subs = json.loads(r["sub_ids"]) if r else ([tid] if store.get_task(tid) else None)
    if subs is None:
        return None, ("TASK_NOT_FOUND", f"task {tid} not found")
    outs = [cancel(s) for s in subs]
    if any(o is False for o in outs):
        return None, ("TASK_NOT_CANCELABLE", f"task {tid} is already in a terminal state")
    return load_task(tid), None


# ---------------- version / errors ----------------

HTTP = {"TASK_NOT_FOUND": 404, "TASK_NOT_CANCELABLE": 400, "VERSION_NOT_SUPPORTED": 400, "INVALID_ARGUMENT": 400,
        "UNSUPPORTED_OPERATION": 400, "CONTENT_TYPE_NOT_SUPPORTED": 400}
RPC = {"TASK_NOT_FOUND": -32001, "TASK_NOT_CANCELABLE": -32002, "UNSUPPORTED_OPERATION": -32004,
       "CONTENT_TYPE_NOT_SUPPORTED": -32005, "VERSION_NOT_SUPPORTED": -32009, "INVALID_ARGUMENT": -32602}


def version_error(req):
    v = (req.headers.get("A2A-Version") or req.query_params.get("A2A-Version") or "").strip()
    if not v:
        return None
    mm = ".".join(v.split(".")[:2])
    return None if mm in SUPPORTED or v.split(".")[0] == "1" else ("VERSION_NOT_SUPPORTED", f"A2A version {v} not supported")


def rest_ok(obj, code=200):
    return JSONResponse(obj, status_code=code, media_type=MEDIA)


def rest_err(e):
    reason, message = e
    code = HTTP.get(reason, 400)
    return JSONResponse({"error": {"code": code, "status": {404: "NOT_FOUND"}.get(code, "FAILED_PRECONDITION" if reason != "INVALID_ARGUMENT" else "INVALID_ARGUMENT"),
                                   "message": message,
                                   "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                                                "domain": "a2a-protocol.org"}]}},
                        status_code=code, media_type=MEDIA)


async def body_json(req):
    try:
        return await req.json()
    except Exception:
        return None


# ---------------- REST binding ----------------

for prefix in ("/a2a", "/a2a/v1", ""):
    async def send(req: Request):
        if (e := version_error(req)):
            return rest_err(e)
        body = await body_json(req)
        if not isinstance(body, dict):
            return rest_err(("INVALID_ARGUMENT", "body must be a JSON SendMessageRequest"))
        task, e = handle_send(body)
        return rest_err(e) if e else rest_ok({"task": task})

    async def get_task(task_id: str, req: Request):
        if (e := version_error(req)):
            return rest_err(e)
        if task_id.endswith(":cancel"):
            return rest_err(("UNSUPPORTED_OPERATION", "use POST to cancel"))
        t = load_task(task_id)
        if not t:
            return rest_err(("TASK_NOT_FOUND", f"task {task_id} not found"))
        hl = req.query_params.get("historyLength")
        if hl is not None and hl.isdigit():
            t["history"] = t.get("history", [])[-int(hl):] if int(hl) else []
        return rest_ok(t)

    async def task_post(rest: str, req: Request):
        if (e := version_error(req)):
            return rest_err(e)
        if rest.endswith(":cancel"):
            t, e = handle_cancel(rest[:-len(":cancel")])
            return rest_err(e) if e else rest_ok(t)
        if rest.endswith(":subscribe"):
            return rest_err(("UNSUPPORTED_OPERATION", "streaming is not supported"))
        return rest_err(("UNSUPPORTED_OPERATION", "push notifications are not supported"))

    async def list_tasks(req: Request):
        ctx = req.query_params.get("contextId")
        rows = store.conn().execute("SELECT task_id FROM a2a_tasks WHERE (? IS NULL OR context_id=?) ORDER BY updated_at DESC LIMIT 100",
                                    (ctx, ctx)).fetchall()
        tasks = [t for t in (load_task(r["task_id"]) for r in rows) if t]
        return rest_ok({"tasks": tasks, "nextPageToken": "", "pageSize": len(tasks), "totalSize": len(tasks)})

    router.add_api_route(prefix + "/message:send", send, methods=["POST"])
    router.add_api_route(prefix + "/message:stream", lambda: rest_err(("UNSUPPORTED_OPERATION", "streaming is not supported")), methods=["POST"])
    router.add_api_route(prefix + "/tasks", list_tasks, methods=["GET"])
    router.add_api_route(prefix + "/tasks/{task_id}", get_task, methods=["GET"])
    router.add_api_route(prefix + "/tasks/{rest:path}", task_post, methods=["POST"])
    router.add_api_route(prefix + "/extendedAgentCard", lambda: rest_err(("UNSUPPORTED_OPERATION", "no extended card")), methods=["GET"])


# ---------------- JSON-RPC binding ----------------

def v03(task):
    """Down-convert a v1.0 Task to v0.3 JSON-RPC shapes (kind fields, lowercase states)."""
    def part(p):
        return {"kind": "data", "data": p["data"]} if "data" in p else {"kind": "text", "text": p.get("text", "")}
    st = task["status"]
    out = {"kind": "task", "id": task["id"], "contextId": task["contextId"],
           "status": {"state": st["state"].replace("TASK_STATE_", "").lower().replace("_", "-"), "timestamp": st.get("timestamp")},
           "artifacts": [dict(a, parts=[part(p) for p in a["parts"]]) for a in task.get("artifacts", [])],
           "metadata": task.get("metadata", {})}
    if st.get("message"):
        m = st["message"]
        out["status"]["message"] = {"kind": "message", "role": "agent", "messageId": m["messageId"],
                                    "parts": [part(p) for p in m["parts"]], "taskId": task["id"], "contextId": task["contextId"]}
    return out


async def rpc(req: Request):
    body = await body_json(req)
    if body is None:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                             "error": {"code": -32600, "message": "Invalid Request"}})
    id_, method, params = body.get("id"), body["method"], body.get("params") or {}
    old = "/" in method

    def ok(task, wrap=True):
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": v03(task) if old else ({"task": task} if wrap else task)})

    def bad(e):
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": RPC.get(e[0], -32603), "message": e[1]}})

    if (e := version_error(req)):
        return bad(e)
    if method in ("message/send", "SendMessage"):
        task, e = handle_send(params)
        return bad(e) if e else ok(task)
    if method in ("tasks/get", "GetTask"):
        t = load_task(str(params.get("id")))
        return ok(t, wrap=False) if t else bad(("TASK_NOT_FOUND", "Task not found"))
    if method in ("tasks/cancel", "CancelTask"):
        t, e = handle_cancel(str(params.get("id")))
        return bad(e) if e else ok(t, wrap=False)
    if method in ("message/stream", "SendStreamingMessage", "tasks/resubscribe", "SubscribeToTask"):
        return bad(("UNSUPPORTED_OPERATION", "streaming is not supported"))
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}})


for path in ("/a2a", "/a2a/", "/a2a/v1", "/"):
    router.add_api_route(path, rpc, methods=["POST"])
