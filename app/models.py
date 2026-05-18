from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator


# ── Extraction schema ─────────────────────────────────────────────────────────

def _coerce_number(v: Any) -> Optional[float]:
    """Coerce LLM numeric output to float.

    Handles: plain float/int, "1,000.00" (US), "1.000,50" (European),
    currency prefixes ("$100", "€50.00"), None.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # European format: digits.digits,digits  e.g. "1.234,56"
        if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
            s = s.replace(".", "").replace(",", ".")
        else:
            # Strip currency symbols, spaces, commas used as thousands separators
            s = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
        try:
            return float(s) if s else None
        except ValueError:
            return None
    return None


class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    discount_percent: Optional[float] = None  # e.g. 10.0 means 10% off this line
    amount: Optional[float] = None

    @field_validator("quantity", "unit_price", "discount_percent", "amount", mode="before")
    @classmethod
    def coerce_number(cls, v: Any) -> Optional[float]:
        return _coerce_number(v)


class InvoiceExtraction(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None   # YYYY-MM-DD
    currency: Optional[str] = None       # ISO 4217
    vendor_name: Optional[str] = None
    subtotal: Optional[float] = None
    discount: Optional[float] = None     # document-level discount amount (positive number)
    shipping: Optional[float] = None     # shipping / freight / handling charge
    tax_total: Optional[float] = None
    total: Optional[float] = None
    line_items: List[LineItem] = []

    @field_validator("subtotal", "discount", "shipping", "tax_total", "total", mode="before")
    @classmethod
    def coerce_number(cls, v: Any) -> Optional[float]:
        return _coerce_number(v)


# ── Validation result ─────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    expected: Optional[float]
    actual: Optional[float]
    delta: Optional[float]
    message: str


@dataclass
class ValidationReport:
    all_passed: bool
    checks: List[CheckResult] = field(default_factory=list)
    failure_summary: str = ""   # injected into retry prompt when not empty


# ── API request / response shapes ─────────────────────────────────────────────

class SubmitRequest(BaseModel):
    document_text: str


class SubmitResponse(BaseModel):
    request_id: str
    status: str
    duplicate: bool = False


class InvoiceResponse(BaseModel):
    request_id: str
    status: str
    attempt_count: int
    extraction_method: Optional[str]
    extraction_model: Optional[str]
    result: Optional[Dict[str, Any]]
    validation_checks: Optional[Dict[str, Any]]
    error: Optional[str]
    created_at: str
    updated_at: str


# ── Config API shapes ─────────────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    llm_models: List[str]
    llm_timeout: int
    enable_invoice2data: bool
    enable_spacy: bool
    enable_bert_ner: bool
    max_attempts: int
    tolerance: float
    fault_mode: str = "none"
    fault_slow_delay: float = 5.0


class ConfigUpdate(BaseModel):
    llm_models: Optional[List[str]] = None
    llm_timeout: Optional[int] = None
    enable_invoice2data: Optional[bool] = None
    enable_spacy: Optional[bool] = None
    enable_bert_ner: Optional[bool] = None
    max_attempts: Optional[int] = None
    tolerance: Optional[float] = None
    fault_mode: Optional[str] = None
    fault_slow_delay: Optional[float] = None


# ── Test / compare API shapes ─────────────────────────────────────────────────

class TestRequest(BaseModel):
    document_text: str


class TestResult(BaseModel):
    extraction_method: str
    extraction_model: Optional[str]
    result: Optional[Dict[str, Any]]
    validation_checks: Optional[Dict[str, Any]]
    latency_ms: float
    error: Optional[str] = None


class CompareResponse(BaseModel):
    results: List[TestResult]
