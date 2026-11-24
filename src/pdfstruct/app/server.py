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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__, export, prompts
from ..errors import EncryptedPDFError, ExtractionCancelled, PDFStructError
from ..extractor import extract, extract_isolated
from ..fields import FieldSpec, apply_spec, evaluate_field
from ..models import Document
from ..templates import TemplateStore, spec_yaml

MAX_UPLOAD = 200 * 1024 * 1024
STATIC = resources.files("pdfstruct.app") / "static"
_ID = re.compile(r"^[a-f0-9]{32}$")


BUSY = ("queued", "reading")


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
    auto_template: bool = True
    # queue / progress
    state: str = "queued"  # queued -> reading -> done | error | cancelled
    stage: str = ""  # open | page | ocr | fields
    units_done: int = 0
    units_total: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def locked(self) -> bool:
        return bool(self.error and self.error.get("error") == "EncryptedPDFError")

    @property
    def busy(self) -> bool:
        return self.state in BUSY

    def progress(self, queue_pos: int | None = None) -> dict:
        n, k = self.units_total, self.units_done
        if self.state == "queued":
            text, pct = (f"รอคิว (ลำดับที่ {queue_pos})" if queue_pos else "รอคิว"), 0
        elif self.state == "reading":
            if self.stage == "ocr":
                text = f"OCR หน้า {k + 1}/{n}"
            elif self.stage == "fields":
                text = "จับคู่ Template และดึงตัวแปร"
            elif self.stage == "page" and n and k < n:
                text = f"อ่านหน้า {k + 1}/{n}"
            elif self.stage == "page":
                text = "จัดโครงสร้างเอกสาร"
            else:
                text = "เปิดไฟล์"
            pct = round(100 * min(k, n) / n * 0.95) if n else 2
            pct = 99 if self.stage == "fields" else max(pct, 2)
        elif self.state == "done":
            text, pct = "เสร็จ", 100
        elif self.state == "cancelled":
            text, pct = "ยกเลิกแล้ว", 0
        else:
            text, pct = ("ล็อกด้วยรหัสผ่าน" if self.locked else (self.error or {}).get("message", "อ่านไม่ได้")), 100
        end = self.finished_at or time.time()
        return {
            "state": self.state,
            "stage": self.stage,
            "done": k,
            "total": n,
            "percent": pct,
            "text": text,
            "elapsed": round(end - self.started_at, 1) if self.started_at else 0.0,
        }

    def brief(self, queue_pos: int | None = None) -> dict:
        return {
            "id": self.id,
            "kind": "document",
            "filename": self.filename,
            "state": self.state,
            "progress": self.progress(queue_pos),
            "locked": self.locked,
            "added_at": self.added_at,
            "finished_at": self.finished_at,
        }

    def summary(self, queue_pos: int | None = None) -> dict:
        d = self.doc
        base = {
            "id": self.id,
            "filename": self.filename,
            "added_at": self.added_at,
            "state": self.state,
            "progress": self.progress(queue_pos),
            "locked": self.locked,
            "error": self.error,
        }
        if d is None:
            return base
        found = sum(1 for f in d.fields.values() if f.found)
        return {
            **base,
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
            "error": None,
        }


@dataclass
class Job:
    id: str
    kind: str
    name: str = ""
    doc_ids: list[str] = field(default_factory=list)
    state: str = "queued"  # queued -> waiting -> learning -> done | error | cancelled
    step: int = 0
    total: int = 1
    message: str = "รอคิว"
    done: bool = False
    error: str | None = None
    result: Any = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def summary(self, with_result: bool = True) -> dict:
        end = self.finished_at or time.time()
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "state": self.state,
            "step": self.step,
            "total": self.total,
            "percent": 100 if self.done and not self.error else round(100 * self.step / max(self.total, 1)),
            "message": self.message,
            "done": self.done,
            "error": self.error,
            "doc_count": len(self.doc_ids),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed": round(end - (self.started_at or self.created_at), 1),
            "result": self.result if with_result else None,
        }


class AppState:
    def __init__(self, store_dir: Path, *, isolate: bool = True, timeout: float = 120.0, workers: int = 3):
        self.store = TemplateStore(store_dir)
        self.workdir = Path(tempfile.mkdtemp(prefix="pdfstruct-app-"))
        self.docs: dict[str, DocEntry] = {}
        self.jobs: dict[str, Job] = {}
        self.isolate = isolate
        self.timeout = timeout  # seconds without progress before a document is given up
        self.workers = max(1, workers)
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(24)
        # documents are read in parallel by a small pool; learning jobs have their own pool
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="pdfstruct-read")
        self.learn_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pdfstruct-learn")
        self._template_locks: dict[str, threading.Lock] = {}

    def cleanup(self) -> None:
        for entry in list(self.docs.values()):
            entry.cancel.set()
        for job in list(self.jobs.values()):
            job.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.learn_pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def template_lock(self, name: str) -> threading.Lock:
        """One writer per template: two jobs never rewrite the same template file at once."""
        with self.lock:
            return self._template_locks.setdefault(name, threading.Lock())

    def queue_position(self, entry: DocEntry) -> int | None:
        if entry.state != "queued":
            return None
        queued = sorted((e for e in self.docs.values() if e.state == "queued"), key=lambda e: e.added_at)
        return next((i + 1 for i, e in enumerate(queued) if e.id == entry.id), None)

    # ------------------------------------------------------------------ #
    def _extract(self, entry: DocEntry, password: str | None, progress) -> Document:
        if self.isolate:
            return extract_isolated(
                entry.path, timeout=self.timeout, password=password, progress=progress, cancel=entry.cancel
            )

        def checked(stage: str, done: int, total: int) -> None:
            if entry.cancel.is_set():
                raise ExtractionCancelled("cancelled")
            progress(stage, done, total)

        return extract(entry.path, password=password, progress=checked)

    def _run(self, entry: DocEntry, password: str | None) -> None:
        """Worker: read one queued document, reporting progress as it goes."""
        if entry.cancel.is_set():
            entry.state, entry.finished_at = "cancelled", time.time()
            entry.finished.set()
            return
        entry.state, entry.started_at, entry.stage = "reading", time.time(), "open"

        def progress(stage: str, done: int, total: int) -> None:
            entry.stage, entry.units_done, entry.units_total = stage, done, total

        try:
            doc = self._extract(entry, password, progress)
            doc.source.filename = entry.filename
            if entry.auto_template:
                entry.stage = "fields"
                found = self.store.best(doc)
                if found:
                    tpl, res = found
                    apply_spec(doc, tpl.spec, pdf_path=entry.path, password=password)
                    doc.extraction.template, doc.extraction.template_score = tpl.name, res.score
            entry.doc, entry.error, entry.password = doc, None, password
            entry.state = "done"
        except ExtractionCancelled:
            entry.state = "cancelled"
            entry.error = {"error": "ExtractionCancelled", "message": "ยกเลิกแล้ว"}
        except PDFStructError as exc:
            entry.state, entry.error = "error", {"error": type(exc).__name__, "message": str(exc)}
        except Exception as exc:  # a worker must never die silently
            entry.state, entry.error = "error", {"error": type(exc).__name__, "message": str(exc)[:300]}
        finally:
            entry.finished_at = time.time()
            entry.finished.set()

    def _enqueue(self, entry: DocEntry, password: str | None) -> None:
        entry.state, entry.stage, entry.units_done, entry.units_total = "queued", "", 0, 0
        entry.started_at = entry.finished_at = None
        entry.cancel, entry.finished = threading.Event(), threading.Event()
        self.pool.submit(self._run, entry, password)

    def add_document(
        self, filename: str, data: bytes, auto_template: bool, password: str | None = None, wait: bool = False
    ) -> DocEntry:
        doc_id = uuid.uuid4().hex
        safe = re.sub(r"[^\w.\-ก-๙ ]+", "_", Path(filename).name)[:120] or "document.pdf"
        path = self.workdir / f"{doc_id}.pdf"
        path.write_bytes(data)
        entry = DocEntry(id=doc_id, filename=safe, path=path, added_at=time.time(), auto_template=auto_template)
        with self.lock:
            self.docs[doc_id] = entry
        self._enqueue(entry, password)
        if wait:
            entry.finished.wait()
        return entry

    def check_password(self, entry: DocEntry, password: str) -> bool:
        """Quick check, so a wrong password is reported at once instead of after queueing."""
        from pypdf import PdfReader

        try:
            reader = PdfReader(str(entry.path), strict=False)
            return not reader.is_encrypted or bool(reader.decrypt(password))
        except Exception:
            return True  # let the full read decide

    def unlock(self, entry: DocEntry, password: str, auto_template: bool, wait: bool = False) -> DocEntry:
        """Try a password on a locked document. Raises EncryptedPDFError if it is wrong."""
        if not entry.locked:
            raise ValueError("document is not locked")
        if not password or not self.check_password(entry, password):
            raise EncryptedPDFError("wrong password")
        entry.error, entry.auto_template = None, auto_template
        self._enqueue(entry, password)
        if wait:
            entry.finished.wait()
        return entry

    def cancel_document(self, entry: DocEntry) -> DocEntry:
        if entry.busy:
            entry.cancel.set()
            if entry.state == "queued":  # not started: show it at once
                entry.state, entry.finished_at = "cancelled", time.time()
                entry.error = {"error": "ExtractionCancelled", "message": "ยกเลิกแล้ว"}
        return entry

    def retry_document(self, entry: DocEntry) -> DocEntry:
        if entry.busy or entry.locked or entry.state == "done":
            raise ValueError("only failed or cancelled documents can be read again")
        entry.error = None
        self._enqueue(entry, entry.password)
        return entry

    def doc(self, doc_id: str) -> DocEntry:
        entry = self.docs.get(doc_id)
        if entry is None:
            raise KeyError(doc_id)
        return entry

    def activity(self) -> dict:
        now = time.time()
        docs = sorted(self.docs.values(), key=lambda e: e.added_at)
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at)
        return {
            "workers": self.workers,
            "active": any(e.busy for e in docs) or any(not j.done for j in jobs),
            "counts": {
                s: sum(1 for e in docs if e.state == s) for s in ("queued", "reading", "done", "error", "cancelled")
            },
            "documents": [
                e.brief(self.queue_position(e)) for e in docs if e.busy or (e.finished_at and now - e.finished_at < 600)
            ],
            "jobs": [j.summary(with_result=False) for j in jobs],
        }

    def learn_async(self, name: str, doc_ids: list[str], description: str | None) -> Job:
        n = len(doc_ids)
        job = Job(id=uuid.uuid4().hex, kind="learn", name=name, doc_ids=list(doc_ids), total=2 * n + 1)
        self.jobs[job.id] = job

        def run() -> None:
            job.started_at = time.time()
            try:
                # 1. wait until every sample has been read (they may still be in the queue)
                while True:
                    if job.cancel.is_set():
                        raise ExtractionCancelled("cancelled")
                    entries = [self.doc(i) for i in doc_ids]
                    ready = sum(1 for e in entries if not e.busy)
                    job.step = ready
                    if ready == n:
                        break
                    job.state, job.message = "waiting", f"รอไฟล์ตัวอย่างอ่านเสร็จ {ready}/{n}"
                    time.sleep(0.3)
                usable = [e for e in entries if e.doc is not None]
                skipped = [e.filename for e in entries if e.doc is None]
                if not usable:
                    raise PDFStructError("ไม่มีไฟล์ตัวอย่างที่อ่านได้")
                # 2. learn (one writer per template)
                job.state, job.message = "learning", "เทียบตัวอย่าง แยกตัวแปร / ข้อความคงที่"
                with self.template_lock(name):
                    tpl = self.store.learn(name, [e.doc for e in usable], description=description)
                job.step = n + 1
                # 3. test the template on every sample
                checks = []
                for k, e in enumerate(usable, 1):
                    if job.cancel.is_set():
                        raise ExtractionCancelled("cancelled")
                    job.message = f"ทดสอบ Template กับตัวอย่าง {k}/{len(usable)}"
                    test = Document.model_validate(e.doc.model_dump())
                    test.fields = {}
                    test.extraction.missing_required_fields = []
                    apply_spec(test, tpl.spec, pdf_path=e.path, password=e.password)
                    checks.append(
                        {
                            "doc_id": e.id,
                            "filename": e.filename,
                            "found": sum(1 for f in test.fields.values() if f.found),
                            "total": len(test.fields),
                        }
                    )
                    job.step = n + 1 + k
                job.result = {"template": template_detail(tpl), "checks": checks, "skipped": skipped}
                job.step, job.state = job.total, "done"
                job.message = "เสร็จแล้ว" + (f" (ข้ามไฟล์ที่อ่านไม่ได้ {len(skipped)} ไฟล์)" if skipped else "")
            except ExtractionCancelled:
                job.state, job.error, job.message = "cancelled", "ยกเลิกแล้ว", "ยกเลิกแล้ว"
            except KeyError:
                job.state, job.error = "error", "ไฟล์ตัวอย่างถูกลบไปแล้ว"
                job.message = job.error
            except PDFStructError as exc:
                job.state, job.error, job.message = "error", str(exc), str(exc)
            except Exception as exc:  # never leave the UI polling forever
                job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
                job.message = job.error
            finally:
                job.finished_at = time.time()
                job.done = True

        self.learn_pool.submit(run)
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
                    return self._json([e.summary(state.queue_position(e)) for e in items])
                if method == "POST" and len(p) == 1:
                    data = self._body()
                    filename = unquote(self.headers.get("X-Filename", "document.pdf"))
                    auto = q.get("auto_template", ["1"])[0] == "1"
                    wait = q.get("wait", ["0"])[0] == "1"  # sync clients/tests; the app does not wait
                    entry = state.add_document(filename, data, auto, wait=wait)
                    return self._json(entry.summary(state.queue_position(entry)), 201 if wait else 202)
                entry = state.doc(p[1])
                if method == "GET" and len(p) == 2:
                    payload = entry.doc.model_dump(mode="json") if entry.doc else None
                    summary = entry.summary(state.queue_position(entry))
                    return self._json({"summary": summary, "document": payload, "edits": entry.edits})
                if method == "POST" and p[2:] == ["cancel"]:
                    return self._json(state.cancel_document(entry).summary())
                if method == "POST" and p[2:] == ["retry"]:
                    return self._json(state.retry_document(entry).summary(state.queue_position(entry)), 202)
                if method == "DELETE" and len(p) == 2:
                    state.cancel_document(entry)
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
                        state.unlock(entry, str(body.get("password") or ""), auto, wait=body.get("wait") is True)
                    except EncryptedPDFError:
                        return self._json({"error": "EncryptedPDFError", "message": "รหัสผ่านไม่ถูกต้อง"}, 403)
                    return self._json(entry.summary(state.queue_position(entry)))
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
                    with state.template_lock(name):
                        tpl = state.store.add_field(name, body.get("field") or {}, sample)
                    return self._json(template_detail(tpl), 201)
                if method == "DELETE" and len(p) == 4 and p[2] == "fields":
                    with state.template_lock(name):
                        return self._json(template_detail(state.store.remove_field(name, p[3])))
                if method == "PUT" and p[2:] == ["spec"]:
                    import yaml

                    text = self._body().decode("utf-8")
                    try:
                        data = yaml.safe_load(text) or {}
                    except yaml.YAMLError as exc:
                        raise ValueError(f"YAML ไม่ถูกต้อง: {exc}") from exc
                    with state.template_lock(name):
                        tpl = state.store.set_spec(name, data)
                    return self._json(template_detail(tpl))
                if method == "DELETE" and len(p) == 2:
                    with state.template_lock(name):
                        state.store.delete(name)
                    return self._json({"ok": True})

            if method == "GET" and p == ["activity"]:
                return self._json(state.activity())
            if method == "GET" and p == ["jobs"]:
                jobs = sorted(state.jobs.values(), key=lambda j: -j.created_at)
                return self._json([j.summary(with_result=False) for j in jobs])
            if method == "GET" and p[:1] == ["jobs"] and len(p) == 2:
                return self._json(state.jobs[p[1]].summary())
            if method == "POST" and p[:1] == ["jobs"] and p[2:] == ["cancel"]:
                job = state.jobs[p[1]]
                job.cancel.set()
                return self._json(job.summary(with_result=False))

            raise KeyError("/".join(p))

    return Handler


def make_server(state: AppState, port: int = 0) -> ThreadingHTTPServer:
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, port_ref))
    port_ref[0] = server.server_address[1]
    server.daemon_threads = True
    return server
