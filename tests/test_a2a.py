from fastapi.testclient import TestClient
from app.main import app
from app import store
from test_agent import pkg

c = TestClient(app)
H = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}


def msg(package, mid="m1", **extra):
    return {"messageId": mid, "role": "ROLE_USER",
            "parts": [{"text": d["content"], "metadata": {"id": d["id"]}} for d in package["documents"]], **extra}


def rest_send(package, mid="m1", base="/a2a", **extra):
    return c.post(f"{base}/message:send", json={"message": msg(package, mid, **extra)}, headers=H)


def data_of(task):
    return task["artifacts"][0]["parts"][0]["data"]


def setup_function():
    store.reset()


def test_card():
    for p in ("/.well-known/agent-card.json", "/a2a/.well-known/agent-card.json"):
        j = c.get(p).json()
        assert j["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON" and j["skills"]


def test_rest_pay_retry_get():
    p = pkg(None)
    r = rest_send(p)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/a2a+json")
    t = r.json()["task"]
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and data_of(t)["action"] == "pay"
    assert {e["doc_id"] for e in data_of(t)["evidence"]} >= {"approval", "invoice"}
    t2 = rest_send(p, "m2").json()["task"]  # blind retry, new messageId
    assert t2["id"] == t["id"] and data_of(t2)["receipt"] == data_of(t)["receipt"]
    g = c.get(f"/a2a/tasks/{t['id']}", headers=H)
    assert g.status_code == 200 and data_of(g.json())["action"] == "pay"
    assert c.get("/a2a/tasks%2Fnope".replace("%2F", "/"), headers=H).status_code == 404
    assert len(store.list_payments()) == 1


def test_encoded_colon_path():
    r = c.post("/a2a/message%3Asend", json={"message": msg(pkg(None))}, headers=H)
    assert r.status_code == 200


def test_same_invoice_other_task_duplicate():
    rest_send(pkg(None), taskId="A")
    t = rest_send(pkg(None), taskId="B").json()["task"]
    assert data_of(t)["action"] == "reject_duplicate"


def test_escalate_then_approval_followup_then_cancel_rules():
    p = pkg(None, amount="7,500.00")
    t = rest_send(p).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and data_of(t)["action"] == "escalate"
    assert store.list_payments() == []
    approval = {"messageId": "m2", "role": "ROLE_USER", "taskId": t["id"],
                "parts": [{"text": "Approved by: CFO\nFor: USD 7,500.00", "metadata": {"id": "cfo-approval"}}]}
    t2 = c.post("/a2a/message:send", json={"message": approval}, headers=H).json()["task"]
    assert t2["id"] == t["id"] and data_of(t2)["action"] == "pay" and data_of(t2)["receipt"]
    # paid task is terminal -> not cancelable
    r = c.post(f"/a2a/tasks/{t['id']}:cancel", headers=H)
    assert r.status_code == 400 and r.json()["error"]["details"][0]["reason"] == "TASK_NOT_CANCELABLE"


def test_cancel_paused_task():
    t = rest_send(pkg(None, amount="7,500.00")).json()["task"]
    r = c.post(f"/a2a/tasks/{t['id']}:cancel", headers=H)
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_CANCELED"


def test_multi_package_one_decision_each():
    a, b = pkg("x", inv="INV-1"), pkg("y", inv="INV-2", amount="9,000.00")
    body = {"message": {"messageId": "mm", "role": "ROLE_USER", "parts": [{"data": {"packages": [
        {"package_id": "P1", "documents": a["documents"]}, {"package_id": "P2", "documents": b["documents"]}]}}]}}
    t = c.post("/a2a/message:send", json=body, headers=H).json()["task"]
    ds = data_of(t)["decisions"]
    assert [d["package_id"] for d in ds] == ["P1", "P2"] and [d["action"] for d in ds] == ["pay", "escalate"]


def test_version_and_errors():
    r = c.post("/a2a/message:send", json={"message": msg(pkg(None))}, headers={"A2A-Version": "9.0"})
    assert r.status_code == 400
    assert c.post("/a2a/message:send", content=b"nope", headers=H).status_code == 400


def test_jsonrpc_both_styles():
    m = msg(pkg(None))
    r = c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": m}}).json()
    assert r["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    m03 = {"kind": "message", "role": "user", "messageId": "z",
           "parts": [{"kind": "text", "text": d["content"]} for d in pkg(None)["documents"]]}
    r = c.post("/a2a/", json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": m03}}).json()
    assert r["result"]["kind"] == "task" and r["result"]["status"]["state"] == "completed"
    assert c.post("/a2a/", json={"jsonrpc": "2.0", "id": 3, "method": "x"}).json()["error"]["code"] == -32601
