# FynOS Invoice Parsing Service — Design Document

> Full thought process, architectural decisions, data flows, and trade-off reasoning.

---

## 1. Problem Framing

The challenge: accept raw post-OCR invoice text, extract structured financial data, validate that the numbers are internally consistent, and expose the result via a polling API.

Three scenarios are explicitly tested:
1. **Deliberate duplicate** — same invoice submitted twice
2. **Timeout retry** — client never received the first 202, resends
3. **D-4471** — extractor fails, client immediately resubmits the same doc

These three scenarios push on the same seam: **what does "same invoice" mean, and when should the service create a new record vs. return an existing one?** Everything else in the design flows from getting that answer right.

---

## 2. Thought Process — Before Writing Code

### 2.1 What can go wrong with invoice parsing?

Before choosing an architecture, I enumerated the failure modes:

| Failure | Where it happens | Impact |
|---|---|---|
| LLM hallucinates a number | Tier 1 extraction | Wrong financial data — dangerous |
| LLM is down / rate-limited | Tier 1 extraction | No extraction at all |
| LLM returns malformed JSON | Tier 1, instructor layer | Retry internally, then move on |
| Extracted `qty × unit_price ≠ amount` | Validation | Possibly a typo on the invoice, possibly hallucination |
| LLM "corrects" a number to pass math but changes the meaning | Retry loop | Harder to detect — mitigated by anchoring all retries to the original source text |
| Regex can't parse an unusual format | Tier 3 | Partial extraction — better than nothing |
| Process crashes mid-extraction | Startup | Zombie `PROCESSING` rows — healed by `startup_recovery()` |

This led directly to:
- **Validation as a first-class concern**, not an afterthought
- **Multi-tier fallback** so a single point of failure (LLM down) doesn't kill the service
- **Startup recovery** to handle crash restarts gracefully

### 2.2 Architecture options considered

**Option A — Single LLM, no fallback**

Simple. One LLM, one call, validate the result, done. The problem: when the LLM is unavailable (rate limit, outage, cost cap) every invoice fails. In a financial context this is unacceptable — partial data is better than no data.

**Option B — Multi-LLM chain only**

Try LLM 1, then LLM 2, then LLM 3. Still fails if all LLMs are down simultaneously, or if the operator hasn't configured any API keys.

**Option C — Multi-tier: LLM chain → deterministic extractors → regex heuristic**

The selected approach. The key insight: **invoice extraction is a solved problem for structured formats** (invoice2data, spaCy patterns) and a tractable problem even for unstructured formats (regex can reliably extract totals, dates, invoice numbers). LLMs add line-item extraction and vendor name parsing that regex struggles with, but the business-critical numbers (total, subtotal, tax) are nearly always parseable by heuristics.

Result: the service degrades gracefully. With no API keys at all, it still extracts the key financial fields from most invoices via Tier 3.

### 2.3 Validation design

The arithmetic validation engine runs independently of the extraction method. Every result — LLM, invoice2data, spaCy, or regex — goes through the same three checks.

**Why independent validation?** Because extraction and validation have different failure modes. An LLM can produce syntactically valid JSON with wrong numbers. A regex extractor can match the wrong total line. Validation is the safety net that catches extraction errors regardless of their source.

**Why only two critical checks?** The third check (`Σ line_items ≈ subtotal`) is advisory because partial extraction is expected and correct. A 30-line invoice where only 15 lines were extracted is not an error — it's a limitation of the extractor. Failing `COMPLETED` on partial extraction would create false negatives on long invoices.

**Why tolerance = 0.01?** This is 1 cent in any currency. Floating-point rounding in PDF-to-text extraction can produce sub-cent discrepancies. A stricter tolerance (e.g. exact equality) would flag valid extractions as failures. A looser tolerance would mask genuine arithmetic errors.

### 2.4 Idempotency design

The dedup key is `SHA-256(document_text.strip().lower())`. This makes the dedup content-addressed: two submissions of the same invoice text will always produce the same hash, regardless of the client, timestamp, or request ID.

**Why not a client-provided idempotency key?** The challenge spec says no authentication is required. Without authentication, a client-provided key can't be scoped to a client — two different clients could accidentally collide. Content-hash is universal and deterministic.

**The FAILED exception:**

The state table for dedup:

```
Non-terminal (PENDING, PROCESSING, VALIDATING) → return existing (in flight)
COMPLETED                                       → return existing (success, idempotent)
NEEDS_REVIEW                                    → return existing (needs human, don't create noise)
FAILED                                          → NEW request_id (explicit retry)
```

`FAILED` is treated differently because it signals a **system-level failure**, not a business duplicate. When an extractor fails, the operator likely fixed something (updated API keys, restored network connectivity). The resubmission is intentional. Creating a new record:
- Gives the operator a clean slate for the retry
- Preserves the failed record as audit evidence
- Avoids silently returning a failed result to a client who thinks they're retrying

---

## 3. Data Flow — Request Lifecycle

### 3.1 Submit (POST /invoices)

```
Client
  │
  ├─ POST /invoices {"document_text": "..."}
  │
  ▼
main.py: submit_invoice()
  │
  ├─ Parse body (JSON or text/plain)
  ├─ Compute content_hash = SHA-256(text.strip().lower())
  ├─ db.get_by_hash(content_hash)
  │     ├─ Found + status != FAILED → return existing request_id (duplicate: true)
  │     └─ Not found / FAILED → continue
  │
  ├─ db.insert_invoice(content_hash, document_text) → request_id (UUID)
  ├─ background_tasks.add_task(run_processor, request_id, settings)
  │
  └─ Return 202 {request_id, status: "PENDING", duplicate: false}
```

### 3.2 Processor (BackgroundTask)

```
processor.run(request_id, settings)
  │
  ├─ attempt = 0
  ├─ previous_failures = None
  │
  └─ LOOP (while attempt < max_attempts):
       │
       ├─ attempt += 1
       ├─ db.update_status(PROCESSING, attempt_count=attempt)
       │
       ├─ extract_with_fallback(document_text, previous_failures, settings)
       │     │
       │     ├─ Tier 1: for each model in llm_models:
       │     │     ├─ apply_llm_fault() → may inject fault for testing
       │     │     ├─ extract_llm(text, previous_failures, model, timeout)
       │     │     │     └─ LiteLLM + instructor → InvoiceExtraction (Pydantic)
       │     │     └─ on availability error → try next model
       │     │
       │     ├─ Tier 2a: extract_invoice2data(text) if enabled
       │     ├─ Tier 2b: extract_spacy(text) if enabled
       │     ├─ Tier 2c: extract_bert_ner(text) if enabled
       │     └─ Tier 3: extract_heuristic(text) — always
       │
       ├─ If extraction is None (all tiers failed):
       │     └─ db.update_status(FAILED) → RETURN
       │
       ├─ If extraction has no financial data (no total, no subtotal, no lines):
       │     └─ db.update_status(FAILED, "critically empty") → RETURN
       │
       ├─ db.update_status(VALIDATING, result=extraction)
       │
       ├─ run_checks(extraction, tolerance)
       │     ├─ Check each line: qty × unit_price × (1-discount%) ≈ amount
       │     ├─ Check total: subtotal - discount + shipping + tax ≈ total
       │     └─ Advisory: Σ line amounts ≈ subtotal
       │
       ├─ If all_passed:
       │     └─ db.update_status(COMPLETED) → RETURN
       │
       └─ If failed:
             ├─ previous_failures = failure_summary (injected into next LLM prompt)
             ├─ db.update_status(VALIDATING, result=best_so_far)
             └─ continue loop
  │
  └─ (loop exhausted) db.update_status(NEEDS_REVIEW) → RETURN
```

### 3.3 Poll (GET /invoices/{request_id})

```
Client
  │
  ├─ GET /invoices/{request_id}
  │
  ▼
main.py: get_invoice()
  │
  ├─ db.get_by_id(request_id)
  │     ├─ Not found → 404
  │     └─ Found → build InvoiceResponse
  │
  └─ Return 200 {request_id, status, attempt_count, extraction_method,
                 extraction_model, result, validation_checks, error,
                 created_at, updated_at}
```

Clients poll until `status` is `COMPLETED`, `NEEDS_REVIEW`, or `FAILED`.

---

## 4. Component Breakdown

### 4.1 `app/config.py` — Settings

Single `@dataclass` loaded from environment at startup. The `settings` object is a module-level singleton mutated in-memory by `PUT /config`. This means runtime config changes (swap LLM model, change tolerance) take effect on the next request without a restart.

**Design decision**: mutable singleton over immutable config reload. For a single-process service this is safe. For a multi-process deployment, you'd need a shared config store (Redis, DB).

### 4.2 `app/database.py` — SQLite storage

Thin CRUD layer over `sqlite3`. Each call opens and closes a connection via a context manager — no connection pool needed at SQLite's concurrency model.

`startup_recovery()` runs once at startup and moves any rows stuck in `PROCESSING` or `VALIDATING` (older than 60 seconds) to `FAILED`. This handles the crash-restart scenario without requiring a separate health-check job.

The `content_hash` unique index is the enforcement mechanism for deduplication. If two requests race to insert the same hash, only one INSERT succeeds; the other gets an `IntegrityError` and the dedup logic handles it.

### 4.3 `app/extractor.py` — Extraction tiers

**Tier 1: LLM via LiteLLM + instructor**

LiteLLM provides a unified interface to every major LLM provider with a single import. `instructor` patches the LiteLLM client to enforce Pydantic schema output — it retries internally (up to 2 times) when the model returns malformed JSON.

The `SYSTEM_PROMPT` is carefully crafted to:
- Prohibit hallucination ("Extract ONLY information explicitly present")
- Define the exact format for ambiguous fields (currency = ISO 4217, date = YYYY-MM-DD)
- Distinguish subtotal, discount, shipping, and tax roles to avoid common confusion

The `RETRY_TEMPLATE` embeds the `failure_summary` from the validator directly into the next LLM prompt. This is deliberate: the model sees the exact arithmetic error in its own previous output and the source text simultaneously, giving it the best chance of correcting the error vs. repeating it.

**Tier 2a: invoice2data**

YAML template engine. The `templates/generic.yml` file defines regex patterns for common invoice formats. Fast (< 20 ms), deterministic, zero LLM cost. Weak on unusual formats and line items, strong on well-structured invoices.

**Tier 2b: spaCy EntityRuler**

Custom entity patterns registered at model load time (lazy singleton). Matches invoice numbers, dates, currency codes, and monetary amounts. Falls back from `en_core_web_sm` to a blank English model if the spaCy model isn't installed.

**Tier 2c: BERT NER (opt-in)**

HuggingFace pipeline using a BERT model fine-tuned on invoice data. High quality on entity recognition but requires ~400 MB download on first use. Disabled by default to avoid blocking the demo setup.

**Tier 3: Regex heuristic**

Runs unconditionally. Handles: invoice number, date, currency, total, subtotal, tax, vendor name, and basic tabular line items (requires 2+ spaces between columns). Returns `None` only if it finds nothing at all — otherwise always produces a partial result.

### 4.4 `app/validator.py` — Arithmetic validation

Pure function. Takes an `InvoiceExtraction` and returns a `ValidationReport`. No I/O, no state.

Critical checks block `COMPLETED`. Advisory checks are informational only.

The `failure_summary` string is structured specifically to help the LLM understand what to fix:

```
Arithmetic errors found in your previous extraction.
Re-examine the source text and correct these values:

- Line item 0 ("Widget A"): qty×unit_price=50.0000, reported amount=45.0 (Δ=5.0000)
```

This is more effective than a generic "your numbers are wrong" because it points to the exact field and the exact discrepancy.

### 4.5 `app/processor.py` — State machine

The processor is the orchestrator. It owns the retry loop and all state transitions. Each iteration:
1. Sets status to `PROCESSING`
2. Calls `extract_with_fallback`
3. If null or empty → `FAILED` (terminal)
4. Sets status to `VALIDATING`
5. Runs checks
6. If passed → `COMPLETED` (terminal)
7. If failed → store `failure_summary`, loop
8. After `max_attempts` → `NEEDS_REVIEW` (terminal)

The "critically empty" check (step 3b) short-circuits the retry loop when extraction produces no financial data at all. Retrying three times with a fundamentally unparseable document wastes LLM tokens and time.

### 4.6 `app/fault_injection.py` — Test modes

Six fault modes injectable via `PUT /config`:

| Mode | Behavior |
|---|---|
| `none` | Normal operation |
| `llm_unavailable` | All LLM calls raise 503 → fallback to Tier 2/3 |
| `llm_timeout` | All LLM calls simulate timeout → fallback to Tier 2/3 |
| `llm_slow` | LLM calls sleep `fault_slow_delay` seconds → tests latency tolerance |
| `llm_bad_math` | Attempt 1: return wrong arithmetic → validator catches, retry injects correction → Attempt 2: returns correct |
| `llm_bad_math_always` | Every attempt returns wrong arithmetic → exhausts retries → `NEEDS_REVIEW` |
| `llm_first_fails` | First model in chain raises 503 → system falls to second model |

These modes allow demonstrating all the interesting code paths without needing to actually be rate-limited or have an intermittent network.

---

## 5. State Machine — Full Detail

```
                    ┌─────────────────────────────────┐
                    │            PENDING               │
                    │   inserted by POST /invoices     │
                    └──────────────┬──────────────────┘
                                   │ BackgroundTask starts
                    ┌──────────────▼──────────────────┐
                    │           PROCESSING             │
                    │   extraction attempt in flight   │
                    └──────────────┬──────────────────┘
                                   │
              ┌────────────────────┼──────────────────────┐
              │                    │                      │
    all tiers fail         extraction not empty    extraction empty
    (none produced         or system error          (no financial data)
     a result)
              │                    │                      │
              ▼                    ▼                      ▼
           FAILED          ┌──────────────┐           FAILED
         (terminal)        │  VALIDATING  │          (terminal)
                           │ checks run   │
                           └──────┬───────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
              all critical             one+ critical
               checks pass               check fails
                    │                           │
                    ▼                           │
               COMPLETED                  attempts < max?
              (terminal)                        │
                                     ┌──────────┴──────────┐
                                     │                     │
                                   yes                    no
                                     │                     │
                              PROCESSING             NEEDS_REVIEW
                              (retry loop,          (terminal)
                               with corrective
                               failure_summary
                               in next prompt)
```

### Why terminal states are absorbing

Once a record reaches `COMPLETED`, `NEEDS_REVIEW`, or `FAILED`, it never transitions again. This is a deliberate choice:

- **Immutability of completed work**: A `COMPLETED` result is a fact. Allowing it to be overwritten would break the dedup guarantee — a second submission returns the existing result, which could change under the client.
- **Audit trail**: `FAILED` records document what went wrong. `NEEDS_REVIEW` records document that a human was needed. Overwriting them destroys the audit trail.
- **Client reliability**: A client polling until `COMPLETED` can trust that the result won't change after it receives `COMPLETED`.

---

## 6. Idempotency — Full Reasoning

### Why content-hash, not request-ID

Content-hash dedup solves the **timeout retry** problem automatically. The client doesn't need to generate, store, or resend an idempotency key. The service recognizes the same invoice regardless of which client sent it.

### The FAILED exception — full reasoning

Consider this sequence:
1. `POST /invoices` → `request_id = AAA`, extraction starts
2. LLM is down, all tiers fail → `AAA` is `FAILED`
3. Operator restores LLM access
4. Client resends the same invoice text

At step 4, if the service returns `AAA` (the failed result) with `duplicate: true`, the client sees `FAILED` and has no way to trigger a retry without the operator manually deleting the `AAA` row. That's bad UX and bad operability.

If instead the service creates a fresh `request_id = BBB`:
- `BBB` processes normally → `COMPLETED`
- `AAA` remains as the failure audit record
- The operator can see both: "it failed at 10:00, succeeded at 10:05 after the fix"

This is the correct behavior. `FAILED` is not a business duplicate — it's a system failure that the client is retrying.

### NEEDS_REVIEW vs FAILED re-submission

`NEEDS_REVIEW` returns the existing record (no new request_id) because:
- The extraction *succeeded* — data was extracted and stored
- The failure is *arithmetic inconsistency*, not a system error
- Creating a new record would trigger another extraction attempt that will likely produce the same inconsistent result
- The correct next step is a human reviewing the extracted data, not the system retrying automatically

---

## 7. LLM Prompt Engineering

### First extraction prompt

```
System: You are an invoice data extraction specialist...
        Rules:
        - Extract ONLY information explicitly present. Never infer or hallucinate.
        - Return null for any field not found.
        - Normalize dates to YYYY-MM-DD format.
        - Normalize numbers to decimal format.
        - currency must be a 3-letter ISO 4217 code or null.
        - subtotal: the pre-discount, pre-shipping sum of line items as printed.
        - discount: document-level discount as a positive number, or null.
        - shipping: shipping, freight, or handling charge, or null.
        - tax_total: total tax amount (sum all tax lines if multiple), or null.
        - line_items: extract every line item found.
        - Return valid JSON only. No prose, no markdown, no explanation.

User: Extract invoice data from the following text:
      <invoice>
      {document_text}
      </invoice>
```

**Design choices:**
- `<invoice>` XML tags: helps the model distinguish instruction from data
- Explicit null instruction: prevents the model from guessing missing fields
- Separate `subtotal`, `discount`, `shipping`, `tax_total`, `total`: many invoices have all five; conflating them produces wrong totals
- "No prose, no markdown": instructor enforces the Pydantic schema but belt-and-suspenders instructions reduce token waste

### Retry prompt

```
User: Extract invoice data from the following text.

      Your previous extraction had arithmetic errors that must be corrected:
      {failure_summary}

      Re-examine the source text carefully and return corrected values.

      <invoice>
      {document_text}
      </invoice>
```

**Key design choice**: the retry prompt includes the **original document text** every time. This prevents the model from "correcting" numbers by fabricating new ones — it must re-read the source. The `failure_summary` tells it exactly which fields to look at and what the discrepancy was.

---

## 8. Number Coercion Design

LLM output for numeric fields is inconsistent across models and input formats. The `_coerce_number()` function handles:

| Input | Output | Why |
|---|---|---|
| `100.0` | `100.0` | Already float |
| `"1,000.50"` | `1000.5` | US thousands separator |
| `"1.000,50"` | `1000.5` | European format |
| `"$1,234.56"` | `1234.56` | Currency prefix |
| `"€1.234,56"` | `1234.56` | European + currency |
| `""` | `None` | Empty string |
| `None` | `None` | Pass-through |

European format detection: if the string matches `\d{1,3}(\.\d{3})+(,\d+)?$` (dot as thousands, comma as decimal), swap them. Otherwise strip non-numeric chars except `.` and `-`.

This is important because:
- Non-European LLMs may return European-format numbers from European invoices in the source text
- invoice2data and spaCy may return raw strings from OCR text
- The validator needs clean floats; coercion centralizes this at the model boundary

---

## 9. Error Handling Strategy

### What gets retried

- LLM arithmetic validation failure → retry with corrective prompt (up to `max_attempts`)
- LLM internal JSON parse failure → instructor retries (up to 2 times internally)
- LLM availability error (503, 429, timeout, connection error) → **no retry on same model**, move to next model in chain

### What does NOT get retried

- Empty/critically empty extraction → `FAILED` immediately (retrying with the same tier chain will produce the same empty result)
- Unexpected Python exceptions in extraction → `FAILED` (unknown state, not safe to retry)
- Tier 2/3 failures → fall through to next tier (not retried because they're deterministic; same input = same output)

### Why not retry Tier 2/3?

Deterministic extractors produce the same result for the same input. If invoice2data fails to match any template, retrying it won't change anything. The right response is to try the next tier, not retry the same one.

LLMs are non-deterministic (temperature > 0), so retrying with a corrective prompt can produce a different (better) result.

---

## 10. Observability

Current logging:
- Every status transition: `request=XXX attempt=1 status=PROCESSING`
- Every validation failure: `request=XXX attempt=1 validation failed: <summary[:120]>`
- Terminal state: `request=XXX status=COMPLETED` (or FAILED, NEEDS_REVIEW)
- Startup recovery: `healed N stuck rows → FAILED`

All logs go to stdout via Python's `logging` module at INFO/WARNING level. Format: `%(asctime)s %(levelname)s %(name)s %(message)s`.

**With more time**: structured JSON logging with `request_id`, `attempt`, `model`, `prompt_tokens`, `latency_ms` per attempt — making it easy to trace exactly what happened to a given invoice across its retry loop.

---

## 11. What Was Not Built (and Why)

| Feature | Why not built |
|---|---|
| Authentication | Explicitly out of scope per challenge spec |
| Webhook / callback | Polling is sufficient for the challenge scope; webhook adds infrastructure |
| Horizontal scaling | `BackgroundTasks` is single-process; scope doesn't require it |
| PDF/image ingestion | Spec says "post-OCR" — raw text input only |
| Human review UI | `NEEDS_REVIEW` state exists; the UI to action it is out of scope |
| Confidence scoring | Architecturally straightforward; cut for time |
| Per-vendor template learning | Valuable for production; cut for time |
