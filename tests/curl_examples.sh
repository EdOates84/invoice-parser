#!/usr/bin/env bash
# Quick curl commands for manual testing.
# Run from repo root. Requires jq for pretty output.
# Usage: bash tests/curl_examples.sh

BASE="http://localhost:8000"

echo "=== TC01: Happy Path ==="
REQ=$(curl -s -X POST "$BASE/invoices" \
  -H "Content-Type: application/json" \
  -d '{"document_text": "INVOICE\n\nFrom: TechCorp Solutions Ltd.\n\nInvoice Number: INV-2024-0847\nInvoice Date: 2024-11-15\nCurrency: USD\n\nDESCRIPTION                    QTY    UNIT PRICE    AMOUNT\nWeb Development Services       40     125.00         5,000.00\nUI/UX Design                   20      95.00         1,900.00\nServer Configuration Setup      1     450.00           450.00\nCode Review & QA               16      85.00         1,360.00\n\nSubtotal: $8,710.00\nTax (8%): $696.80\nTOTAL DUE: $9,406.80"}')
echo "$REQ" | jq .
ID=$(echo "$REQ" | jq -r .request_id)
echo "Polling $ID ..."
sleep 5
curl -s "$BASE/invoices/$ID" | jq '{status, attempt_count, extraction_method, validation: .validation_checks.all_passed}'

echo ""
echo "=== TC02: Line Item Math Error (triggers retry) ==="
REQ=$(curl -s -X POST "$BASE/invoices" \
  -H "Content-Type: application/json" \
  -d '{"document_text": "INVOICE\n\nInvoice No: INV-2024-0293\nDate: 2024-11-20\nCurrency: USD\n\nITEM                    QTY    UNIT PRICE    AMOUNT\nErgonomic Laptop Stand   3      45.00         125.00\n4-Port USB Hub           2      30.00          60.00\nHDMI Monitor Cable       4      15.00          60.00\nWireless Mouse           1      55.00          55.00\n\nSub Total: $300.00\nGST (10%): $30.00\nTotal: $330.00"}')
echo "$REQ" | jq .
ID=$(echo "$REQ" | jq -r .request_id)
echo "Polling $ID (may take longer due to retries)..."
sleep 15
curl -s "$BASE/invoices/$ID" | jq '{status, attempt_count, checks: [.validation_checks.checks[] | {name, passed, delta}]}'

echo ""
echo "=== TC10: Garbage Input ==="
REQ=$(curl -s -X POST "$BASE/invoices" \
  -H "Content-Type: application/json" \
  -d '{"document_text": "Meeting Notes - Product Team Standup\nNovember 15 2024\n\nAttendees: Alice Bob Carlos\nDiscussion: sprint retrospective WIP limits feature deployment coffee machine holiday party planning"}')
echo "$REQ" | jq .
ID=$(echo "$REQ" | jq -r .request_id)
sleep 10
curl -s "$BASE/invoices/$ID" | jq '{status, attempt_count, error}'

echo ""
echo "=== TC11: Duplicate Submission (submit TC01 invoice number again) ==="
REQ=$(curl -s -X POST "$BASE/invoices" \
  -H "Content-Type: application/json" \
  -d '{"document_text": "INVOICE COPY\n\nTechCorp Solutions Ltd.\n\nInvoice #: INV-2024-0847\nDate: 15-Nov-2024\nCurrency: USD\n\nWeb Development Services    40hrs @ $125.00    $5,000.00\nUI/UX Design               20hrs @ $95.00     $1,900.00\nServer Configuration        1 @ $450.00          $450.00\nCode Review & QA           16hrs @ $85.00     $1,360.00\n\nSubtotal: $8,710.00\nTax (8%): $696.80\nTotal: $9,406.80"}')
echo "$REQ" | jq '{request_id, status, duplicate}'

echo ""
echo "=== Service health ==="
curl -s "$BASE/health" | jq . 2>/dev/null || curl -s "$BASE/" | jq .
