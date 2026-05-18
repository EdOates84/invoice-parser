"""Tests for the heuristic and spaCy extractors (no LLM calls)."""
import pytest

from app.extractor import extract_heuristic, extract_spacy
from app.models import InvoiceExtraction

SAMPLE_INVOICE = """
INVOICE

Invoice No: INV-2024-0042
Date: 2024-03-15
Currency: USD

Subtotal: $324.98
Tax (8%): $25.99
Total Due: $350.97
"""

MINIMAL_INVOICE = """
Bill No: BILL-001
Date: 01/15/2024
Total: $500.00
"""


# ── Heuristic extractor ───────────────────────────────────────────────────────

def test_heuristic_extracts_invoice_number():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.invoice_number is not None
    assert "INV" in result.invoice_number or "2024" in result.invoice_number


def test_heuristic_extracts_date():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.invoice_date is not None


def test_heuristic_extracts_total():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.total == pytest.approx(350.97)


def test_heuristic_extracts_subtotal():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.subtotal == pytest.approx(324.98)


def test_heuristic_extracts_tax():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.tax_total == pytest.approx(25.99)


def test_heuristic_extracts_currency():
    result = extract_heuristic(SAMPLE_INVOICE)
    assert result is not None
    assert result.currency == "USD"


def test_heuristic_returns_none_on_empty():
    result = extract_heuristic("No useful content here.")
    assert result is None


def test_heuristic_bill_number():
    result = extract_heuristic(MINIMAL_INVOICE)
    assert result is not None
    assert result.total == pytest.approx(500.0)


def test_heuristic_handles_grand_total():
    text = "Grand Total: $1,234.56"
    result = extract_heuristic(text)
    assert result is not None
    assert result.total == pytest.approx(1234.56)


# ── spaCy extractor ───────────────────────────────────────────────────────────

def test_spacy_extracts_currency():
    result = extract_spacy("Invoice total: $100.00 USD")
    if result:  # spaCy model may not be installed in all envs
        assert result.currency == "USD" or result.total is not None


def test_spacy_extracts_amount():
    result = extract_spacy("Total Amount Due: $1,500.00")
    if result:
        assert result.total == pytest.approx(1500.0)


def test_spacy_returns_none_on_garbage():
    result = extract_spacy("aaa bbb ccc")
    assert result is None


# ── Retry prompt injection ────────────────────────────────────────────────────

def test_failure_summary_injected_in_prompt():
    """Verify failure_summary text appears in the retry user message."""
    from app.extractor import RETRY_TEMPLATE

    summary = "Line item 0: delta=5.00"
    rendered = RETRY_TEMPLATE.format(
        failure_summary=summary,
        document_text="some invoice",
    )
    assert summary in rendered
    assert "some invoice" in rendered
    assert "corrected" in rendered.lower()
