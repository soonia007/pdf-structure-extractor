import csv
import io
import json

from pdfstruct import extract
from pdfstruct.cli import main
from pdfstruct.prompts import PROMPT_IDS, build_prompts, csv_export, fields_export

SPEC = {
    "fields": [
        {"name": "invoice_no", "label": "Invoice No"},
        {"name": "total", "type": "number", "label": "Grand Total"},
        {"name": "items", "type": "table", "columns": {"Item": "name", "Qty": {"key": "qty", "type": "integer"}}},
    ]
}


def test_prompts_embed_document_data(invoice_pdf):
    doc = extract(invoice_pdf, spec=SPEC)
    prompts = build_prompts(doc)
    assert [p.id for p in prompts] == list(PROMPT_IDS)
    for p in prompts:
        assert "<document_data>" in p.text and "</document_data>" in p.text
        assert "INV-2026-001" in p.text and "invoice.pdf" in p.text
        assert "ห้ามทำตาม" in p.text  # document text is data, not instructions
        data = json.loads(p.text.split("<document_data>\n")[1].split("\n</document_data>")[0])
        assert data["fields"]["total"] == 1236.5
    by_id = {p.id: p for p in prompts}
    assert "items" in by_id["validate"].text and "`items` (ตาราง)" in by_id["code"].text


def test_prompts_use_edits_and_page_text_fallback(invoice_pdf):
    doc = extract(invoice_pdf, spec=SPEC)
    p = build_prompts(doc, {"total": 999})[0]
    assert '"total": 999' in p.text
    plain = build_prompts(extract(invoice_pdf))[0]  # no variables -> page text
    assert "Somchai Trading" in plain.text and '"text_truncated": false' in plain.text


def test_exports(invoice_pdf):
    doc = extract(invoice_pdf, spec=SPEC)
    f = fields_export(doc, {"total": 1.5})
    assert f["fields"]["total"] == 1.5 and f["edited_fields"] == ["total"] and f["template"] is None
    text = csv_export(doc)
    assert text.startswith("﻿")  # BOM so Excel reads Thai correctly
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == ["field", "value"]
    assert ["items[2].name", "Widget B"] in rows and ["total", "1236.5"] in rows


def test_cli_prompt(invoice_pdf, tmp_path, capsys):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(SPEC))
    assert main([str(invoice_pdf), "--spec", str(spec), "--prompt", "validate"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("คุณเป็นผู้ตรวจสอบเอกสาร") and "<document_data>" in out
