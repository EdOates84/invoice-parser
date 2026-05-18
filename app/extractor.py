from __future__ import annotations

import re
import time
from typing import Optional, Tuple

import instructor
import litellm
from json_repair import repair_json

from app.config import Settings
from app.models import InvoiceExtraction, LineItem

# Suppress LiteLLM verbose logging
litellm.suppress_debug_info = True

# ── Type alias ────────────────────────────────────────────────────────────────
# Returns (extraction, method_name, model_name_or_None)
ExtractionResult = Tuple[Optional[InvoiceExtraction], str, Optional[str]]


# ── LLM prompt templates ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an invoice data extraction specialist working for an accounting platform.
Extract structured data from raw invoice text (already processed by OCR).

Rules:
- Extract ONLY information explicitly present in the text. Never infer or hallucinate.
- Return null for any field not found.
- Normalize dates to YYYY-MM-DD format.
- Normalize numbers to decimal format (e.g. 1234.56, not "1,234.56" or "$1,234.56").
- currency must be a 3-letter ISO 4217 code (USD, EUR, GBP, INR, etc.) or null.
- subtotal: the pre-discount, pre-shipping sum of line items as printed on the invoice.
- discount: document-level discount as a positive number (e.g. 47.50), or null. Do NOT negate it.
- shipping: shipping, freight, or handling charge as a positive number, or null.
- tax_total: total tax amount (sum all tax lines if multiple), or null.
- line_items: extract every line item found. If quantity or unit_price is missing, set to null.
- line_items[].discount_percent: per-line discount as a percentage number (e.g. 10 for 10%), or null.
- Return valid JSON only. No prose, no markdown, no explanation."""

USER_TEMPLATE = """Extract invoice data from the following text:

<invoice>
{document_text}
</invoice>"""

RETRY_TEMPLATE = """Extract invoice data from the following text.

Your previous extraction had arithmetic errors that must be corrected:
{failure_summary}

Re-examine the source text carefully and return corrected values.

<invoice>
{document_text}
</invoice>"""


# ── Tier 1: LLM extraction ────────────────────────────────────────────────────

def _build_messages(document_text: str, previous_failures: Optional[str]) -> list:
    if previous_failures:
        user_content = RETRY_TEMPLATE.format(
            failure_summary=previous_failures,
            document_text=document_text,
        )
    else:
        user_content = USER_TEMPLATE.format(document_text=document_text)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def extract_llm(
    document_text: str,
    previous_failures: Optional[str],
    model: str,
    timeout: int,
) -> InvoiceExtraction:
    """Call LLM via LiteLLM + instructor.

    instructor enforces Pydantic schema and retries internally on parse failures.
    Raises on LLM unavailability / timeout.
    """
    client = instructor.from_litellm(litellm.completion)
    return client.chat.completions.create(
        model=model,
        response_model=InvoiceExtraction,
        messages=_build_messages(document_text, previous_failures),
        max_tokens=2048,
        timeout=timeout,
        max_retries=2,  # instructor internal retries for malformed JSON
    )


# ── Tier 2a: invoice2data (YAML template engine) ──────────────────────────────

def extract_invoice2data(document_text: str) -> Optional[InvoiceExtraction]:
    """Template-based extraction using invoice2data."""
    try:
        import os
        import tempfile

        from invoice2data import extract_data
        from invoice2data.extract.loader import read_templates

        templates_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates"
        )
        templates = read_templates(templates_dir)

        # invoice2data expects a file path, write text to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(document_text)
            tmp_path = f.name

        try:
            result = extract_data(tmp_path, templates=templates)
        finally:
            os.unlink(tmp_path)

        if not result:
            return None

        return InvoiceExtraction(
            invoice_number=str(result.get("invoice_number")) if result.get("invoice_number") else None,
            invoice_date=str(result.get("date")) if result.get("date") else None,
            currency=result.get("currency"),
            total=float(result["amount"]) if result.get("amount") else None,
        )
    except Exception:
        return None


# ── Tier 2b: spaCy EntityRuler ────────────────────────────────────────────────

_spacy_nlp = None


def _get_spacy_nlp():
    global _spacy_nlp
    if _spacy_nlp is not None:
        return _spacy_nlp

    import spacy

    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        nlp = spacy.blank("en")

    ruler = nlp.add_pipe("entity_ruler", last=True)
    patterns = [
        {"label": "INVOICE_NUM", "pattern": [{"TEXT": {"REGEX": r"(?:INV|inv)[-/]?\d{2,15}"}}]},
        {"label": "INVOICE_NUM", "pattern": [{"TEXT": {"REGEX": r"[A-Z]{2,4}[-/]\d{3,12}"}}]},
        {"label": "AMOUNT",      "pattern": [{"TEXT": {"REGEX": r"\$?\d{1,3}(?:,\d{3})*\.\d{2}"}}]},
        {"label": "DATE",        "pattern": [{"TEXT": {"REGEX": r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"}}]},
        {"label": "DATE",        "pattern": [{"TEXT": {"REGEX": r"\d{4}[/\-]\d{2}[/\-]\d{2}"}}]},
        {"label": "CURRENCY",    "pattern": [{"TEXT": {"REGEX": r"\b(?:USD|EUR|GBP|INR|AUD|CAD|SGD|AED|JPY|CNY)\b"}}]},
    ]
    ruler.add_patterns(patterns)
    _spacy_nlp = nlp
    return nlp


def extract_spacy(document_text: str) -> Optional[InvoiceExtraction]:
    """spaCy EntityRuler extraction — fast, no training needed."""
    try:
        nlp = _get_spacy_nlp()
        doc = nlp(document_text[:100_000])  # cap for safety

        invoice_number: Optional[str] = None
        invoice_date: Optional[str] = None
        currency: Optional[str] = None
        amounts: list[float] = []

        for ent in doc.ents:
            if ent.label_ == "INVOICE_NUM" and invoice_number is None:
                invoice_number = ent.text
            elif ent.label_ == "DATE" and invoice_date is None:
                invoice_date = ent.text
            elif ent.label_ == "CURRENCY" and currency is None:
                currency = ent.text
            elif ent.label_ == "AMOUNT":
                cleaned = re.sub(r"[^\d.]", "", ent.text)
                try:
                    amounts.append(float(cleaned))
                except ValueError:
                    pass

        if not any([invoice_number, invoice_date, amounts]):
            return None

        total = max(amounts) if amounts else None
        return InvoiceExtraction(
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            currency=currency,
            total=total,
        )
    except Exception:
        return None


# ── Tier 2c: BERT NER (optional, heavy) ──────────────────────────────────────

def extract_bert_ner(document_text: str) -> Optional[InvoiceExtraction]:
    """HuggingFace BERT NER fine-tuned on invoice data."""
    try:
        from transformers import pipeline as hf_pipeline

        ner = hf_pipeline(
            "token-classification",
            model="drajend9/bert-finetuned-ner-invoice",
            aggregation_strategy="simple",
        )
        entities = ner(document_text[:512])  # BERT max token limit

        result: dict = {}
        for ent in entities:
            label = ent["entity_group"].upper()
            text = ent["word"].strip()
            if "INVOICE" in label and "invoice_number" not in result:
                result["invoice_number"] = text
            elif "DATE" in label and "invoice_date" not in result:
                result["invoice_date"] = text
            elif "TOTAL" in label or "AMOUNT" in label:
                cleaned = re.sub(r"[^\d.]", "", text)
                try:
                    result["total"] = float(cleaned)
                except ValueError:
                    pass

        return InvoiceExtraction(**result) if result else None
    except Exception:
        return None


# ── Tier 3: Regex heuristic (always available) ───────────────────────────────

_PATTERNS = {
    "invoice_number": [
        r"(?:invoice[ \t]*(?:no|number|num|#)[ \t:]*)([\w][\w\-/\.]{1,30})",
        r"(?:bill[ \t]*(?:no|number|num|#)[ \t:]*)([\w\-/]{2,20})",
        r"\b(INV[-/]?\d{3,15})\b",
        r"(?:invoice[ \t]*(?:no|number|num|#)?[ \t:]*)((?!No\b|Number\b)[\w][\w\-/\.]{2,30})",
    ],
    "invoice_date": [
        r"(?:(?:invoice[ \t]*)?date[ \t:]*)(\d{4}[\/\-]\d{2}[\/\-]\d{2})",
        r"(?:(?:invoice[ \t]*)?date[ \t:]*)(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    ],
    "currency": [r"\b(USD|EUR|GBP|INR|AUD|CAD|SGD|AED|JPY|CNY)\b"],
    "total": [
        r"(?<!\bsub)(?<!sub)(?:total[ \t]*(?:due|amount)?[ \t:]*)\$?\s*([\d,]+\.\d{2})",
        r"(?:grand[ \t]*total[ \t:]*)\$?\s*([\d,]+\.\d{2})",
        r"(?:amount[ \t]*(?:due|payable)[ \t:]*)\$?\s*([\d,]+\.\d{2})",
    ],
    "subtotal": [
        r"(?:sub[ \t]*total[ \t:]*)\$?\s*([\d,]+\.\d{2})",
        r"(?:net[ \t]*amount[ \t:]*)\$?\s*([\d,]+\.\d{2})",
    ],
    "tax_total": [
        r"(?:(?:gst|vat|tax|hst)[ \t]*(?:\([^)]*\))?[ \t:]*)\$?\s*([\d,]+\.\d{2})",
        r"(?:tax[ \t]*amount[ \t:]*)\$?\s*([\d,]+\.\d{2})",
    ],
    "vendor_name": [
        r"(?:vendor|supplier|from|bill[ \t]*from|sold[ \t]*by)[ \t:]*([A-Z][A-Za-z0-9 &,\.]{2,50})",
        r"(?:company|firm)[ \t:]*([A-Z][A-Za-z0-9 &,\.]{2,50})",
    ],
}

# Line item: description (text)  qty (number)  unit_price ($num)  amount ($num)
# Handles: "Widget Pro X     2    $49.99    $99.98"
# Also:    "Widget Pro X     2    49.99     99.98"
_LINE_ITEM_RE = re.compile(
    r"^((?:[A-Za-z0-9][A-Za-z0-9 &\-\(\)/\.']{1,60}?))"   # description
    r"[ \t]{2,}"                                              # 2+ spaces separator
    r"(\d+(?:\.\d+)?)"                                        # quantity
    r"[ \t]+\$?([\d,]+\.\d{2})"                               # unit_price
    r"[ \t]+\$?([\d,]+\.\d{2})",                              # amount
    re.MULTILINE,
)

# Skip lines that look like headers or section labels
_SKIP_WORDS = re.compile(
    r"^\s*(?:description|item|product|particulars|qty|quantity|"
    r"unit\s*price|rate|amount|total|subtotal|tax|gst|vat|discount|"
    r"invoice|date|bill|vendor|from|to|page)\b",
    re.IGNORECASE,
)


def _parse_line_items(text: str) -> list[LineItem]:
    items = []
    for m in _LINE_ITEM_RE.finditer(text):
        desc = m.group(1).strip()
        if _SKIP_WORDS.match(desc):
            continue
        try:
            qty   = float(m.group(2))
            price = float(m.group(3).replace(",", ""))
            amt   = float(m.group(4).replace(",", ""))
            items.append(LineItem(description=desc, quantity=qty, unit_price=price, amount=amt))
        except ValueError:
            continue
    return items


def _first_match(text: str, patterns: list[str]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _parse_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def extract_heuristic(document_text: str) -> Optional[InvoiceExtraction]:
    """Regex-based extraction — deterministic, zero deps, always runs."""
    inv_num    = _first_match(document_text, _PATTERNS["invoice_number"])
    inv_date   = _first_match(document_text, _PATTERNS["invoice_date"])
    currency   = _first_match(document_text, _PATTERNS["currency"])
    total      = _parse_float(_first_match(document_text, _PATTERNS["total"]))
    subtotal   = _parse_float(_first_match(document_text, _PATTERNS["subtotal"]))
    tax_total  = _parse_float(_first_match(document_text, _PATTERNS["tax_total"]))
    vendor     = _first_match(document_text, _PATTERNS["vendor_name"])
    line_items = _parse_line_items(document_text)

    if not any([inv_num, inv_date, total, subtotal, line_items]):
        return None

    return InvoiceExtraction(
        invoice_number=inv_num,
        invoice_date=inv_date,
        currency=currency,
        vendor_name=vendor,
        total=total,
        subtotal=subtotal,
        tax_total=tax_total,
        line_items=line_items,
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def extract_with_fallback(
    document_text: str,
    previous_failures: Optional[str],
    config: Settings,
) -> ExtractionResult:
    """Try each extraction tier in order. Returns first successful result.

    Tier 1: each LLM in config.llm_models (availability errors → try next)
    Tier 2: invoice2data → spaCy → BERT NER (if enabled)
    Tier 3: regex heuristic (always)

    async so that fault-injection slow-delay uses asyncio.sleep (non-blocking).
    Note: the underlying LLM and extractor calls are synchronous — for production
    use asyncio.to_thread() wrappers or aiosqlite to avoid event-loop blocking.
    """
    # ── Tier 1: LLM chain ────────────────────────────────────────────────────
    from app.fault_injection import apply_llm_fault
    for i, model in enumerate(config.llm_models):
        try:
            injected = await apply_llm_fault(
                i, previous_failures, config.fault_mode, config.fault_slow_delay
            )
            if injected is not None:
                return injected, "llm", model
            t0 = time.perf_counter()
            result = extract_llm(document_text, previous_failures, model, config.llm_timeout)
            _ = time.perf_counter() - t0
            if result:
                return result, "llm", model
        except Exception as exc:
            err_str = str(exc).lower()
            # Availability / timeout errors → try next model
            if any(k in err_str for k in ("rate limit", "429", "503", "502", "timeout",
                                           "connection", "unavailable", "overloaded")):
                continue
            # Other errors (auth, bad model name) → also try next
            continue

    # ── Tier 2a: invoice2data ────────────────────────────────────────────────
    if config.enable_invoice2data:
        result = extract_invoice2data(document_text)
        if result:
            return result, "invoice2data", None

    # ── Tier 2b: spaCy ───────────────────────────────────────────────────────
    if config.enable_spacy:
        result = extract_spacy(document_text)
        if result:
            return result, "spacy", None

    # ── Tier 2c: BERT NER ────────────────────────────────────────────────────
    if config.enable_bert_ner:
        result = extract_bert_ner(document_text)
        if result:
            return result, "bert_ner", None

    # ── Tier 3: heuristic (always) ───────────────────────────────────────────
    result = extract_heuristic(document_text)
    if result:
        return result, "heuristic", None

    return None, "none", None
