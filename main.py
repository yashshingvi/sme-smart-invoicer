"""FastAPI app for SME Smart Invoicer POC.

Endpoints:
  GET  /                      → dashboard HTML
  POST /api/invoices/upload   → accept image, kick OCR pipeline (background)
  GET  /api/invoices          → list invoices (filter by status, vendor)
  GET  /api/invoices/{id}     → invoice detail with line items
  POST /api/invoices/{id}/pay → mark invoice as paid
  GET  /api/vendors           → vendor list with spend totals
  GET  /api/dashboard         → aggregate cards
  GET  /api/invoices/{id}/status → cheap status poll
  GET  /uploads/{filename}    → serve original invoice image
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import models
import ocr

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".tif", ".tiff"}

app = FastAPI(title="SME Smart Invoicer", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    models.init_db()


# ---------- Static & root ----------

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


# ---------- Upload + pipeline ----------

def _run_pipeline(invoice_id: str, file_path: str) -> None:
    try:
        models.set_status(invoice_id, "PROCESSING")
        result = ocr.process_invoice(file_path)
        e = result.extraction

        vendor_id = None
        if e.get("vendor_name"):
            vendor_id = models.upsert_vendor(
                name=e["vendor_name"],
                gstin=e.get("gstin"),
                state_code=e.get("state_code"),
            )

        status = "NEEDS_REVIEW" if result.confidence < 0.7 else "EXTRACTED"
        if not e.get("total"):
            status = "NEEDS_REVIEW"

        models.update_extraction(
            invoice_id,
            vendor_id=vendor_id,
            extraction=e,
            raw_ocr_text=result.text,
            confidence=result.confidence,
            status=status,
        )
    except Exception as exc:  # pragma: no cover - defensive
        models.set_status(invoice_id, "OCR_FAILED", error=str(exc))


@app.post("/api/invoices/upload")
async def upload_invoice(file: UploadFile, background_tasks: BackgroundTasks) -> JSONResponse:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    file_hash = hashlib.sha256(data).hexdigest()

    existing = models.find_invoice_by_hash(file_hash)
    if existing:
        return JSONResponse(
            {"invoice_id": existing["id"], "status": existing["status"], "duplicate": True},
            status_code=200,
        )

    safe_name = f"{file_hash[:16]}{ext}"
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(data)

    invoice_id = models.create_invoice(file_path=f"/uploads/{safe_name}", file_hash=file_hash)
    background_tasks.add_task(_run_pipeline, invoice_id, str(dest))

    return JSONResponse({"invoice_id": invoice_id, "status": "PENDING", "duplicate": False}, status_code=201)


# ---------- Reads ----------

@app.get("/api/invoices")
def list_invoices(status: str | None = None, vendor_id: str | None = None, limit: int = 200):
    return {"invoices": models.list_invoices(status=status, vendor_id=vendor_id, limit=limit)}


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str):
    inv = models.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@app.get("/api/invoices/{invoice_id}/status")
def invoice_status(invoice_id: str):
    inv = models.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {
        "id": inv["id"],
        "status": inv["status"],
        "confidence": inv["confidence"],
        "error": inv.get("error"),
    }


@app.post("/api/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str):
    inv = models.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    models.mark_paid(invoice_id)
    return {"id": invoice_id, "payment_status": "PAID"}


@app.get("/api/vendors")
def list_vendors():
    return {"vendors": models.list_vendors()}


@app.get("/api/dashboard")
def dashboard():
    return models.dashboard_stats()


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- Dev entrypoint ----------

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
