from __future__ import annotations

import logging
from typing import Optional

from app import database as db
from app.config import Settings
from app.extractor import extract_with_fallback
from app.validator import report_to_dict, run_checks

logger = logging.getLogger(__name__)

# ── Legal state transitions (matches spec vocabulary) ─────────────────────────
# PENDING → VALIDATING → COMPLETED    (terminal)
#                └──────► NEEDS_REVIEW (terminal, after max retries)
# PENDING → FAILED       (terminal, extraction error or critically empty)
# VALIDATING → FAILED    (terminal, unexpected error mid-attempt)
#
# VALIDATING covers both extraction (LLM/tier calls) AND arithmetic checks.
# No PROCESSING state — matches spec: PENDING, VALIDATING, NEEDS_REVIEW, COMPLETED, FAILED.


async def run(request_id: str, settings: Settings) -> None:
    """State-machine processor — runs as a FastAPI BackgroundTask.

    Retry loop:
      - Each attempt: PENDING → VALIDATING (extraction + arithmetic checks).
      - Failed validation injects structured failure_summary into next LLM prompt.
      - After MAX_ATTEMPTS with persistent failures → NEEDS_REVIEW.
      - Any unrecoverable error or critically empty result → FAILED.
    """
    attempt = 0
    previous_failures: Optional[str] = None

    while attempt < settings.max_attempts:
        attempt += 1

        # ── VALIDATING (extraction phase) ─────────────────────────────────────
        db.update_status(request_id, "VALIDATING", attempt_count=attempt)
        logger.info("request=%s attempt=%d status=VALIDATING (extracting)", request_id, attempt)

        try:
            extraction, method, model = await extract_with_fallback(
                _get_document_text(request_id),
                previous_failures,
                settings,
            )
        except Exception as exc:
            logger.exception("request=%s extraction raised unexpectedly", request_id)
            db.update_status(
                request_id,
                "FAILED",
                error=f"Unexpected extraction error: {exc}",
            )
            return

        if extraction is None:
            logger.warning("request=%s all tiers exhausted → FAILED", request_id)
            db.update_status(
                request_id,
                "FAILED",
                error="All extraction tiers failed to produce a result.",
                extraction_method=method,
            )
            return

        # ── Critically empty check ────────────────────────────────────────────
        no_amounts = extraction.total is None and extraction.subtotal is None
        no_lines = not extraction.line_items
        if no_amounts and no_lines:
            logger.warning(
                "request=%s attempt=%d critically empty extraction → FAILED", request_id, attempt
            )
            db.update_status(
                request_id,
                "FAILED",
                error="Extraction produced no financial data (total, subtotal, and line_items all missing).",
                extraction_method=method,
                extraction_model=model,
                result=extraction.model_dump(),
            )
            return

        # ── VALIDATING (arithmetic checks phase) ──────────────────────────────
        db.update_status(
            request_id,
            "VALIDATING",
            extraction_method=method,
            extraction_model=model,
            result=extraction.model_dump(),
        )
        logger.info(
            "request=%s attempt=%d status=VALIDATING (checking) method=%s model=%s",
            request_id, attempt, method, model,
        )

        report = run_checks(extraction, tolerance=settings.tolerance)
        checks_dict = report_to_dict(report)

        if report.all_passed:
            db.update_status(
                request_id,
                "COMPLETED",
                result=extraction.model_dump(),
                validation_checks=checks_dict,
            )
            logger.info("request=%s status=COMPLETED", request_id)
            return

        logger.warning(
            "request=%s attempt=%d validation failed: %s",
            request_id, attempt, report.failure_summary[:120],
        )
        previous_failures = report.failure_summary

        db.update_status(
            request_id,
            "VALIDATING",
            result=extraction.model_dump(),
            validation_checks=checks_dict,
        )

    # ── NEEDS_REVIEW (retry exhausted) ───────────────────────────────────────
    db.update_status(request_id, "NEEDS_REVIEW")
    logger.warning(
        "request=%s status=NEEDS_REVIEW after %d attempts", request_id, attempt
    )


def _get_document_text(request_id: str) -> str:
    row = db.get_by_id(request_id)
    if not row:
        raise RuntimeError(f"Invoice row not found for request_id={request_id}")
    return row["document_text"]
