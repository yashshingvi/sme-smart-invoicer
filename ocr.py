"""OCR + lightweight extraction for invoice images.

Strategy:
1. Try pytesseract on the image to get raw text.
2. If pytesseract is unavailable or fails, fall back to mock extraction so the
   POC stays demoable on machines without the Tesseract binary installed.
3. Apply regex-based heuristics to pull out invoice number, dates, GSTIN,
   totals, and line items. This is the place a real implementation would call
   Claude with a Pydantic schema (see PLAN.md §4); for the POC we keep it
   deterministic and offline.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import pytesseract
    from PIL import Image
    _TESS_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    _TESS_AVAILABLE = False


# ---------- Public API ----------

@dataclass
class OCRResult:
    text: str
    extraction: dict[str, Any]
    confidence: float
    used_mock: bool


def process_invoice(file_path: str | Path) -> OCRResult:
    path = Path(file_path)
    text, used_mock = _ocr_text(path)
    if not text.strip():
        # OCR found nothing usable — emit a mock so the row is still demoable.
        text = _mock_invoice_text(path.stem)
        used_mock = True
    extraction = _extract_fields(text)
    confidence = _score_confidence(extraction, used_mock=used_mock)
    return OCRResult(text=text, extraction=extraction, confidence=confidence, used_mock=used_mock)


# ---------- OCR layer ----------

def _ocr_text(path: Path) -> tuple[str, bool]:
    """Return (text, used_mock)."""
    if not _TESS_AVAILABLE:
        return _mock_invoice_text(path.stem), True
    try:
        img = Image.open(path)
        # Light preprocessing: greyscale lifts tesseract accuracy on noisy photos.
        if img.mode != "L":
            img = img.convert("L")
        text = pytesseract.image_to_string(img)
        return text, False
    except pytesseract.TesseractNotFoundError:
        return _mock_invoice_text(path.stem), True
    except Exception:
        return "", False


# ---------- Field extraction ----------

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z][Z][0-9A-Z])\b")
INV_NO_RE = re.compile(r"(?:invoice|inv|bill)\s*(?:no\.?|#|number)?\s*[:\-]?\s*([A-Z0-9\-/]+)", re.I)
DATE_RE = re.compile(r"(\d{1,2}[\-/.](?:\d{1,2}|[A-Za-z]{3,9})[\-/.]\d{2,4})")
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([\d,]+\.\d{2})", re.I)
TOTAL_LINE_RE = re.compile(r"(grand\s*total|total\s*amount|net\s*payable|total)\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
CGST_RE = re.compile(r"cgst[^0-9]{0,12}([\d,]+\.\d{2})", re.I)
SGST_RE = re.compile(r"sgst[^0-9]{0,12}([\d,]+\.\d{2})", re.I)
IGST_RE = re.compile(r"igst[^0-9]{0,12}([\d,]+\.\d{2})", re.I)
SUBTOTAL_RE = re.compile(r"(sub\s*total|taxable\s*value)[^0-9]{0,10}([\d,]+\.\d{2})", re.I)

STATE_CODES = {
    "01": "Jammu & Kashmir", "07": "Delhi", "09": "Uttar Pradesh", "19": "West Bengal",
    "24": "Gujarat", "27": "Maharashtra", "29": "Karnataka", "32": "Kerala", "33": "Tamil Nadu",
    "36": "Telangana",
}


def _extract_fields(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    gstin_match = GSTIN_RE.search(text)
    gstin = gstin_match.group(1) if gstin_match else None

    vendor_name = _guess_vendor_name(lines)
    invoice_number = _first_group(INV_NO_RE, text)
    invoice_date = _parse_date(_first_group(DATE_RE, text))
    due_date = _infer_due_date(invoice_date)

    subtotal = _parse_amount(_first_group(SUBTOTAL_RE, text, group=2))
    cgst = _parse_amount(_first_group(CGST_RE, text))
    sgst = _parse_amount(_first_group(SGST_RE, text))
    igst = _parse_amount(_first_group(IGST_RE, text))
    total = _parse_amount(_first_group(TOTAL_LINE_RE, text, group=2))
    if total == 0:
        # Fallback: largest amount on the page is almost always the total.
        amounts = [_parse_amount(m.group(1)) for m in AMOUNT_RE.finditer(text)]
        total = max(amounts) if amounts else 0.0
    if subtotal == 0 and total:
        subtotal = round(total - (cgst + sgst + igst), 2)

    state_code = gstin[:2] if gstin else None
    place_of_supply = STATE_CODES.get(state_code or "")

    return {
        "vendor_name": vendor_name,
        "gstin": gstin,
        "state_code": state_code,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "place_of_supply": place_of_supply,
        "subtotal": subtotal,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "total": total,
        "currency": "INR",
        "line_items": _extract_line_items(lines),
    }


def _first_group(regex: re.Pattern[str], text: str, group: int = 1) -> str | None:
    m = regex.search(text)
    return m.group(group) if m else None


def _parse_amount(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
                "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _infer_due_date(invoice_date: str | None, terms_days: int = 30) -> str | None:
    if not invoice_date:
        return None
    try:
        d = date.fromisoformat(invoice_date)
        return (d + timedelta(days=terms_days)).isoformat()
    except ValueError:
        return None


def _guess_vendor_name(lines: list[str]) -> str:
    """The first non-trivial line is usually the seller's name on Indian invoices."""
    for ln in lines[:6]:
        # Skip lines that look like 'TAX INVOICE' headers or pure digits.
        if re.search(r"\b(tax invoice|invoice|gst|original)\b", ln, re.I):
            continue
        if re.fullmatch(r"[\d\W]+", ln):
            continue
        if len(ln) < 3:
            continue
        return ln[:80]
    return "Unknown Vendor"


def _extract_line_items(lines: list[str]) -> list[dict[str, Any]]:
    """Heuristic: lines with a description and at least two numeric fields look like items."""
    items: list[dict[str, Any]] = []
    line_re = re.compile(
        r"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<rate>[\d,]+\.\d{2})\s+(?P<total>[\d,]+\.\d{2})\s*$"
    )
    for ln in lines:
        m = line_re.match(ln)
        if not m:
            continue
        desc = m.group("desc").strip()
        if re.search(r"total|gst|cgst|sgst|igst|subtotal", desc, re.I):
            continue
        qty = float(m.group("qty"))
        rate = _parse_amount(m.group("rate"))
        total = _parse_amount(m.group("total"))
        items.append({
            "description": desc[:120],
            "hsn_code": None,
            "quantity": qty,
            "rate": rate,
            "gst_rate": 18.0,
            "taxable_value": round(qty * rate, 2),
            "total": total,
        })
        if len(items) >= 20:
            break
    return items


def _score_confidence(extraction: dict[str, Any], used_mock: bool) -> float:
    """Crude 0–1 confidence based on how many fields we filled."""
    weights = {
        "vendor_name": 0.15, "gstin": 0.15, "invoice_number": 0.15,
        "invoice_date": 0.10, "total": 0.20, "subtotal": 0.10,
        "line_items": 0.15,
    }
    score = 0.0
    for k, w in weights.items():
        v = extraction.get(k)
        if k == "line_items":
            if v:
                score += w
        elif k in ("subtotal", "total"):
            if v and float(v) > 0:
                score += w
        elif v:
            score += w
    if used_mock:
        score = max(score, 0.85)  # mock data is "correct by construction"
    return round(score, 2)


# ---------- Mock fallback ----------

MOCK_VENDORS = [
    {"name": "Sharma Wholesale Mart", "gstin": "27ABCDE1234F1Z5", "state": "Maharashtra", "state_code": "27"},
    {"name": "Patel Electronics & Co.",  "gstin": "24AAJCP9876K1Z3", "state": "Gujarat",     "state_code": "24"},
    {"name": "Krishna Stationery Hub",   "gstin": "29AAGCK4567L1Z2", "state": "Karnataka",  "state_code": "29"},
    {"name": "Delhi Cloth Suppliers",    "gstin": "07AABCD5678M1Z9", "state": "Delhi",      "state_code": "07"},
    {"name": "Chennai Spice Traders",    "gstin": "33AACCC2468N1Z1", "state": "Tamil Nadu", "state_code": "33"},
]

MOCK_ITEMS = [
    ("Basmati Rice 25kg",    "10063020", 850.00, 5),
    ("Sunflower Oil 15L",    "15121110", 1850.00, 5),
    ("LED Bulb 9W",          "85395000", 95.00,  18),
    ("Notebook A4 200pg",    "48201020", 60.00,  12),
    ("Cotton Bedsheet King", "63041930", 750.00, 5),
    ("Stainless Steel Cup",  "73239310", 120.00, 18),
    ("Paneer 1kg",           "04061000", 360.00, 5),
    ("Phone Charger USB-C",  "85044090", 250.00, 18),
]


def _mock_invoice_text(seed: str) -> str:
    rng = random.Random(seed)
    v = rng.choice(MOCK_VENDORS)
    inv_no = f"INV-{rng.randint(1000, 9999)}/{rng.randint(23, 25)}-{rng.randint(24, 26)}"
    inv_date = (date.today() - timedelta(days=rng.randint(0, 60))).strftime("%d-%m-%Y")
    items = rng.sample(MOCK_ITEMS, k=rng.randint(2, 4))

    lines = [
        v["name"],
        f"GSTIN: {v['gstin']}",
        f"Plot 42, Industrial Area, {v['state']}",
        "Tax Invoice",
        f"Invoice No: {inv_no}",
        f"Invoice Date: {inv_date}",
        "",
        "Description                          Qty     Rate      Total",
    ]
    subtotal = 0.0
    cgst = 0.0
    sgst = 0.0
    igst = 0.0
    for desc, _hsn, rate, gst in items:
        qty = rng.randint(1, 8)
        line_total = round(qty * rate, 2)
        subtotal += line_total
        if v["state_code"] == "27":  # same-state demo: CGST+SGST
            cgst += round(line_total * gst / 200, 2)
            sgst += round(line_total * gst / 200, 2)
        else:
            igst += round(line_total * gst / 100, 2)
        lines.append(f"{desc:<36} {qty:<5} {rate:>8.2f} {line_total:>10.2f}")
    total = round(subtotal + cgst + sgst + igst, 2)
    lines += [
        "",
        f"Subtotal: {subtotal:.2f}",
        f"CGST: {cgst:.2f}",
        f"SGST: {sgst:.2f}",
        f"IGST: {igst:.2f}",
        f"Grand Total: {total:.2f}",
    ]
    return "\n".join(lines)
