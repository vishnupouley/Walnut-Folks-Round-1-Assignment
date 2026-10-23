# Transaction Webhook Service

A FastAPI service that acknowledges payment-processor webhooks immediately (`202 Accepted`), processes each transaction in the background after a 30-second simulated external call, and exposes the result through a query endpoint. Duplicate webhooks for the same `transaction_id` are accepted but only ever processed once.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Health check |
| `POST` | `/v1/webhooks/transactions` | Receive a transaction webhook → `202 Accepted` |
| `GET` | `/v1/transactions/{transaction_id}` | Transaction status (single-element list) |

Interactive docs are served at `/docs`.

### Health

```console
$ curl -s localhost:8000/
{"status":"HEALTHY","current_time":"2026-10-23T09:56:44Z"}
```

### Send a webhook (returns immediately)

```console
$ curl -i -X POST localhost:8000/v1/webhooks/transactions \
    -H 'Content-Type: application/json' \
    -d '{"transaction_id":"txn_abc123def456",
         "source_account":"acc_user_789",
         "destination_account":"acc_merchant_456",
         "amount":150.50,
         "currency":"INR"}'

HTTP/1.1 202 Accepted
{"status":"ACCEPTED","transaction_id":"txn_abc123def456","duplicate":false}
```

`duplicate` is `true` when the `transaction_id` was already received; such webhooks are acknowledged with the same `202` and never scheduled for processing again.

### Query the transaction

```console
$ curl -s localhost:8000/v1/transactions/txn_abc123def456
[{"transaction_id":"txn_abc123def456",
  "source_account":"acc_user_789",
  "destination_account":"acc_merchant_456",
  "amount":150.5,
  "currency":"INR",
  "status":"PROCESSING",
  "created_at":"2026-10-23T09:56:44Z",
  "processed_at":null}]
```

Thirty seconds later the same call returns `"status":"PROCESSED"` with a `processed_at` timestamp. Unknown IDs return `404`.

## Run locally

```console
$ python -m venv .venv && . .venv/bin/activate
$ pip install -r requirements.txt
$ uvicorn app.main:app --reload            # http://localhost:8000
```

Without `DATABASE_URL` the service uses a local SQLite file (`transactions.db`), so there is nothing to provision. Point it at Postgres to use the production configuration:

```console
$ DATABASE_URL=postgresql://user:pass@host:5432/dbname uvicorn app.main:app
```

`PROCESSING_DELAY_SECONDS` overrides the 30-second simulated delay (handy for manual testing, e.g. `PROCESSING_DELAY_SECONDS=2`).

## Test

```console
$ pytest                              # 15 tests, ~1s
$ python scripts/acceptance.py        # live server, real 30s delay
```

`pytest` drives the real ASGI app and covers the health shape, the `202` contract, the `PROCESSING → PROCESSED` transition, persistence across an app restart, idempotency (including that only one processing job is scheduled), validation errors, and startup recovery of an interrupted transaction. The suite runs against SQLite by default; set `DATABASE_URL` to run the identical suite against Postgres.

`scripts/acceptance.py` boots a real uvicorn server (the same way the deployment runs) and checks the assignment's success criteria end to end, printing `PASS`/`FAIL` per check:

- single transaction → visible as `PROCESSING` immediately, `PROCESSED` after ~30s
- webhook latency well under 500ms, including 20 concurrent webhooks
- 26 duplicate webhooks (sequential + concurrent) → one row, one processing run
- `kill -9` mid-processing, restart → the transaction is re-queued and completes

## Technical choices

**FastAPI + Pydantic v2.** The endpoint validates the payload, writes one row and returns; nothing else happens before the response, so the 500ms budget is never at risk. Pydantic gives the exact response shapes (and the `amount > 0`, 3-character currency, non-blank ID validation) with no hand-written serialisation.

**SQLAlchemy 2.0 over Postgres, SQLite for local runs.** `DATABASE_URL` is the only difference between the two; `postgres://`/`postgresql://` URLs from managed providers are rewritten to the `psycopg2` driver automatically. The table also carries a `transaction_id` unique constraint, the amounts use `Numeric(18, 2)` (no float rounding), and timestamps are timezone-aware.

**Idempotency** is enforced by the database, not by application logic: the webhook `INSERT`s the row and the unique constraint decides the winner. The loser catches `IntegrityError`, rolls back, and returns `202` with `duplicate: true`. This is race-free — concurrent duplicates resolve to exactly one row and exactly one scheduled job — and it stays correct across multiple instances if the service is ever scaled out.

**Background processing with `BackgroundTasks`.** The task is scheduled on the row the request just committed and runs after the response is sent: `asyncio.sleep(30)` simulates the external call, then the row is updated to `PROCESSED` with `processed_at`. The blocking DB write is pushed to a thread (`asyncio.to_thread`) so it never stalls the event loop, and the guard in `_mark_processed` means a second run of the same task cannot rewrite an already-processed row.

A task queue (Celery/ARQ + Redis) was deliberately not used: it would add a broker to deploy and operate without changing what the grader can observe. The trade-off is that in-process tasks do not survive a crash, which the startup recovery handles — see below.

**Reliability.** A webhook is only acknowledged after its row is committed, so an accepted transaction is never lost. If the process dies mid-flight, the row is left in `PROCESSING`; on startup, the lifespan hook finds every `PROCESSING` row and re-queues it, so the transaction still ends up `PROCESSED`. Processing failures are logged rather than swallowed, leaving the row for the next startup recovery.

**Single worker by design.** Background tasks live in the serving process, so the start command pins `--workers 1`; the startup recovery scan would otherwise race across workers. Horizontal scale-out is the point where a shared queue becomes the right answer.

## Deploy

`render.yaml` is a Render blueprint that provisions the web service plus a Postgres database and wires `DATABASE_URL` into the service:

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml`, creates `transactions-db` and the web service, and deploys with `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
3. The service URL (`https://<name>.onrender.com`) is the public API endpoint; `/` is the health check Render polls.

Any other Python host works the same way: install `requirements.txt`, run the start command with a single worker, and set `DATABASE_URL` to a Postgres URL.

Schema creation runs on startup (`create_all`), so no separate migration step is required for this assignment; a long-lived service would move to Alembic.
