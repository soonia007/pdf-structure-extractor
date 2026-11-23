"""Ruled tables without row lines (bank statements) and stable Excel / CSV export."""

import csv
import io
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))

import corpus  # noqa: E402

from pdfstruct import extract  # noqa: E402
from pdfstruct.cli import main  # noqa: E402
from pdfstruct.export import merged_tables, table_csv, typed_cell, xlsx_export  # noqa: E402

needs_thai_font = pytest.mark.skipif(not corpus.THAI_FONT.exists(), reason="Thai font not installed")


@pytest.fixture(scope="module")
def statement(tmp_path_factory):
    if not corpus.THAI_FONT.exists():
        pytest.skip("Thai font not installed")
    return corpus.bank_statement(tmp_path_factory.mktemp("st") / "statement.pdf", n_rows=60, per_page=28)


@needs_thai_font
def test_statement_rows_are_split_exactly(statement):
    path, truth = statement
    doc = extract(path)
    tables = [t for p in doc.pages for t in p.tables]
    assert [t.strategy for t in tables] == ["columns"] * 3
    assert tables[0].rows[0] == truth[0]  # two-line headers joined: "เวลา/วันที่มีผล", "ยอดคงเหลือ (บาท)"
    rows = [[c or "" for c in r] for t in tables for r in t.rows[1:]]
    assert rows == truth[1:]  # every row, every cell, across 3 pages


@needs_thai_font
def test_tables_are_merged_across_pages(statement):
    path, truth = statement
    merged = merged_tables(extract(path))
    assert len(merged) == 1
    t = merged[0]
    assert (t["n_rows"], t["n_cols"], t["pages"], t["span"]) == (60, 7, [1, 2, 3], "หน้า 1–3")
    assert t["header"] == truth[0]


@needs_thai_font
def test_xlsx_is_typed_and_complete(statement):
    path, truth = statement
    wb = load_workbook(io.BytesIO(xlsx_export(extract(path))))
    assert wb.sheetnames == ["ข้อมูลเอกสาร", "ตาราง 1 (หน้า 1–3)"]
    ws = wb["ตาราง 1 (หน้า 1–3)"]
    assert ws.max_row == 61 and ws.max_column == 7 and ws.freeze_panes == "A2"
    assert [c.value for c in ws[1]] == truth[0]
    assert ws["A2"].value == "06-11-26" and ws["A2"].data_type == "s"  # dates stay text
    assert ws["B3"].value == truth[2][1] and isinstance(ws["B3"].value, str)  # times stay text
    assert ws["E2"].value == 78397.73 and ws["E2"].number_format == "#,##0.00"  # amounts are numbers
    total = sum(ws.cell(row=r, column=4).value or 0 for r in range(2, 62))
    expected = sum(float(r[3].replace(",", "")) for r in truth[1:] if r[3])
    assert total == pytest.approx(expected)


def test_ordinary_ruled_table_is_unchanged(sample_pdf):
    t = extract(sample_pdf).pages[0].tables[0]
    assert t.strategy == "lines" and t.rows[0] == ["Item", "Qty", "Price"]


@pytest.mark.parametrize(
    ("text", "value", "fmt"),
    [
        ("1,234.50", 1234.5, "#,##0.00"),
        ("(500.00)", -500.0, "#,##0.00"),
        ("-12", -12, None),
        ("499.00", 499.0, "#,##0.00"),
        ("0105556123456", "0105556123456", None),  # leading zero: an id, not a number
        ("1234567890123456", "1234567890123456", None),  # > 15 digits: Excel would round it
        ("06-11-26", "06-11-26", None),
        ("12:04", "12:04", None),
        ("INV-001", "INV-001", None),
        ("", None, None),
    ],
)
def test_typed_cell(text, value, fmt):
    assert typed_cell(text) == (value, fmt)


def test_no_formula_injection(sample_pdf):
    doc = extract(sample_pdf)
    doc.pages[0].tables[0].rows[1][0] = '=HYPERLINK("http://evil","x")'
    ws = load_workbook(io.BytesIO(xlsx_export(doc)))["ตาราง 1 (หน้า 1)"]
    assert ws["A2"].data_type == "s" and ws["A2"].value.startswith("=HYPERLINK")  # text, not a formula
    rows = list(csv.reader(io.StringIO(table_csv(merged_tables(doc)[0]).lstrip("﻿"))))
    assert rows[1][0].startswith("'=")


def test_cli_excel(sample_pdf, tmp_path):
    out = tmp_path / "out.xlsx"
    assert main([str(sample_pdf), "--excel", str(out), "-o", str(tmp_path / "o.json")]) == 0
    assert "ตาราง 1 (หน้า 1)" in load_workbook(out).sheetnames
