from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _parse_models(raw: str) -> List[str]:
    """Split comma-separated model string, skip blanks."""
    return [m.strip() for m in raw.split(",") if m.strip()]


@dataclass
class Settings:
    # ── Tier 1: LLM chain ────────────────────────────────────────────────────
    llm_models: List[str] = field(
        default_factory=lambda: _parse_models(
            os.getenv("LLM_MODELS", "anthropic/claude-haiku-4-5-20251001")
        )
    )
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "30"))

    # ── Tier 2: non-LLM extractors ───────────────────────────────────────────
    enable_invoice2data: bool = os.getenv("ENABLE_INVOICE2DATA", "true").lower() == "true"
    enable_spacy: bool = os.getenv("ENABLE_SPACY", "true").lower() == "true"
    enable_bert_ner: bool = os.getenv("ENABLE_BERT_NER", "false").lower() == "true"

    # ── Validation / retry ───────────────────────────────────────────────────
    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "3"))
    tolerance: float = float(os.getenv("TOLERANCE", "0.01"))

    # ── Fault injection (test only) ──────────────────────────────────────────
    fault_mode: str = os.getenv("FAULT_MODE", "none")
    fault_slow_delay: float = float(os.getenv("FAULT_SLOW_DELAY", "5.0"))

    # ── Storage ───────────────────────────────────────────────────────────────
    database_url: str = os.getenv("DATABASE_URL", "./invoices.db")


# Single shared instance — mutated in-memory by PUT /config
settings = Settings()
