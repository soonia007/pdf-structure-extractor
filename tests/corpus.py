"""Realistic test documents drawn at exact positions, with known ground truth.

Each builder returns the PDF path and the values a perfect reader would
extract, so the learner can be measured (learn on 2 samples, read a 3rd).
"""

from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4
THAI_FONT = Path("/usr/share/fonts/truetype/freefont/FreeSerif.ttf")
THAI_BOLD = Path("/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf")


def _thai_fonts() -> bool:
    if not THAI_FONT.exists():
        return False
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "TH" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("TH", str(THAI_FONT)))
        pdfmetrics.registerFont(TTFont("TH-Bold", str(THAI_BOLD if THAI_BOLD.exists() else THAI_FONT)))
    return True


def _text(c: canvas.Canvas, x: float, y: float, s: str, font: str = "Helvetica", size: float = 10) -> None:
    c.setFont(font, size)
    c.drawString(x, H - y, s)


def _right(c: canvas.Canvas, x: float, y: float, s: str, font: str = "Helvetica", size: float = 10) -> None:
    c.setFont(font, size)
    c.drawRightString(x, H - y, s)


# --------------------------------------------------------------------------- #
# 1. Invoice with side-by-side pairs, a label-row/value-row grid, a borderless
#    table and a totals box on the right.
# --------------------------------------------------------------------------- #
def kv_invoice(path: Path, d: dict) -> tuple[Path, dict]:
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 50, 60, "TAX INVOICE", "Helvetica-Bold", 18)
    _text(c, 380, 60, "ACME Supplies Co., Ltd.", "Helvetica-Bold", 10)
    _text(c, 380, 74, "Tax ID 0105556123456", size=9)

    _text(c, 50, 110, "Invoice No:", "Helvetica-Bold")
    _text(c, 135, 110, d["invoice_no"])
    _text(c, 330, 110, "Date:", "Helvetica-Bold")
    _text(c, 400, 110, d["date"])
    _text(c, 50, 128, "Customer:", "Helvetica-Bold")
    _text(c, 135, 128, d["customer"])
    _text(c, 330, 128, "Due Date:", "Helvetica-Bold")
    _text(c, 400, 128, d["due_date"])

    _text(c, 50, 165, "PO Number", "Helvetica-Bold")
    _text(c, 200, 165, "Payment Terms", "Helvetica-Bold")
    _text(c, 380, 165, "Sales Rep", "Helvetica-Bold")
    _text(c, 50, 181, d["po"])
    _text(c, 200, 181, d["terms"])
    _text(c, 380, 181, d["rep"])

    y = 225
    _text(c, 50, y, "Description", "Helvetica-Bold")
    _right(c, 330, y, "Qty", "Helvetica-Bold")
    _right(c, 430, y, "Unit Price", "Helvetica-Bold")
    _right(c, 530, y, "Amount", "Helvetica-Bold")
    c.line(50, H - y - 6, 530, H - y - 6)
    for desc, qty, price, amount in d["items"]:
        y += 18
        _text(c, 50, y, desc)
        _right(c, 330, y, qty)
        _right(c, 430, y, price)
        _right(c, 530, y, amount)
    y += 34
    for label, key in (("Subtotal", "subtotal"), ("VAT 7%", "vat"), ("Total", "total")):
        _text(c, 380, y, label, "Helvetica-Bold" if key == "total" else "Helvetica")
        _right(c, 530, y, d[key], "Helvetica-Bold" if key == "total" else "Helvetica")
        y += 16
    _text(c, 50, 790, "Thank you for your business.", size=8)
    c.save()
    truth = {
        "invoice_no": d["invoice_no"],
        "date": d["date_iso"],
        "customer": d["customer"],
        "due_date": d["due_iso"],
        "po_number": d["po"],
        "payment_terms": d["terms"],
        "sales_rep": d["rep"],
        "subtotal": d["subtotal_num"],
        "vat_7": d["vat_num"],
        "total": d["total_num"],
    }
    return path, truth


KV_INVOICES = [
    dict(invoice_no="INV-2026-0101", date="05/01/2026", date_iso="2026-01-05", customer="Bangkok Trading",
         due_date="04/02/2026", due_iso="2026-02-04", po="PO-7781", terms="Net 30", rep="Anan K.",
         items=[("Copy paper A4", "10", "120.00", "1,200.00"), ("Stapler", "2", "85.00", "170.00")],
         subtotal="1,370.00", subtotal_num=1370.0, vat="95.90", vat_num=95.9, total="1,465.90", total_num=1465.9),
    dict(invoice_no="INV-2026-0102", date="12/01/2026", date_iso="2026-01-12", customer="Siam Logistics",
         due_date="11/02/2026", due_iso="2026-02-11", po="PO-7802", terms="Net 15", rep="Malee S.",
         items=[("Toner cartridge", "3", "1,250.00", "3,750.00")],
         subtotal="3,750.00", subtotal_num=3750.0, vat="262.50", vat_num=262.5, total="4,012.50", total_num=4012.5),
    dict(invoice_no="INV-2026-0145", date="20/01/2026", date_iso="2026-01-20", customer="Chiang Mai Foods",
         due_date="19/02/2026", due_iso="2026-02-19", po="PO-7860", terms="Cash", rep="Somsak P.",
         items=[("Envelope C4", "100", "3.50", "350.00"), ("Marker pen", "12", "25.00", "300.00"),
                ("Whiteboard 90x120", "1", "1,890.00", "1,890.00")],
         subtotal="2,540.00", subtotal_num=2540.0, vat="177.80", vat_num=177.8, total="2,717.80", total_num=2717.8),
]  # fmt: skip


# --------------------------------------------------------------------------- #
# 2. Thai application form: labels without colons, id / phone / email values.
# --------------------------------------------------------------------------- #
def thai_form(path: Path, d: dict) -> tuple[Path, dict]:
    if not _thai_fonts():
        raise RuntimeError("Thai font missing")
    c = canvas.Canvas(str(path), pagesize=A4)
    _text(c, 200, 60, "แบบฟอร์มสมัครสมาชิก", "TH-Bold", 18)
    rows = [
        ("ชื่อ-นามสกุล", d["name"]),
        ("เลขประจำตัวประชาชน", d["citizen_id"]),
        ("วันเกิด", d["birth"]),
        ("โทรศัพท์", d["phone"]),
        ("อีเมล", d["email"]),
        ("จังหวัด", d["province"]),
    ]
    y = 110
    for label, value in rows:
        _text(c, 60, y, label, "TH-Bold", 12)
        _text(c, 200, y, value, "TH", 12)
        y += 24
    _text(c, 60, y + 20, "ข้าพเจ้ายืนยันว่าข้อมูลข้างต้นเป็นความจริง", "TH", 11)
    c.save()
    truth = {
        "name": d["name"],
        "citizen_id": d["citizen_id"],
        "birth_date": d["birth_iso"],
        "phone": d["phone"],
        "email": d["email"],
        "province": d["province"],
    }
    return path, truth


THAI_FORMS = [
    dict(name="นายสมชาย ใจดี", citizen_id="1-1001-00123-45-6", birth="5 มีนาคม 2530", birth_iso="1987-03-05",
         phone="081-234-5678", email="somchai@example.com", province="เชียงใหม่"),
    dict(name="นางสาวมาลี ศรีสุข", citizen_id="3-1002-00456-78-1", birth="12 ตุลาคม 2535", birth_iso="1992-10-12",
         phone="089-876-5432", email="malee.s@example.co.th", province="ขอนแก่น"),
    dict(name="นายอนันต์ แก้วดี", citizen_id="1-5099-00789-12-3", birth="1 มกราคม 2541", birth_iso="1998-01-01",
         phone="062-111-2222", email="anan@example.org", province="ภูเก็ต"),
]  # fmt: skip


# --------------------------------------------------------------------------- #
# 3. Ruled item table that continues on page 2 without repeating the header.
# --------------------------------------------------------------------------- #
def long_table(path: Path, n_rows: int, title: str = "STOCK REPORT") -> tuple[Path, dict]:
    c = canvas.Canvas(str(path), pagesize=A4)
    rows = [(f"SKU-{1000 + i}", f"Item number {i + 1}", str((i * 7) % 50 + 1)) for i in range(n_rows)]

    def grid(top: float, data: list[tuple[str, ...]]) -> None:
        xs = [50, 150, 400, 520]
        h = 18
        for r in range(len(data) + 1):
            c.line(xs[0], H - (top + r * h), xs[-1], H - (top + r * h))
        for x in xs:
            c.line(x, H - top, x, H - (top + len(data) * h))
        for r, row in enumerate(data):
            for col, cell in enumerate(row):
                font = "Helvetica-Bold" if r == 0 and data[0][0] == "SKU" else "Helvetica"
                _text(c, xs[col] + 4, top + r * h + 13, cell, font, 9)

    _text(c, 50, 50, title, "Helvetica-Bold", 16)
    first = [("SKU", "Description", "Qty")] + rows[:35]
    grid(80, first)
    c.showPage()
    grid(60, rows[35:])
    c.save()
    return path, {"rows": [{"sku": a, "description": b, "qty": int(q)} for a, b, q in rows]}


# --------------------------------------------------------------------------- #
# 4. Scanned copy: page rendered to an image, no text layer at all.
# --------------------------------------------------------------------------- #
def scanned_copy(src: Path, dst: Path, dpi: int = 200) -> Path:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(src))
    images = [pdf[i].render(scale=dpi / 72).to_pil().convert("L") for i in range(len(pdf))]
    pdf.close()
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=dpi)
    dst.write_bytes(buf.getvalue())
    return dst
