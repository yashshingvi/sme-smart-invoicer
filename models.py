"""SQLite data layer for SME Smart Invoicer POC.

Uses stdlib sqlite3 only — no SQLAlchemy. Schema mirrors the Postgres design
in PLAN.md but trimmed to POC essentials.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(__file__).parent / "invoices.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    gstin       TEXT UNIQUE,
    pan         TEXT,
    address     TEXT,
    state_code  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    vendor_id       TEXT REFERENCES vendors(id),
    invoice_number  TEXT,
    invoice_date    TEXT,
    due_date        TEXT,
    place_of_supply TEXT,

    subtotal        REAL DEFAULT 0,
    cgst            REAL DEFAULT 0,
    sgst            REAL DEFAULT 0,
    igst            REAL DEFAULT 0,
    total           REAL DEFAULT 0,

    currency        TEXT DEFAULT 'INR',
    status          TEXT NOT NULL,          -- PENDING, PROCESSING, EXTRACTED, NEEDS_REVIEW, APPROVED, OCR_FAILED
    payment_status  TEXT DEFAULT 'UNPAID',  -- UNPAID, PARTIAL, PAID

    file_path       TEXT,
    file_hash       TEXT UNIQUE,
    raw_ocr_text    TEXT,
    extraction      TEXT,                   -- JSON blob of LLM/heuristic extraction
    confidence      REAL DEFAULT 0,
    error           TEXT,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_items (
    id             TEXT PRIMARY KEY,
    invoice_id     TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no        INTEGER,
    description    TEXT,
    hsn_code       TEXT,
    quantity       REAL,
    rate           REAL,
    gst_rate       REAL,
    taxable_value  REAL,
    total          REAL
);

CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date   ON invoices(invoice_date);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


# ---------- Vendor ops ----------

def upsert_vendor(
    name: str,
    gstin: str | None = None,
    address: str | None = None,
    state_code: str | None = None,
) -> str:
    """Insert vendor if new (deduped on GSTIN, else on normalized name). Return id."""
    name_norm = (name or "Unknown Vendor").strip()
    with get_conn() as conn:
        if gstin:
            row = conn.execute("SELECT id FROM vendors WHERE gstin = ?", (gstin,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM vendors WHERE LOWER(name) = LOWER(?) AND gstin IS NULL",
                (name_norm,),
            ).fetchone()
        if row:
            return row["id"]
        vid = new_id()
        conn.execute(
            """INSERT INTO vendors (id, name, gstin, address, state_code, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, name_norm, gstin, address, state_code, now_iso()),
        )
        return vid


def list_vendors() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT v.*,
                      COUNT(i.id)           AS invoice_count,
                      COALESCE(SUM(i.total), 0) AS total_spend
               FROM vendors v
               LEFT JOIN invoices i ON i.vendor_id = v.id
               GROUP BY v.id
               ORDER BY total_spend DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Invoice ops ----------

def create_invoice(file_path: str, file_hash: str) -> str:
    iid = new_id()
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO invoices (id, status, file_path, file_hash, created_at, updated_at)
               VALUES (?, 'PENDING', ?, ?, ?, ?)""",
            (iid, file_path, file_hash, ts, ts),
        )
    return iid


def find_invoice_by_hash(file_hash: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, status FROM invoices WHERE file_hash = ?", (file_hash,)
        ).fetchone()
    return dict(row) if row else None


def set_status(invoice_id: str, status: str, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE invoices SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, now_iso(), invoice_id),
        )


def update_extraction(
    invoice_id: str,
    *,
    vendor_id: str | None,
    extraction: dict[str, Any],
    raw_ocr_text: str,
    confidence: float,
    status: str,
) -> None:
    e = extraction
    with get_conn() as conn:
        conn.execute(
            """UPDATE invoices SET
                   vendor_id       = ?,
                   invoice_number  = ?,
                   invoice_date    = ?,
                   due_date        = ?,
                   place_of_supply = ?,
                   subtotal        = ?,
                   cgst            = ?,
                   sgst            = ?,
                   igst            = ?,
                   total           = ?,
                   currency        = ?,
                   raw_ocr_text    = ?,
                   extraction      = ?,
                   confidence      = ?,
                   status          = ?,
                   updated_at      = ?
               WHERE id = ?""",
            (
                vendor_id,
                e.get("invoice_number"),
                e.get("invoice_date"),
                e.get("due_date"),
                e.get("place_of_supply"),
                float(e.get("subtotal") or 0),
                float(e.get("cgst") or 0),
                float(e.get("sgst") or 0),
                float(e.get("igst") or 0),
                float(e.get("total") or 0),
                e.get("currency", "INR"),
                raw_ocr_text,
                json.dumps(e),
                confidence,
                status,
                now_iso(),
                invoice_id,
            ),
        )
        # Replace line items
        conn.execute("DELETE FROM line_items WHERE invoice_id = ?", (invoice_id,))
        for idx, li in enumerate(e.get("line_items", []), start=1):
            conn.execute(
                """INSERT INTO line_items
                   (id, invoice_id, line_no, description, hsn_code,
                    quantity, rate, gst_rate, taxable_value, total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id(),
                    invoice_id,
                    idx,
                    li.get("description"),
                    li.get("hsn_code"),
                    float(li.get("quantity") or 0),
                    float(li.get("rate") or 0),
                    float(li.get("gst_rate") or 0),
                    float(li.get("taxable_value") or 0),
                    float(li.get("total") or 0),
                ),
            )


def mark_paid(invoice_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE invoices SET payment_status = 'PAID', updated_at = ? WHERE id = ?",
            (now_iso(), invoice_id),
        )


def get_invoice(invoice_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT i.*, v.name AS vendor_name, v.gstin AS vendor_gstin
               FROM invoices i
               LEFT JOIN vendors v ON v.id = i.vendor_id
               WHERE i.id = ?""",
            (invoice_id,),
        ).fetchone()
        if not row:
            return None
        inv = dict(row)
        items = conn.execute(
            "SELECT * FROM line_items WHERE invoice_id = ? ORDER BY line_no",
            (invoice_id,),
        ).fetchall()
        inv["line_items"] = [dict(r) for r in items]
    return inv


def list_invoices(
    status: str | None = None,
    vendor_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    q = """SELECT i.id, i.invoice_number, i.invoice_date, i.due_date,
                  i.total, i.status, i.payment_status, i.confidence, i.created_at,
                  v.name AS vendor_name, v.gstin AS vendor_gstin
           FROM invoices i
           LEFT JOIN vendors v ON v.id = i.vendor_id
           WHERE 1=1"""
    args: list[Any] = []
    if status:
        q += " AND i.status = ?"
        args.append(status)
    if vendor_id:
        q += " AND i.vendor_id = ?"
        args.append(vendor_id)
    q += " ORDER BY i.created_at DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def dashboard_stats() -> dict[str, Any]:
    """Aggregate stats for the dashboard cards."""
    with get_conn() as conn:
        totals = conn.execute(
            """SELECT
                   COUNT(*)                                       AS total_invoices,
                   COALESCE(SUM(total), 0)                        AS total_value,
                   COALESCE(SUM(cgst + sgst + igst), 0)           AS itc_accrued,
                   SUM(CASE WHEN status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review,
                   SUM(CASE WHEN payment_status = 'UNPAID' THEN 1 ELSE 0 END) AS unpaid_count,
                   COALESCE(SUM(CASE WHEN payment_status = 'UNPAID' THEN total ELSE 0 END), 0)
                                                                  AS payable_total
               FROM invoices
               WHERE status NOT IN ('PENDING', 'OCR_FAILED')"""
        ).fetchone()
        top_vendors = conn.execute(
            """SELECT v.name, v.gstin,
                      COUNT(i.id)               AS invoice_count,
                      COALESCE(SUM(i.total), 0) AS spend
               FROM vendors v
               JOIN invoices i ON i.vendor_id = v.id
               GROUP BY v.id
               ORDER BY spend DESC
               LIMIT 5"""
        ).fetchall()
        recent = conn.execute(
            """SELECT i.id, i.invoice_number, i.total, i.status, v.name AS vendor_name
               FROM invoices i
               LEFT JOIN vendors v ON v.id = i.vendor_id
               ORDER BY i.created_at DESC LIMIT 5"""
        ).fetchall()
    return {
        **dict(totals),
        "top_vendors": [dict(r) for r in top_vendors],
        "recent": [dict(r) for r in recent],
    }
