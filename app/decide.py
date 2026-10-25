"""Business rules: facts in, one action out, with the evidence for every reason."""
from .extract import norm_id, norm_vendor, money

# Action names; remap here once the exam spec fixes the vocabulary.
PAY, HOLD, ESCALATE, REJECT_DUP, REJECT = "pay", "hold", "escalate", "reject_duplicate", "reject"
AMOUNT_TOLERANCE = 0.01


def val(field):
    return field.get("value") if isinstance(field, dict) else field


def ev(*items):
    return [e for e in items if e and e.get("doc_id")]


def invoice_key(vendor, number):
    return f"{norm_vendor(vendor)}|{norm_id(number)}"


def limit_for(approval, limits):
    names = [str(approval.get("approver") or "").lower(), str(approval.get("role") or "").lower()]
    for l in limits:
        key = str(l.get("role_or_name") or "").lower()
        if key and any(n and (key in n or n in key) for n in names):
            return l
    return None


def decide(f, payments_lookup):
    inv = f.get("invoice", {})
    vendor, number, amount = val(inv.get("vendor")), val(inv.get("invoice_number")), money(val(inv.get("amount")))
    reasons, evidence = [], []

    def out(action, reason, *e):
        reasons.append(reason); evidence.extend(ev(*e))
        return {"action": action, "reasons": reasons, "evidence": evidence,
                "invoice": {"vendor": vendor, "invoice_number": number, "amount": amount,
                            "currency": val(inv.get("currency")), "po_number": val(inv.get("po_number"))},
                "invoice_key": invoice_key(vendor, number) if number else None}

    if not number or amount is None:
        return out(HOLD, "Invoice number or amount could not be established from the documents.",
                   (inv.get("invoice_number") or {}).get("evidence"), (inv.get("amount") or {}).get("evidence"))

    key = invoice_key(vendor, number)
    # 1. duplicate: only the SAME invoice (vendor + invoice number). Same amount on another invoice is fine.
    prior = payments_lookup(key)
    if prior and prior["status"] in ("paid", "pending"):
        r = out(REJECT_DUP, f"Invoice {number} was already paid (receipt {((prior.get('receipt') or {}).get('receipt_id'))}).",
                inv["invoice_number"].get("evidence"))
        r["prior_payment"] = prior
        return r
    for p in f.get("prior_payments", []):
        same_no = norm_id(p.get("invoice_number")) == norm_id(number)
        same_vendor = not p.get("vendor") or not vendor or norm_vendor(p["vendor"]) == norm_vendor(vendor)
        if same_no and same_vendor:
            return out(REJECT_DUP, f"Payment history shows invoice {number} already paid.",
                       p.get("evidence"), inv["invoice_number"].get("evidence"))

    # 2. explicit rejection
    for a in f.get("approvals", []):
        if a.get("status") == "rejected":
            return out(REJECT, f"Approval was rejected by {a.get('approver')}.", a.get("evidence"))

    # 3. unverified bank-detail change (classic fraud pattern)
    bc = f.get("bank_change")
    if bc and not bc.get("verified"):
        return out(HOLD, "Vendor bank details changed without independent verification.", bc.get("evidence"))

    # 4. goods / services receipt
    rec = f.get("receipts", [])
    if f.get("po") and not rec:
        return out(HOLD, "No goods-received / delivery confirmation for a PO invoice.", (inv.get("po_number") or {}).get("evidence"))
    for r in rec:
        if not r.get("complete"):
            return out(HOLD, "Receipt shows goods not fully received.", r.get("evidence"))

    # 5. PO match
    po = f.get("po") or {}
    if po:
        if val(po.get("po_number")) and val(inv.get("po_number")) and norm_id(val(po["po_number"])) != norm_id(val(inv["po_number"])):
            return out(HOLD, "Invoice PO number does not match the purchase order.",
                       po["po_number"].get("evidence"), inv["po_number"].get("evidence"))
        po_amt = money(val(po.get("amount")))
        if po_amt is not None and amount > po_amt + AMOUNT_TOLERANCE:
            return out(HOLD, f"Invoice amount {amount} exceeds PO amount {po_amt}.",
                       po["amount"].get("evidence"), inv["amount"].get("evidence"))

    # 6. approval within limit
    approved = [a for a in f.get("approvals", []) if a.get("status") == "approved"]
    limits = f.get("limits", [])
    if not approved:
        return out(ESCALATE, "No approval on file; route for approval.", inv["amount"].get("evidence"))
    best_gap = None
    for a in approved:
        l = limit_for(a, limits)
        cap = l["limit"] if l else a.get("amount")
        if cap is None or amount <= cap + AMOUNT_TOLERANCE:
            if a.get("amount") is not None and amount > a["amount"] + AMOUNT_TOLERANCE:
                best_gap = (a, l, a["amount"]); continue
            reasons.append(f"Approved by {a.get('approver')}" + (f" within limit {cap}." if cap is not None else "."))
            evidence.extend(ev(a.get("evidence"), (l or {}).get("evidence")))
            return out(PAY, f"Invoice {number} for {amount} is matched, received and approved; pay once.",
                       inv["invoice_number"].get("evidence"), inv["amount"].get("evidence"))
        best_gap = (a, l, cap)
    a, l, cap = best_gap
    return out(ESCALATE, f"Amount {amount} exceeds {a.get('approver')}'s approval limit {cap}; escalate.",
               a.get("evidence"), (l or {}).get("evidence"), inv["amount"].get("evidence"))
