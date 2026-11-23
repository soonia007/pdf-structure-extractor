"""Learning quality on realistic layouts. These numbers must not go down.

Each case learns a template from 2 documents and reads a 3rd one it has not seen.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import corpus  # noqa: E402
import measure  # noqa: E402

from pdfstruct import TemplateStore, extract, extract_auto, ocr  # noqa: E402
from pdfstruct.kv import find_text_tables, match_label  # noqa: E402
from pdfstruct.templates import value_pattern  # noqa: E402

needs_ocr = pytest.mark.skipif(
    not (ocr.status().available and "tha" in ocr.status().languages), reason="Tesseract with Thai not installed"
)
needs_thai_font = pytest.mark.skipif(not corpus.THAI_FONT.exists(), reason="Thai font not installed")


def test_invoice_layouts(tmp_path):
    """Side-by-side pairs, value far right, label row over value row, totals box."""
    r = measure.score(corpus.kv_invoice, corpus.KV_INVOICES, "kv_invoice", tmp_path)
    assert r["matched_template"] == "kv_invoice"
    assert (r["found"], r["named"]) == (10, 10), r


@needs_thai_font
def test_thai_form_without_colons(tmp_path):
    r = measure.score(corpus.thai_form, corpus.THAI_FORMS, "thai_form", tmp_path)
    assert (r["found"], r["named"]) == (6, 6), r


def test_borderless_item_table(tmp_path):
    paths = [corpus.kv_invoice(tmp_path / f"k{i}.pdf", d)[0] for i, d in enumerate(corpus.KV_INVOICES)]
    store = TemplateStore(tmp_path / "t")
    store.learn("kv", [extract(p) for p in paths[:2]])
    doc = extract_auto(paths[2], store)
    assert doc.fields["items"].value == [
        {"description": "Envelope C4", "qty": 100, "unit_price": 3.5, "amount": 350.0},
        {"description": "Marker pen", "qty": 12, "unit_price": 25.0, "amount": 300.0},
        {"description": "Whiteboard 90x120", "qty": 1, "unit_price": 1890.0, "amount": 1890.0},
    ]
    assert all(f.confidence and f.confidence >= 0.8 for f in doc.fields.values())


def test_text_table_stops_before_totals(tmp_path):
    p, _ = corpus.kv_invoice(tmp_path / "a.pdf", corpus.KV_INVOICES[1])  # single item row
    tables = [t for t in extract(p).pages[0].tables if t.strategy == "text"]
    assert len(tables) == 1 and tables[0].rows == [
        ["Description", "Qty", "Unit Price", "Amount"],
        ["Toner cartridge", "3", "1,250.00", "3,750.00"],
    ]


def test_table_continues_on_next_page(tmp_path):
    a, _ = corpus.long_table(tmp_path / "a.pdf", 40)
    b, _ = corpus.long_table(tmp_path / "b.pdf", 45)
    c, truth = corpus.long_table(tmp_path / "c.pdf", 50)
    store = TemplateStore(tmp_path / "t")
    store.learn("stock", [extract(a), extract(b)])
    doc = extract_auto(c, store)
    assert doc.fields["items"].value == truth["rows"]  # 35 rows on page 1 + 15 without a header on page 2


@needs_ocr
def test_scanned_invoice_is_learned_and_read(tmp_path):
    def scanned(path, d):
        src, truth = corpus.kv_invoice(path.with_suffix(".src.pdf"), d)
        return corpus.scanned_copy(src, path), truth

    r = measure.score(scanned, corpus.KV_INVOICES, "scanned_invoice", tmp_path)
    assert r["found"] >= 9, r  # OCR may misread a character now and then


@needs_ocr
@needs_thai_font
def test_scanned_thai_page_text(tmp_path):
    src, _ = corpus.thai_form(tmp_path / "s.pdf", corpus.THAI_FORMS[0])
    doc = extract(corpus.scanned_copy(src, tmp_path / "scan.pdf", dpi=300))
    page = doc.pages[0]
    assert page.engine == "ocr" and not page.needs_ocr and page.ocr_confidence > 0.6
    rows = [[s.text for s in ln.segments] for ln in page.lines]
    assert ["ชื่อ-นามสกุล", "นายสมชาย ใจดี"] in rows  # Thai rebuilt without stray spaces
    assert ["เลขประจำตัวประชาชน", "1-1001-00123-45-6"] in rows


def test_without_ocr_engine_scans_are_flagged(tmp_path, monkeypatch):
    src, _ = corpus.kv_invoice(tmp_path / "a.pdf", corpus.KV_INVOICES[0])
    scan = corpus.scanned_copy(src, tmp_path / "scan.pdf")
    monkeypatch.setattr(ocr, "status", lambda: ocr.OcrStatus(False, None, None, (), "missing"))
    page = extract(scan).pages[0]
    assert page.needs_ocr and page.engine != "ocr"
    assert any("Tesseract" in w for w in page.warnings)
    assert extract(scan, ocr="off").pages[0].needs_ocr


@pytest.mark.parametrize(
    ("text", "label", "quality", "rest"),
    [
        ("Invoice No: INV-1", "Invoice No", 3, "INV-1"),
        ("Date:", "Date", 3, ""),
        ("Due Date: 05/02", "Date", 1, "05/02"),  # inside another label: lowest rank
        ("Date of birth: 1990", "Date", 2, "1990"),
        ("Tax ID 0105556123456", "Tax ID", 3, "0105556123456"),
        ("ชื่อ-นามสกุล นายสมชาย ใจดี", "ชื่อ-นามสกุล", 3, "นายสมชาย ใจดี"),
        ("Time 10:30", "Time", 3, "10:30"),
        ("Subtotal", "Total", 1, ""),
    ],
)
def test_match_label(text, label, quality, rest):
    m = match_label(text, label)
    assert (m.quality, m.rest) == (quality, rest)


def test_match_label_word_boundary():
    assert match_label("Notes: hello", "No") is None


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("somchai@example.com", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("1-1001-00123-45-6", r"\d\-\d{4}\-\d{5}\-\d{2}\-\d"),
        ("081-234-5678", r"\d{3}\-\d{3}\-\d{4}"),
        ("INV-2026-0101", r"[A-Z]+\-\d+\-\d+"),
        ("Bangkok Trading", None),
    ],
)
def test_value_pattern(value, pattern):
    assert value_pattern(value, "string") == pattern


def test_find_text_tables_needs_numbers():
    assert find_text_tables([]) == []
