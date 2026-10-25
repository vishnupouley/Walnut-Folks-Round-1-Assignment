"""Live acceptance run against a real uvicorn server with the real 30s delay.

Covers the assignment's success criteria: single transaction, duplicate
prevention, latency under load, and restart recovery. Prints PASS/FAIL per check
and exits non-zero on any failure.

    .venv/bin/python scripts/acceptance.py
"""

import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
# Uses DATABASE_URL when set (e.g. Postgres), else a throwaway SQLite file.
DB_URL = os.environ.get("DATABASE_URL") or (
    "sqlite:///"
    + os.path.join(tempfile.gettempdir(), f"acceptance_{uuid.uuid4().hex}.db")
)
ENV = dict(os.environ, DATABASE_URL=DB_URL, PROCESSING_DELAY_SECONDS="30")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{f' -- {detail}' if detail else ''}",
        flush=True,
    )
    if not ok:
        failures.append(name)


def start_server() -> subprocess.Popen:
    log_path = os.path.join(tempfile.gettempdir(), f"uvicorn_{uuid.uuid4().hex}.log")
    log = open(log_path, "wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        env=ENV,
        stdout=log,
        stderr=log,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            print(f"server exited early; log at {log_path}")
            sys.exit(1)
        try:
            if httpx.get(f"{BASE}/", timeout=2).status_code == 200:
                print(f"server up on {BASE} (pid {process.pid})", flush=True)
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    print(f"server never became ready; log at {log_path}")
    sys.exit(1)


def webhook(client: httpx.Client, transaction_id: str, amount=1500, currency="INR"):
    body = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": amount,
        "currency": currency,
    }
    started = time.perf_counter()
    response = client.post("/v1/webhooks/transactions", json=body)
    return response, (time.perf_counter() - started) * 1000


def status_of(client: httpx.Client, transaction_id: str) -> dict:
    return client.get(f"/v1/transactions/{transaction_id}").json()[0]


def main() -> int:
    print(f"database: {DB_URL}\n", flush=True)
    server = start_server()

    with httpx.Client(base_url=BASE, timeout=10) as client:
        # --- health -------------------------------------------------------
        started = time.perf_counter()
        health = client.get("/").json()
        health_ms = (time.perf_counter() - started) * 1000
        check(
            "GET / returns HEALTHY with IST timestamp",
            health["status"] == "HEALTHY" and health["current_time"].endswith(" IST"),
            f"{health} in {health_ms:.1f}ms",
        )

        # --- single transaction, full lifecycle ---------------------------
        txn = "txn_single_" + uuid.uuid4().hex[:8]
        response, post_ms = webhook(client, txn, amount=150.50, currency="INR")
        check(
            "webhook returns 202 Accepted",
            response.status_code == 202,
            f"body={response.text}",
        )
        check("webhook responds < 500ms", post_ms < 500, f"{post_ms:.1f}ms")

        immediate = status_of(client, txn)
        check(
            "transaction visible immediately as PROCESSING",
            immediate["status"] == "PROCESSING" and immediate["processed_at"] is None,
            f"status={immediate['status']} processed_at={immediate['processed_at']}",
        )
        check(
            "stored amount/currency match the webhook",
            immediate["amount"] == 150.50 and immediate["currency"] == "INR",
            f"amount={immediate['amount']} currency={immediate['currency']}",
        )

        print("waiting 33s for the 30s background job...", flush=True)
        time.sleep(33)
        processed = status_of(client, txn)
        created = processed["created_at"]
        check(
            "transaction becomes PROCESSED after ~30s",
            processed["status"] == "PROCESSED"
            and processed["processed_at"] is not None,
            f"status={processed['status']} created_at={created} processed_at={processed['processed_at']}",
        )

        # --- duplicate prevention ----------------------------------------
        dup = "txn_dup_" + uuid.uuid4().hex[:8]
        webhook(client, dup)
        sequential = [webhook(client, dup)[0] for _ in range(5)]

        with ThreadPoolExecutor(max_workers=20) as pool:
            concurrent = list(pool.map(lambda _: webhook(client, dup)[0], range(20)))

        codes = [r.status_code for r in sequential + concurrent]
        check(
            "all 26 duplicate webhooks return 202",
            set(codes) == {202},
            f"codes={sorted(set(codes))}",
        )
        rows = client.get(f"/v1/transactions/{dup}").json()
        check("exactly one transaction row exists", len(rows) == 1, f"rows={len(rows)}")

        # --- latency under load ------------------------------------------
        heavy = ["txn_load_" + uuid.uuid4().hex[:8] for _ in range(20)]
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda t: webhook(client, t), heavy))
        latencies = [ms for _, ms in results]
        check(
            "20 concurrent webhooks all return 202",
            all(r.status_code == 202 for r, _ in results),
            f"codes={sorted({r.status_code for r, _ in results})}",
        )
        check(
            "every concurrent webhook responds < 500ms",
            max(latencies) < 500,
            f"p50={statistics.median(latencies):.1f}ms max={max(latencies):.1f}ms",
        )

        # --- reliability: crash mid-processing, restart, recover -----------
        crashed = "txn_crash_" + uuid.uuid4().hex[:8]
        webhook(client, crashed)
        check(
            "crash test transaction accepted",
            status_of(client, crashed)["status"] == "PROCESSING",
        )

    print("killing the server mid-processing (simulated crash)...", flush=True)
    server.kill()
    server.wait(timeout=15)

    server = start_server()
    with httpx.Client(base_url=BASE, timeout=10) as client:
        immediate = status_of(client, crashed)
        check(
            "interrupted transaction is re-queued on startup",
            immediate["status"] == "PROCESSING",
            f"status={immediate['status']}",
        )
        print("waiting 33s for recovery to finish...", flush=True)
        time.sleep(33)
        recovered = status_of(client, crashed)
        check(
            "interrupted transaction is PROCESSED after restart",
            recovered["status"] == "PROCESSED"
            and recovered["processed_at"] is not None,
            f"status={recovered['status']} processed_at={recovered['processed_at']}",
        )

        # Duplicates still not reprocessed after restart.
        rows = client.get(f"/v1/transactions/{dup}").json()
        check(
            "duplicate transaction still a single row after restart",
            len(rows) == 1,
            f"rows={len(rows)}",
        )

    server.terminate()
    server.wait(timeout=15)

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL LIVE CHECKS PASSED")
    for name in failures:
        print(f"  - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
