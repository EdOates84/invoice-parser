from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import database as db
from app.config import settings
from app.extractor import extract_with_fallback
from app.models import (
    CompareResponse,
    ConfigResponse,
    ConfigUpdate,
    InvoiceResponse,
    SubmitRequest,
    SubmitResponse,
    TestRequest,
    TestResult,
)
from app.processor import run as run_processor
from app.validator import report_to_dict, run_checks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    healed = db.startup_recovery()
    if healed:
        logger.warning("startup_recovery: healed %d stuck rows → FAILED", healed)
    # Load persisted config from DB, override env-based defaults
    saved = db.get_app_config()
    if saved:
        for key, val in saved.items():
            if hasattr(settings, key):
                setattr(settings, key, val)
        logger.info("startup: loaded config from DB (%d keys)", len(saved))
    yield


app = FastAPI(
    title="FynOS Invoice Parsing Service",
    description="Multi-tier invoice extraction with arithmetic validation.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def serve_ui() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Config endpoints ──────────────────────────────────────────────────────────

@app.get("/config", response_model=ConfigResponse, tags=["Config"])
def get_config() -> ConfigResponse:
    """Return current runtime configuration."""
    return ConfigResponse(
        llm_models=settings.llm_models,
        llm_timeout=settings.llm_timeout,
        enable_invoice2data=settings.enable_invoice2data,
        enable_spacy=settings.enable_spacy,
        enable_bert_ner=settings.enable_bert_ner,
        max_attempts=settings.max_attempts,
        tolerance=settings.tolerance,
        fault_mode=settings.fault_mode,
        fault_slow_delay=settings.fault_slow_delay,
    )


@app.put("/config", response_model=ConfigResponse, tags=["Config"])
def update_config(update: ConfigUpdate) -> ConfigResponse:
    """Update runtime config in-memory and persist to DB (survives restart)."""
    if update.llm_models is not None:
        settings.llm_models = update.llm_models
    if update.llm_timeout is not None:
        settings.llm_timeout = update.llm_timeout
    if update.enable_invoice2data is not None:
        settings.enable_invoice2data = update.enable_invoice2data
    if update.enable_spacy is not None:
        settings.enable_spacy = update.enable_spacy
    if update.enable_bert_ner is not None:
        settings.enable_bert_ner = update.enable_bert_ner
    if update.max_attempts is not None:
        settings.max_attempts = update.max_attempts
    if update.tolerance is not None:
        settings.tolerance = update.tolerance
    if update.fault_mode is not None:
        settings.fault_mode = update.fault_mode
    if update.fault_slow_delay is not None:
        settings.fault_slow_delay = update.fault_slow_delay
    # Persist to DB so config survives process restarts
    db.save_app_config({
        "llm_models":          settings.llm_models,
        "llm_timeout":         settings.llm_timeout,
        "enable_invoice2data": settings.enable_invoice2data,
        "enable_spacy":        settings.enable_spacy,
        "enable_bert_ner":     settings.enable_bert_ner,
        "max_attempts":        settings.max_attempts,
        "tolerance":           settings.tolerance,
        "fault_mode":          settings.fault_mode,
        "fault_slow_delay":    settings.fault_slow_delay,
    })
    return get_config()


# ── Core invoice endpoints ────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


_TERMINAL_RESUBMIT = {"FAILED"}        # allow fresh request
_TERMINAL_DEDUPLICATE = {"COMPLETED", "NEEDS_REVIEW"}  # return existing


@app.post("/invoices", response_model=SubmitResponse, status_code=202, tags=["Invoices"])
async def submit_invoice(
    raw_request: Request,
    background_tasks: BackgroundTasks,
) -> SubmitResponse:
    """Submit invoice text for async extraction.

    Accepts two content types:
    - **application/json**: `{"document_text": "raw invoice text..."}`
    - **text/plain**: raw invoice text as body (no JSON wrapping needed)

    Deduplication (content-hash):
    - Non-terminal (PENDING/PROCESSING/VALIDATING) → return existing request_id
    - COMPLETED / NEEDS_REVIEW → return existing (idempotent)
    - FAILED → create new request (explicit retry path)
    """
    content_type = raw_request.headers.get("content-type", "application/json").lower()

    if "text/plain" in content_type:
        body_bytes = await raw_request.body()
        document_text = body_bytes.decode("utf-8", errors="replace")
    else:
        # Default: application/json → parse {"document_text": "..."}
        try:
            body = await raw_request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid JSON body. Send {\"document_text\": \"...\"} or use Content-Type: text/plain with raw text.")
        if isinstance(body, dict):
            document_text = body.get("document_text", "")
        else:
            raise HTTPException(status_code=422, detail="Body must be JSON object with 'document_text' key.")

    if not document_text.strip():
        raise HTTPException(status_code=422, detail="document_text must not be empty")

    content_hash = _content_hash(document_text)
    existing = db.get_by_hash(content_hash)

    if existing:
        status = existing["status"]
        if status not in _TERMINAL_RESUBMIT:
            # Return existing request_id — no new record created
            return SubmitResponse(
                request_id=existing["request_id"],
                status=status,
                duplicate=True,
            )

    request_id, created_new = db.insert_invoice(content_hash, document_text)
    if not created_new:
        # Lost a concurrent-insert race — return the winner's record as a duplicate
        return SubmitResponse(request_id=request_id, status="PENDING", duplicate=True)

    background_tasks.add_task(run_processor, request_id, settings)
    return SubmitResponse(request_id=request_id, status="PENDING", duplicate=False)


@app.get("/invoices", tags=["Invoices"])
def list_invoices(limit: int = 50) -> list:
    """List all submitted invoices (newest first)."""
    rows = db.list_invoices(limit)
    return [
        {
            "request_id": r["request_id"],
            "status": r["status"],
            "attempt_count": r["attempt_count"],
            "extraction_method": r["extraction_method"],
            "extraction_model": r["extraction_model"],
            "error": r["error"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


_TERMINAL = {"COMPLETED", "NEEDS_REVIEW", "FAILED", "DUPLICATE"}
_SSE_POLL  = 0.15  # seconds between DB polls — fast enough to catch brief VALIDATING state
_SSE_MAX   = 300   # max seconds before stream closes regardless


@app.get("/invoices/{request_id}/stream", tags=["Invoices"])
async def stream_invoice_status(request_id: str) -> StreamingResponse:
    """SSE stream — emits a JSON event on every status change until terminal."""

    async def _generate():
        last_status = None
        elapsed = 0.0
        while elapsed < _SSE_MAX:
            row = db.get_by_id(request_id)
            if row is None:
                yield f"data: {json.dumps({'error': 'not_found'})}\n\n"
                return
            status = row["status"]
            if status != last_status:
                payload = json.dumps({
                    "status":            status,
                    "attempt_count":     row["attempt_count"],
                    "extraction_method": row["extraction_method"],
                    "extraction_model":  row["extraction_model"],
                })
                yield f"data: {payload}\n\n"
                last_status = status
            if status in _TERMINAL:
                return
            await asyncio.sleep(_SSE_POLL)
            elapsed += _SSE_POLL
        yield f"data: {json.dumps({'status': 'timeout'})}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/invoices/{request_id}", response_model=InvoiceResponse, tags=["Invoices"])
def get_invoice(request_id: str) -> InvoiceResponse:
    """Retrieve processing state and result for a submitted invoice."""
    row = db.get_by_id(request_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"request_id '{request_id}' not found")

    return InvoiceResponse(
        request_id=row["request_id"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        extraction_method=row["extraction_method"],
        extraction_model=row["extraction_model"],
        result=json.loads(row["result"]) if row["result"] else None,
        validation_checks=json.loads(row["validation_checks"]) if row["validation_checks"] else None,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.delete("/invoices/{request_id}", status_code=204, tags=["Invoices"])
def delete_invoice(request_id: str) -> None:
    """Permanently delete an invoice record."""
    if not db.delete_invoice(request_id):
        raise HTTPException(status_code=404, detail=f"request_id '{request_id}' not found")


# ── Test / compare endpoints (UI support) ────────────────────────────────────

async def _parse_document_text(raw_request: Request) -> str:
    """Extract document_text from either JSON body or plain text body."""
    content_type = raw_request.headers.get("content-type", "application/json").lower()
    if "text/plain" in content_type:
        body_bytes = await raw_request.body()
        return body_bytes.decode("utf-8", errors="replace")
    try:
        body = await raw_request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON. Send {\"document_text\": \"...\"} or Content-Type: text/plain.")
    if not isinstance(body, dict) or "document_text" not in body:
        raise HTTPException(status_code=422, detail="JSON body must contain 'document_text' key.")
    return body["document_text"]


@app.post("/invoices/test", response_model=TestResult, tags=["Testing"])
async def test_extraction(raw_request: Request) -> TestResult:
    """Synchronous extraction for UI testing — runs full pipeline, returns immediately.

    Accepts **application/json** `{"document_text": "..."}` or **text/plain** raw body.
    """
    document_text = await _parse_document_text(raw_request)
    if not document_text.strip():
        raise HTTPException(status_code=422, detail="document_text must not be empty")

    t0 = time.perf_counter()
    try:
        extraction, method, model = await extract_with_fallback(
            document_text, None, settings
        )
        if extraction is None:
            return TestResult(
                extraction_method="none",
                extraction_model=None,
                result=None,
                validation_checks=None,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error="All extraction tiers failed to produce a result.",
            )
        report = run_checks(extraction, tolerance=settings.tolerance)
        return TestResult(
            extraction_method=method,
            extraction_model=model,
            result=extraction.model_dump(),
            validation_checks=report_to_dict(report),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
    except Exception as exc:
        return TestResult(
            extraction_method="error",
            extraction_model=None,
            result=None,
            validation_checks=None,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            error=str(exc),
        )


async def _run_single_tier(
    document_text: str,
    tier_name: str,
    tier_fn,
    model: Optional[str] = None,
) -> TestResult:
    t0 = time.perf_counter()
    try:
        extraction = tier_fn()
        if extraction is None:
            return TestResult(
                extraction_method=tier_name,
                extraction_model=model,
                result=None,
                validation_checks=None,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error="No result",
            )
        report = run_checks(extraction, tolerance=settings.tolerance)
        return TestResult(
            extraction_method=tier_name,
            extraction_model=model,
            result=extraction.model_dump(),
            validation_checks=report_to_dict(report),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
    except Exception as exc:
        return TestResult(
            extraction_method=tier_name,
            extraction_model=model,
            result=None,
            validation_checks=None,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            error=str(exc),
        )


@app.post("/invoices/compare", response_model=CompareResponse, tags=["Testing"])
async def compare_tiers(raw_request: Request) -> CompareResponse:
    """Run all configured tiers in parallel and return side-by-side results.

    Accepts **application/json** `{"document_text": "..."}` or **text/plain** raw body.
    """
    from app.extractor import (
        extract_bert_ner,
        extract_heuristic,
        extract_invoice2data,
        extract_llm,
        extract_spacy,
    )

    text = await _parse_document_text(raw_request)
    if not text.strip():
        raise HTTPException(status_code=422, detail="document_text must not be empty")
    tasks = []

    # Tier 1: each configured LLM
    for model in settings.llm_models:
        tasks.append(
            _run_single_tier(
                text,
                tier_name="llm",
                tier_fn=lambda m=model: extract_llm(text, None, m, settings.llm_timeout),
                model=model,
            )
        )

    # Tier 2a: invoice2data
    if settings.enable_invoice2data:
        tasks.append(
            _run_single_tier(text, "invoice2data", lambda: extract_invoice2data(text))
        )

    # Tier 2b: spaCy
    if settings.enable_spacy:
        tasks.append(
            _run_single_tier(text, "spacy", lambda: extract_spacy(text))
        )

    # Tier 2c: BERT NER
    if settings.enable_bert_ner:
        tasks.append(
            _run_single_tier(text, "bert_ner", lambda: extract_bert_ner(text))
        )

    # Tier 3: heuristic (always)
    tasks.append(
        _run_single_tier(text, "heuristic", lambda: extract_heuristic(text))
    )

    results = await asyncio.gather(*tasks)
    return CompareResponse(results=list(results))
