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
