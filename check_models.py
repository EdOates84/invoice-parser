#!/usr/bin/env python3
"""Model health checker — tests every extraction tier and reports status."""

import asyncio
import os
import sys
import time
from typing import Optional

# Load .env if present
from dotenv import load_dotenv
load_dotenv()

PROBE_TEXT = "Invoice No: PROBE-001\nDate: 2024-01-15\nTotal: $100.00"
OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m~\033[0m"
SKIP = "\033[90m-\033[0m"


def _row(icon, label, detail="", latency_ms: Optional[float] = None):
    lat = f"  ({latency_ms:.0f}ms)" if latency_ms else ""
    print(f"  {icon}  {label:<50} {detail}{lat}")


# ── Tier 1: LLM models ───────────────────────────────────────────────────────

def check_llm(model: str) -> tuple[bool, str, float]:
    try:
        import instructor
        import litellm
        from app.models import InvoiceExtraction

        litellm.suppress_debug_info = True
        client = instructor.from_litellm(litellm.completion)

        t0 = time.perf_counter()
        result = client.chat.completions.create(
            model=model,
            response_model=InvoiceExtraction,
            messages=[{"role": "user", "content": f"Extract invoice data:\n{PROBE_TEXT}"}],
            max_tokens=256,
            timeout=15,
        )
        ms = (time.perf_counter() - t0) * 1000
        return True, f"invoice_number={result.invoice_number} total={result.total}", ms
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000 if 't0' in dir() else 0
        return False, str(e)[:80], ms


# ── Tier 2a: invoice2data ─────────────────────────────────────────────────────

def check_invoice2data() -> tuple[bool, str]:
    try:
        from app.extractor import extract_invoice2data
        result = extract_invoice2data(PROBE_TEXT)
        return True, f"result={'ok' if result else 'empty (no matching template)'}"
    except Exception as e:
        return False, str(e)[:80]


# ── Tier 2b: spaCy ────────────────────────────────────────────────────────────

def check_spacy() -> tuple[bool, str]:
    try:
        import spacy
        from app.extractor import extract_spacy
        result = extract_spacy(PROBE_TEXT)
        return True, f"result={'ok' if result else 'no entities found'}"
    except Exception as e:
        return False, str(e)[:80]


# ── Tier 2c: BERT NER ─────────────────────────────────────────────────────────

def check_bert_ner() -> tuple[bool, str]:
    try:
        from transformers import pipeline as hf_pipeline
        t0 = time.perf_counter()
        ner = hf_pipeline("token-classification", model="drajend9/bert-finetuned-ner-invoice",
                          aggregation_strategy="simple")
        result = ner(PROBE_TEXT[:256])
        ms = (time.perf_counter() - t0) * 1000
        return True, f"{len(result)} entities found"
    except Exception as e:
        return False, str(e)[:80]


# ── Tier 3: heuristic regex ───────────────────────────────────────────────────

def check_heuristic() -> tuple[bool, str]:
    try:
        from app.extractor import extract_heuristic
        result = extract_heuristic(PROBE_TEXT)
        if result:
            return True, f"invoice_number={result.invoice_number} total={result.total}"
        return False, "no fields extracted"
    except Exception as e:
        return False, str(e)[:80]


# ── Ollama connectivity ───────────────────────────────────────────────────────

def check_ollama_server() -> tuple[bool, str]:
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            import json
            data = json.loads(r.read())
            models = [m["name"] for m in data.get("models", [])]
            return True, f"{len(models)} model(s) available: {', '.join(models[:3]) or 'none pulled'}"
    except Exception as e:
        return False, f"not running — start with: ollama serve"


# ── API key presence check ────────────────────────────────────────────────────

def _key_status(env_var: str) -> str:
    v = os.getenv(env_var, "")
    if not v or v.endswith("..."):
        return f"\033[91mmissing\033[0m ({env_var})"
    return f"\033[92mset\033[0m ({env_var}=...{v[-4:]})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n\033[1mFynOS Invoice Parser — Model Health Check\033[0m")
    print("=" * 60)

    # ── API keys ─────────────────────────────────────────────────────────────
    print("\n\033[1mAPI Keys\033[0m")
    for provider, key_var in [
        ("Anthropic", "ANTHROPIC_API_KEY"),
        ("OpenAI",    "OPENAI_API_KEY"),
        ("Groq",      "GROQ_API_KEY"),
        ("Cerebras",  "CEREBRAS_API_KEY"),
    ]:
        print(f"  {provider:<12} {_key_status(key_var)}")

    # ── Tier 1: LLMs ─────────────────────────────────────────────────────────
    print("\n\033[1mTier 1: LLM Models\033[0m")
    llm_models_raw = os.getenv("LLM_MODELS", "")
    llm_models = [m.strip() for m in llm_models_raw.split(",") if m.strip()]

    if not llm_models:
        _row(WARN, "LLM_MODELS not set in .env", "skipping LLM checks")
    else:
        for model in llm_models:
            ok, detail, ms = check_llm(model)
            _row(OK if ok else FAIL, model, detail, ms)

    # Also probe Groq + Cerebras with default free models if keys are set
    extra = []
    if os.getenv("GROQ_API_KEY") and "groq/" not in llm_models_raw:
        extra.append("groq/llama-3.3-70b-versatile")
    if os.getenv("CEREBRAS_API_KEY") and "cerebras/" not in llm_models_raw:
        extra.append("cerebras/llama-4-scout-17b-16e-instruct")
    for model in extra:
        ok, detail, ms = check_llm(model)
        _row(OK if ok else FAIL, f"{model} (probe)", detail, ms)

    # ── Ollama (local) ────────────────────────────────────────────────────────
    print("\n\033[1mTier 1 (local): Ollama\033[0m")
    ok, detail = check_ollama_server()
    _row(OK if ok else WARN, "ollama serve", detail)
    if not ok:
        _row(SKIP, "Install: brew install ollama", "then: ollama pull qwen2.5:7b")

    # ── Tier 2: open-source ───────────────────────────────────────────────────
    print("\n\033[1mTier 2: Open-Source Non-LLM Extractors\033[0m")

    ok, detail = check_invoice2data()
    _row(OK if ok else FAIL, "invoice2data (YAML templates)", detail)

    ok, detail = check_spacy()
    _row(OK if ok else FAIL, "spaCy EntityRuler", detail)

    bert_enabled = os.getenv("ENABLE_BERT_NER", "false").lower() == "true"
    if bert_enabled:
        ok, detail = check_bert_ner()
        _row(OK if ok else FAIL, "BERT NER (drajend9/bert-finetuned-ner-invoice)", detail)
    else:
        _row(SKIP, "BERT NER", "disabled (set ENABLE_BERT_NER=true to enable)")

    # ── Tier 3: heuristic ─────────────────────────────────────────────────────
    print("\n\033[1mTier 3: Regex Heuristic (always available)\033[0m")
    ok, detail = check_heuristic()
    _row(OK if ok else FAIL, "regex heuristic", detail)

    print()


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
