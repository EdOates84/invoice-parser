"""Fault injection for testing error-handling paths."""
from __future__ import annotations
import asyncio
from typing import Optional
from app.models import InvoiceExtraction, LineItem


async def apply_llm_fault(
    model_index: int,
    previous_failures: Optional[str],
    fault_mode: str,
    fault_slow_delay: float,
) -> Optional[InvoiceExtraction]:
    """Return a fake result or raise to simulate faults. None = proceed normally.

    async so that llm_slow uses asyncio.sleep instead of blocking time.sleep.
    """
    if fault_mode == "none":
        return None
    if fault_mode == "llm_unavail":
        raise RuntimeError("Simulated LLM unavailable")
    if fault_mode == "llm_slow":
        await asyncio.sleep(fault_slow_delay)
        return None
    if fault_mode == "llm_bad_json" and previous_failures is None:
        raise ValueError("Simulated malformed JSON")
    if fault_mode == "llm_bad_math" and previous_failures is None:
        return InvoiceExtraction(
            invoice_number="INV-FAULT", invoice_date="2024-01-01", currency="USD",
            subtotal=100.00, tax_total=10.00, total=999.99,
            line_items=[LineItem(description="Item A", quantity=2.0, unit_price=50.00, amount=50.00)],
        )
    return None
