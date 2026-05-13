# SME Smart Invoicer — POC Plan

AI-powered invoice management for micro-SMEs in India. Accepts vendor invoices (PDF, image, WhatsApp forward), extracts structured data via OCR + LLM, and surfaces a dashboard for tracking expenses, GST input credit, and payment due dates.

## 1. Problem & Target User

**User**: Micro-SME proprietor (1–20 employees) — kirana stores, small distributors, service businesses, D2C sellers. Receives 20–200 vendor invoices/month across paper, PDF email attachments, and WhatsApp images.

**Pain points**:
- Manual data entry into Tally/Excel; 5–10 min per invoice.
- GST input tax credit (ITC) leaks because invoices aren't reconciled with GSTR-2B.
- No visibility into payment due dates → late payment penalties or strained vendor relationships.
- Multi-language invoices (English + Hindi/regional script in vendor names, addresses).

**POC scope**: Single-tenant web app. Upload → extract → review → store → dashboard. Out of scope for POC: GSTR filing, full multi-tenant SaaS, mobile app.

## 2. Architecture

```
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  Web UI     │───▶│  FastAPI     │───▶│ Postgres        │
│ (React+Vite)│    │  (REST)      │    │ (invoices,      │
└─────────────┘    └──────┬───────┘    │  vendors, items)│
       ▲                  │            └─────────────────┘
       │                  ▼
       │           ┌──────────────┐    ┌─────────────────┐
       │           │ Celery worker│───▶│ Local FS / S3   │
       │           │ (async OCR)  │    │ (invoice files) │
       │           └──────┬───────┘    └─────────────────┘
       │                  │
       │                  ▼
       │           ┌──────────────┐
       │           │ OCR pipeline │
       │           │  EasyOCR     │
       │           │  + Claude    │
       │           │  (extraction)│
       │           └──────────────┘
       │
       └─── WebSocket / poll for extraction status
```

**Why this shape**:
- FastAPI gives async I/O and auto-OpenAPI docs, good fit for upload + status polling.
- OCR is slow (1–10s/page), so it's pushed to Celery with Redis broker — UI stays responsive.
- Postgres holds normalized invoice + line-item data; raw OCR JSON and confidence scores live in JSONB columns for debugging.
- Files stored locally for POC; abstract storage layer so S3 swap is one-line.

## 3. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.11, FastAPI, Pydantic v2 | Async, strong typing, fast iteration |
| Worker | Celery + Redis | Mature, simple for async OCR jobs |
| DB | PostgreSQL 16 (JSONB for raw OCR) | Relational data + flexible OCR payload |
| OCR (primary) | EasyOCR | Better than Tesseract on photos, supports Hindi+Devanagari |
| OCR (fallback) | pytesseract | Lightweight, fast on clean PDFs |
| Structuring | Claude (Sonnet 4.6 via Anthropic SDK) | Robust JSON extraction from messy OCR text |
| PDF parsing | pdfplumber (text PDFs), pdf2image (scanned) | Skip OCR when PDF has embedded text |
| Frontend | React 18 + Vite + Tailwind + shadcn/ui | Fast scaffold, clean UI primitives |
| Auth | FastAPI-Users + JWT (single-tenant for POC) | Minimal viable auth |
| Deployment | Docker Compose (POC), Fly.io / Railway (demo) | One-command local; easy demo URL |

### OCR choice: EasyOCR vs Tesseract

| | pytesseract | EasyOCR | PaddleOCR |
|---|---|---|---|
| Accuracy on clean PDFs | High | High | High |
| Accuracy on phone photos | Low–Medium | Medium–High | High |
| Hindi/Devanagari | Yes (extra training data) | Yes (built-in) | Yes |
| Speed (CPU) | ~0.5s/page | ~3–5s/page | ~3–5s/page |
| Setup complexity | Low (system binary) | Medium (PyTorch) | Medium |
| Layout/rotated text | Weak | Good | Best |

**Decision**: EasyOCR as primary (covers the WhatsApp-photo case which is 60%+ of micro-SME invoices); pytesseract as fast path for text-extractable PDFs. Document AI / Textract is a Phase-3 plug-in for users willing to pay for higher accuracy.

### Why LLM for extraction, not regex

Indian invoices have no standard layout. Vendor names, GSTINs, HSN codes, and tax breakdowns appear in arbitrary positions. Regex/template approaches need per-vendor tuning. A constrained-output LLM call (Claude with a Pydantic schema in the prompt) gives 90%+ field accuracy out of the box and gracefully handles unseen layouts. Cost: ~₹0.10–0.30 per invoice with prompt caching on the schema/few-shot examples.

## 4. OCR Pipeline Design

```
Upload (PDF/JPG/PNG)
   │
   ▼
┌─────────────────────┐
│ 1. Ingest           │  Validate type, dedupe by file hash,
│                     │  store original, create Invoice row
│                     │  (status=PENDING)
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 2. Pre-process      │  PDF → text via pdfplumber (skip OCR
│                     │  if extractable). Else: pdf2image →
│                     │  deskew, denoise (OpenCV), upscale.
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 3. OCR              │  EasyOCR (en + hi). Output: list of
│                     │  (text, bbox, confidence). Persist
│                     │  raw OCR JSON to invoices.raw_ocr.
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 4. LLM extraction   │  Claude call with system prompt =
│                     │  "Extract invoice fields per schema",
│                     │  user = concatenated OCR text +
│                     │  bbox hints. Output: validated
│                     │  Pydantic InvoiceExtraction model.
│                     │  Cache the schema/few-shot block.
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 5. Post-validate    │  GSTIN checksum, HSN format check,
│                     │  tax math (subtotal + tax = total),
│                     │  date sanity. Flag low-confidence
│                     │  fields for human review.
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 6. Persist & notify │  Write Invoice + LineItems. Set
│                     │  status=NEEDS_REVIEW if any flag,
│                     │  else EXTRACTED. WebSocket push.
└─────────────────────┘
```

**Confidence model**: Two signals merged into a per-field score (0–1):
- OCR confidence from EasyOCR (per token, averaged over the field's bbox).
- LLM self-reported confidence (ask for it in the JSON output).

Fields below 0.7 get yellow-highlighted in the review UI.

**Failure modes & handling**:
- OCR returns garbage (blurry photo) → status=OCR_FAILED, prompt user to re-upload.
- LLM returns invalid JSON → retry once with stricter prompt; on second failure, surface raw text to user.
- Duplicate invoice (same hash, or same vendor+invoice_number) → flag, don't re-process.

## 5. Invoice Data Model

```sql
-- Vendor: deduped on GSTIN (or normalized name if no GSTIN)
vendors (
  id UUID PK,
  name TEXT NOT NULL,
  gstin TEXT UNIQUE,            -- 15-char GSTIN
  pan TEXT,
  address TEXT,
  state_code TEXT,              -- first 2 digits of GSTIN
  payment_terms_days INT,
  created_at TIMESTAMPTZ
)

invoices (
  id UUID PK,
  vendor_id UUID FK,
  invoice_number TEXT,
  invoice_date DATE,
  due_date DATE,                -- inferred from terms if absent
  place_of_supply TEXT,         -- state code, drives CGST/SGST vs IGST

  subtotal NUMERIC(14,2),
  cgst NUMERIC(14,2),
  sgst NUMERIC(14,2),
  igst NUMERIC(14,2),
  cess NUMERIC(14,2),
  total NUMERIC(14,2),
  round_off NUMERIC(14,2),

  currency TEXT DEFAULT 'INR',
  status TEXT,                  -- PENDING, NEEDS_REVIEW, EXTRACTED, APPROVED, PAID
  payment_status TEXT,          -- UNPAID, PARTIAL, PAID
  paid_amount NUMERIC(14,2) DEFAULT 0,

  file_path TEXT,
  file_hash TEXT UNIQUE,
  raw_ocr JSONB,                -- full EasyOCR output for debugging
  extraction JSONB,             -- full LLM output incl. per-field confidence
  review_notes TEXT,

  created_at TIMESTAMPTZ,
  approved_at TIMESTAMPTZ,
  approved_by UUID
)

line_items (
  id UUID PK,
  invoice_id UUID FK,
  line_no INT,
  description TEXT,
  hsn_code TEXT,                -- 4-8 digit HSN/SAC
  quantity NUMERIC(14,3),
  unit TEXT,
  rate NUMERIC(14,2),
  discount NUMERIC(14,2),
  taxable_value NUMERIC(14,2),
  gst_rate NUMERIC(5,2),        -- 0, 5, 12, 18, 28
  cgst NUMERIC(14,2),
  sgst NUMERIC(14,2),
  igst NUMERIC(14,2),
  total NUMERIC(14,2)
)

payments (
  id UUID PK,
  invoice_id UUID FK,
  amount NUMERIC(14,2),
  paid_at DATE,
  method TEXT,                  -- UPI, NEFT, CASH, CHEQUE
  reference TEXT,               -- UTR / cheque number
  notes TEXT
)
```

GSTIN structure used to infer state_code and determine intra- vs inter-state (CGST+SGST vs IGST). This is checked against the LLM's extracted tax breakdown as a validation step.

## 6. Dashboard Features

**Phase-1 (POC must-have)**:
1. **Upload** — drag/drop or click; multi-file; shows live extraction status.
2. **Inbox** — list of invoices with filters (status, vendor, date range, amount); badges for NEEDS_REVIEW.
3. **Review pane** — side-by-side: original image/PDF on left, editable extracted fields on right, low-confidence fields highlighted, save → APPROVED.
4. **Vendor list** — auto-built from extractions; click into a vendor to see all their invoices and total spend.
5. **Dashboard cards** — total payable this month, overdue count, GST input credit accrued (CGST+SGST+IGST sum on APPROVED invoices), top 5 vendors by spend.
6. **Export** — CSV/Excel of invoices + line items (Tally-compatible column layout).

**Phase-2 (nice-to-have)**:
7. Payment due calendar with reminders.
8. Mark-as-paid workflow with UTR capture.
9. GSTR-2B reconciliation (upload GSTR-2B JSON, match invoices, flag mismatches).
10. Bulk approve.

**Phase-3 (post-POC)**:
11. WhatsApp Business inbox — forward an invoice photo to a number, it lands in the app.
12. Payment gateway integration — one-click pay-vendor from approved invoice.
13. Tally/Zoho Books direct sync.

## 7. Implementation Roadmap

Calibrated for one engineer, ~2 weeks to demo-able POC.

**Week 1 — Pipeline & data layer**
- Day 1: Repo scaffold (FastAPI + Postgres + Celery + Redis via Docker Compose). DB schema migrations (Alembic). Pydantic models.
- Day 2: Upload endpoint, file storage abstraction, file-hash dedup, Invoice row creation.
- Day 3: OCR worker — EasyOCR integration, pdfplumber path for text PDFs, OpenCV preprocessing.
- Day 4: LLM extraction step — Claude SDK with cached schema/few-shot prompt, Pydantic-validated output, retry on JSON parse failure.
- Day 5: Post-validation (GSTIN checksum, tax math), confidence scoring, status state machine.

**Week 2 — UI & polish**
- Day 6: React scaffold (Vite + Tailwind + shadcn), auth, upload UI with status polling.
- Day 7: Inbox + filters; vendor list auto-built from invoices.
- Day 8: Review pane (PDF.js for preview, editable fields, save).
- Day 9: Dashboard cards, CSV export.
- Day 10: Seed data, demo script, README, deploy to Fly.io.

**Buffer (Week 3)**: GSTR-2B upload + reconciliation, payment-due calendar, polish.

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| EasyOCR accuracy on bad phone photos | Add preprocessing (deskew, contrast); offer Document AI fallback as paid tier |
| LLM hallucinated fields | Always show source image alongside extraction; require explicit user approval; never auto-post to accounting |
| GSTIN/HSN regulatory drift | Validate via official GSTN API in Phase 3; for POC use checksum + format only |
| Hindi/regional script accuracy | EasyOCR `hi` model; test on 20 real Hindi-bilingual invoices before claiming support |
| Per-invoice LLM cost at scale | Prompt caching for schema (90%+ savings); batch via Anthropic Batch API for non-realtime backlog |

**Open questions for product**:
- Is payment gateway integration (pay vendor with one click) part of the POC demo, or a Phase-3 hook?
- Single-user POC or multi-user from day one?
- Do we need a mobile capture flow, or is "WhatsApp forward to a number" sufficient for the mobile use case?

## 9. Success Metrics for POC

- Process 50 real invoices end-to-end with ≥90% field accuracy on header fields (vendor, GSTIN, total, date) and ≥80% on line items.
- Median extraction time under 15s per invoice.
- Reviewer can approve a typical invoice in under 30 seconds.
- Zero data-loss bugs (file always recoverable from storage).
