"""Local HTTP backend for the desktop app (stdlib only).

Security model: the server binds to 127.0.0.1 only, checks the Host header
(blocks DNS-rebinding), and every request must carry the per-launch secret
token, either as the ``pdfstruct_token`` cookie (set when the window first
opens ``/?token=...``) or an ``X-Token`` header. Uploaded PDFs live in a
private temporary folder that is deleted when the app exits.
"""

from __future__ import annotations

import io
import json
import mimetypes
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__, export, prompts
from ..errors import EncryptedPDFError, PDFStructError
from ..extractor import extract, extract_isolated
from ..fields import FieldSpec, apply_spec, evaluate_field
from ..models import Document
from ..templates import TemplateStore, spec_yaml

MAX_UPLOAD = 200 * 1024 * 1024
STATIC = resources.files("pdfstruct.app") / "static"
_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass
class DocEntry:
    id: str
    filename: str
    path: Path
    added_at: float
    doc: Document | None = None
    error: dict | None = None
    edits: dict[str, Any] = field(default_factory=dict)
    # Kept in memory only (never written to disk or returned by the API), so a locked
    # PDF can be re-opened for page images and cropped fields until the app closes.
    password: str | None = field(default=None, repr=False)

    @property
    def locked(self) -> bool:
        return bool(self.error and self.error.get("error") == "EncryptedPDFError")

    def summary(self) -> dict:
        d = self.doc
        if d is None:
            return {
                "id": self.id,
                "filename": self.filename,
                "error": self.error,
                "locked": self.locked,
                "added_at": self.added_at,
            }
        found = sum(1 for f in d.fields.values() if f.found)
        return {
            "id": self.id,
            "filename": self.filename,
            "added_at": self.added_at,
            "page_count": d.source.page_count,
            "size_bytes": d.source.size_bytes,
            "status": d.extraction.status,
            "needs_ocr": any(p.needs_ocr for p in d.pages),
            "template": d.extraction.template,
            "template_score": d.extraction.template_score,
            "fields_found": found,
            "fields_total": len(d.fields),
            "missing_required": d.extraction.missing_required_fields,
            "encrypted": d.source.encrypted,
            "locked": False,
            "error": None,
        }


@dataclass
class Job:
    id: str
    kind: str
    step: int = 0
    total: int = 1
    message: str = ""
    done: bool = False
    error: str | None = None
    result: Any = None


class AppState:
    def __init__(self, store_dir: Path, *, isolate: bool = True, timeout: float = 120.0):
        self.store = TemplateStore(store_dir)
        self.workdir = Path(tempfile.mkdtemp(prefix="pdfstruct-app-"))
        self.docs: dict[str, DocEntry] = {}
        self.jobs: dict[str, Job] = {}
        self.isolate = isolate
        self.timeout = timeout
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(24)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def _extract(self, path: Path, password: str | None = None) -> Document:
        if self.isolate:
            return extract_isolated(path, timeout=self.timeout, password=password)
        return extract(path, password=password)

    def _process(self, entry: DocEntry, auto_template: bool, password: str | None = None) -> None:
        doc = self._extract(entry.path, password)  # raises EncryptedPDFError for a missing/wrong password
        doc.source.filename = entry.filename
        if auto_template:
            found = self.store.best(doc)
            if found:
                tpl, res = found
                apply_spec(doc, tpl.spec, pdf_path=entry.path, password=password)
                doc.extraction.template, doc.extraction.template_score = tpl.name, res.score
        entry.doc, entry.error, entry.password = doc, None, password

    def add_document(self, filename: str, data: bytes, auto_template: bool, password: str | None = None) -> DocEntry:
        doc_id = uuid.uuid4().hex
        safe = re.sub(r"[^\w.\-ก-๙ ]+", "_", Path(filename).name)[:120] or "document.pdf"
        path = self.workdir / f"{doc_id}.pdf"
        path.write_bytes(data)
        entry = DocEntry(id=doc_id, filename=safe, path=path, added_at=time.time())
        try:
            self._process(entry, auto_template, password)
        except PDFStructError as exc:
            entry.error = {"error": type(exc).__name__, "message": str(exc)}
        with self.lock:
            self.docs[doc_id] = entry
        return entry

    def unlock(self, entry: DocEntry, password: str, auto_template: bool) -> DocEntry:
        """Try a password on a locked document. Raises EncryptedPDFError if it is wrong."""
        if not entry.locked:
            raise ValueError("document is not locked")
        if not password:
            raise EncryptedPDFError("password is empty")
        self._process(entry, auto_template, password)
        return entry

    def doc(self, doc_id: str) -> DocEntry:
        entry = self.docs.get(doc_id)
        if entry is None:
            raise KeyError(doc_id)
        return entry

    def learn_async(self, name: str, doc_ids: list[str], description: str | None) -> Job:
        job = Job(id=uuid.uuid4().hex, kind="learn", total=len(doc_ids) + 2)
        self.jobs[job.id] = job

        def run() -> None:
            try:
                docs = []
                for i, did in enumerate(doc_ids):
                    entry = self.doc(did)
                    job.step, job.message = i, f"อ่านตัวอย่าง {i + 1}/{len(doc_ids)}: {entry.filename}"
                    if entry.doc is None:
                        raise PDFStructError(f"{entry.filename} อ่านไม่ได้ จึงใช้เป็นตัวอย่างไม่ได้")
                    docs.append(entry.doc)
                    time.sleep(0.05)
                job.step, job.message = len(doc_ids), "เทียบตัวอย่าง แยกตัวแปร / ข้อความคงที่"
                tpl = self.store.learn(name, docs, description=description)
                job.step, job.message = len(doc_ids) + 1, "ทดสอบ Template กับตัวอย่าง"
                checks = []
                for entry_id, d in zip(doc_ids, docs, strict=True):
                    test = Document.model_validate(d.model_dump())
                    test.fields = {}
                    test.extraction.missing_required_fields = []
                    src = self.docs[entry_id]
                    apply_spec(test, tpl.spec, pdf_path=src.path, password=src.password)
                    checks.append(
                        {
                            "doc_id": entry_id,
                            "found": sum(1 for f in test.fields.values() if f.found),
                            "total": len(test.fields),
                        }
                    )
                job.result = {"template": template_detail(tpl), "checks": checks}
                job.step, job.message = job.total, "เสร็จแล้ว"
            except (PDFStructError, KeyError) as exc:
                job.error = str(exc)
            except Exception as exc:  # never leave the UI polling forever
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.done = True

        threading.Thread(target=run, daemon=True).start()
        return job


def template_summary(t) -> dict:
    rates = [s.found_in / s.samples for s in t.field_stats if s.samples]
    return {
        "name": t.name,
        "description": t.description,
        "samples": len(t.samples),
        "fields": len(t.spec.fields),
        "anchors": len(t.anchors),
        "found_rate": round(sum(rates) / len(rates), 3) if rates else None,
        "manual_spec": t.manual_spec,
        "auto_fields": t.auto_fields,
        "updated_at": t.updated_at,
    }


def template_detail(t) -> dict:
    stats = {s.name: {"found_in": s.found_in, "samples": s.samples} for s in t.field_stats}
    return {
        **template_summary(t),
        "anchors_list": t.anchors,
        "spec": json.loads(t.spec.model_dump_json(exclude_none=True)),
        "spec_yaml": spec_yaml(t.spec),
        "stats": stats,
        "sample_files": [s.filename for s in t.samples],
        "custom_fields": [f.name for f in t.custom_fields],
    }


def render_page_png(path: Path, page_no: int, scale: float, password: str | None = None) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path), password=password)
    try:
        if not 1 <= page_no <= len(pdf):
            raise KeyError(page_no)
        page = pdf[page_no - 1]
        try:
            image = page.render(scale=scale).to_pil()
        finally:
            page.close()
    finally:
        pdf.close()
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_handler(state: AppState, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"pdfstruct/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'",
            )
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200, extra: dict | None = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def _error(self, code: int, message: str) -> None:
            self._json({"error": HTTPStatus(code).phrase, "message": message}, code)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _authed(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Token", ""), state.token):
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("pdfstruct_token")
            return bool(morsel and secrets.compare_digest(morsel.value, state.token))

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                raise ValueError(f"file larger than {MAX_UPLOAD // (1024 * 1024)} MB")
            return self.rfile.read(length) if length else b""

        def _guard(self) -> tuple[list[str], dict] | None:
            if not self._host_ok():
                self._error(403, "bad host")
                return None
            url = urlparse(self.path)
            query = parse_qs(url.query)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            if url.path == "/" and "token" in query:
                if secrets.compare_digest(query["token"][0], state.token):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"pdfstruct_token={state.token}; Path=/; HttpOnly; SameSite=Strict")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
            if not self._authed():
                if parts and parts[0] == "api":
                    self._error(401, "missing or wrong token")
                else:
                    self._send(
                        401,
                        b"<h1>pdfstruct</h1><p>Open the app from the pdfstruct-app window.</p>",
                        "text/html; charset=utf-8",
                    )
                return None
            return parts, query

        # ---------------------------------------------------------- routing
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            guarded = self._guard()
            if guarded is None:
                return
            parts, query = guarded
            try:
                if not parts or parts[0] != "api":
                    return self._static(parts) if method == "GET" else self._error(405, "method not allowed")
                self._api(method, parts[1:], query)
            except KeyError:
                self._error(404, "not found")
            except (ValueError, PDFStructError) as exc:
                self._error(400, str(exc))
            except Exception as exc:  # last resort: report, never crash the server thread
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _static(self, parts: list[str]) -> None:
            name = "/".join(parts) or "index.html"
            if ".." in name or name.startswith("/"):
                raise KeyError(name)
            res = STATIC.joinpath(*name.split("/"))
            if not res.is_file():
                raise KeyError(name)
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "text/javascript"):
                ctype += "; charset=utf-8"
            self._send(200, res.read_bytes(), ctype)

        def _api(self, method: str, p: list[str], q: dict) -> None:
            route = (method, *p[:1])
            if route == ("GET", "info"):
                from .. import ocr

                return self._json(
                    {"version": __version__, "store": str(state.store.root.resolve()), "ocr": ocr.status().to_dict()}
                )

            # documents ---------------------------------------------------
            if p[:1] == ["documents"]:
                if method == "GET" and len(p) == 1:
                    items = sorted(state.docs.values(), key=lambda e: -e.added_at)
                    return self._json([e.summary() for e in items])
                if method == "POST" and len(p) == 1:
                    data = self._body()
                    filename = unquote(self.headers.get("X-Filename", "document.pdf"))
                    auto = q.get("auto_template", ["1"])[0] == "1"
                    entry = state.add_document(filename, data, auto)
                    return self._json(entry.summary(), 201)
                entry = state.doc(p[1])
                if method == "GET" and len(p) == 2:
                    payload = entry.doc.model_dump(mode="json") if entry.doc else None
                    return self._json({"summary": entry.summary(), "document": payload, "edits": entry.edits})
                if method == "DELETE" and len(p) == 2:
                    state.docs.pop(entry.id, None)
                    entry.path.unlink(missing_ok=True)
                    return self._json({"ok": True})
                if method == "GET" and len(p) == 4 and p[2] == "pages":
                    if entry.doc is None:
                        raise KeyError("no document")
                    page_no = int(p[3].removesuffix(".png"))
                    scale = min(max(float(q.get("scale", ["1.5"])[0]), 0.5), 4.0)
                    return self._send(200, render_page_png(entry.path, page_no, scale, entry.password), "image/png")
                if method == "PUT" and p[2:] == ["edits"]:
                    edits = json.loads(self._body() or b"{}")
                    if not isinstance(edits, dict):
                        raise ValueError("edits must be an object")
                    entry.edits = edits
                    return self._json({"ok": True})
                if method == "POST" and p[2:] == ["match"]:
                    if entry.doc is None:
                        raise ValueError("document could not be read")
                    return self._json([r.model_dump() for r in state.store.match(entry.doc)])
                if method == "POST" and p[2:] == ["apply"]:
                    if entry.doc is None:
                        raise ValueError("document could not be read")
                    name = json.loads(self._body() or b"{}").get("template")
                    tpl = state.store.load(name)
                    entry.doc.fields = {}
                    entry.doc.extraction.missing_required_fields = []
                    apply_spec(entry.doc, tpl.spec, pdf_path=entry.path, password=entry.password)
                    result = next((r for r in state.store.match(entry.doc) if r.template == name), None)
                    entry.doc.extraction.template = name
                    entry.doc.extraction.template_score = result.score if result else None
                    entry.edits = {}
                    return self._json(entry.summary())
                if method == "POST" and p[2:] == ["unlock"]:
                    body = json.loads(self._body() or b"{}")
                    try:
                        auto = body.get("auto_template", True) is not False
                        state.unlock(entry, str(body.get("password") or ""), auto)
                    except EncryptedPDFError:
                        return self._json({"error": "EncryptedPDFError", "message": "รหัสผ่านไม่ถูกต้อง"}, 403)
                    return self._json(entry.summary())
                if method == "POST" and p[2:] == ["region-preview"]:
                    body = json.loads(self._body() or b"{}")
                    spec = FieldSpec(
                        name="preview",
                        type=body.get("type") or "string",
                        region=body.get("region"),
                        pattern=body.get("pattern") or None,
                    )
                    import pdfplumber

                    with pdfplumber.open(str(entry.path), password=entry.password or "") as pdf:
                        fv = evaluate_field(entry.doc, spec, pdf)
                    return self._json(fv.model_dump(mode="json"))
                if method == "GET" and p[2:] == ["export"]:
                    if entry.doc is None:
                        raise ValueError("document could not be read")
                    fmt = q.get("format", ["full"])[0]
                    stem = quote(Path(entry.filename).stem)
                    if fmt == "xlsx":
                        body = export.xlsx_export(entry.doc, entry.edits)
                        disp = f"attachment; filename*=UTF-8''{stem}.xlsx"
                        xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        return self._send(200, body, xlsx, {"Content-Disposition": disp})
                    if fmt == "table-csv":
                        tables = export.merged_tables(entry.doc)
                        idx = int(q.get("table", ["1"])[0])
                        if not 1 <= idx <= len(tables):
                            raise KeyError("table")
                        body = export.table_csv(tables[idx - 1]).encode("utf-8")
                        disp = f"attachment; filename*=UTF-8''{stem}.table{idx}.csv"
                        return self._send(200, body, "text/csv; charset=utf-8", {"Content-Disposition": disp})
                    if fmt == "csv":
                        body = prompts.csv_export(entry.doc, entry.edits).encode("utf-8")
                        disp = f"attachment; filename*=UTF-8''{stem}.csv"
                        return self._send(200, body, "text/csv; charset=utf-8", {"Content-Disposition": disp})
                    if fmt == "fields":
                        disp = f"attachment; filename*=UTF-8''{stem}.fields.json"
                        data = prompts.fields_export(entry.doc, entry.edits)
                        return self._json(data, extra={"Content-Disposition": disp})
                    if fmt != "full":
                        raise ValueError("format must be full, fields, csv, xlsx or table-csv")
                    disp = f"attachment; filename*=UTF-8''{stem}.json"
                    return self._json(prompts.full_export(entry.doc, entry.edits), extra={"Content-Disposition": disp})
                if method == "GET" and p[2:] == ["tables"]:
                    if entry.doc is None:
                        raise ValueError("document could not be read")
                    return self._json(export.merged_tables(entry.doc))
                if method == "GET" and p[2:] == ["prompts"]:
                    if entry.doc is None:
                        raise ValueError("document could not be read")
                    return self._json([x.to_dict() for x in prompts.build_prompts(entry.doc, entry.edits)])

            # templates ---------------------------------------------------
            if p[:1] == ["templates"]:
                if method == "GET" and len(p) == 1:
                    return self._json([template_summary(t) for t in state.store.list()])
                name = p[1]
                if method == "GET" and len(p) == 2:
                    return self._json(template_detail(state.store.load(name)))
                if method == "GET" and p[2:] == ["spec.yaml"]:
                    body = spec_yaml(state.store.load(name).spec).encode()
                    disp = f"attachment; filename*=UTF-8''{quote(name)}.yaml"
                    return self._send(200, body, "text/yaml; charset=utf-8", {"Content-Disposition": disp})
                if method == "POST" and p[2:] == ["learn"]:
                    body = json.loads(self._body() or b"{}")
                    ids = [i for i in body.get("doc_ids", []) if isinstance(i, str) and _ID.match(i)]
                    if not ids:
                        raise ValueError("select at least one sample document")
                    state.store._path(name)  # validates the name before starting the job
                    job = state.learn_async(name, ids, body.get("description") or None)
                    return self._json({"job": job.id}, 202)
                if method == "POST" and p[2:] == ["fields"]:
                    body = json.loads(self._body() or b"{}")
                    sample = None
                    if body.get("doc_id"):
                        sample = state.doc(body["doc_id"]).doc
                    tpl = state.store.add_field(name, body.get("field") or {}, sample)
                    return self._json(template_detail(tpl), 201)
                if method == "DELETE" and len(p) == 4 and p[2] == "fields":
                    return self._json(template_detail(state.store.remove_field(name, p[3])))
                if method == "PUT" and p[2:] == ["spec"]:
                    import yaml

                    text = self._body().decode("utf-8")
                    try:
                        data = yaml.safe_load(text) or {}
                    except yaml.YAMLError as exc:
                        raise ValueError(f"YAML ไม่ถูกต้อง: {exc}") from exc
                    tpl = state.store.set_spec(name, data)
                    return self._json(template_detail(tpl))
                if method == "DELETE" and len(p) == 2:
                    state.store.delete(name)
                    return self._json({"ok": True})

            if method == "GET" and p[:1] == ["jobs"] and len(p) == 2:
                job = state.jobs[p[1]]
                return self._json(
                    {"step": job.step, "total": job.total, "message": job.message, "done": job.done,
                     "error": job.error, "result": job.result}
                )  # fmt: skip

            raise KeyError("/".join(p))

    return Handler


def make_server(state: AppState, port: int = 0) -> ThreadingHTTPServer:
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, port_ref))
    port_ref[0] = server.server_address[1]
    server.daemon_threads = True
    return server
