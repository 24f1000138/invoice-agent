import os, tempfile
os.environ["STATE_DB"] = tempfile.mktemp(suffix=".db")
os.environ.pop("LLM_API_KEY", None)
from fastapi.testclient import TestClient
from app.main import app
from app import store, pay

c = TestClient(app)


def pkg(tid, inv="INV-1001", amount="4,500.00", approver="Priya Shah", limit="5,000", extra=None, receipt="All items received in full."):
    docs = [
        {"id": "invoice", "type": "invoice", "content": f"INVOICE\nVendor: Acme Supplies Ltd\nInvoice No: {inv}\nPO Number: PO-77\nTotal Due: USD {amount}"},
        {"id": "po", "type": "purchase_order", "content": "Purchase Order PO-77\nTotal: USD 10,000.00"},
        {"id": "grn", "type": "receipt", "content": f"Goods Received Note for PO-77. {receipt}"},
        {"id": "approval", "type": "approval", "content": f"Approved by: {approver}\nFor: USD {amount}"},
        {"id": "policy", "type": "policy", "content": f"Approval limits\nPriya Shah: USD {limit}\nCFO: USD 1,000,000"},
    ]
    return {"task_id": tid, "documents": docs + (extra or [])}


def setup_function():
    store.reset()


def test_pay_once_and_replay():
    r = c.post("/process", json=pkg("t1")).json()
    assert r["action"] == "pay" and r["receipt"]["receipt_id"]
    assert any(e["doc_id"] == "approval" for e in r["evidence"])
    r2 = c.post("/process", json=pkg("t1")).json()
    assert r2["replayed"] and r2["receipt"] == r["receipt"]
    assert len(store.list_payments()) == 1


def test_same_invoice_new_task_is_duplicate():
    c.post("/process", json=pkg("t1"))
    r = c.post("/process", json=pkg("t2")).json()
    assert r["action"] == "reject_duplicate"
    assert len(store.list_payments()) == 1


def test_different_invoice_same_amount_is_not_duplicate():
    c.post("/process", json=pkg("t1", inv="INV-1001"))
    r = c.post("/process", json=pkg("t2", inv="INV-1002")).json()
    assert r["action"] == "pay"
    assert len(store.list_payments()) == 2


def test_history_duplicate_and_non_duplicate():
    hist = [{"id": "hist", "type": "payment_history", "content": "Payment history\nINV-1001 paid on 2026-09-01 USD 4,500.00"}]
    assert c.post("/process", json=pkg("t1", extra=hist)).json()["action"] == "reject_duplicate"
    assert c.post("/process", json=pkg("t2", inv="INV-1009", extra=hist)).json()["action"] == "pay"


def test_over_limit_escalates():
    r = c.post("/process", json=pkg("t1", amount="7,500.00")).json()
    assert r["action"] == "escalate" and "receipt" not in r
    assert store.list_payments() == []


def test_unverified_bank_change_holds():
    bc = [{"id": "email", "type": "bank_change", "content": "Please update our bank details to new account 99887766."}]
    r = c.post("/process", json=pkg("t1", extra=bc)).json()
    assert r["action"] == "hold" and r["evidence"][0]["doc_id"] == "email"


def test_partial_receipt_holds():
    assert c.post("/process", json=pkg("t1", receipt="Partial delivery, 3 units short.")).json()["action"] == "hold"


def test_crash_after_payment_does_not_pay_twice(monkeypatch):
    calls = []
    real = pay.execute
    import app.agent as agent

    def flaky(*a):
        calls.append(1)
        rec = real(*a)
        raise RuntimeError("crash after rail accepted payment")
    monkeypatch.setattr(agent, "execute", flaky)
    try:
        c.post("/process", json=pkg("t1"))
    except RuntimeError:
        pass
    monkeypatch.setattr(agent, "execute", lambda *a: (calls.append(1), real(*a))[1])
    r = c.post("/process", json=pkg("t1")).json()
    assert r["action"] == "pay" and r["receipt"]["receipt_id"]
    # retry reused the same idempotency key, and only one payment row exists
    assert len(store.list_payments()) == 1 and store.list_payments()[0]["status"] == "paid"


def test_bad_receipt_rejected(monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "execute", lambda k, inv, t: pay.validate({"id": "r1", "status": "paid", "amount": 1.0}, inv))
    r = c.post("/process", json=pkg("t1")).json()
    assert r["status"] == "payment_failed"
    monkeypatch.setattr(agent, "execute", lambda k, inv, t: pay.validate({"id": "r2", "status": "paid", "amount": inv["amount"]}, inv))
    r = c.post("/process", json=pkg("t1")).json()
    assert r["receipt"]["receipt_id"] == "r2"
