#!/usr/bin/env python3
"""
Dataset runner — processes all invoices directly via extractor + validator.
No server required. Falls through to heuristic if no LLM key is configured.

Usage:
    python tests/run_dataset.py
    python tests/run_dataset.py --category happy_path
    python tests/run_dataset.py --category broken_ocr
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Make sure app package is importable from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.extractor import extract_with_fallback
from app.validator import report_to_dict, run_checks
from tests.invoice_dataset import ALL_INVOICES

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def ok(m):   return f"{GREEN}{m}{RESET}"
def fail(m): return f"{RED}{m}{RESET}"
def warn(m): return f"{YELLOW}{m}{RESET}"
def info(m): return f"{CYAN}{m}{RESET}"
def dim(m):  return f"{DIM}{m}{RESET}"

# ── Field comparison ──────────────────────────────────────────────────────────

NUMERIC_TOL = 0.02  # 2 cents for expected-field comparisons

def _cmp_field(key: str, extracted: Any, expected: Any) -> tuple[str, bool]:
    """Returns (display_string, passed)."""
    if expected is None:
        # We expect null — check if extracted is also null
        if extracted is None:
            return ok(f"{key}=null ✓"), True
        return warn(f"{key}={extracted} (expected null)"), False
    if extracted is None:
        return fail(f"{key}=null (expected {expected})"), False
    if isinstance(expected, float):
        try:
            delta = abs(float(extracted) - expected)
            if delta <= NUMERIC_TOL:
                return ok(f"{key}={extracted} ✓"), True
            return fail(f"{key}={extracted} (expected {expected}, Δ={delta:.4f})"), False
        except (TypeError, ValueError):
            return fail(f"{key}={extracted!r} (expected {expected})"), False
    if isinstance(expected, str):
        if str(extracted).strip() == expected.strip():
            return ok(f"{key}={extracted!r} ✓"), True
        return fail(f"{key}={extracted!r} (expected {expected!r})"), False
    # int (line_items_count)
    if isinstance(expected, int):
        if int(extracted) == expected:
            return ok(f"{key}={extracted} ✓"), True
        return fail(f"{key}={extracted} (expected {expected})"), False
    return info(f"{key}={extracted}"), True


def _check_expected(result_dict: Optional[dict], expected: dict) -> tuple[list[str], int, int]:
    """Compare extracted result against expected spec. Returns (lines, passed, total)."""
    lines = []
    passed = total = 0

    if result_dict is None:
        for key in ("invoice_number", "invoice_date", "currency",
                    "subtotal", "tax_total", "total"):
            if key in expected and expected[key] is not None:
                lines.append(fail(f"  {key}: no result extracted"))
                total += 1
        return lines, passed, total

    r = result_dict
    field_map = {
        "invoice_number": r.get("invoice_number"),
        "invoice_date":   r.get("invoice_date"),
        "currency":       r.get("currency"),
        "subtotal":       r.get("subtotal"),
        "tax_total":      r.get("tax_total"),
        "total":          r.get("total"),
    }

    for key, exp_val in expected.items():
        if key in ("line_items_count", "should_fail_validation",
                   "failing_check", "notes_for_reviewer",
                   "invoice_date_ambiguous"):
            continue
        if key not in field_map:
            continue
        total += 1
        line, ok_flag = _cmp_field(key, field_map[key], exp_val)
        lines.append(f"  {line}")
        if ok_flag:
            passed += 1

    # line_items_count
    if "line_items_count" in expected:
        exp_count = expected["line_items_count"]
        got_count = len(r.get("line_items") or [])
        total += 1
        line, ok_flag = _cmp_field("line_items_count", got_count, exp_count)
        lines.append(f"  {line}")
        if ok_flag:
            passed += 1

    return lines, passed, total


# ── Per-invoice runner ────────────────────────────────────────────────────────

def run_one(inv: dict) -> dict:
    """Run extraction + validation on one invoice dict. Returns summary dict."""
    t0 = time.perf_counter()
    try:
        extraction, method, model = extract_with_fallback(
            inv["text"], None, settings
        )
    except Exception as exc:
        return {
            "name": inv["name"],
            "category": inv["category"],
            "method": "error",
            "model": None,
            "result": None,
            "validation": None,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "field_passed": 0,
            "field_total": 0,
            "val_all_passed": None,
        }

    if extraction is None:
        return {
            "name": inv["name"],
            "category": inv["category"],
            "method": method,
            "model": model,
            "result": None,
            "validation": None,
            "error": "All extraction tiers returned None",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "field_passed": 0,
            "field_total": 0,
            "val_all_passed": None,
        }

    report = run_checks(extraction, tolerance=settings.tolerance)
    val_dict = report_to_dict(report)

    expected = inv.get("expected", {})
    result_dict = extraction.model_dump()
    _, field_passed, field_total = _check_expected(result_dict, expected)

    return {
        "name": inv["name"],
        "category": inv["category"],
        "method": method,
        "model": model,
        "result": result_dict,
        "validation": val_dict,
        "error": None,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "field_passed": field_passed,
        "field_total": field_total,
        "val_all_passed": val_dict["all_passed"],
        "expected": expected,
    }


# ── Printer ───────────────────────────────────────────────────────────────────

def print_result(inv: dict, r: dict, index: int, total: int) -> None:
    idx_str = f"[{index:02d}/{total}]"
    cat = inv["category"]
    name = inv["name"]

    # Header
    print(f"\n{BOLD}{idx_str} [{cat}]{RESET} {name}")
    print(dim(f"  notes: {inv.get('notes', '')}"))

    method_str = r["method"]
    if r["model"]:
        method_str += f"/{r['model'].split('/')[-1]}"
    print(f"  tier: {info(method_str)}  latency: {r['latency_ms']}ms")

    if r["error"]:
        print(f"  {fail('ERROR: ' + r['error'])}")
        return

    result = r.get("result") or {}
    expected = r.get("expected") or {}

    # ── Extracted fields vs expected ─────────────────────────────────────────
    field_lines, fp, ft = _check_expected(result, expected)
    if ft > 0:
        frac = f"{fp}/{ft}"
        colour = ok if fp == ft else (warn if fp >= ft // 2 else fail)
        print(f"  fields: {colour(frac)} correct")
        for line in field_lines:
            print(line)
    else:
        # No expected spec (e.g. adversarial with notes_for_reviewer only)
        r_total = result.get("total")
        r_inv = result.get("invoice_number")
        print(f"  extracted: inv={r_inv!r}  total={r_total}  items={len(result.get('line_items') or [])}")

    # ── Validation checks ─────────────────────────────────────────────────────
    val = r.get("validation") or {}
    checks = val.get("checks", [])
    should_fail = expected.get("should_fail_validation", False)

    if not checks:
        print(f"  validation: {dim('no checks run (null/missing fields)')}")
    elif val.get("all_passed"):
        if should_fail is True:
            print(f"  validation: {warn('ALL PASSED — but source has a math error (expected failure)')}")
        else:
            print(f"  validation: {ok(f'all {len(checks)} checks passed')}")
    else:
        failed_names = [c["name"] for c in checks if not c["passed"]]
        if should_fail is True:
            print(f"  validation: {ok(f'correctly caught math error → {failed_names}')}")
        elif should_fail == "maybe":
            print(f"  validation: {warn(f'failed (ambiguous case) → {failed_names}')}")
        else:
            print(f"  validation: {fail(f'unexpected failure → {failed_names}')}")
        for c in checks:
            if not c["passed"]:
                print(f"    {warn(c['message'][:100])}")


# ── Category summary ──────────────────────────────────────────────────────────

def print_category_summary(results: list[dict]) -> None:
    from collections import defaultdict
    cats: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r)

    print(f"\n{'━'*68}")
    print(f"{BOLD}CATEGORY SUMMARY{RESET}")
    print(f"{'━'*68}")
    print(f"{'Category':<22} {'Count':>5}  {'Method breakdown':<30}  {'Fields'}")
    print(f"{'─'*68}")

    total_fp = total_ft = 0
    for cat, rs in sorted(cats.items()):
        methods: dict[str, int] = {}
        fp = ft = 0
        for r in rs:
            m = r.get("method", "error")
            methods[m] = methods.get(m, 0) + 1
            fp += r.get("field_passed", 0)
            ft += r.get("field_total", 0)
        total_fp += fp
        total_ft += ft
        meth_str = ", ".join(f"{m}×{n}" for m, n in sorted(methods.items()))
        field_str = f"{fp}/{ft}" if ft else "n/a"
        print(f"  {cat:<20} {len(rs):>5}  {meth_str:<30}  {field_str}")

    print(f"{'─'*68}")
    overall = f"{total_fp}/{total_ft}" if total_ft else "n/a"
    pct = f"({100*total_fp//total_ft}%)" if total_ft else ""
    print(f"  {'TOTAL':<20} {len(results):>5}  {'':30}  {overall} {pct}")


def print_validation_summary(results: list[dict], invoices: list[dict]) -> None:
    print(f"\n{'━'*68}")
    print(f"{BOLD}VALIDATION OUTCOMES{RESET}")
    print(f"{'━'*68}")

    rows = []
    for inv, r in zip(invoices, results):
        expected = inv.get("expected", {})
        should_fail = expected.get("should_fail_validation", False)
        val = r.get("validation") or {}
        all_passed = val.get("all_passed")
        has_checks = bool((val.get("checks") or []))

        if not has_checks:
            outcome = "skipped"
        elif all_passed and should_fail is True:
            outcome = "FALSE_PASS"  # validator missed a real error
        elif not all_passed and should_fail is True:
            outcome = "CORRECT_FAIL"
        elif all_passed and should_fail is False:
            outcome = "CORRECT_PASS"
        elif not all_passed and should_fail is False:
            outcome = "UNEXPECTED_FAIL"
        else:
            outcome = "ambiguous"
        rows.append((inv["category"], inv["name"], outcome))

    counts: dict[str, int] = {}
    for _, _, o in rows:
        counts[o] = counts.get(o, 0) + 1

    for outcome, n in sorted(counts.items()):
        colour = {
            "CORRECT_PASS": ok,
            "CORRECT_FAIL": ok,
            "FALSE_PASS": fail,
            "UNEXPECTED_FAIL": warn,
            "skipped": dim,
            "ambiguous": warn,
        }.get(outcome, info)
        print(f"  {colour(outcome):<30} {n:>3}")

    # List any unexpected failures
    unexpected = [(cat, name) for cat, name, o in rows if o == "UNEXPECTED_FAIL"]
    if unexpected:
        print(f"\n  {warn('Unexpected validation failures:')}")
        for cat, name in unexpected:
            print(f"    [{cat}] {name}")


def print_method_summary(results: list[dict]) -> None:
    print(f"\n{'━'*68}")
    print(f"{BOLD}EXTRACTION METHOD BREAKDOWN{RESET}")
    print(f"{'━'*68}")
    counts: dict[str, int] = {}
    for r in results:
        m = r.get("method", "error")
        counts[m] = counts.get(m, 0) + 1
    for m, n in sorted(counts.items(), key=lambda x: -x[1]):
        bar = "█" * n
        print(f"  {m:<15} {n:>3}  {bar}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Run invoice dataset through the extraction pipeline")
    parser.add_argument("--category", default=None, help="Filter to one category")
    parser.add_argument("--verbose", action="store_true", help="Show full extraction result JSON")
    args = parser.parse_args()

    invoices = ALL_INVOICES
    if args.category:
        invoices = [i for i in invoices if i["category"] == args.category]
        if not invoices:
            print(f"No invoices in category '{args.category}'")
            sys.exit(1)

    print(f"{BOLD}Invoice Parser — Dataset Runner{RESET}")
    print(f"Extractor tier: heuristic fallback (spaCy + regex — no LLM key needed)")
    print(f"Invoices: {len(invoices)}")
    print(f"Tolerance: {settings.tolerance}")

    results = []
    for i, inv in enumerate(invoices, 1):
        r = run_one(inv)
        results.append(r)
        print_result(inv, r, i, len(invoices))

    print_category_summary(results)
    print_validation_summary(results, invoices)
    print_method_summary(results)

    # Totals
    total_fp = sum(r.get("field_passed", 0) for r in results)
    total_ft = sum(r.get("field_total", 0) for r in results)
    errors = sum(1 for r in results if r.get("error"))
    print(f"\n{BOLD}Total field accuracy: {total_fp}/{total_ft}", end="")
    if total_ft:
        print(f" ({100*total_fp//total_ft}%)", end="")
    print(RESET)
    if errors:
        print(warn(f"Extraction errors: {errors}"))


if __name__ == "__main__":
    main()
