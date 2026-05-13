"""Seed the SQLite DB with demo invoices and generate sample invoice images.

Run: python seed.py
  - wipes invoices.db (delete file if present) and recreates schema
  - generates N sample invoice PNGs under samples/
  - copies them into uploads/ and inserts processed invoice rows
"""
from __future__ import annotations

import hashlib
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import models
import ocr

BASE_DIR = Path(__file__).parent
SAMPLES_DIR = BASE_DIR / "samples"
UPLOADS_DIR = BASE_DIR / "uploads"
SAMPLES_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)


def _font(size: int) -> ImageFont.ImageFont:
    """Best-effort font loader — falls back to PIL default if no TTF available."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_invoice_image(text: str, out_path: Path) -> None:
    """Render plain-text invoice content onto a PNG so pytesseract has something to chew on."""
    w, h = 900, 1200
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    title_font = _font(22)
    body_font = _font(18)

    lines = text.splitlines()
    y = 30
    for i, line in enumerate(lines):
        font = title_font if i == 0 else body_font
        draw.text((40, y), line, fill="black", font=font)
        bbox = draw.textbbox((40, y), line, font=font)
        y = bbox[3] + 6
        if y > h - 40:
            break
    # Light border to look invoice-y.
    draw.rectangle([(15, 15), (w - 15, h - 15)], outline="black", width=2)
    img.save(out_path, "PNG")


def seed(num_invoices: int = 12, reset: bool = True) -> None:
    if reset and models.DB_PATH.exists():
        models.DB_PATH.unlink()
    models.init_db()

    rng = random.Random(42)
    for idx in range(1, num_invoices + 1):
        seed_key = f"sample-{idx}"
        text = ocr._mock_invoice_text(seed_key)

        sample_png = SAMPLES_DIR / f"invoice_{idx:02d}.png"
        render_invoice_image(text, sample_png)

        # Compute hash and place into uploads/ so the FastAPI app can serve it.
        data = sample_png.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        upload_name = f"{h[:16]}.png"
        upload_path = UPLOADS_DIR / upload_name
        shutil.copyfile(sample_png, upload_path)

        if models.find_invoice_by_hash(h):
            continue

        invoice_id = models.create_invoice(file_path=f"/uploads/{upload_name}", file_hash=h)

        # Run real extraction on the rendered image (uses pytesseract if available;
        # otherwise the mock-text path produces the same fields).
        result = ocr.process_invoice(upload_path)
        e = result.extraction

        vendor_id = None
        if e.get("vendor_name"):
            vendor_id = models.upsert_vendor(
                name=e["vendor_name"],
                gstin=e.get("gstin"),
                state_code=e.get("state_code"),
            )

        status = "NEEDS_REVIEW" if result.confidence < 0.7 or not e.get("total") else "EXTRACTED"
        # Spread some across APPROVED + PAID for nicer dashboard demo.
        if rng.random() < 0.35:
            status = "APPROVED"

        models.update_extraction(
            invoice_id,
            vendor_id=vendor_id,
            extraction=e,
            raw_ocr_text=result.text,
            confidence=result.confidence,
            status=status,
        )
        if status == "APPROVED" and rng.random() < 0.5:
            models.mark_paid(invoice_id)

        print(f"  · invoice_{idx:02d} → {e.get('vendor_name')} ₹{e.get('total')} [{status}]")

    print(f"\nSeeded {num_invoices} invoices into {models.DB_PATH}")
    print(f"Sample images: {SAMPLES_DIR}/")


if __name__ == "__main__":
    seed()
