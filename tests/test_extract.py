import json

import jsonschema
import pytest

from pdfstruct import (
    Document,
    EncryptedPDFError,
    NotAPDFError,
    extract,
    extract_isolated,
)
from pdfstruct.cli import main
from pdfstruct.normalize import normalize_text


def _blocks(doc, kind):
    return [b for p in doc.pages for b in p.blocks if b.type == kind]


def test_basic_structure(sample_pdf):
    doc = extract(sample_pdf)
    assert doc.extraction.status == "ok"
    assert doc.source.page_count == 2
    assert len(doc.source.sha256) == 64
    assert doc.metadata.get("Title") == "Sample Report"
    assert [o.title for o in doc.outline] == ["Introduction", "Results"]
    assert [o.page for o in doc.outline] == [1, 2]
    assert all(p.engine == "pdfplumber" for p in doc.pages)


def test_headings_and_levels(sample_pdf):
    doc = extract(sample_pdf)
    headings = {b.text: b.heading_level for b in _blocks(doc, "heading")}
    assert headings["Introduction"] == 1
    assert headings["Results"] == 1
    assert headings["Background"] == 2


def test_lists_paragraphs(sample_pdf):
    doc = extract(sample_pdf)
    items = [b.text for b in _blocks(doc, "list_item")]
    assert any("First point" in t for t in items)
    assert any("Second point" in t for t in items)
    assert any("first paragraph" in b.text for b in _blocks(doc, "paragraph"))


def test_header_footer_detected(sample_pdf):
    doc = extract(sample_pdf)
    assert any("ACME Corp" in b.text for b in _blocks(doc, "header"))
    assert any(b.text.startswith("Page") for b in _blocks(doc, "footer"))
    # header text must not leak into paragraphs
    assert not any("ACME Corp" in b.text for b in _blocks(doc, "paragraph"))


def test_table(sample_pdf):
    doc = extract(sample_pdf)
    tables = doc.pages[0].tables
    assert len(tables) == 1
    assert tables[0].rows[0] == ["Item", "Qty", "Price"]
    assert tables[0].n_rows == 3 and tables[0].n_cols == 3
    # table cell text is not duplicated as blocks
    assert not any(b.text.startswith("Apple") for b in doc.pages[0].blocks)


def test_links(sample_pdf):
    doc = extract(sample_pdf)
    assert any(link.uri == "https://example.com" for link in doc.pages[0].links)


def test_output_matches_json_schema(sample_pdf):
    doc = extract(sample_pdf)
    jsonschema.validate(json.loads(doc.model_dump_json()), Document.model_json_schema())


def test_deterministic(sample_pdf):
    a = extract(sample_pdf).model_dump(exclude={"extraction"})
    b = extract(sample_pdf).model_dump(exclude={"extraction"})
    assert a == b


def test_not_a_pdf(tmp_path):
    f = tmp_path / "fake.pdf"
    f.write_text("hello")
    with pytest.raises(NotAPDFError):
        extract(f)


def test_encrypted(encrypted_pdf):
    with pytest.raises(EncryptedPDFError):
        extract(encrypted_pdf)
    doc = extract(encrypted_pdf, password="secret")
    assert doc.source.encrypted
    assert "Introduction" in doc.pages[0].text


def test_truncated_pdf_does_not_crash(sample_pdf, tmp_path):
    data = sample_pdf.read_bytes()
    f = tmp_path / "truncated.pdf"
    f.write_bytes(data[: int(len(data) * 0.7)])
    try:
        doc = extract(f)
    except Exception as exc:  # only our own error types are acceptable
        from pdfstruct import PDFStructError

        assert isinstance(exc, PDFStructError)
    else:
        assert doc.extraction.status in ("ok", "partial", "failed")


def test_blank_page_needs_ocr(blank_pdf):
    doc = extract(blank_pdf)
    assert doc.pages[0].needs_ocr


def test_isolated(sample_pdf):
    doc = extract_isolated(sample_pdf, timeout=60)
    assert doc.source.page_count == 2


def test_cli(sample_pdf, tmp_path, capsys):
    out = tmp_path / "out.json"
    assert main([str(sample_pdf), "-o", str(out), "--pretty"]) == 0
    data = json.loads(out.read_text())
    assert data["schema_version"] == "1.0.0"
    assert main(["--schema"]) == 0


def test_normalize_thai_and_ligatures():
    assert normalize_text("นํา") == "นำ"  # น + ํ + า -> นำ
    assert normalize_text("ﬁle  \x00name") == "file name"


def test_committed_schema_is_up_to_date():
    from pathlib import Path

    committed = json.loads((Path(__file__).parents[1] / "schema" / "document.schema.json").read_text())
    assert committed == Document.model_json_schema(), "run: pdfstruct --schema > schema/document.schema.json"
