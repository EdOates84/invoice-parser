from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional, Tuple
from uuid import uuid4

from app.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(settings.database_url, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS invoices (
    request_id        TEXT PRIMARY KEY,
    content_hash      TEXT NOT NULL,
    document_text     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT,
    extraction_model  TEXT,
    result            TEXT,
    validation_checks TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Partial unique index: one active row per content_hash at a time.
-- FAILED rows are excluded so a resubmit after failure creates a fresh record.
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash_active
    ON invoices(content_hash) WHERE status NOT IN ('FAILED');

CREATE TABLE IF NOT EXISTS app_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def init_db() -> None:
    with _conn() as con:
        con.executescript(DDL)


# ── Startup recovery ──────────────────────────────────────────────────────────

def startup_recovery() -> int:
    """Move stuck VALIDATING rows (>60s old) to FAILED on restart.

    Returns count of rows healed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE invoices
            SET status     = 'FAILED',
                error      = 'Process interrupted — service restarted mid-extraction',
                updated_at = ?
            WHERE status = 'VALIDATING'
              AND updated_at < ?
            """,
            (_now(), cutoff),
        )
        return cur.rowcount


# ── CRUD ──────────────────────────────────────────────────────────────────────

def insert_invoice(content_hash: str, document_text: str) -> Tuple[str, bool]:
    """Insert a new invoice row atomically.

    Returns (request_id, created_new):
      - created_new=True  → fresh row inserted; caller should start processing
      - created_new=False → lost a concurrent-insert race; returns the winner's
                            request_id so the caller can return it as a duplicate
    """
    request_id = str(uuid4())
    now = _now()
    with _conn() as con:
        try:
            con.execute(
                """
                INSERT INTO invoices
                  (request_id, content_hash, document_text, status, created_at, updated_at)
                VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (request_id, content_hash, document_text, now, now),
            )
            return request_id, True
        except sqlite3.IntegrityError:
            # Partial unique index conflict: a concurrent request inserted first.
            # Fetch that winner's row so the caller can return it as a duplicate.
            row = con.execute(
                """
                SELECT request_id, status FROM invoices
                WHERE content_hash = ? AND status NOT IN ('FAILED')
                ORDER BY created_at DESC LIMIT 1
                """,
                (content_hash,),
            ).fetchone()
            if row:
                return row["request_id"], False
            # Extremely unlikely: winner was immediately FAILED between insert and
            # this SELECT — fall through and let the caller retry via get_by_hash.
            return request_id, False


def get_by_id(request_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM invoices WHERE request_id = ?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def get_by_hash(content_hash: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM invoices WHERE content_hash = ? ORDER BY created_at DESC LIMIT 1",
            (content_hash,),
        ).fetchone()
    return dict(row) if row else None


def list_invoices(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM invoices ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_invoice(request_id: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM invoices WHERE request_id = ?", (request_id,))
    return cur.rowcount > 0


def update_status(
    request_id: str,
    status: str,
    *,
    attempt_count: Optional[int] = None,
    extraction_method: Optional[str] = None,
    extraction_model: Optional[str] = None,
    result: Optional[dict] = None,
    validation_checks: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    values: list = [status, _now()]

    if attempt_count is not None:
        fields.append("attempt_count = ?")
        values.append(attempt_count)
    if extraction_method is not None:
        fields.append("extraction_method = ?")
        values.append(extraction_method)
    if extraction_model is not None:
        fields.append("extraction_model = ?")
        values.append(extraction_model)
    if result is not None:
        fields.append("result = ?")
        values.append(json.dumps(result))
    if validation_checks is not None:
        fields.append("validation_checks = ?")
        values.append(json.dumps(validation_checks))
    if error is not None:
        fields.append("error = ?")
        values.append(error)

    values.append(request_id)

    with _conn() as con:
        con.execute(
            f"UPDATE invoices SET {', '.join(fields)} WHERE request_id = ?",
            values,
        )


# ── App config persistence ─────────────────────────────────────────────────────

def get_app_config() -> Optional[dict]:
    """Return persisted config dict, or None if not yet saved."""
    with _conn() as con:
        row = con.execute("SELECT config_json FROM app_config WHERE id = 1").fetchone()
    if row:
        return json.loads(row["config_json"])
    return None


def save_app_config(config: dict) -> None:
    """Upsert the config row (single row, id=1)."""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO app_config (id, config_json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json,
                                          updated_at  = excluded.updated_at
            """,
            (json.dumps(config), _now()),
        )
