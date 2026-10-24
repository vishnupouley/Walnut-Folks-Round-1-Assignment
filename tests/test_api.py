"""End-to-end tests against the real ASGI app (requirements 1-6)."""

import time

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.db import SessionLocal, Transaction, istnow
from app.main import app


def payload(**overrides):
    body = {
        "transaction_id": "txn_abc123def456",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    body.update(overrides)
    return body


# --- Requirement 1: health check -------------------------------------------


def test_health_check(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HEALTHY"
    time.strptime(body["current_time"], "%Y-%m-%dT%H:%M:%S IST")


# --- Requirement 2: webhook accepts fast with 202 --------------------------


def test_webhook_returns_202(client):
    response = client.post("/v1/webhooks/transactions", json=payload())
    assert response.status_code == 202
    assert response.json() == {
        "status": "ACCEPTED",
        "transaction_id": "txn_abc123def456",
        "duplicate": False,
    }


def test_webhook_creates_processing_row_immediately(client, monkeypatch):
    """Before the 30s job runs, the transaction is visible as PROCESSING."""

    async def never_runs(_transaction_id):  # schedule without completing
        return None

    monkeypatch.setattr(main_module, "process_transaction", never_runs)

    client.post("/v1/webhooks/transactions", json=payload())
    body = client.get("/v1/transactions/txn_abc123def456").json()
    assert body[0]["status"] == "PROCESSING"
    assert body[0]["processed_at"] is None


def test_query_returns_404_for_unknown_transaction(client):
    response = client.get("/v1/transactions/txn_does_not_exist")
    assert response.status_code == 404


# --- Requirement 3: background processing + storage ------------------------


def test_transaction_is_processed_after_delay(client):
    client.post(
        "/v1/webhooks/transactions", json=payload(amount=150.50, currency="USD")
    )
    body = client.get("/v1/transactions/txn_abc123def456").json()

    assert isinstance(body, list) and len(body) == 1
    transaction = body[0]
    assert transaction["status"] == "PROCESSED"
    assert transaction["processed_at"] is not None
    assert transaction["processed_at"] >= transaction["created_at"]
    assert transaction["amount"] == 150.50
    assert transaction["currency"] == "USD"
    assert set(transaction) == {
        "transaction_id",
        "source_account",
        "destination_account",
        "amount",
        "currency",
        "status",
        "created_at",
        "processed_at",
    }


def test_result_is_persisted_across_app_restart(client):
    client.post("/v1/webhooks/transactions", json=payload())

    with TestClient(app) as restarted:  # new lifespan, same database
        body = restarted.get("/v1/transactions/txn_abc123def456").json()
    assert body[0]["status"] == "PROCESSED"
    assert body[0]["processed_at"] is not None


# --- Requirement 4: idempotency -------------------------------------------


def test_duplicate_webhooks_are_acked_but_processed_once(client, monkeypatch):
    calls = []
    real_process = main_module.process_transaction

    async def spy(transaction_id):
        calls.append(transaction_id)
        await real_process(transaction_id)

    monkeypatch.setattr(main_module, "process_transaction", spy)

    responses = [
        client.post("/v1/webhooks/transactions", json=payload()) for _ in range(5)
    ]

    assert [r.status_code for r in responses] == [202] * 5
    assert [r.json()["duplicate"] for r in responses] == [False, True, True, True, True]
    assert calls == ["txn_abc123def456"]  # exactly one processing job scheduled

    body = client.get("/v1/transactions/txn_abc123def456").json()
    assert len(body) == 1
    assert body[0]["status"] == "PROCESSED"


def test_duplicate_after_completion_does_not_alter_result(client):
    client.post("/v1/webhooks/transactions", json=payload())
    first = client.get("/v1/transactions/txn_abc123def456").json()[0]

    response = client.post("/v1/webhooks/transactions", json=payload())
    second = client.get("/v1/transactions/txn_abc123def456").json()[0]

    assert response.json()["duplicate"] is True
    assert second == first


# --- Requirement 6: reliability ------------------------------------------


def test_interrupted_transactions_are_recovered_on_startup():
    """A row left in PROCESSING by a crash is finished after restart."""
    now_ist = istnow()
    with SessionLocal() as session:
        session.add(
            Transaction(
                transaction_id="txn_orphaned",
                source_account="acc_user_789",
                destination_account="acc_merchant_456",
                amount=10,
                currency="INR",
                status="PROCESSING",
                created_at=now_ist,
                processed_at=None,
            )
        )
        session.commit()

    with TestClient(app) as restarted:
        deadline = time.time() + 10
        while time.time() < deadline:
            body = restarted.get("/v1/transactions/txn_orphaned").json()
            if body[0]["status"] == "PROCESSED":
                break
            time.sleep(0.05)

    assert body[0]["status"] == "PROCESSED"
    assert body[0]["processed_at"] is not None


@pytest.mark.parametrize(
    "bad_payload,reason",
    [
        ({"transaction_id": ""}, "blank transaction_id"),
        ({"amount": 0}, "zero amount"),
        ({"amount": -5}, "negative amount"),
        ({"currency": "INRUPEE"}, "currency not 3 chars"),
        ({"source_account": "   "}, "blank source_account"),
    ],
)
def test_invalid_payloads_are_rejected(client, bad_payload, reason):
    response = client.post("/v1/webhooks/transactions", json=payload(**bad_payload))
    assert response.status_code == 422, reason


def test_currency_is_normalized_to_uppercase(client):
    client.post("/v1/webhooks/transactions", json=payload(currency="usd"))
    body = client.get("/v1/transactions/txn_abc123def456").json()
    assert body[0]["currency"] == "USD"
