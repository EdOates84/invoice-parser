"""Unit tests for the arithmetic validation engine."""
import pytest

from app.models import InvoiceExtraction, LineItem
from app.validator import run_checks


def make_invoice(**kwargs) -> InvoiceExtraction:
    return InvoiceExtraction(**kwargs)


# ── Check 1: line item qty × unit_price ≈ amount ─────────────────────────────

def test_line_item_pass():
    inv = make_invoice(line_items=[LineItem(description="Widget", quantity=2, unit_price=50.0, amount=100.0)])
    report = run_checks(inv)
    assert report.all_passed
    assert report.checks[0].passed


def test_line_item_fail():
    inv = make_invoice(line_items=[LineItem(description="Widget", quantity=2, unit_price=50.0, amount=99.0)])
    report = run_checks(inv)
    assert not report.all_passed
    assert not report.checks[0].passed
    assert report.checks[0].delta == pytest.approx(1.0, abs=1e-4)


def test_line_item_skipped_when_null():
    # amount is null → check skipped (not failed)
    inv = make_invoice(line_items=[LineItem(description="Service fee", quantity=None, unit_price=None, amount=50.0)])
    report = run_checks(inv)
    # Only subtotal check could fire if subtotal is set — no checks here
    assert report.all_passed


def test_line_item_tolerance_edge():
    # delta exactly at tolerance boundary → pass
    inv = make_invoice(line_items=[LineItem(quantity=3, unit_price=33.333, amount=99.999)])
    report = run_checks(inv, tolerance=0.01)
    assert report.all_passed


def test_multiple_line_items_one_fails():
    inv = make_invoice(line_items=[
        LineItem(description="A", quantity=1, unit_price=10.0, amount=10.0),
        LineItem(description="B", quantity=2, unit_price=20.0, amount=35.0),  # wrong
    ])
    report = run_checks(inv)
    assert not report.all_passed
    assert report.checks[0].passed
    assert not report.checks[1].passed


# ── Check 2: Σ amounts ≈ subtotal ────────────────────────────────────────────

def test_subtotal_pass():
    inv = make_invoice(
        subtotal=100.0,
        line_items=[LineItem(amount=60.0), LineItem(amount=40.0)],
    )
    report = run_checks(inv)
    assert report.all_passed


def test_subtotal_fail():
    inv = make_invoice(
        subtotal=105.0,
        line_items=[LineItem(amount=60.0), LineItem(amount=40.0)],
    )
    report = run_checks(inv)
    # subtotal is ADVISORY — does not block all_passed (only critical checks do)
    # sum(lines)=100, subtotal=105: delta=5, ratio=100/105=0.952 > 0.90 → not partial
    # → advisory check fires with passed=False, but all_passed still True
    assert report.all_passed  # advisory failure does not block completion
    sub_check = next(c for c in report.checks if c.name == "subtotal")
    assert not sub_check.passed          # advisory check correctly flags the delta
    assert sub_check.delta == pytest.approx(5.0, abs=1e-4)
    assert report.failure_summary == ""  # advisory failures excluded from retry prompt


def test_subtotal_skipped_no_line_items():
    inv = make_invoice(subtotal=100.0)
    report = run_checks(inv)
    assert report.all_passed  # no line items → subtotal check skipped


# ── Check 3: subtotal + tax_total ≈ total ─────────────────────────────────────

def test_total_pass():
    inv = make_invoice(subtotal=100.0, tax_total=10.0, total=110.0)
    report = run_checks(inv)
    assert report.all_passed


def test_total_fail():
    inv = make_invoice(subtotal=100.0, tax_total=10.0, total=115.0)
    report = run_checks(inv)
    assert not report.all_passed
    total_check = next(c for c in report.checks if c.name == "total")
    assert not total_check.passed
    assert total_check.delta == pytest.approx(5.0, abs=1e-4)


def test_total_skipped_when_tax_null():
    inv = make_invoice(subtotal=100.0, tax_total=None, total=100.0)
    report = run_checks(inv)
    assert report.all_passed  # tax_total null → check skipped


# ── Full invoice ──────────────────────────────────────────────────────────────

def test_full_invoice_all_pass():
    inv = make_invoice(
        invoice_number="INV-001",
        subtotal=200.0,
        tax_total=20.0,
        total=220.0,
        line_items=[
            LineItem(description="A", quantity=2, unit_price=50.0, amount=100.0),
            LineItem(description="B", quantity=4, unit_price=25.0, amount=100.0),
        ],
    )
    report = run_checks(inv)
    assert report.all_passed
    assert len(report.checks) == 4  # 2 line + 1 subtotal + 1 total
    assert report.failure_summary == ""


def test_failure_summary_populated():
    inv = make_invoice(subtotal=100.0, tax_total=10.0, total=999.0)
    report = run_checks(inv)
    assert not report.all_passed
    assert "total" in report.failure_summary.lower()
    assert "Re-examine" in report.failure_summary


# ── Number coercion (Pydantic validators) ─────────────────────────────────────

def test_coerce_us_format():
    item = LineItem(quantity="2", unit_price="1,000.00", amount="2,000.00")
    assert item.unit_price == pytest.approx(1000.0)
    assert item.amount == pytest.approx(2000.0)


def test_coerce_european_format():
    item = LineItem(quantity="1", unit_price="1.234,56", amount="1.234,56")
    assert item.unit_price == pytest.approx(1234.56)


def test_coerce_currency_symbol():
    item = LineItem(amount="$99.99")
    assert item.amount == pytest.approx(99.99)


def test_coerce_null():
    item = LineItem(amount=None)
    assert item.amount is None
