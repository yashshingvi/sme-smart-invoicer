# SME Smart Invoicer — POC

AI-powered invoice intake for Indian micro-SMEs. Upload an invoice photo or PDF,
get back structured fields (vendor, GSTIN, totals, line items), and track them
on a dashboard.

See [PLAN.md](./PLAN.md) for the full product/architecture rationale. This
README covers running the POC.

## Stack (POC scope)

- **FastAPI + Uvicorn** — REST API and background OCR jobs.
- **SQLite (stdlib `sqlite3`)** — single-file DB at `invoices.db`.
- **pytesseract + Pillow** — OCR primary path. If the Tesseract binary isn't
  installed, the pipeline gracefully falls back to deterministic mock
  extraction so the POC stays demoable on a fresh laptop.
- **Single-file HTML/JS dashboard** — no React build step; served straight from
  FastAPI at `/`.

The full production design (Postgres, Celery, EasyOCR, Claude extraction, React
frontend) is described in `PLAN.md`. This POC keeps the same shape but
substitutes the heavyweight pieces for stdlib-friendly replacements.

## Files

```
main.py        FastAPI app: upload, list, detail, status, pay, dashboard.
ocr.py         pytesseract + heuristic field extraction. Mock fallback.
models.py      SQLite schema + CRUD (no ORM).
seed.py        Generates sample invoice PNGs and seeds the DB.
index.html     Dashboard UI (vanilla JS).
samples/       Generated sample invoice images.
uploads/       Stored uploaded invoice files (also served at /uploads/...).
invoices.db    SQLite database (created on startup).
```

## Quick start

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. (optional) Install Tesseract for real OCR
#    macOS:   brew install tesseract
#    Ubuntu:  sudo apt-get install tesseract-ocr
#    If skipped, the pipeline uses mock extraction — everything still works.

# 3. Seed sample data
python seed.py

# 4. Run the app
python main.py
#    or: uvicorn main:app --reload

# 5. Open http://localhost:8000
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/`                              | Dashboard HTML |
| `POST` | `/api/invoices/upload`           | Upload one invoice (multipart `file`) |
| `GET`  | `/api/invoices`                  | List invoices (filters: `status`, `vendor_id`) |
| `GET`  | `/api/invoices/{id}`             | Invoice detail + line items |
| `GET`  | `/api/invoices/{id}/status`      | Cheap status poll while OCR runs |
| `POST` | `/api/invoices/{id}/pay`         | Mark invoice as paid |
| `GET`  | `/api/vendors`                   | Vendor list with invoice counts and spend |
| `GET`  | `/api/dashboard`                 | Aggregate cards (totals, ITC, top vendors) |
| `GET`  | `/uploads/{filename}`            | Serve original invoice image |

OpenAPI docs are live at `/docs` once the server is running.

## Pipeline

```
upload  →  store file + hash-dedupe  →  background OCR  →  heuristic extraction
        →  upsert vendor + invoice rows  →  status = EXTRACTED | NEEDS_REVIEW
```

Confidence under 0.7 (or missing total) routes the invoice to `NEEDS_REVIEW`.
The dashboard shows the count; clicking a row opens a side-by-side review with
the original image. In the full design (PLAN.md §4) this step is a Claude LLM
call with a Pydantic schema; the POC uses regex heuristics to keep things
offline and free.

## Limitations (POC scope)

- No auth — single-tenant local demo.
- No PDF text path (`pdfplumber` / `pdf2image`); PDFs are accepted but routed
  through the same image-OCR pipeline and may extract poorly.
- No LLM call — extraction is regex-based and won't handle unusual layouts.
- No Celery — OCR runs in FastAPI's `BackgroundTasks`. Fine for a few uploads
  at a time; not for production load.

These are deliberate cuts; the production path is in `PLAN.md`.
