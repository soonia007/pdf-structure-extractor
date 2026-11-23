import json

import pytest

from pdfstruct import TemplateError, TemplateStore, extract, extract_auto
from pdfstruct.cli import main
from pdfstruct.templates import generalise, infer_type


@pytest.fixture
def invoices(make_invoice):
    return [
        make_invoice(1, "INV-2026-001", "15/03/2026", "Somchai Trading", [["Widget A", "2", "100.00", "200.00"]],
                     "200.00"),
        make_invoice(2, "INV-2026-002", "16/03/2026", "Malee Shop",
                     [["Widget B", "3", "345.50", "1,036.50"], ["Bolt", "10", "1.00", "10.00"]], "1,046.50"),
        make_invoice(3, "INV-2026-117", "01/04/2026", "Kasem Ltd", [["Nut", "5", "2.00", "10.00"]], "10.00"),
    ]  # fmt: skip


def _fields(t):
    return {f.name: f for f in t.spec.fields}


def test_learn_from_two_samples(tmp_path, invoices):
    store = TemplateStore(tmp_path)
    t = store.learn("acme_invoice", [extract(p) for p in invoices[:2]], description="ACME invoices")
    f = _fields(t)
    assert list(f) == ["invoice_no", "date", "customer", "grand_total", "items"]  # reading order
    assert f["invoice_no"].pattern == r"[A-Z]+\-\d+\-\d+"
    assert f["date"].type == "date" and f["grand_total"].type == "number"
    assert f["customer"].label == ["Customer"]
    assert all(x.required for x in f.values() if x.type != "table")
    assert {c.key: c.type for c in f["items"].columns.values()} == {
        "item": "string", "qty": "integer", "unit_price": "number", "amount": "number"}  # fmt: skip
    # same value in every sample => fixed text: anchor, not a variable
    assert "tax_id" not in f and "seller" not in f
    assert {"Tax ID", "Seller", "Unit Price", "INVOICE"} <= set(t.anchors)
    assert (tmp_path / "acme_invoice.template.json").exists()


def test_template_file_does_not_store_values(tmp_path, invoices):
    store = TemplateStore(tmp_path)
    store.learn("acme_invoice", [extract(p) for p in invoices[:2]])
    text = (tmp_path / "acme_invoice.template.json").read_text()
    assert "Somchai" not in text and "Malee" not in text and "1,036.50" not in text


def test_incremental_learning_and_duplicates(tmp_path, invoices):
    store = TemplateStore(tmp_path)
    store.learn("acme_invoice", [extract(invoices[0])])
    t = store.learn("acme_invoice", [extract(invoices[0]), extract(invoices[1])])
    assert len(t.samples) == 2  # the duplicate sample is ignored
    assert "tax_id" not in _fields(t)  # became constant once a 2nd sample arrived


def test_match_and_auto_extract(tmp_path, invoices, sample_pdf):
    store = TemplateStore(tmp_path)
    store.learn("acme_invoice", [extract(p) for p in invoices[:2]])
    results = store.match(extract(invoices[2]))
    assert results[0].template == "acme_invoice" and results[0].score == 1.0
    assert store.best(extract(sample_pdf)) is None  # an unrelated report is not an invoice

    doc = extract_auto(invoices[2], store)
    assert doc.extraction.template == "acme_invoice"
    v = {k: x.value for k, x in doc.fields.items()}
    assert v["invoice_no"] == "INV-2026-117" and v["date"] == "2026-04-01" and v["grand_total"] == 10.0
    assert v["customer"] == "Kasem Ltd"
    assert v["items"] == [{"item": "Nut", "qty": 5, "unit_price": 2.0, "amount": 10.0}]

    unmatched = extract_auto(sample_pdf, store)
    assert unmatched.extraction.template is None and unmatched.fields == {}


def test_learn_thai(tmp_path, make_thai_invoice):
    a = make_thai_invoice(1, "TX-0042", "5 มีนาคม 2569", [["สินค้า ก", "1", "500.00"]], "500.00")
    b = make_thai_invoice(2, "TX-0043", "9 เมษายน 2569", [["สินค้า ข", "4", "125.25"]], "501.00")
    store = TemplateStore(tmp_path)
    t = store.learn("thai_tax_invoice", [extract(a), extract(b)])
    f = _fields(t)
    assert f["doc_no"].label == ["เลขที่"] and f["doc_no"].pattern == r"[A-Z]+\-\d+"
    assert f["date"].type == "date"
    assert f["grand_total"].type == "number"
    assert {c.key for c in f["items"].columns.values()} == {"item", "qty", "price"}
    c = make_thai_invoice(3, "TX-0099", "1 พฤษภาคม 2569", [["สินค้า ค", "2", "10.00"]], "20.00")
    v = {k: x.value for k, x in extract_auto(c, store).fields.items()}
    assert v["doc_no"] == "TX-0099" and v["date"] == "2026-05-01" and v["grand_total"] == 20.0


def test_manual_spec_survives_relearning(tmp_path, invoices):
    store = TemplateStore(tmp_path)
    store.learn("acme_invoice", [extract(invoices[0])])
    store.set_spec("acme_invoice", {"fields": [{"name": "total", "type": "number", "label": "Grand Total"}]})
    t = store.learn("acme_invoice", [extract(invoices[1])])
    assert t.manual_spec and [f.name for f in t.spec.fields] == ["total"]
    assert len(t.samples) == 2  # anchors / samples still learn


def test_store_robustness(tmp_path):
    store = TemplateStore(tmp_path)
    with pytest.raises(TemplateError):
        store.load("../etc/passwd")
    with pytest.raises(TemplateError):
        store.load("missing")
    (tmp_path / "broken.template.json").write_text("{not json")
    assert store.list() == []  # corrupt file skipped, not fatal
    with pytest.raises(TemplateError):
        store.learn("x", [])


@pytest.mark.parametrize(
    ("raw", "typ"),
    [("15/03/2026", "date"), ("1,036.50 THB", "number"), ("12", "integer"), ("0105556123456", "string"),
     ("Somchai Trading", "string"), ("yes", "boolean"), ("INV-001", "string")],
)  # fmt: skip
def test_infer_type(raw, typ):
    assert infer_type(raw) == typ


def test_generalise():
    assert generalise("INV-2026-001") == r"[A-Z]+\-\d+\-\d+"
    assert generalise("Somchai Trading") is None
    assert generalise("12345") is None


def test_cli_template_flow(tmp_path, invoices, capsys):
    store = str(tmp_path / "tpl")
    assert main(["template", "--store", store, "learn", "acme", str(invoices[0]), str(invoices[1])]) == 0
    assert "invoice_no" in capsys.readouterr().out
    assert main(["template", "--store", store, "list"]) == 0
    assert capsys.readouterr().out.startswith("acme\t2 samples")
    assert main(["template", "--store", store, "match", str(invoices[2])]) == 0
    assert json.loads(capsys.readouterr().out)[0]["score"] == 1.0
    out = tmp_path / "o.json"
    rc = main([str(invoices[2]), "--auto-template", "--store", store, "--fields-only", "-o", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["template"] == "acme" and data["fields"]["invoice_no"] == "INV-2026-117"
    assert main(["template", "--store", store, "show", "nope"]) == 2
