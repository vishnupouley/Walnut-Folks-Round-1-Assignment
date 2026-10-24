"""Webhook receiver: acknowledge fast, process in the background, idempotently."""

import asyncio
import logging
from contextlib import asynccontextmanager

from datetime import timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Transaction, get_session, init_db, utcnow
from .processing import interrupted_transaction_ids, process_transaction
from .schemas import (
    HealthResponse,
    TransactionCreate,
    TransactionResponse,
    WebhookAck,
)

logger = logging.getLogger(__name__)

# IST is UTC+05:30 (fixed offset, does not require external tzdata on Windows)
IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    recovery_tasks: set[asyncio.Task] = set()
    for transaction_id in interrupted_transaction_ids():
        task = asyncio.create_task(process_transaction(transaction_id))
        recovery_tasks.add(task)
        task.add_done_callback(recovery_tasks.discard)
    yield


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)


@app.get("/", response_model=HealthResponse)
def health() -> HealthResponse:
    now_utc = utcnow()
    now_ist = now_utc.astimezone(IST)
    return HealthResponse(
        status="HEALTHY",
        current_time=now_ist.strftime("%Y-%m-%dT%H:%M:%S IST"),
    )


@app.post(
    "/v1/webhooks/transactions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAck,
)
def receive_webhook(
    payload: TransactionCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> WebhookAck:
    transaction = Transaction(
        transaction_id=payload.transaction_id,
        source_account=payload.source_account,
        destination_account=payload.destination_account,
        amount=payload.amount,
        currency=payload.currency,
        status="PROCESSING",
        created_at=utcnow(),
        processed_at=None,
    )
    session.add(transaction)
    try:
        session.commit()
    except IntegrityError:
        # Duplicate transaction_id: the unique constraint decided the race, so
        # exactly one webhook ever schedules processing. Ack it and move on.
        session.rollback()
        logger.info("duplicate webhook ignored for %s", payload.transaction_id)
        return WebhookAck(transaction_id=payload.transaction_id, duplicate=True)

    background_tasks.add_task(process_transaction, transaction.transaction_id)
    return WebhookAck(transaction_id=payload.transaction_id, duplicate=False)


@app.get("/v1/transactions/{transaction_id}", response_model=list[TransactionResponse])
def get_transaction(transaction_id: str, session: Session = Depends(get_session)):
    transaction = session.scalar(
        select(Transaction).where(Transaction.transaction_id == transaction_id)
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return [transaction]
