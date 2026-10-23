"""Test configuration: isolated temp DB, short processing delay.

Set DATABASE_URL (e.g. a Postgres URL) to run the same suite against another
engine; otherwise a throwaway SQLite file is used.
"""

import os
import tempfile
import uuid
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{Path(tempfile.gettempdir()) / f'webhook_test_{uuid.uuid4().hex}.db'}",
)
os.environ["PROCESSING_DELAY_SECONDS"] = "0.05"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    # `with` runs the lifespan (schema creation + interrupted-work recovery).
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session():
    with SessionLocal() as db_session:
        yield db_session
