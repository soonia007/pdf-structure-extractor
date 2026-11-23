import json

import pytest

from pdfstruct import SpecError, extract, load_spec
from pdfstruct.cli import main
from pdfstruct.fields import to_date, to_number

INVOICE_SPEC = {
    "include": ["metadata", "pages.tables"],
    "fields": [
        {"name": "invoice_no", "label": ["Invoice No", "เลขที่"], "pattern": r"[A-Z]+-[\d-]+", "required": True},
        {"name": "issue_date", "type": "date", "label": "Date"},
        {"name": "tax_id", "regex": r"\b(\d{13})\b"},
        {"name": "customer", "label": "Customer"},
        {"name": "paid", "type": "boolean", "label": "Paid"},
        {"name": "total", "type": "number", "label": ["Grand Total", "ยอดรวม"], "required": True},
        {"name": "title", "source": "metadata.Title"},
        {"name": "page_count", "type": "integer", "source": "source.page_count"},
        {"name": "all_amounts", "type": "number", "regex": r"\d[\d,]*\.\d{2}", "all": True},
        {
            "name": "items",
            "type": "table",
            "columns": {
                "Item": "name",
                "Qty": {"key": "qty", "type": "integer"},
                "Unit Price": {"key": "unit_price", "type": "number"},
                "Amount": {"key": "amount", "type": "number"},
            },
        },
        {"name": "po_number", "label": "PO Number", "default": "N/A"},
    ],
}


def test_invoice_fields(invoice_pdf):
    doc = extract(invoice_pdf, spec=INVOICE_SPEC)
    v = {k: f.value for k, f in doc.fields.items()}
    assert v["invoice_no"] == "INV-2026-001"
    assert v["issue_date"] == "2026-03-15"
    assert v["tax_id"] == "0105556123456"
    assert v["customer"] == "Somchai Trading Co., Ltd."
    assert v["paid"] is True
    assert v["total"] == 1236.50
    assert v["title"] == "Invoice"
    assert v["page_count"] == 1
    assert 1036.50 in v["all_amounts"]
    assert v["items"] == [
        {"name": "Widget A", "qty": 2, "unit_price": 100.0, "amount": 200.0},
        {"name": "Widget B", "qty": 3, "unit_price": 345.5, "amount": 1036.5},
    ]
    assert doc.fields["customer"].method == "label:next-line"
    assert doc.fields["po_number"].found is False and v["po_number"] == "N/A"
    assert doc.extraction.missing_required_fields == []


def test_thai_invoice(thai_invoice_pdf):
    spec = {
        "fields": [
            {"name": "doc_no", "label": "เลขที่", "pattern": r"[A-Z]+-\d+"},
            {"name": "issue_date", "type": "date", "label": "วันที่"},
            {"name": "tax_id", "label": "เลขประจำตัวผู้เสียภาษี", "pattern": r"\d{13}"},
            {"name": "total", "type": "number", "label": "ยอดรวมทั้งสิ้น"},
            {
                "name": "items",
                "type": "table",
                "columns": {
                    "รายการ": "name",
                    "จำนวน": {"key": "qty", "type": "integer"},
                    "ราคา": {"key": "price", "type": "number"},
                },
            },
        ]
    }
    doc = extract(thai_invoice_pdf, spec=spec)
    v = {k: f.value for k, f in doc.fields.items()}
    assert v["doc_no"] == "TX-0042"
    assert v["issue_date"] == "2026-03-05"  # พ.ศ. 2569 -> ค.ศ. 2026
    assert v["tax_id"] == "0105556123456"
    assert v["total"] == 1001.0
    assert v["items"][1] == {"name": "สินค้า ข", "qty": 4, "price": 125.25}


def test_required_missing_is_reported(invoice_pdf):
    doc = extract(invoice_pdf, spec={"fields": [{"name": "contract_no", "label": "Contract", "required": True}]})
    assert doc.extraction.missing_required_fields == ["contract_no"]
    assert doc.extraction.status == "ok"  # the PDF itself was read fine


@pytest.mark.parametrize(
    "bad",
    [
        {"fields": [{"name": "x"}]},  # no method
        {"fields": [{"name": "x", "label": "a", "regex": "b"}]},  # two methods
        {"fields": [{"name": "x", "regex": "("}]},  # bad regex
        {"fields": [{"name": "x", "label": "a"}, {"name": "x", "label": "b"}]},  # duplicate
        {"fields": [{"name": "x", "type": "table", "label": "a"}]},  # table needs columns
        {"include": ["nonsense"]},
        {"fields": [{"name": "bad name!", "label": "a"}]},
    ],
)
def test_invalid_specs(bad):
    with pytest.raises(SpecError):
        load_spec(bad)


def test_yaml_spec_and_cli(invoice_pdf, tmp_path):
    spec = tmp_path / "invoice.yaml"
    spec.write_text(
        "fields:\n"
        "  - name: total\n    type: number\n    label: Grand Total\n    required: true\n"
        "  - name: tax_id\n    regex: '\\b(\\d{13})\\b'\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    assert main([str(invoice_pdf), "--spec", str(spec), "--fields-only", "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["fields"] == {"total": 1236.5, "tax_id": "0105556123456"}


def test_include_filters_output(invoice_pdf, tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"include": ["pages.tables"], "fields": []}))
    out = tmp_path / "out.json"
    main([str(invoice_pdf), "--spec", str(spec), "-o", str(out)])
    data = json.loads(out.read_text())
    assert "metadata" not in data and "outline" not in data
    assert set(data["pages"][0]) >= {"tables", "number"} and "blocks" not in data["pages"][0]


def test_cli_bad_spec_exit_code(invoice_pdf, tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text('{"fields": [{"name": "x"}]}')
    assert main([str(invoice_pdf), "--spec", str(spec)]) == 2


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("15/03/2026", "2026-03-15"),
        ("2026-03-15", "2026-03-15"),
        ("5 มีนาคม 2569", "2026-03-05"),
        ("5 มี.ค. 69", "2026-03-05"),
        ("๕ มีนาคม ๒๕๖๙", "2026-03-05"),
        ("15 Mar 2026", "2026-03-15"),
        ("March 15, 2026", "2026-03-15"),
        ("01/02/2569", "2026-02-01"),
    ],
)
def test_dates(raw, iso):
    assert to_date(raw) == iso


@pytest.mark.parametrize(
    ("raw", "num"),
    [("1,234.50 บาท", 1234.5), ("฿ 99", 99.0), ("(500.00)", -500.0), ("-12.5", -12.5), ("๑,๐๐๐", 1000.0)],
)
def test_numbers(raw, num):
    assert to_number(raw) == num


# ---------------------------------------------------------------- region (crop)
def _region_of(doc, text, pad=0.004):
    """Region (fractions) around the block containing ``text`` — simulates a user's drawn box."""
    for p in doc.pages:
        for b in p.blocks:
            if text in b.text:
                return {
                    "page": p.number,
                    "x0": max(0, b.bbox.x0 / p.width - pad),
                    "top": max(0, b.bbox.top / p.height - pad),
                    "x1": min(1, b.bbox.x1 / p.width + pad),
                    "bottom": min(1, b.bbox.bottom / p.height + pad),
                }
    raise AssertionError(text)


def test_region_fields(invoice_pdf):
    base = extract(invoice_pdf)
    table = base.pages[0].tables[0].bbox
    w, h = base.pages[0].width, base.pages[0].height
    spec = {
        "fields": [
            {"name": "customer", "region": _region_of(base, "Somchai")},
            {"name": "total", "type": "number", "region": _region_of(base, "Grand Total"), "pattern": r"[\d,]+\.\d{2}"},
            {"name": "invoice_no", "region": _region_of(base, "Invoice No"), "pattern": r"INV-[\d-]+"},
            {"name": "lines", "type": "table",
             "region": {"page": 1, "x0": table.x0 / w - 0.01, "top": table.top / h - 0.01, "x1": table.x1 / w + 0.01,
                        "bottom": table.bottom / h + 0.01}},
            {"name": "nothing", "region": {"page": 1, "x0": 0.9, "top": 0.9, "x1": 0.99, "bottom": 0.99}},
            {"name": "bad_page", "region": {"page": 5, "x0": 0.1, "top": 0.1, "x1": 0.2, "bottom": 0.2}},
        ]
    }  # fmt: skip
    doc = extract(invoice_pdf, spec=spec)
    f = doc.fields
    assert f["customer"].value == "Somchai Trading Co., Ltd." and f["customer"].method == "region"
    assert f["customer"].bbox is not None and f["customer"].page == 1
    assert f["total"].value == 1236.5
    assert f["invoice_no"].value == "INV-2026-001"
    assert f["lines"].value[1] == {"item": "Widget B", "qty": "3", "unit_price": "345.50", "amount": "1,036.50"}
    assert not f["nothing"].found and "empty" in f["nothing"].error
    assert not f["bad_page"].found


def test_region_needs_pdf(invoice_pdf):
    from pdfstruct.fields import Spec, apply_spec

    doc = extract(invoice_pdf)
    spec = Spec.model_validate({"fields": [{"name": "c", "region": _region_of(doc, "Somchai")}]})
    apply_spec(doc, spec)  # no pdf_path
    assert not doc.fields["c"].found and "need the PDF" in doc.fields["c"].error


@pytest.mark.parametrize(
    "region",
    [
        {"page": 1, "x0": 0.5, "top": 0.1, "x1": 0.4, "bottom": 0.2},  # inverted
        {"page": 0, "x0": 0.1, "top": 0.1, "x1": 0.2, "bottom": 0.2},  # page 0
        {"page": 1, "x0": 0.1, "top": 0.1, "x1": 1.5, "bottom": 0.2},  # outside page
    ],
)
def test_invalid_regions(region):
    with pytest.raises(SpecError):
        load_spec({"fields": [{"name": "x", "region": region}]})
