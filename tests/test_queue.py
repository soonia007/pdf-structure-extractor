"""Background queue of the app: many files at once, learning while files are read, live status, cancel."""

import http.client
import json
import threading
import time

import pytest

import pdfstruct.app.server as server_mod
from pdfstruct.app.server import AppState, make_server


def _client(state, port):
    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers = {"Host": f"127.0.0.1:{port}", "X-Token": state.token}
        if isinstance(body, dict | list):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, json.loads(data) if data else None

    return call


@pytest.fixture
def make_app(tmp_path):
    servers = []

    def make(workers=3):
        state = AppState(tmp_path / f"t{len(servers)}", isolate=False, workers=workers)
        srv = make_server(state)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append((srv, state))
        return state, _client(state, srv.server_address[1])

    yield make
    for srv, state in servers:
        srv.shutdown()
        state.cleanup()


@pytest.fixture
def slow_extract(monkeypatch):
    """Make reading take a while (and report progress) so queue states can be observed."""
    real = server_mod.extract

    def slow(path, *, progress=None, **kw):
        for i in range(10):
            if progress:
                progress("page", i, 10)  # raises ExtractionCancelled when cancelled
            time.sleep(0.05)
        return real(path, **kw)

    monkeypatch.setattr(server_mod, "extract", slow)


def _upload(call, path, auto=1):
    return call("POST", f"/api/documents?auto_template={auto}", path.read_bytes())


def _wait(call, pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        act = call("GET", "/api/activity")[1]
        if pred(act):
            return act
        time.sleep(0.05)
    raise AssertionError("timed out: " + json.dumps(act, ensure_ascii=False)[:500])


def test_upload_returns_at_once_and_reports_progress(make_app, invoice_pdf, slow_extract):
    _, call = make_app(workers=1)
    status, s = _upload(call, invoice_pdf)
    assert status == 202 and s["state"] in ("queued", "reading") and "page_count" not in s
    seen = set()

    def progressed(act):
        doc = act["documents"][0]
        seen.add(doc["progress"]["text"])
        return doc["state"] == "done"

    act = _wait(call, progressed)
    assert any(t.startswith("อ่านหน้า") for t in seen)  # live page progress was visible
    assert act["documents"][0]["progress"]["percent"] == 100 and not act["active"]
    assert call("GET", f"/api/documents/{s['id']}")[1]["summary"]["page_count"] == 1


def test_many_files_are_read_in_parallel_up_to_the_limit(make_app, invoice_pdf, slow_extract):
    _, call = make_app(workers=2)
    ids = [_upload(call, invoice_pdf)[1]["id"] for _ in range(6)]
    peak = 0
    positions = set()

    def all_done(act):
        nonlocal peak
        peak = max(peak, act["counts"]["reading"])
        positions.update(d["progress"]["text"] for d in act["documents"] if d["state"] == "queued")
        return act["counts"]["done"] == 6

    _wait(call, all_done)
    assert peak == 2  # never more than the worker limit
    assert any("ลำดับที่" in t for t in positions)  # queued files show their place in the queue
    assert {call("GET", f"/api/documents/{i}")[1]["summary"]["state"] for i in ids} == {"done"}


def test_learning_waits_for_files_still_being_read(make_app, make_invoice, slow_extract):
    _, call = make_app(workers=1)
    a = make_invoice(41, "INV-41", "01/03/2026", "Q Co", [["Q", "1", "1.00", "1.00"]], "1.00")
    b = make_invoice(42, "INV-42", "02/03/2026", "R Co", [["R", "2", "2.00", "4.00"]], "4.00")
    ids = [_upload(call, p, auto=0)[1]["id"] for p in (a, b)]
    status, body = call("POST", "/api/templates/early/learn", {"doc_ids": ids})
    assert status == 202
    messages = set()

    def done(act):
        job = act["jobs"][0]
        messages.add(job["message"])
        return job["done"]

    _wait(call, done)
    assert any(m.startswith("รอไฟล์ตัวอย่างอ่านเสร็จ") for m in messages)
    job = call("GET", f"/api/jobs/{body['job']}")[1]
    assert job["state"] == "done" and job["error"] is None and job["percent"] == 100
    assert [c["found"] for c in job["result"]["checks"]] == [5, 5]


def test_several_templates_train_at_the_same_time(make_app, make_invoice):
    _, call = make_app(workers=3)
    pdfs = [make_invoice(50 + i, f"INV-{i}", f"0{i + 1}/04/2026", f"C{i}", [["X", "1", "1.00", "1.00"]], "1.00")
            for i in range(4)]  # fmt: skip
    ids = [_upload(call, p, auto=0)[1]["id"] for p in pdfs]
    jobs = [
        call("POST", "/api/templates/alpha/learn", {"doc_ids": ids[:2]})[1]["job"],
        call("POST", "/api/templates/beta/learn", {"doc_ids": ids[2:]})[1]["job"],
        call("POST", "/api/templates/alpha/learn", {"doc_ids": ids[2:]})[1]["job"],  # same template at once
    ]
    _wait(call, lambda act: all(j["done"] for j in act["jobs"]))
    states = [call("GET", f"/api/jobs/{j}")[1]["state"] for j in jobs]
    assert states == ["done", "done", "done"]
    templates = {t["name"]: t for t in call("GET", "/api/templates")[1]}
    assert set(templates) == {"alpha", "beta"}
    assert templates["alpha"]["samples"] == 4  # both jobs' samples, no lost update
    assert len(call("GET", "/api/jobs")[1]) == 3


def test_cancel_queued_and_running_then_retry(make_app, invoice_pdf, slow_extract):
    _, call = make_app(workers=1)
    first = _upload(call, invoice_pdf)[1]["id"]
    second = _upload(call, invoice_pdf)[1]["id"]
    _wait(call, lambda act: any(d["id"] == first and d["state"] == "reading" for d in act["documents"]))
    s = call("POST", f"/api/documents/{second}/cancel")[1]
    assert s["state"] == "cancelled"  # queued: cancelled at once
    call("POST", f"/api/documents/{first}/cancel")
    _wait(call, lambda act: not act["active"])
    assert call("GET", f"/api/documents/{first}")[1]["summary"]["state"] == "cancelled"  # stopped mid-read

    status, s = call("POST", f"/api/documents/{second}/retry")
    assert status == 202
    _wait(call, lambda act: not act["active"])
    assert call("GET", f"/api/documents/{second}")[1]["summary"]["state"] == "done"
    assert call("POST", f"/api/documents/{second}/retry")[0] == 400  # nothing to retry


def test_cancel_learning_job_while_waiting(make_app, invoice_pdf, slow_extract):
    _, call = make_app(workers=1)
    ids = [_upload(call, invoice_pdf, auto=0)[1]["id"] for _ in range(3)]
    job = call("POST", "/api/templates/nope/learn", {"doc_ids": ids})[1]["job"]
    call("POST", f"/api/jobs/{job}/cancel")
    _wait(call, lambda act: act["jobs"][0]["done"])
    assert call("GET", f"/api/jobs/{job}")[1]["state"] == "cancelled"
    assert call("GET", "/api/templates")[1] == []


def test_isolated_timeout_is_measured_from_last_progress(sample_pdf):
    """A long document that keeps reporting progress is not cut off by the timeout."""
    from pdfstruct import extract_isolated

    events = []
    doc = extract_isolated(sample_pdf, timeout=30, progress=lambda *a: events.append(a))
    assert doc.source.page_count == 2
    assert events[0] == ("open", 0, 2) and events[-1] == ("page", 2, 2)
