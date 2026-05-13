"""OCR + structured extraction.

Two-stage pipeline:
1. Raw OCR via pytesseract (PDFs via pdfplumber text first, fallback to image OCR).
2. Structured extraction via Claude (if ANTHROPIC_API_KEY is set), else a regex fallback.
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    from pdf2image import convert_from_path
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False


EXTRACTION_SYSTEM_PROMPT = """You are an invoice data extraction assistant for Indian SME accounting.
Extract the following fields from the OCR'd invoice text. Respond with ONLY valid JSON, no commentary.

Schema:
{
  "vendor_name": string | null,
  "vendor_gstin": string | null,       // 15-char GSTIN
  "vendor_address": string | null,
  "invoice_number": string | null,
  "invoice_date": "YYYY-MM-DD" | null,
  "due_date": "YYYY-MM-DD" | null,
  "place_of_supply": string | null,    // state name or code
  "subtotal": number,                  // taxable value before tax
  "cgst": number,
  "sgst": number,
  "igst": number,
  "cess": number,
  "total": number,
  "round_off": number,
  "currency": "INR" | "USD" | ...,
  "line_items": [
    {
      "description": string,
      "hsn_code": string | null,
      "quantity": number,
      "unit": string | null,
      "rate": number,
      "taxable_value": number,
      "gst_rate": number,
      "total": number
    }
  ],
  "confidence": number                 // your 0-1 confidence in this extraction
}

Rules:
- If a field is missing/illegible, use null (or 0 for amounts).
- Dates may appear as DD/MM/YYYY or DD-MM-YYYY in Indian invoices — normalize to YYYY-MM-DD.
- subtotal + cgst + sgst + igst + cess + round_off should ~= total. If math is off, prefer values that match.
"""


def ocr_image(image_path: Path) -> str:
    if not HAS_TESSERACT:
        return ""
    img = Image.open(image_path)
    return pytesseract.image_to_string(img, lang="eng")


def ocr_pdf(pdf_path: Path) -> str:
    text = ""
    if HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    text += page_text + "\n"
        except Exception:
            pass

    if text.strip():
        return text

    if HAS_PDF2IMAGE and HAS_TESSERACT:
        try:
            images = convert_from_path(str(pdf_path), dpi=200)
            for img in images:
                text += pytesseract.image_to_string(img, lang="eng") + "\n"
        except Exception:
            pass
    return text


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".pdf"}:
        return ocr_pdf(file_path)
    if suffix in {".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"}:
        return ocr_image(file_path)
    try:
        return file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# ---------- Structured extraction ----------

def _parse_date(s: str) -> Optional[str]:
    s = s.strip()
    formats = [
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%Y-%m-%d", "%Y/%m/%d",
        "%d/%m/%y", "%d-%m-%y",
        "%d %b %Y", "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _num(s: str) -> float:
    try:
        return float(s.replace(",", "").replace("₹", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def regex_extract(text: str) -> dict:
    """Fallback extraction using regex. Conservative — fields default to null/0."""
    result = {
        "vendor_name": None,
        "vendor_gstin": None,
        "vendor_address": None,
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "place_of_supply": None,
        "subtotal": 0.0,
        "cgst": 0.0,
        "sgst": 0.0,
        "igst": 0.0,
        "cess": 0.0,
        "total": 0.0,
        "round_off": 0.0,
        "currency": "INR",
        "line_items": [],
        "confidence": 0.4,
    }

    gstin_match = re.search(
        r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z])\b", text
    )
    if gstin_match:
        result["vendor_gstin"] = gstin_match.group(1)

    inv_match = re.search(
        r"(?:Invoice\s*(?:No|Number|#)\.?\s*[:\-]?\s*)([A-Z0-9\-\/]+)",
        text, re.IGNORECASE,
    )
    if inv_match:
        result["invoice_number"] = inv_match.group(1).strip()

    date_match = re.search(
        r"(?:Invoice\s*Date|Date)\s*[:\-]?\s*"
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}-\d{2}-\d{2})",
        text, re.IGNORECASE,
    )
    if date_match:
        result["invoice_date"] = _parse_date(date_match.group(1))

    due_match = re.search(
        r"Due\s*Date\s*[:\-]?\s*"
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}-\d{2}-\d{2})",
        text, re.IGNORECASE,
    )
    if due_match:
        result["due_date"] = _parse_date(due_match.group(1))

    total_match = re.search(
        r"(?:Grand\s*Total|Total\s*Amount|Total)\s*[:\-]?\s*"
        r"(?:Rs\.?|INR|₹)?\s*([0-9,]+\.?\d*)",
        text, re.IGNORECASE,
    )
    if total_match:
        result["total"] = _num(total_match.group(1))

    sub_match = re.search(
        r"(?:Subtotal|Sub\s*Total|Taxable\s*(?:Value|Amount))\s*[:\-]?\s*"
        r"(?:Rs\.?|INR|₹)?\s*([0-9,]+\.?\d*)",
        text, re.IGNORECASE,
    )
    if sub_match:
        result["subtotal"] = _num(sub_match.group(1))

    cgst_match = re.search(r"CGST[^0-9]*([0-9,]+\.?\d*)", text, re.IGNORECASE)
    if cgst_match:
        result["cgst"] = _num(cgst_match.group(1))
    sgst_match = re.search(r"SGST[^0-9]*([0-9,]+\.?\d*)", text, re.IGNORECASE)
    if sgst_match:
        result["sgst"] = _num(sgst_match.group(1))
    igst_match = re.search(r"IGST[^0-9]*([0-9,]+\.?\d*)", text, re.IGNORECASE)
    if igst_match:
        result["igst"] = _num(igst_match.group(1))

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        # Heuristic: vendor name is usually one of the first 3 non-empty lines,
        # and not a label like "Tax Invoice" / "Invoice".
        for cand in lines[:5]:
            up = cand.upper()
            if any(kw in up for kw in ("INVOICE", "TAX INV", "BILL", "GSTIN")):
                continue
            if len(cand) >= 3:
                result["vendor_name"] = cand
                break

    return result


def llm_extract(text: str) -> Optional[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not text.strip():
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None

    client = Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": EXTRACTION_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"OCR text from invoice:\n\n{text}\n\nReturn JSON only.",
                }
            ],
        )
        content = msg.content[0].text.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
    except Exception as e:
        print(f"LLM extraction failed: {e}")
        return None


def extract_structured(text: str) -> dict:
    """Try LLM first, fall back to regex."""
    llm_result = llm_extract(text)
    if llm_result:
        llm_result.setdefault("confidence", 0.85)
        llm_result["_method"] = "llm"
        return llm_result
    result = regex_extract(text)
    result["_method"] = "regex"
    return result
