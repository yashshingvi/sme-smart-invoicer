from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class LineItemBase(BaseModel):
    line_no: int = 1
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity: float = 1.0
    unit: Optional[str] = None
    rate: float = 0.0
    discount: float = 0.0
    taxable_value: float = 0.0
    gst_rate: float = 18.0
    total: float = 0.0


class LineItemOut(LineItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_id: str


class VendorBase(BaseModel):
    name: str
    gstin: Optional[str] = None
    pan: Optional[str] = None
    address: Optional[str] = None
    state_code: Optional[str] = None
    payment_terms_days: int = 30


class VendorOut(VendorBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


class InvoiceBase(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    place_of_supply: Optional[str] = None
    subtotal: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    cess: float = 0.0
    total: float = 0.0
    round_off: float = 0.0
    currency: str = "INR"


class InvoiceUpdate(InvoiceBase):
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    status: Optional[str] = None
    payment_status: Optional[str] = None
    paid_amount: Optional[float] = None
    review_notes: Optional[str] = None
    line_items: Optional[List[LineItemBase]] = None


class InvoiceOut(InvoiceBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendor_id: Optional[str] = None
    status: str
    payment_status: str
    paid_amount: float
    file_path: Optional[str] = None
    created_at: datetime
    approved_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    vendor: Optional[VendorOut] = None
    line_items: List[LineItemOut] = []


class InvoiceListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    total: float
    status: str
    payment_status: str
    created_at: datetime
    vendor: Optional[VendorOut] = None


class DashboardStats(BaseModel):
    total_invoices: int
    total_payable: float
    overdue_count: int
    overdue_amount: float
    gst_input_credit: float
    needs_review: int
    top_vendors: List[dict]
    monthly_spend: List[dict]


class OcrPreviewResponse(BaseModel):
    raw_text: str
    extraction: dict
