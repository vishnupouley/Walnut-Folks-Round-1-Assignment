"""Background processing: 30s simulated work, then persist the final state."""

import asyncio
import logging
import os

from sqlalchemy import select

from .db import SessionLocal, Transaction, istnow

logger = logging.getLogger(__name__)

# 30s per the spec; overridable so the suite and manual checks do not have to
# wait half a minute per transaction.
DELAY_SECONDS = float(os.environ.get("PROCESSING_DELAY_SECONDS", "30"))


async def process_transaction(transaction_id: str) -> None:
    """Runs after the 202 is sent. Sleeps, then marks the row PROCESSED."""
    await asyncio.sleep(DELAY_SECONDS)
    try:
        # Sync DB driver: keep it off the event loop so other webhooks stay fast.
        await asyncio.to_thread(_mark_processed, transaction_id)
    except Exception:
        # Leave the row in PROCESSING; startup recovery re-queues it.
        logger.exception("processing failed for %s", transaction_id)


def _mark_processed(transaction_id: str) -> None:
    with SessionLocal() as session:
        transaction = session.scalar(
            select(Transaction).where(Transaction.transaction_id == transaction_id)
        )
        if transaction is None or transaction.status == "PROCESSED":
            return
        transaction.status = "PROCESSED"
        transaction.processed_at = istnow()
        session.commit()


def interrupted_transaction_ids() -> list[str]:
    """Transactions stuck in PROCESSING (e.g. the process restarted mid-flight)."""
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Transaction.transaction_id).where(
                    Transaction.status == "PROCESSING"
                )
            )
        )
