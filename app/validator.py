from __future__ import annotations

from typing import List, Optional

from app.models import CheckResult, InvoiceExtraction, ValidationReport


def _check(
    name: str,
    expected: Optional[float],
    actual: Optional[float],
    tolerance: float,
    message_template: str,
) -> CheckResult:
    if expected is None or actual is None:
        return CheckResult(
            name=name,
            passed=True,  # skip — not an error
            expected=expected,
            actual=actual,
            delta=None,
            message=f"{name}: skipped (null value)",
        )
    delta = abs(expected - actual)
    passed = delta <= tolerance
    return CheckResult(
        name=name,
        passed=passed,
        expected=round(expected, 4),
        actual=round(actual, 4),
        delta=round(delta, 4),
        message=message_template.format(
            expected=expected, actual=actual, delta=delta
        ) if not passed else f"{name}: OK",
    )


def run_checks(extraction: InvoiceExtraction, tolerance: float = 0.01) -> ValidationReport:
    """Run arithmetic checks.

    CRITICAL checks (block COMPLETED):
      1. qty × unit_price ≈ amount  per extracted line item
      2. subtotal + tax_total ≈ total  (document-level consistency)

    ADVISORY checks (shown but never block COMPLETED):
      3. Σ line_item.amount ≈ subtotal  — skipped when sum < 90% of subtotal
         (partial extraction is normal for long invoices; advisory only)
    """
    critical: List[CheckResult] = []
    advisory: List[CheckResult] = []

    # ── Critical 1: per line item qty × unit_price × (1 - discount%) ≈ amount ──
    for i, item in enumerate(extraction.line_items):
        if item.quantity is not None and item.unit_price is not None and item.amount is not None:
            base = round(item.quantity * item.unit_price, 4)
            if item.discount_percent is not None:
                expected = round(base * (1 - item.discount_percent / 100), 4)
                formula = f"qty×unit_price×(1-{item.discount_percent}%)"
            else:
                expected = base
                formula = "qty×unit_price"
            desc = item.description or f"item_{i}"
            critical.append(
                _check(
                    name=f"line_item_{i}",
                    expected=expected,
                    actual=item.amount,
                    tolerance=tolerance,
                    message_template=(
                        f'Line item {i} ("{desc}"): '
                        f"{formula}={expected:.4f}, "
                        f"reported amount={item.amount} "
                        f"(Δ={{delta:.4f}})"
                    ),
                )
            )

    # ── Critical 2: subtotal - discount + shipping + tax_total ≈ total ──────────
    if (
        extraction.subtotal is not None
        and extraction.tax_total is not None
        and extraction.total is not None
    ):
        discount_val = extraction.discount or 0.0
        shipping_val = extraction.shipping or 0.0
        computed_total = round(
            extraction.subtotal - discount_val + shipping_val + extraction.tax_total, 4
        )
        parts = [f"subtotal({extraction.subtotal})"]
        if discount_val:
            parts.append(f"-discount({discount_val})")
        if shipping_val:
            parts.append(f"+shipping({shipping_val})")
        parts.append(f"+tax({extraction.tax_total})")
        formula = "".join(parts)
        critical.append(
            _check(
                name="total",
                expected=computed_total,
                actual=extraction.total,
                tolerance=tolerance,
                message_template=(
                    f"Total: {formula}={{expected:.4f}}, "
                    "reported total={actual:.4f} (Δ={delta:.4f})"
                ),
            )
        )

    # ── Advisory: Σ line_item.amount ≈ subtotal ───────────────────────────────
    # Only run when extracted lines cover ≥90% of reported subtotal.
    # If sum < 90% of subtotal → partial extraction likely; note but don't fail.
    items_with_amount = [it.amount for it in extraction.line_items if it.amount is not None]
    if items_with_amount and extraction.subtotal is not None:
        computed_subtotal = round(sum(items_with_amount), 4)
        partial = (extraction.subtotal > 0 and
                   computed_subtotal < extraction.subtotal * 0.90)
        if partial:
            critical.append(CheckResult(
                name="subtotal",
                passed=False,
                expected=extraction.subtotal,
                actual=computed_subtotal,
                delta=round(abs(computed_subtotal - extraction.subtotal), 4),
                message=(
                    f"subtotal: incomplete extraction — "
                    f"sum of {len(items_with_amount)} extracted lines={computed_subtotal:.2f} "
                    f"covers less than 90% of reported subtotal={extraction.subtotal:.2f}. "
                    f"Re-extract all line items from the invoice."
                ),
            ))
        else:
            advisory.append(
                _check(
                    name="subtotal",
                    expected=computed_subtotal,
                    actual=extraction.subtotal,
                    tolerance=tolerance,
                    message_template=(
                        "Subtotal: sum of line item amounts={expected:.4f}, "
                        "reported subtotal={actual:.4f} (Δ={delta:.4f})"
                    ),
                )
            )

    all_checks = critical + advisory
    # all_passed driven by CRITICAL checks only
    all_passed = all(c.passed for c in critical)
    # failure_summary for LLM retry: only critical failures
    critical_failed = [c for c in critical if not c.passed]
    failure_summary = _build_failure_summary(critical_failed) if critical_failed else ""

    return ValidationReport(
        all_passed=all_passed,
        checks=all_checks,
        failure_summary=failure_summary,
    )


def _build_failure_summary(failed_checks: List[CheckResult]) -> str:
    """Build human-readable failure text injected into retry LLM prompt."""
    lines = [
        "Arithmetic errors found in your previous extraction.",
        "Re-examine the source text and correct these values:\n",
    ]
    for c in failed_checks:
        lines.append(f"- {c.message}")
    lines.append("\nReturn corrected JSON only. Do not add explanation.")
    return "\n".join(lines)


def report_to_dict(report: ValidationReport) -> dict:
    return {
        "all_passed": report.all_passed,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "expected": c.expected,
                "actual": c.actual,
                "delta": c.delta,
                "message": c.message,
            }
            for c in report.checks
        ],
    }
