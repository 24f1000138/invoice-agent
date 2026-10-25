---
title: Invoice Agent
emoji: 🧾
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Invoice agent

FastAPI service: reads an invoice package, decides one action (pay / hold / escalate /
reject / reject_duplicate), cites evidence (doc_id + verbatim quote), pays only approved
invoices, and stores task + payment state in SQLite so retries never pay twice.

Submit `https://<host>/a2a/` as the A2A base URL.

A2A v1.0 HTTP+JSON binding (also mounted at `/` and `/a2a/v1`):
`GET /.well-known/agent-card.json`, `POST /a2a/message:send`, `GET /a2a/tasks/{id}`,
`POST /a2a/tasks/{id}:cancel`, `GET /a2a/tasks`. JSON-RPC (`SendMessage`/`GetTask`/`CancelTask`
and v0.3 `message/send`/`tasks/get`/`tasks/cancel`) at `POST /a2a`.
Escalated invoices pause in `TASK_STATE_INPUT_REQUIRED`; a follow-up message with the same
`taskId` carrying the approval resumes the task and pays. Debug: `GET /state`, `GET /health`.

Env: `PUBLIC_URL` (for the agent card), `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` with fallbacks
`LLM2_*`, `LLM3_*` (any OpenAI-compatible API), `PAYMENT_URL`, `PAYMENT_TOKEN`, `STATE_DB`, `ADMIN_TOKEN`.

Run locally: `pip install -r requirements.txt && uvicorn app.main:app --port 8000`
Tests: `PYTHONPATH=.:tests pytest -q tests`

## Free HTTPS hosting plan
1. Hugging Face Spaces (Docker SDK, free, HTTPS at https://<user>-<space>.hf.space). Push this
   folder; Dockerfile already listens on 7860. Set secrets in Space settings.
2. Fallback: Render free web service (Docker). Sleeps after idle; first request is slow.
3. Free-tier disks are wiped on restart. If the grader checks state across a restart, point
   state at a free hosted DB (Neon Postgres / Turso) instead of the local SQLite file.
