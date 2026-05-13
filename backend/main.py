"""FastAPI app: invoice CRUD + OCR upload + dashboard stats."""
from __future__ import annotations

import hashlib
import shutil
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, joinedload

from . import models, schemas, ocr
from .database import SessionLocal, engine, init_db, get_db

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"}

app = FastAPI(title="SME Smart Invoicer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ----------------- helpers -----------------

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_or_create_vendor(
    db: Session, name: Optional[str], gstin: Optional[str], address: Optional[str]
) -> Optional[models.Vendor]:
    if not name and not gstin:
        return None

    vendor = None
    if gstin:
        vendor = db.query(models.Vendor).filter(models.Vendor.gstin == gstin).first()
    if not vendor and name:
        vendor = (
            db.query(models.Vendor)
            .filter(func.lower(models.Vendor.name) == name.lower())
            .first()
        )
    if vendor:
        return vendor

    vendor = models.Vendor(
        name=name or "Unknown Vendor",
        gstin=gstin,
        address=address,
        state_code=gstin[:2] if gstin and len(gstin) >= 2 else None,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    try:
        return datetime.fromisoformat(str(s)).date()
    except ValueError:
        return None


def _apply_extraction(db: Session, invoice: models.Invoice, extraction: dict) -> None:
    vendor = _get_or_create_vendor(
        db,
        extraction.get("vendor_name"),
        extraction.get("vendor_gstin"),
        extraction.get("vendor_address"),
    )
    if vendor:
        invoice.vendor_id = vendor.id

    invoice.invoice_number = extraction.get("invoice_number")
    invoice.invoice_date = _parse_date(extraction.get("invoice_date"))
    invoice.due_date = _parse_date(extraction.get("due_date"))
    invoice.place_of_supply = extraction.get("place_of_supply")

    invoice.subtotal = float(extraction.get("subtotal") or 0)
    invoice.cgst = float(extraction.get("cgst") or 0)
    invoice.sgst = float(extraction.get("sgst") or 0)
    invoice.igst = float(extraction.get("igst") or 0)
    invoice.cess = float(extraction.get("cess") or 0)
    invoice.total = float(extraction.get("total") or 0)
    invoice.round_off = float(extraction.get("round_off") or 0)
    invoice.currency = extraction.get("currency") or "INR"
    invoice.extraction = extraction

    # If no explicit due_date, infer from invoice_date + vendor terms
    if not invoice.due_date and invoice.invoice_date and vendor:
        invoice.due_date = invoice.invoice_date + timedelta(
            days=vendor.payment_terms_days or 30
        )

    # Replace line items
    invoice.line_items.clear()
    for idx, li in enumerate(extraction.get("line_items") or [], start=1):
        invoice.line_items.append(
            models.LineItem(
                line_no=idx,
                description=li.get("description"),
                hsn_code=li.get("hsn_code"),
                quantity=float(li.get("quantity") or 1),
                unit=li.get("unit"),
                rate=float(li.get("rate") or 0),
                taxable_value=float(li.get("taxable_value") or 0),
                gst_rate=float(li.get("gst_rate") or 18),
                total=float(li.get("total") or 0),
            )
        )

    # Status logic
    confidence = float(extraction.get("confidence") or 0)
    if confidence < 0.7 or invoice.total == 0 or not invoice.invoice_number:
        invoice.status = "NEEDS_REVIEW"
    else:
        invoice.status = "EXTRACTED"


# ----------------- routes: invoices -----------------

@app.post("/api/invoices/upload", response_model=schemas.InvoiceOut)
async def upload_invoice(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / unique_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    fhash = _file_hash(dest)
    existing = (
        db.query(models.Invoice).filter(models.Invoice.file_hash == fhash).first()
    )
    if existing:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            409,
            detail={
                "message": "Duplicate invoice (same content already uploaded)",
                "invoice_id": existing.id,
            },
        )

    invoice = models.Invoice(
        file_path=str(dest.relative_to(BASE_DIR)),
        file_hash=fhash,
        status="PENDING",
    )
    db.add(invoice)
    db.flush()

    raw_text = ocr.extract_text(dest)
    invoice.raw_ocr_text = raw_text

    if not raw_text.strip():
        invoice.status = "OCR_FAILED"
        db.commit()
        db.refresh(invoice)
        return invoice

    extraction = ocr.extract_structured(raw_text)
    _apply_extraction(db, invoice, extraction)

    db.commit()
    db.refresh(invoice)
    return invoice


@app.post("/api/ocr/preview", response_model=schemas.OcrPreviewResponse)
async def ocr_preview(file: UploadFile = File(...)):
    """Run OCR + extraction without persisting. Useful for testing."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    tmp = UPLOAD_DIR / f"_preview_{uuid.uuid4().hex}{ext}"
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        raw = ocr.extract_text(tmp)
        extracted = ocr.extract_structured(raw) if raw.strip() else {}
        return schemas.OcrPreviewResponse(raw_text=raw, extraction=extracted)
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/invoices", response_model=List[schemas.InvoiceListItem])
def list_invoices(
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    vendor_id: Optional[str] = None,
    q: Optional[str] = Query(None, description="search by invoice number or vendor name"),
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(models.Invoice).options(joinedload(models.Invoice.vendor))
    if status:
        query = query.filter(models.Invoice.status == status)
    if payment_status:
        query = query.filter(models.Invoice.payment_status == payment_status)
    if vendor_id:
        query = query.filter(models.Invoice.vendor_id == vendor_id)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(models.Vendor).filter(
            (models.Invoice.invoice_number.ilike(like))
            | (models.Vendor.name.ilike(like))
        )
    query = query.order_by(desc(models.Invoice.created_at)).offset(offset).limit(limit)
    return query.all()


@app.get("/api/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)):
    invoice = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.vendor), joinedload(models.Invoice.line_items))
        .filter(models.Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return invoice


@app.put("/api/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def update_invoice(
    invoice_id: str,
    payload: schemas.InvoiceUpdate,
    db: Session = Depends(get_db),
):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    if payload.vendor_name or payload.vendor_gstin:
        vendor = _get_or_create_vendor(db, payload.vendor_name, payload.vendor_gstin, None)
        if vendor:
            invoice.vendor_id = vendor.id

    for field in (
        "invoice_number", "invoice_date", "due_date", "place_of_supply",
        "subtotal", "cgst", "sgst", "igst", "cess", "total", "round_off",
        "currency", "review_notes",
    ):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(invoice, field, val)

    if payload.status:
        invoice.status = payload.status
        if payload.status == "APPROVED" and not invoice.approved_at:
            invoice.approved_at = datetime.utcnow()
    if payload.payment_status:
        invoice.payment_status = payload.payment_status
    if payload.paid_amount is not None:
        invoice.paid_amount = payload.paid_amount
        if invoice.paid_amount >= invoice.total and invoice.total > 0:
            invoice.payment_status = "PAID"
        elif invoice.paid_amount > 0:
            invoice.payment_status = "PARTIAL"

    if payload.line_items is not None:
        invoice.line_items.clear()
        for idx, li in enumerate(payload.line_items, start=1):
            invoice.line_items.append(
                models.LineItem(
                    line_no=idx,
                    description=li.description,
                    hsn_code=li.hsn_code,
                    quantity=li.quantity,
                    unit=li.unit,
                    rate=li.rate,
                    discount=li.discount,
                    taxable_value=li.taxable_value,
                    gst_rate=li.gst_rate,
                    total=li.total,
                )
            )

    db.commit()
    db.refresh(invoice)
    return invoice


@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: str, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.file_path:
        fp = BASE_DIR / invoice.file_path
        fp.unlink(missing_ok=True)
    db.delete(invoice)
    db.commit()
    return {"ok": True}


@app.get("/api/invoices/{invoice_id}/file")
def get_invoice_file(invoice_id: str, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice or not invoice.file_path:
        raise HTTPException(404, "File not found")
    full_path = BASE_DIR / invoice.file_path
    if not full_path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(full_path)


# ----------------- routes: vendors -----------------

@app.get("/api/vendors", response_model=List[schemas.VendorOut])
def list_vendors(db: Session = Depends(get_db)):
    return db.query(models.Vendor).order_by(models.Vendor.name).all()


# ----------------- routes: dashboard -----------------

@app.get("/api/dashboard/stats", response_model=schemas.DashboardStats)
def dashboard_stats(db: Session = Depends(get_db)):
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.vendor))
        .all()
    )
    today = date.today()
    total_invoices = len(invoices)
    total_payable = sum(
        (inv.total - inv.paid_amount) for inv in invoices
        if inv.payment_status != "PAID" and inv.status in ("EXTRACTED", "APPROVED", "NEEDS_REVIEW")
    )
    overdue = [
        inv for inv in invoices
        if inv.due_date and inv.due_date < today and inv.payment_status != "PAID"
    ]
    overdue_count = len(overdue)
    overdue_amount = sum(inv.total - inv.paid_amount for inv in overdue)
    gst_input_credit = sum(
        (inv.cgst + inv.sgst + inv.igst + inv.cess) for inv in invoices
        if inv.status == "APPROVED"
    )
    needs_review = sum(1 for inv in invoices if inv.status == "NEEDS_REVIEW")

    # Top 5 vendors by total spend
    spend = defaultdict(float)
    names = {}
    for inv in invoices:
        if inv.vendor:
            spend[inv.vendor.id] += inv.total
            names[inv.vendor.id] = inv.vendor.name
    top_vendors = sorted(spend.items(), key=lambda x: x[1], reverse=True)[:5]
    top_vendors_list = [
        {"vendor_id": vid, "name": names[vid], "total": round(amt, 2)}
        for vid, amt in top_vendors
    ]

    # Monthly spend for last 6 months
    monthly = defaultdict(float)
    for inv in invoices:
        if inv.invoice_date:
            key = inv.invoice_date.strftime("%Y-%m")
            monthly[key] += inv.total
    monthly_list = [
        {"month": k, "total": round(v, 2)}
        for k, v in sorted(monthly.items())
    ][-6:]

    return schemas.DashboardStats(
        total_invoices=total_invoices,
        total_payable=round(total_payable, 2),
        overdue_count=overdue_count,
        overdue_amount=round(overdue_amount, 2),
        gst_input_credit=round(gst_input_credit, 2),
        needs_review=needs_review,
        top_vendors=top_vendors_list,
        monthly_spend=monthly_list,
    )


# ----------------- routes: export -----------------

@app.get("/api/export/csv")
def export_csv(db: Session = Depends(get_db)):
    import csv, io
    invoices = (
        db.query(models.Invoice)
        .options(joinedload(models.Invoice.vendor))
        .order_by(models.Invoice.invoice_date.desc().nullslast())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Invoice #", "Date", "Due Date", "Vendor", "GSTIN",
        "Subtotal", "CGST", "SGST", "IGST", "Total",
        "Status", "Payment Status",
    ])
    for inv in invoices:
        writer.writerow([
            inv.invoice_number or "",
            inv.invoice_date.isoformat() if inv.invoice_date else "",
            inv.due_date.isoformat() if inv.due_date else "",
            inv.vendor.name if inv.vendor else "",
            inv.vendor.gstin if inv.vendor and inv.vendor.gstin else "",
            inv.subtotal, inv.cgst, inv.sgst, inv.igst, inv.total,
            inv.status, inv.payment_status,
        ])
    return JSONResponse(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoices.csv"},
    )


# ----------------- frontend mount -----------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
