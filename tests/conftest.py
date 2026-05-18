"""Pytest configuration — initialise a fresh in-memory SQLite DB before each test."""
import pytest

from app import database as db
from app.config import settings


@pytest.fixture(autouse=True)
def init_test_db(tmp_path):
    """Point the database at a temp file so tests are isolated."""
    settings.database_url = str(tmp_path / "test.db")
    db.init_db()
    yield
    # cleanup handled by tmp_path fixture
