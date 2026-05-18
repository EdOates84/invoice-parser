"""Integration tests for the FastAPI endpoints.

Uses httpx.AsyncClient against the ASGI app — no real LLM calls.
LLM calls are expected to fail (no key in test env) and fall through
to heuristic extraction, which is always available.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

INVOICE_TEXT = """
INVOICE No: TEST-001
Date: 2024-01-15
Currency: USD

Widget A  x2  @ $50.00 = $100.00
Subtotal: $100.00
Tax: $10.00
Total: $110.00
"""

INVOICE_TEXT_2 = """
INVOICE No: TEST-002
Date: 2024-02-20
Total: $999.00
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── POST /invoices ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_submit_returns_pending(client):
    resp = await client.post("/invoices", json={"document_text": INVOICE_TEXT})
    assert resp.status_code == 202
    data = resp.json()
    assert "request_id" in data
    assert data["status"] == "PENDING"
    assert data["duplicate"] is False


@pytest.mark.anyio
async def test_submit_empty_text_rejected(client):
    resp = await client.post("/invoices", json={"document_text": "   "})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_submit_duplicate_returns_existing(client):
    # First submission
    r1 = await client.post("/invoices", json={"document_text": INVOICE_TEXT_2})
    assert r1.status_code == 202
    first_id = r1.json()["request_id"]

    # Second submission with same text → should return same request_id
    r2 = await client.post("/invoices", json={"document_text": INVOICE_TEXT_2})
    assert r2.status_code == 202
    data2 = r2.json()
    assert data2["request_id"] == first_id
    assert data2["duplicate"] is True


# ── GET /invoices/{request_id} ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_get_not_found(client):
    resp = await client.get("/invoices/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_get_returns_submitted_invoice(client):
    text = "INVOICE No: GET-TEST-001\nTotal: $42.00"
    r = await client.post("/invoices", json={"document_text": text})
    request_id = r.json()["request_id"]

    resp = await client.get(f"/invoices/{request_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["request_id"] == request_id
    assert data["status"] in {"PENDING", "PROCESSING", "VALIDATING", "COMPLETED", "NEEDS_REVIEW", "FAILED"}


# ── GET /config + PUT /config ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_get_config(client):
    resp = await client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_models" in data
    assert "tolerance" in data
    assert isinstance(data["llm_models"], list)


@pytest.mark.anyio
async def test_put_config_updates_tolerance(client):
    resp = await client.put("/config", json={"tolerance": 0.05})
    assert resp.status_code == 200
    assert resp.json()["tolerance"] == pytest.approx(0.05)

    # Verify GET reflects change
    resp2 = await client.get("/config")
    assert resp2.json()["tolerance"] == pytest.approx(0.05)

    # Restore
    await client.put("/config", json={"tolerance": 0.01})


@pytest.mark.anyio
async def test_put_config_updates_models(client):
    new_models = ["ollama/llama3.2:8b"]
    resp = await client.put("/config", json={"llm_models": new_models})
    assert resp.status_code == 200
    assert resp.json()["llm_models"] == new_models


# ── POST /invoices/test ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_test_endpoint_returns_result(client):
    text = "Invoice No: T-100\nTotal Due: $250.00"
    resp = await client.post("/invoices/test", json={"document_text": text})
    assert resp.status_code == 200
    data = resp.json()
    assert "extraction_method" in data
    assert "latency_ms" in data
    # Should at minimum hit heuristic extractor
    assert data["extraction_method"] in {
        "llm", "invoice2data", "spacy", "bert_ner", "heuristic", "none", "error"
    }


@pytest.mark.anyio
async def test_test_endpoint_rejects_empty(client):
    resp = await client.post("/invoices/test", json={"document_text": ""})
    assert resp.status_code == 422


# ── POST /invoices/compare ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_compare_returns_multiple_results(client):
    text = "Invoice No: C-001\nTotal: $100.00"
    resp = await client.post("/invoices/compare", json={"document_text": text})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert isinstance(data["results"], list)
    assert len(data["results"]) >= 1  # at minimum heuristic always runs


# ── UI ────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_ui_served(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "FynOS" in resp.text


# ══════════════════════════════════════════════════════════════════════════════
# Assignment Scenarios (spec §"Scenarios you must handle")
# ══════════════════════════════════════════════════════════════════════════════

INVOICE_D4471 = """INVOICE
Invoice No: D-4471
Date: 2025-11-15
Currency: USD
Vendor: Test Corp
Server License    1    5000.00    5000.00
Support Plan      1    1200.00    1200.00
Subtotal:  $6200.00
Tax (8%):   $496.00
Total Due: $6696.00"""


@pytest.mark.anyio
async def test_scenario1_same_invoice_submitted_twice(client):
    """Scenario 1: same invoice submitted twice → second call returns existing request_id.

    Expected: second POST returns same request_id + duplicate=True.
    Reasoning: double-processing an invoice in accounting is worse than returning a
    cached result. The system deduplicates via SHA-256 content hash.
    """
    r1 = await client.post("/invoices",
        content=INVOICE_D4471.encode(), headers={"content-type": "text/plain"})
    assert r1.status_code == 202
    rid1 = r1.json()["request_id"]
    dup1 = r1.json()["duplicate"]

    r2 = await client.post("/invoices",
        content=INVOICE_D4471.encode(), headers={"content-type": "text/plain"})
    assert r2.status_code == 202
    data2 = r2.json()

    # Must return SAME request_id and flag as duplicate
    assert data2["request_id"] == rid1, "Second submission must return existing request_id"
    assert data2["duplicate"] is True, "Second submission must set duplicate=True"


@pytest.mark.anyio
async def test_scenario2_client_retry_after_timeout(client):
    """Scenario 2: client retries same submission (never saw first 202 response).

    Expected: retry returns same request_id transparently via content-hash dedup
    when the first request is still in a non-FAILED terminal state.

    Reasoning: client that timed out has no ID to re-send — hash-based dedup
    works without client coordination. We verify at the dedup layer by directly
    inserting a PENDING row and confirming the second POST returns it.
    """
    import hashlib
    from app import database as db

    text = "Invoice No: TIMEOUT-SCENARIO-002\nDate: 2025-01-01\nWidget A  1  $99.00  $99.00\nSubtotal: $99.00\nTax: $9.90\nTotal: $108.90"
    content_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()

    # Manually insert a PENDING row (simulates: first request received but
    # client timed out before seeing the 202 response)
    result = db.insert_invoice(content_hash, text)
    rid1 = result[0] if isinstance(result, tuple) else result

    # Client resends identical request after timeout (never got first response)
    r2 = await client.post("/invoices",
        content=text.encode(), headers={"content-type": "text/plain"})
    assert r2.status_code == 202
    data2 = r2.json()

    # Must return the SAME request_id — dedup found in-flight PENDING row
    assert data2["request_id"] == rid1, \
        "Retry after timeout must return the existing in-flight request_id"
    assert data2["duplicate"] is True, "Must flag as duplicate"

    # Client can now poll GET with the returned request_id
    r3 = await client.get(f"/invoices/{rid1}")
    assert r3.status_code == 200
    assert r3.json()["request_id"] == rid1


@pytest.mark.anyio
async def test_scenario3_d4471_fails_then_resubmit(client):
    """Scenario 3: extractor fails on D-4471, client immediately resubmits.

    Expected:
    - First submission with all LLMs disabled → falls through to heuristic → processes
    - If processing fails → FAILED (terminal)
    - Resubmit same text when FAILED → creates NEW request_id (explicit retry path)
    - Old record (BBB) preserved as audit trail of failure
    - New record (CCC) processes independently

    Reasoning: FAILED = system error. Resubmission is explicit retry. Creating new
    request_id preserves the failure evidence and gives clean audit trail.
    """
    # Disable all LLMs so extraction falls to heuristic (simulating degraded environment)
    await client.put("/config", json={"llm_models": []})

    # First submission
    r1 = await client.post("/invoices",
        content=INVOICE_D4471.encode(), headers={"content-type": "text/plain"})
    assert r1.status_code == 202
    rid1 = r1.json()["request_id"]
    assert r1.json()["duplicate"] is False

    # Poll until terminal
    import asyncio
    for _ in range(10):
        await asyncio.sleep(0.5)
        status_r = await client.get(f"/invoices/{rid1}")
        if status_r.json()["status"] in ("COMPLETED", "FAILED", "NEEDS_REVIEW"):
            break

    first_status = status_r.json()["status"]
    # Heuristic may succeed (COMPLETED) or partially fail — either is fine
    # What matters: if FAILED, resubmit creates new record

    if first_status == "FAILED":
        # Client resubmits — must get NEW request_id
        r2 = await client.post("/invoices",
            content=INVOICE_D4471.encode(), headers={"content-type": "text/plain"})
        assert r2.status_code == 202
        rid2 = r2.json()["request_id"]
        assert rid2 != rid1, "FAILED resubmit must create new request_id"
        assert r2.json()["duplicate"] is False

        # Old record preserved
        old = await client.get(f"/invoices/{rid1}")
        assert old.json()["status"] == "FAILED"
    else:
        # First attempt succeeded (heuristic worked) — dedup returns same ID
        r2 = await client.post("/invoices",
            content=INVOICE_D4471.encode(), headers={"content-type": "text/plain"})
        assert r2.json()["request_id"] == rid1
        assert r2.json()["duplicate"] is True

    # Restore config
    await client.put("/config", json={"llm_models": []})


@pytest.mark.anyio
async def test_all_7_required_fields_always_present(client):
    """Spec: result must always contain all 7 required fields (null if not found)."""
    text = "Invoice No: FIELD-TEST-001\nTotal: $100.00"
    r = await client.post("/invoices/test",
        content=text.encode(), headers={"content-type": "text/plain"})
    assert r.status_code == 200
    result = r.json().get("result")
    assert result is not None
    for field in ["invoice_number", "invoice_date", "currency",
                  "subtotal", "tax_total", "total", "line_items"]:
        assert field in result, f"Required field '{field}' missing from result"
    assert isinstance(result["line_items"], list)


@pytest.mark.anyio
async def test_state_machine_transitions_are_valid(client):
    """Verify only legal state transitions occur during processing."""
    # Spec vocabulary: PENDING, VALIDATING, NEEDS_REVIEW, COMPLETED, FAILED
    # No PROCESSING state — VALIDATING covers both extraction and arithmetic checks
    TERMINAL = {"COMPLETED", "FAILED", "NEEDS_REVIEW"}
    VALID_TRANSITIONS = {
        "PENDING":    {"VALIDATING", "FAILED"},
        "VALIDATING": {"COMPLETED", "VALIDATING", "NEEDS_REVIEW", "FAILED"},
    }
    text = "Invoice No: SM-001\nDate: 2025-01-01\nItem A  2  $50  $100\nTotal: $100.00"
    r = await client.post("/invoices",
        content=text.encode(), headers={"content-type": "text/plain"})
    rid = r.json()["request_id"]

    import asyncio
    prev_status = "PENDING"
    for _ in range(15):
        await asyncio.sleep(0.3)
        inv = (await client.get(f"/invoices/{rid}")).json()
        curr = inv["status"]
        if prev_status not in TERMINAL:
            allowed = VALID_TRANSITIONS.get(prev_status, set()) | TERMINAL
            assert curr in allowed, f"Illegal transition {prev_status} → {curr}"
        prev_status = curr
        if curr in TERMINAL:
            break

    assert prev_status in TERMINAL, "Processing must reach a terminal state"


@pytest.mark.anyio
async def test_process_restart_recovery(client):
    """Spec: state must survive a process restart (startup_recovery heals stuck rows)."""
    from app import database as db
    from app.config import settings

    # Manually insert a stuck PROCESSING row (simulates crash mid-extraction)
    import sqlite3, datetime
    old_time = (datetime.datetime.utcnow() - datetime.timedelta(seconds=120)).isoformat()
    with sqlite3.connect(settings.database_url) as con:
        con.execute("""
            INSERT INTO invoices (request_id, content_hash, document_text, status,
                                  attempt_count, created_at, updated_at)
            VALUES ('stuck-row-001', 'deadbeef', 'test', 'VALIDATING', 1, ?, ?)
        """, (old_time, old_time))

    # Run startup recovery (normally runs on app startup)
    healed = db.startup_recovery()
    assert healed >= 1, "startup_recovery must heal stuck rows"

    # Stuck row now FAILED
    row = db.get_by_id("stuck-row-001")
    assert row["status"] == "FAILED"
    assert "restarted" in row["error"].lower()
