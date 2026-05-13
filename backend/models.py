import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Date, DateTime, Text, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from .database import Base


def gen_id() -> str:
    return str(uuid.uuid4())


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False, index=True)
    gstin = Column(String, unique=True, nullable=True, index=True)
    pan = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    state_code = Column(String, nullable=True)
    payment_terms_days = Column(Integer, default=30)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoices = relationship("Invoice", back_populates="vendor")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(String, primary_key=True, default=gen_id)
    vendor_id = Column(String, ForeignKey("vendors.id"), nullable=True)
    invoice_number = Column(String, nullable=True, index=True)
    invoice_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    place_of_supply = Column(String, nullable=True)

    subtotal = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    cess = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    round_off = Column(Float, default=0.0)

    currency = Column(String, default="INR")
    status = Column(String, default="PENDING", index=True)
    # PENDING -> EXTRACTED -> NEEDS_REVIEW -> APPROVED -> PAID
    payment_status = Column(String, default="UNPAID")
    paid_amount = Column(Float, default=0.0)

    file_path = Column(String, nullable=True)
    file_hash = Column(String, nullable=True, unique=True, index=True)
    raw_ocr_text = Column(Text, nullable=True)
    extraction = Column(JSON, nullable=True)
    review_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)

    vendor = relationship("Vendor", back_populates="invoices")
    line_items = relationship(
        "LineItem", back_populates="invoice", cascade="all, delete-orphan"
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id = Column(String, primary_key=True, default=gen_id)
    invoice_id = Column(String, ForeignKey("invoices.id"), nullable=False)
    line_no = Column(Integer, default=1)
    description = Column(Text, nullable=True)
    hsn_code = Column(String, nullable=True)
    quantity = Column(Float, default=1.0)
    unit = Column(String, nullable=True)
    rate = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    taxable_value = Column(Float, default=0.0)
    gst_rate = Column(Float, default=18.0)
    total = Column(Float, default=0.0)

    invoice = relationship("Invoice", back_populates="line_items")
