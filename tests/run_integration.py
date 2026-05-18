#!/usr/bin/env python3
"""
Integration test runner for the invoice parsing service.

Reads raw OCR text from tests/invoices/TC*.txt files.
Submits each as { "document_text": "..." } to POST /invoices.
Polls until terminal state, reports pass/fail.

Usage:
    python tests/run_integration.py
    python tests/run_integration.py --url http://localhost:8000
    python tests/run_integration.py --ids TC01,TC02,TC06
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

POLL_INTERVAL = 2
POLL_TIMEOUT  = 120
TERMINAL      = {"COMPLETED", "NEEDS_REVIEW", "FAILED"}

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(m):   return f"{GREEN}✓ {m}{RESET}"
def fail(m): return f"{RED}✗ {m}{RESET}"
def warn(m): return f"{YELLOW}⚠ {m}{RESET}"
def info(m): return f"{CYAN}  {m}{RESET}"


def strip_comments(text: str) -> str:
    """Remove comment lines starting with # before sending to API."""
    lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    return "\n".join(lines).strip()


def poll_until_terminal(base_url: str, request_id: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        r = requests.get(f"{base_url}/invoices/{request_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") in TERMINAL:
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{request_id} did not reach terminal state within {POLL_TIMEOUT}s")


def load_test_cases(invoices_dir: Path, filter_ids: Optional[list[str]]) -> list[dict]:
    """
    Load TC*.txt files from invoices_dir.
    Returns list of dicts with id, name, document_text, raw_text.
    """
    cases = []
    for path in sorted(invoices_dir.glob("TC*.txt")):
        tc_id = re.match(r"(TC\d+)", path.stem)
        if not tc_id:
            continue
        tc_id = tc_id.group(1)
        if filter_ids and tc_id not in filter_ids:
            continue
        raw = path.read_text(encoding="utf-8")
        cases.append({
            "id": tc_id,
            "name": path.stem,
            "raw_text": raw,
            "document_text": strip_comments(raw),
        })
    return cases


def run(base_url: str, cases: list[dict]) -> None:
    passed_total = 0
    failed_total = 0

    for tc in cases:
        tc_id = tc["id"]
        print(f"\n{BOLD}[{tc_id}]{RESET} {tc['name']}")

        # ── Submit ────────────────────────────────────────────────────────────
        try:
            r = requests.post(
                f"{base_url}/invoices",
                json={"document_text": tc["document_text"]},
                timeout=15,
            )
            r.raise_for_status()
            submit = r.json()
        except Exception as exc:
            print(fail(f"SUBMIT failed: {exc}"))
            failed_total += 1
            continue

        request_id = submit.get("request_id")
        duplicate  = submit.get("duplicate", False)
        print(info(f"request_id={request_id}  status={submit.get('status')}  duplicate={duplicate}"))

        # ── Poll ──────────────────────────────────────────────────────────────
        try:
            result = poll_until_terminal(base_url, request_id)
        except Exception as exc:
            print(fail(str(exc)))
            failed_total += 1
            continue

        status        = result.get("status")
        attempt_count = result.get("attempt_count", 1)
        method        = result.get("extraction_method")
        model         = result.get("extraction_model")

        print(info(f"status={status}  attempts={attempt_count}  method={method}/{model}"))

        # ── Print validation summary ──────────────────────────────────────────
        checks = (result.get("validation_checks") or {}).get("checks", [])
        if checks:
            passed_checks = [c["name"] for c in checks if c.get("passed")]
            failed_checks = [c["name"] for c in checks if not c.get("passed")]
            if failed_checks:
                print(warn(f"failed checks: {failed_checks}"))
                for c in checks:
                    if not c.get("passed"):
                        print(warn(f"  {c['message']}"))
            else:
                print(ok(f"all {len(passed_checks)} validation checks passed"))
        else:
            print(info("no validation checks (null extraction or no line items)"))

        # ── Print extraction fields ───────────────────────────────────────────
        if result.get("result"):
            r = result["result"]
            print(info(
                f"inv={r.get('invoice_number')}  "
                f"date={r.get('invoice_date')}  "
                f"cur={r.get('currency')}  "
                f"subtotal={r.get('subtotal')}  "
                f"tax={r.get('tax_total')}  "
                f"total={r.get('total')}  "
                f"items={len(r.get('line_items', []))}"
            ))

        if result.get("error"):
            print(warn(f"error: {result['error']}"))

        # ── Count pass/fail ───────────────────────────────────────────────────
        if status in ("COMPLETED", "NEEDS_REVIEW", "FAILED"):
            passed_total += 1
            print(ok(f"→ {tc_id} reached terminal state: {status}"))
        else:
            failed_total += 1
            print(fail(f"→ {tc_id} unexpected state: {status}"))

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed_total + failed_total
    print(f"\n{'─'*60}")
    print(f"{BOLD}Results: {passed_total}/{total} reached terminal state{RESET}")
    print("Review statuses above — NEEDS_REVIEW/FAILED may be expected for some cases.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",  default="http://localhost:8000")
    parser.add_argument("--ids",  default="", help="e.g. TC01,TC02")
    parser.add_argument("--dir",  default="tests/invoices", help="directory with TC*.txt files")
    args = parser.parse_args()

    invoices_dir = Path(args.dir)
    if not invoices_dir.exists():
        print(f"ERROR: {invoices_dir} not found")
        sys.exit(1)

    filter_ids = [x.strip() for x in args.ids.split(",") if x.strip()] or None
    cases = load_test_cases(invoices_dir, filter_ids)

    if not cases:
        print("No test cases found. Check --dir and --ids.")
        sys.exit(1)

    print(f"{BOLD}Invoice Parser — Integration Tests{RESET}")
    print(f"Service : {args.url}")
    print(f"Cases   : {[c['id'] for c in cases]}")

    run(args.url, cases)
