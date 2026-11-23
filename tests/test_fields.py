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
