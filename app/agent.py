"""Orchestrates one task: extract -> decide -> (maybe) pay, persisting each step so a
retry or crash resumes instead of repeating work."""
import hashlib, json, time
from . import store
from .extract import extract
from .decide import decide, PAY, REJECT_DUP, ESCALATE, HOLD
from .pay import execute, PaymentError


def task_id_of(pkg):
    for k in ("task_id", "taskId", "id", "package_id", "request_id"):
        if pkg.get(k):
            return str(pkg[k])
    return "auto-" + req_hash(pkg)[:24]


def req_hash(pkg):
    return hashlib.sha256(json.dumps(pkg, sort_keys=True, default=str).encode()).hexdigest()


def process(pkg):
    tid, h, now = task_id_of(pkg), req_hash(pkg), time.time()

    # 1. claim / replay / continue
    with store.tx() as c:
        t = store.get_task(tid)
        if t and t["status"] == "canceled":
            return dict(t["result"], replayed=True)
        if t and t["status"] == "done":
            if h == t["request_hash"] or t["result"]["action"] not in (ESCALATE, HOLD):
                store.log(c, tid, "replay", {})
                return dict(t["result"], replayed=True)
            # follow-up turn on a paused task (e.g. approval arrives): merge documents and decide again
            prev = json.loads(t["package"] or "{}")
            seen = {json.dumps(d, sort_keys=True, default=str) for d in prev.get("documents", [])}
            merged = list(prev.get("documents", [])) + [d for d in pkg.get("documents", [])
                                                       if json.dumps(d, sort_keys=True, default=str) not in seen]
            pkg = dict(prev, **{k: v for k, v in pkg.items() if k != "documents"}, documents=merged)
            c.execute("UPDATE tasks SET status='received', decision=NULL, request_hash=?, package=?, attempts=attempts+1, updated_at=? WHERE task_id=?",
                      (h, json.dumps(pkg, default=str), now, tid))
            store.log(c, tid, "continue", {"docs": len(merged)})
        elif not t:
            c.execute("INSERT INTO tasks(task_id,request_hash,package,status,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (tid, h, json.dumps(pkg, default=str), "received", 1, now, now))
        else:
            c.execute("UPDATE tasks SET attempts=attempts+1, updated_at=? WHERE task_id=?", (now, tid))
        store.log(c, tid, "attempt", {"hash": h})

    # 2. decide (reuse a stored decision on retry so the outcome is stable)
    t = store.get_task(tid)
    if t["package"]:
        pkg = json.loads(t["package"])
    d = t["decision"]
    if d is None:
        facts = extract(pkg)
        d = decide(facts, store.get_payment)
        d["facts_source"] = facts.get("source")
        with store.tx() as c:
            c.execute("UPDATE tasks SET decision=?, status='decided', updated_at=? WHERE task_id=?",
                      (json.dumps(d), time.time(), tid))
            store.log(c, tid, "decided", {"action": d["action"]})

    result = {"task_id": tid, "context_id": pkg.get("context_id"), "action": d["action"], "reasons": d["reasons"], "evidence": d["evidence"],
              "invoice": d["invoice"]}

    # 3. pay exactly once per invoice
    if d["action"] == PAY:
        key = d["invoice_key"]
        with store.tx() as c:
            p = store.get_payment(key)
            if p and p["task_id"] != tid and p["status"] in ("paid", "pending"):
                # another task got there first -> this one is a duplicate
                result.update(action=REJECT_DUP, reasons=[f"Invoice already paid by task {p['task_id']}."],
                              prior_receipt=p["receipt"])
                p = "dup"
            elif p is None:
                c.execute("INSERT INTO payments(invoice_key,task_id,amount,currency,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (key, tid, d["invoice"]["amount"], d["invoice"].get("currency"), "pending", time.time(), time.time()))
            elif p["status"] == "failed":
                c.execute("UPDATE payments SET status='pending', task_id=?, updated_at=? WHERE invoice_key=?", (tid, time.time(), key))
            c.execute("UPDATE tasks SET status='executing', updated_at=? WHERE task_id=?", (time.time(), tid))
        if p != "dup":
            p = store.get_payment(key)
            if p["status"] == "paid":  # crashed after paying, before finishing the task
                receipt = p["receipt"]
            else:
                try:
                    receipt = execute(key, d["invoice"], tid)  # same idempotency key on every retry
                except PaymentError as e:
                    with store.tx() as c:
                        c.execute("UPDATE payments SET status='failed', updated_at=? WHERE invoice_key=?", (time.time(), key))
                        c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE task_id=?", (time.time(), tid))
                        store.log(c, tid, "payment_failed", str(e))
                    return dict(result, status="payment_failed", error=str(e))
                with store.tx() as c:
                    c.execute("UPDATE payments SET status='paid', receipt=?, updated_at=? WHERE invoice_key=?",
                              (json.dumps(receipt), time.time(), key))
                    store.log(c, tid, "paid", receipt)
            result["receipt"] = receipt

    result["status"] = "completed"
    with store.tx() as c:
        c.execute("UPDATE tasks SET status='done', result=?, updated_at=? WHERE task_id=?",
                  (json.dumps(result, default=str), time.time(), tid))
    return result


def cancel(tid):
    """Only a task paused for approval/info can be canceled; finished or paid ones cannot."""
    with store.tx() as c:
        t = store.get_task(tid)
        if not t:
            return None
        r = t["result"] or {}
        if t["status"] == "canceled":
            return r
        if t["status"] != "done" or r.get("action") != ESCALATE:
            return False
        r = dict(r, status="canceled")
        c.execute("UPDATE tasks SET status='canceled', result=?, updated_at=? WHERE task_id=?", (json.dumps(r), time.time(), tid))
        store.log(c, tid, "canceled", {})
        return r
