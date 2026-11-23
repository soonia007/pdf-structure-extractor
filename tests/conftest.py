"""Generate fixture PDFs on the fly with reportlab so the repo stays binary-free."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, A4[1] - 30, "ACME Corp - Quarterly Report")
    canvas.drawString(A4[0] / 2, 25, f"Page {doc.page}")
    canvas.restoreState()


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Sample Report", author="Tester")
    table = Table([["Item", "Qty", "Price"], ["Apple", "3", "10.00"], ["Banana", "5", "4.50"]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story = [
        Paragraph('<a name="intro"/>Introduction', styles["Heading1"]),
        Paragraph("This is the first paragraph of the document. " * 4, styles["BodyText"]),
        Paragraph("Background", styles["Heading2"]),
        Paragraph(
            'See <link href="https://example.com" color="blue">example.com</link> for details.', styles["BodyText"]
        ),
        ListFlowable(
            [
                ListItem(Paragraph("First point", styles["BodyText"])),
                ListItem(Paragraph("Second point", styles["BodyText"])),
            ],
            bulletType="bullet",
        ),
        Spacer(1, 12),
        table,
        PageBreak(),
        Paragraph("Results", styles["Heading1"]),
        Paragraph("Second page body text that is long enough to be a paragraph. " * 3, styles["BodyText"]),
    ]
    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)

    # add a bookmark outline with pypdf
    writer = PdfWriter(clone_from=PdfReader(path))
    writer.add_outline_item("Introduction", 0)
    writer.add_outline_item("Results", 1)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture(scope="session")
def encrypted_pdf(sample_pdf, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "encrypted.pdf"
    writer = PdfWriter(clone_from=PdfReader(sample_pdf))
    writer.encrypt(user_password="secret", owner_password="owner", algorithm="AES-128")
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture(scope="session")
def blank_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


THAI_FONT = Path("/usr/share/fonts/truetype/freefont/FreeSerif.ttf")


def _invoice(path: Path, font: str, lines_top: list[str], header: list[str], rows: list[list[str]], total: str):
    styles = getSampleStyleSheet()
    body = styles["BodyText"].clone("b", fontName=font, fontSize=11, leading=15)
    title = styles["Heading1"].clone("h", fontName=font)
    table = Table([header, *rows])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTNAME", (0, 0), (-1, -1), font)]))
    story = [Paragraph(lines_top[0], title)]
    story += [Paragraph(t, body) for t in lines_top[1:]]
    story += [Spacer(1, 10), table, Spacer(1, 10), Paragraph(total, body)]
    SimpleDocTemplate(str(path), pagesize=A4, title="Invoice").build(story)
    return path


@pytest.fixture(scope="session")
def invoice_pdf(tmp_path_factory) -> Path:
    return _invoice(
        tmp_path_factory.mktemp("pdf") / "invoice.pdf",
        "Helvetica",
        [
            "INVOICE",
            "Invoice No: INV-2026-001",
            "Date: 15/03/2026",
            "Tax ID: 0105556123456",
            "Customer:",
            "Somchai Trading Co., Ltd.",
            "Paid: yes",
        ],
        ["Item", "Qty", "Unit Price", "Amount"],
        [["Widget A", "2", "100.00", "200.00"], ["Widget B", "3", "345.50", "1,036.50"]],
        "Grand Total: 1,236.50 THB",
    )


@pytest.fixture(scope="session")
def thai_invoice_pdf(tmp_path_factory) -> Path:
    if not THAI_FONT.exists():
        pytest.skip("Thai-capable font not installed")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("ThaiFont", str(THAI_FONT)))
    return _invoice(
        tmp_path_factory.mktemp("pdf") / "thai_invoice.pdf",
        "ThaiFont",
        [
            "ใบกำกับภาษี",
            "เลขที่: TX-0042",
            "วันที่ 5 มีนาคม 2569",
            "เลขประจำตัวผู้เสียภาษี 0105556123456",
        ],
        ["รายการ", "จำนวน", "ราคา"],
        [["สินค้า ก", "1", "500.00"], ["สินค้า ข", "4", "125.25"]],
        "ยอดรวมทั้งสิ้น 1,001.00 บาท",
    )
