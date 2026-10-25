# Transaction Webhook Service

A webhook ingestion service built with FastAPI and PostgreSQL/SQLite that receives transaction notifications, acknowledges them immediately with `202 Accepted`, and reliably processes each transaction in the background after a 30-second delay. Duplicate webhooks for the same transaction ID are acknowledged but never processed more than once.

---

## Public Cloud Endpoint

- **Base URL**: `https://transaction-webhook-service-k8oo.onrender.com` 
- **Interactive API Docs**: `https://transaction-webhook-service-k8oo.onrender.com/docs`

The service is configured for automated cloud deployment on Render via the blueprint in `render.yaml`, which provisions both the FastAPI web application and a managed PostgreSQL database.

---

## API Reference

### 1. Health Check
Checks service availability and returns current server time in Indian Standard Time (IST).

- **Method**: `GET`
- **Path**: `/`
- **Response**: `200 OK`

```bash
curl -s http://localhost:8000/
```

```json
{
  "status": "HEALTHY",
  "current_time": "2026-10-25T16:45:00 IST"
}
```

---

### 2. Ingest Transaction Webhook
Receives a transaction webhook and queues background processing. Responds immediately within the 500ms budget.

- **Method**: `POST`
- **Path**: `/v1/webhooks/transactions`
- **Status Code**: `202 Accepted`

```bash
curl -i -X POST http://localhost:8000/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.50,
    "currency": "INR"
  }'
```

```json
{
  "status": "ACCEPTED",
  "transaction_id": "txn_abc123def456",
  "duplicate": false
}
```

If the same `transaction_id` is sent again, the service acknowledges it with `202 Accepted` but marks it as a duplicate and does not trigger duplicate background processing:

```json
{
  "status": "ACCEPTED",
  "transaction_id": "txn_abc123def456",
  "duplicate": true
}
```

---

### 3. Query Transaction Status
Retrieves current processing state and timestamps for testing and verification.

- **Method**: `GET`
- **Path**: `/v1/transactions/{transaction_id}`
- **Response**: `200 OK` (returns a single-element list per specification)

```bash
curl -s http://localhost:8000/v1/transactions/txn_abc123def456
```

**Immediately after ingestion (`status: PROCESSING`):**
```json
[
  {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.5,
    "currency": "INR",
    "status": "PROCESSING",
    "created_at": "2026-10-25T16:45:00 IST",
    "processed_at": null
  }
]
```

**After ~30 seconds (`status: PROCESSED`):**
```json
[
  {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500.5,
    "currency": "INR",
    "status": "PROCESSED",
    "created_at": "2026-10-25T16:45:00 IST",
    "processed_at": "2026-10-25T16:45:30 IST"
  }
]
```

*(Returns `404 Not Found` if the transaction ID does not exist.)*

---

## Setup and Running Locally

### Prerequisites
- Python 3.10+ (tested on Python 3.12 and 3.13)
- pip

### 1. Clone & Set Up Virtual Environment

**On Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run the Development Server

```bash
uvicorn app.main:app --reload
```

By default, the application runs against a local SQLite database (`transactions.db`) created automatically on startup. No separate database provisioning is needed for local development.

### 3. Configuration (Environment Variables)

- `DATABASE_URL`: Connection string for PostgreSQL (e.g. `postgresql://user:password@localhost:5432/dbname`). Defaults to local SQLite if unset.
- `PROCESSING_DELAY_SECONDS`: Duration of simulated external processing. Defaults to `30`. Set to a smaller value (e.g., `2`) for fast manual debugging:
  ```bash
  PROCESSING_DELAY_SECONDS=2 uvicorn app.main:app --reload
  ```

---

## Testing

### Unit and Integration Tests (`pytest`)
Runs 15 automated test cases covering health check contract, immediate 202 response SLA, background state transition, duplicate suppression, crash recovery on reboot, and payload input validation.

```bash
pytest -v
```

### End-to-End Acceptance Script
Launches a live uvicorn server process and verifies real-world behavior:
- Measures webhook latency under concurrent load (< 500ms).
- Simulates 20 concurrent duplicates and verifies single processing execution.
- Kills the server mid-processing (`SIGKILL`) and restarts it to verify automatic recovery of interrupted jobs.

```bash
python scripts/acceptance.py
```

---

## Technical Choices and Architecture

### 1. FastAPI + Pydantic v2
Webhook endpoints in payment gateways (like RazorPay) enforce strict acknowledgement timeouts. FastAPI's minimal routing overhead ensures the initial database insert and `202 Accepted` response return in under 20ms, well below the 500ms threshold. Pydantic v2 enforces strict payload boundaries (ISO 3-letter currency codes, non-empty identifiers, positive amounts) at the serialization layer before application logic runs.

### 2. Database-Enforced Idempotency
Application-level idempotency checks (e.g. reading from the DB or cache before inserting) are vulnerable to race conditions when concurrent duplicate webhooks arrive simultaneously. 

Instead, idempotency is enforced by a database `UNIQUE` constraint on `transaction_id`. The initial request commits the `PROCESSING` row. Concurrent duplicates trigger an `IntegrityError`, caught in `receive_webhook`, which rolls back the transaction and returns `202 Accepted` with `{"duplicate": true}` without spawning a redundant background job. This pattern is atomic and race-free.

### 3. Financial Precision & Datetime Handling
- Currency amounts are stored as `Numeric(18, 2)` mapped to Python's `Decimal` type to prevent binary floating-point rounding errors typical with `FLOAT` columns.
- Timestamps are generated as timezone-aware Python `datetime` objects in Indian Standard Time (IST, UTC+05:30) and formatted consistently across health checks and transaction queries (`YYYY-MM-DDTHH:MM:SS IST`).

### 4. Background Processing & Thread Hygiene
Rather than blocking the HTTP request during the simulated 30-second external API delay, processing is handed off to FastAPI's `BackgroundTasks`. 
The 30-second delay uses non-blocking `asyncio.sleep(30)`, and the subsequent database write is executed via `asyncio.to_thread` so synchronous database engine calls never freeze the main async event loop.

### 5. Crash Recovery
Because in-process background tasks reside in application memory, a server termination or deployment restart during the 30-second window could leave transactions stuck in `PROCESSING`. 
To ensure fault tolerance, the transaction is committed to the database in `PROCESSING` status *before* sending the `202` response. A lifespan startup handler in `app/main.py` scans for any incomplete `PROCESSING` transactions on boot and re-queues them, guaranteeing that every accepted transaction reaches `PROCESSED`.

### 6. Single Worker Architecture
The server is started with `--workers 1`. Because background tasks and crash recovery currently execute in-process, running multiple worker processes without a distributed task broker would cause workers to race during the startup recovery scan. 
If the application needs to scale horizontally across multiple instances or containers in the future, the in-process `BackgroundTasks` can be replaced with a distributed message queue like Redis + Celery/ARQ without altering the external API contract.

### 7. Cloud Deployment via Render Blueprint
The deployment uses `render.yaml` (Infrastructure-as-Code) to provision:
1. A managed PostgreSQL database instance.
2. A Python web service running `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
3. Automatic environment variable binding: Render automatically injects `DATABASE_URL` from the database into the web service.
SQLAlchemy's `Base.metadata.create_all(engine)` automatically initializes tables on startup without requiring manual migration scripts.
