"""Backend API of the desktop app, exercised over real HTTP."""

import http.client
import json
import threading
import time

import pytest

from pdfstruct.app.server import AppState, make_server


@pytest.fixture
def app(tmp_path):
    state = AppState(tmp_path / "templates", isolate=False)
    server = make_server(state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None, headers=None, token=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
        if token:
            hdrs["X-Token"] = state.token
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        ctype = res.getheader("Content-Type", "")
        return res.status, (json.loads(data) if "json" in ctype and data else data), res

    yield state, call, port
    server.shutdown()
    state.cleanup()


def upload(call, path, auto=1):
    return call("POST", f"/api/documents?auto_template={auto}", path.read_bytes(), {"X-Filename": path.name})


def test_auth_and_host_checks(app):
    state, call, port = app
    assert call("GET", "/api/info", token=False)[0] == 401
    assert call("GET", "/", token=False)[0] == 401
    assert call("GET", "/api/info", host="evil.example:80")[0] == 403  # DNS rebinding
    status, _, res = call("GET", f"/?token={state.token}", token=False)
    assert status == 302 and "HttpOnly" in res.getheader("Set-Cookie")
    status, _, _ = call("GET", "/api/info", token=False, headers={"Cookie": f"pdfstruct_token={state.token}"})
    assert status == 200


def test_static_files(app):
    _, call, _ = app
    status, body, res = call("GET", "/")
    assert status == 200 and b"pdfstruct" in body and "text/html" in res.getheader("Content-Type")
    assert call("GET", "/app.js")[0] == 200
    assert call("GET", "/../server.py")[0] == 404


def test_document_flow(app, sample_pdf):
    _, call, _ = app
    status, summary, _ = upload(call, sample_pdf)
    assert status == 201 and summary["page_count"] == 2 and summary["status"] == "ok"
    doc_id = summary["id"]
    status, detail, _ = call("GET", f"/api/documents/{doc_id}")
    assert detail["document"]["pages"][0]["tables"][0]["rows"][0] == ["Item", "Qty", "Price"]
    status, png, res = call("GET", f"/api/documents/{doc_id}/pages/1.png?scale=1")
    assert status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert call("GET", f"/api/documents/{doc_id}/pages/9.png")[0] == 404
    assert call("GET", "/api/documents")[1][0]["id"] == doc_id


def test_bad_upload_is_reported_not_fatal(app):
    _, call, _ = app
    status, summary, _ = call("POST", "/api/documents", b"not a pdf", {"X-Filename": "x.pdf"})
    assert status == 201 and summary["error"]["error"] == "NotAPDFError"
    assert call("GET", "/api/info")[0] == 200  # server still alive


def test_teach_match_apply_export(app, make_invoice):
    _, call, _ = app
    a = make_invoice(11, "INV-1", "01/02/2026", "A Co", [["X", "1", "1.00", "1.00"]], "1.00")
    b = make_invoice(12, "INV-2", "02/02/2026", "B Co", [["Y", "2", "2.00", "4.00"]], "4.00")
    c = make_invoice(13, "INV-3", "03/02/2026", "C Co", [["Z", "3", "3.00", "9.00"]], "9.00")
    ids = [upload(call, p, auto=0)[1]["id"] for p in (a, b)]

    assert call("POST", "/api/templates/bad%20name/learn", {"doc_ids": ids})[0] == 400
    status, body, _ = call("POST", "/api/templates/acme/learn", {"doc_ids": ids, "description": "ACME"})
    assert status == 202
    for _ in range(100):
        job = call("GET", f"/api/jobs/{body['job']}")[1]
        if job["done"]:
            break
        time.sleep(0.05)
    assert job["error"] is None
    assert {c["found"] for c in job["result"]["checks"]} == {5}
    assert call("GET", "/api/templates")[1][0]["name"] == "acme"

    # a new invoice is recognised on upload
    s = upload(call, c)[1]
    assert s["template"] == "acme" and s["fields_found"] == 5
    detail = call("GET", f"/api/documents/{s['id']}")[1]
    assert detail["document"]["fields"]["invoice_no"]["value"] == "INV-3"

    # user edit is applied to the export
    assert call("PUT", f"/api/documents/{s['id']}/edits", {"customer": "C Company"})[0] == 200
    status, exported, res = call("GET", f"/api/documents/{s['id']}/export")
    assert "attachment" in res.getheader("Content-Disposition")
    assert exported["fields"]["customer"] == {**exported["fields"]["customer"], "value": "C Company", "edited": True}

    # manual spec via YAML
    yaml_text = "fields:\n  - name: total\n    type: number\n    label: Grand Total\n"
    status, t, _ = call("PUT", "/api/templates/acme/spec", yaml_text.encode())
    assert status == 200 and t["manual_spec"] and t["fields"] == 1
    assert call("PUT", "/api/templates/acme/spec", b"fields: [")[0] == 400
    s2 = call("POST", f"/api/documents/{s['id']}/apply", {"template": "acme"})[1]
    assert s2["fields_total"] == 1
    status, yml, _ = call("GET", "/api/templates/acme/spec.yaml")
    assert b"Grand Total" in yml

    assert call("POST", f"/api/documents/{s['id']}/match")[1][0]["template"] == "acme"
    assert call("DELETE", "/api/templates/acme")[0] == 200
    assert call("GET", "/api/templates")[1] == []


def test_crop_flow(app, make_invoice):
    _, call, _ = app
    a = make_invoice(21, "INV-9", "05/05/2026", "Crop Co", [["Q", "1", "1.00", "1.00"]], "1.00")
    b = make_invoice(22, "INV-10", "06/05/2026", "Other Co", [["R", "2", "2.00", "4.00"]], "4.00")
    doc_id = upload(call, a, auto=0)[1]["id"]
    doc = call("GET", f"/api/documents/{doc_id}")[1]["document"]
    page = doc["pages"][0]
    blk = next(x for x in page["blocks"] if "Crop Co" in x["text"])
    region = {
        "page": 1,
        "x0": blk["bbox"]["x0"] / page["width"] - 0.005,
        "top": blk["bbox"]["top"] / page["height"] - 0.003,
        "x1": blk["bbox"]["x1"] / page["width"] + 0.005,
        "bottom": blk["bbox"]["bottom"] / page["height"] + 0.003,
    }

    status, prev, _ = call("POST", f"/api/documents/{doc_id}/region-preview", {"region": region, "type": "string"})
    assert status == 200 and prev["found"] and prev["value"] == "Crop Co"
    bad = {**region, "x1": region["x0"]}
    assert call("POST", f"/api/documents/{doc_id}/region-preview", {"region": bad})[0] == 400

    status, t, _ = call(
        "POST", "/api/templates/crop_tpl/fields", {"field": {"name": "buyer", "region": region}, "doc_id": doc_id}
    )
    assert status == 201 and t["custom_fields"] == ["buyer"]
    s = call("POST", f"/api/documents/{doc_id}/apply", {"template": "crop_tpl"})[1]
    assert s["template"] == "crop_tpl"
    detail = call("GET", f"/api/documents/{doc_id}")[1]["document"]
    assert detail["fields"]["buyer"]["value"] == "Crop Co" and detail["fields"]["buyer"]["bbox"]

    # a second invoice of the same layout is recognised and read from the box
    s2 = upload(call, b)[1]
    assert s2["template"] == "crop_tpl"
    assert call("GET", f"/api/documents/{s2['id']}")[1]["document"]["fields"]["buyer"]["value"] == "Other Co"

    assert call("DELETE", "/api/templates/crop_tpl/fields/buyer")[1]["custom_fields"] == []
    assert call("DELETE", "/api/templates/crop_tpl/fields/buyer")[0] == 400


def test_export_formats_and_prompts(app, invoice_pdf):
    state, call, _ = app
    doc_id = upload(call, invoice_pdf, auto=0)[1]["id"]
    entry = state.doc(doc_id)
    from pdfstruct.fields import Spec, apply_spec

    apply_spec(
        entry.doc, Spec.model_validate({"fields": [{"name": "total", "type": "number", "label": "Grand Total"}]})
    )
    call("PUT", f"/api/documents/{doc_id}/edits", {"total": 5})

    status, full, res = call("GET", f"/api/documents/{doc_id}/export?format=full")
    assert status == 200 and full["fields"]["total"]["value"] == 5 and full["fields"]["total"]["edited"]
    status, fields, res = call("GET", f"/api/documents/{doc_id}/export?format=fields")
    assert fields["fields"] == {"total": 5} and ".fields.json" in res.getheader("Content-Disposition")
    status, body, res = call("GET", f"/api/documents/{doc_id}/export?format=csv")
    assert (
        status == 200
        and "text/csv" in res.getheader("Content-Type")
        and body.decode("utf-8-sig").startswith("field,value")
    )
    assert call("GET", f"/api/documents/{doc_id}/export?format=xml")[0] == 400

    status, prompts, _ = call("GET", f"/api/documents/{doc_id}/prompts")
    assert status == 200 and len(prompts) == 7 and '"total": 5' in prompts[0]["text"]


def test_locked_pdf_can_be_unlocked(app, encrypted_pdf):
    state, call, _ = app
    status, s, _ = upload(call, encrypted_pdf)
    assert status == 201 and s["locked"] and s["error"]["error"] == "EncryptedPDFError"
    doc_id = s["id"]

    status, body, _ = call("POST", f"/api/documents/{doc_id}/unlock", {"password": "wrong"})
    assert status == 403 and body["message"] == "รหัสผ่านไม่ถูกต้อง"
    assert call("POST", f"/api/documents/{doc_id}/unlock", {"password": ""})[0] == 403
    assert call("GET", "/api/documents")[1][0]["locked"]  # still locked

    status, s, _ = call("POST", f"/api/documents/{doc_id}/unlock", {"password": "secret"})
    assert status == 200 and not s["locked"] and s["encrypted"] and s["page_count"] == 2
    assert "password" not in json.dumps(call("GET", f"/api/documents/{doc_id}")[1])  # never sent back
    assert call("POST", f"/api/documents/{doc_id}/unlock", {"password": "secret"})[0] == 400  # not locked anymore

    # everything that re-opens the file works with the remembered password
    status, png, _ = call("GET", f"/api/documents/{doc_id}/pages/1.png?scale=1")
    assert status == 200 and png[:4] == b"\x89PNG"
    region = {"page": 1, "x0": 0.05, "top": 0.05, "x1": 0.95, "bottom": 0.2}
    status, prev, _ = call("POST", f"/api/documents/{doc_id}/region-preview", {"region": region})
    assert status == 200 and prev["found"]
    status, _, _ = call(
        "POST", "/api/templates/locked_tpl/fields", {"field": {"name": "top", "region": region}, "doc_id": doc_id}
    )
    assert status == 201
    s = call("POST", f"/api/documents/{doc_id}/apply", {"template": "locked_tpl"})[1]
    assert s["fields_found"] == 1


def test_tables_and_excel_endpoints(app, sample_pdf):
    _, call, _ = app
    doc_id = upload(call, sample_pdf, auto=0)[1]["id"]
    status, tables, _ = call("GET", f"/api/documents/{doc_id}/tables")
    assert status == 200 and tables[0]["header"] == ["Item", "Qty", "Price"] and tables[0]["n_rows"] == 2
    status, body, res = call("GET", f"/api/documents/{doc_id}/export?format=xlsx")
    assert status == 200 and "spreadsheetml" in res.getheader("Content-Type") and body[:2] == b"PK"
    assert ".xlsx" in res.getheader("Content-Disposition")
    status, body, _ = call("GET", f"/api/documents/{doc_id}/export?format=table-csv&table=1")
    assert status == 200 and body.decode("utf-8-sig").startswith("Item,Qty,Price")
    assert call("GET", f"/api/documents/{doc_id}/export?format=table-csv&table=9")[0] == 404
