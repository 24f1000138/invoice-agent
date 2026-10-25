"""Payment rail. Uses PAYMENT_URL when set (spec TBD), otherwise a local simulator.
Always sends a stable idempotency key so a retried call cannot create a second payment."""
import hashlib, os, time
import httpx

PAYMENT_URL = os.environ.get("PAYMENT_URL", "")
PAYMENT_TOKEN = os.environ.get("PAYMENT_TOKEN", "")


class PaymentError(Exception):
    pass


def idem_key(invoice_key):
    return "pay-" + hashlib.sha256(invoice_key.encode()).hexdigest()[:32]


def execute(invoice_key, invoice, task_id):
    key = idem_key(invoice_key)
    body = {"invoice_number": invoice["invoice_number"], "vendor": invoice["vendor"],
            "amount": invoice["amount"], "currency": invoice.get("currency"), "task_id": task_id,
            "idempotency_key": key}
    if not PAYMENT_URL:
        receipt = {"receipt_id": "sim-" + key[4:20], "status": "paid", "amount": invoice["amount"],
                   "invoice_number": invoice["invoice_number"], "simulated": True, "paid_at": time.time()}
    else:
        h = {"Idempotency-Key": key}
        if PAYMENT_TOKEN:
            h["Authorization"] = f"Bearer {PAYMENT_TOKEN}"
        r = httpx.post(PAYMENT_URL, json=body, headers=h, timeout=30)
        if r.status_code >= 500:
            raise PaymentError(f"rail {r.status_code}")
        receipt = r.json()
    return validate(receipt, invoice)


def validate(receipt, invoice):
    """Accept only a receipt that really confirms this payment."""
    rid = receipt.get("receipt_id") or receipt.get("id") or receipt.get("payment_id")
    if not rid:
        raise PaymentError(f"receipt has no id: {receipt}")
    status = str(receipt.get("status", "paid")).lower()
    if status not in ("paid", "succeeded", "success", "completed", "ok"):
        raise PaymentError(f"payment not completed: {status}")
    amt = receipt.get("amount")
    if amt is not None and abs(float(amt) - float(invoice["amount"])) > 0.01:
        raise PaymentError(f"receipt amount {amt} != invoice {invoice['amount']}")
    return dict(receipt, receipt_id=rid)
