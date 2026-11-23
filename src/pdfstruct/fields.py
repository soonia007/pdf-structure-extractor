"""User-defined field extraction ("ฉันต้องการตัวแปรอะไรจากเอกสาร").

A *spec* (YAML or JSON) lists the variables you want. Each field uses exactly
one locating method:

* ``source``  - dotted path into the extracted Document (``metadata.Title``)
* ``regex``   - regular expression over the whole document text
* ``label``   - text label(s) printed next to / above the value (``"ยอดรวม"``)
* ``columns`` - a table recognised by its header cells (``type: table``)
* ``region``  - a box you drew on the page ("crop"), as fractions of the page
                size, so it also works when the PDF page size differs slightly

and is converted to ``type``: string, number, integer, date, boolean or table.
Everything is rule-based and deterministic; the same spec on the same PDF
always gives the same result.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import kv
from .errors import SpecError
from .models import BBox, Document, FieldValue
from .normalize import normalize_text

FieldType = Literal["string", "number", "integer", "date", "boolean", "table"]
INCLUDE_KEYS = {"metadata", "outline", "form_fields", "pages", "warnings"}
PAGE_KEYS = {"text", "blocks", "lines", "tables", "images", "links", "annotations"}


class ColumnSpec(BaseModel):
    key: str
    type: Literal["string", "number", "integer", "date", "boolean"] = "string"


class RegionAnchor(BaseModel):
    """Fixed text near a region and where it was (fractions of the page) when the box was drawn."""

    text: str
    x0: float
    top: float


class Region(BaseModel):
    """A user-drawn box. Coordinates are fractions (0..1) of the page, origin top-left."""

    page: int = Field(ge=1)
    x0: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)
    anchor: RegionAnchor | None = Field(default=None, description="move the box with this text when the layout shifts")

    @model_validator(mode="after")
    def _ordered(self) -> Region:
        if self.x1 - self.x0 < 0.003 or self.bottom - self.top < 0.002:
            raise ValueError("region is too small or inverted")
        return self


class FieldSpec(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: FieldType = "string"
    description: str | None = None
    source: str | None = None
    regex: str | None = None
    label: list[str] | None = None
    columns: dict[str, ColumnSpec] | None = None
    region: Region | None = None
    position: Literal["same-line", "right", "below"] | None = Field(
        default=None, description="label method: where the value sits relative to its label (tried first)"
    )
    pattern: str | None = Field(default=None, description="value must match this regex (label/regex methods)")
    pages: list[int] | None = Field(default=None, description="only search these 1-based pages")
    all: bool = Field(default=False, description="return every match as a list instead of the first")
    required: bool = False
    default: Any = None

    @field_validator("label", mode="before")
    @classmethod
    def _label_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @field_validator("pages", mode="before")
    @classmethod
    def _pages_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, int) else v

    @field_validator("columns", mode="before")
    @classmethod
    def _columns(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {h: {"key": c} if isinstance(c, str) else c for h, c in v.items()}
        return v

    @model_validator(mode="after")
    def _one_method(self) -> FieldSpec:
        methods = [m for m in ("source", "regex", "label", "columns", "region") if getattr(self, m)]
        if len(methods) != 1:
            raise ValueError(f"field '{self.name}': use exactly one of source / regex / label / columns / region")
        if self.columns and self.type != "table":
            raise ValueError(f"field '{self.name}': 'columns' is used with type 'table' only")
        if self.type == "table" and not (self.columns or self.region):
            raise ValueError(f"field '{self.name}': type 'table' needs 'columns' or a 'region'")
        for rx in (self.regex, self.pattern):
            if rx:
                try:
                    re.compile(rx)
                except re.error as exc:
                    raise ValueError(f"field '{self.name}': bad regex {rx!r}: {exc}") from exc
        return self


class Spec(BaseModel):
    version: int = 1
    include: list[str] | None = Field(
        default=None, description="limit output, e.g. [metadata, pages.tables]; omit = everything"
    )
    fields: list[FieldSpec] = []

    @model_validator(mode="after")
    def _check(self) -> Spec:
        names = [f.name for f in self.fields]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate field names: {sorted(dup)}")
        for item in self.include or []:
            top, _, sub = item.partition(".")
            if top not in INCLUDE_KEYS or (sub and (top != "pages" or sub not in PAGE_KEYS)):
                raise ValueError(f"unknown include '{item}'")
        return self


def load_spec(spec: str | Path | dict | Spec) -> Spec:
    try:
        if isinstance(spec, Spec):
            return spec
        if isinstance(spec, dict):
            return Spec.model_validate(spec)
        path = Path(spec)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        return Spec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(f"invalid spec: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise SpecError(f"cannot read spec: {exc}") from exc


# --------------------------------------------------------------------------- #
# Type conversion
# --------------------------------------------------------------------------- #
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_NUMBER = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?|-?\.\d+")
_TH_MONTHS = [
    ("มกราคม", "ม.ค."), ("กุมภาพันธ์", "ก.พ."), ("มีนาคม", "มี.ค."), ("เมษายน", "เม.ย."),
    ("พฤษภาคม", "พ.ค."), ("มิถุนายน", "มิ.ย."), ("กรกฎาคม", "ก.ค."), ("สิงหาคม", "ส.ค."),
    ("กันยายน", "ก.ย."), ("ตุลาคม", "ต.ค."), ("พฤศจิกายน", "พ.ย."), ("ธันวาคม", "ธ.ค."),
]  # fmt: skip
_EN_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _year(y: int) -> int:
    if y < 100:
        y += 2000
    return y - 543 if y > 2400 else y  # Buddhist era -> CE


def _month_index(word: str) -> int | None:
    w = word.strip().lower().rstrip(".")
    for i, (full, abbr) in enumerate(_TH_MONTHS):
        if w in (full, abbr.rstrip("."), abbr.replace(".", "")):
            return i + 1
    for i, m in enumerate(_EN_MONTHS):
        if w.startswith(m):
            return i + 1
    return None


def to_date(raw: str) -> str:
    s = raw.translate(_THAI_DIGITS)
    if m := re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    elif m := re.search(r"(\d{1,2})\s*([^\d\s,]+\.?(?:[^\d\s,]+\.?)?)\s*,?\s*(\d{2,4})", s):
        d, mo, y = int(m[1]), _month_index(m[2]), int(m[3])
        if mo is None:
            raise ValueError(f"unknown month in {raw!r}")
        if y < 100 and re.search(r"[\u0e00-\u0e7f]", m[2]):
            y += 2500  # "5 มี.ค. 69" -> พ.ศ. 2569
    elif m := re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s):
        mo, d, y = _month_index(m[1]), int(m[2]), int(m[3])
        if mo is None:
            raise ValueError(f"unknown month in {raw!r}")
    else:
        raise ValueError(f"not a date: {raw!r}")
    return date(_year(y), mo, d).isoformat()


def to_number(raw: str) -> float:
    s = raw.translate(_THAI_DIGITS)
    m = _NUMBER.search(s)
    if not m:
        raise ValueError(f"not a number: {raw!r}")
    tok = m[0]
    neg = tok.startswith("(") and tok.endswith(")")
    val = float(tok.strip("()").replace(",", ""))
    return -val if neg else val


def convert(raw: str, typ: str) -> Any:
    raw = raw.strip()
    if typ == "string":
        if not raw:
            raise ValueError("empty")
        return raw
    if typ == "number":
        return to_number(raw)
    if typ == "integer":
        n = to_number(raw)
        if n != int(n):
            raise ValueError(f"not an integer: {raw!r}")
        return int(n)
    if typ == "date":
        return to_date(raw)
    if typ == "boolean":
        low = raw.lower()
        if low in ("true", "yes", "y", "1", "x", "✓", "☑", "ใช่", "มี"):
            return True
        if low in ("false", "no", "n", "0", "", "☐", "ไม่", "ไม่ใช่", "ไม่มี"):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    raise ValueError(f"unsupported type {typ}")


# --------------------------------------------------------------------------- #
# Locating methods
# --------------------------------------------------------------------------- #
_SEP = " \t:：=#-–—.|"


def _pages(doc: Document, spec: FieldSpec):
    return [p for p in doc.pages if spec.pages is None or p.number in spec.pages]


def _fold(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _candidates_by_label(doc: Document, spec: FieldSpec):
    """Every place a label's value could be, best first.

    Ranked by how well the label matched (exact > start of a longer label >
    inside other text), then by how close the value is (same segment, right,
    below, text line, next text line), then by reading order.
    """
    labels = spec.label or []
    cands: list[kv.Candidate] = []
    order = 0
    for page in _pages(doc, spec):
        if page.lines:
            for c in kv.label_candidates(page, labels, order):
                cands.append(c)
                order = max(order, c.order)
        # plain text lines: still useful for older outputs and odd layouts
        lines = [ln for ln in page.text.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            best = max((m for lb in labels if (m := kv.match_label(line, lb))), key=lambda m: m.quality, default=None)
            if best is None:
                continue
            order += 1
            if best.rest:
                cands.append(kv.Candidate(best.rest, page.number, "label:same-line", best.quality, 3, order))
            if i + 1 < len(lines):
                cands.append(kv.Candidate(lines[i + 1].strip(), page.number, "label:next-line", best.quality, 4, order))
        for table in page.tables:
            for r, row in enumerate(table.rows):
                for c, cell in enumerate(row):
                    if not cell:
                        continue
                    best = max(
                        (m for lb in labels if (m := kv.match_label(cell, lb))), key=lambda m: m.quality, default=None
                    )
                    if best is None:
                        continue
                    order += 1
                    if best.rest:
                        cands.append(kv.Candidate(best.rest, page.number, "label:table-cell", best.quality, 0, order))
                    right = next((x for x in row[c + 1 :] if x), None)
                    if right:
                        cands.append(kv.Candidate(right, page.number, "label:table-right", best.quality, 1, order))
                    if r + 1 < len(table.rows) and c < len(table.rows[r + 1]) and table.rows[r + 1][c]:
                        below = table.rows[r + 1][c]
                        cands.append(kv.Candidate(below, page.number, "label:table-below", best.quality, 2, order))
    if spec.position:
        preferred = "label:" + spec.position
        for c in cands:
            if c.method == preferred or (spec.position == "below" and c.method == "label:table-below"):
                c.rank = -1  # the learned/declared position wins over other relations
    cands.sort(key=lambda c: (-c.quality, c.rank, c.order))
    yield from cands


def _candidates_by_regex(doc: Document, spec: FieldSpec):
    rx = re.compile(spec.regex, re.MULTILINE)
    for page in _pages(doc, spec):
        for m in rx.finditer(page.text):
            yield kv.Candidate(m.group(1) if rx.groups else m.group(0), page.number, "regex", 3, 0, 0)


def _resolve_source(doc: Document, path: str) -> Any:
    cur: Any = doc.model_dump()
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


_METHOD_CONFIDENCE = {
    "label:same-line": 0.92,
    "label:table-cell": 0.9,
    "label:right": 0.9,
    "label:table-right": 0.9,
    "label:below": 0.82,
    "label:table-below": 0.85,
    "label:next-line": 0.72,
    "regex": 0.85,
    "region": 0.9,
    "region (moved with anchor)": 0.86,
    "region (anchor not found)": 0.6,
    "table": 0.9,
    "source": 1.0,
}


def _confidence(method: str, quality: int = 3, has_pattern: bool = False) -> float:
    """Rough 0..1 score: how sure we are the value belongs to the field (not OCR accuracy)."""
    c = _METHOD_CONFIDENCE.get(method, 0.7)
    c -= {3: 0.0, 2: 0.15, 1: 0.3}.get(quality, 0.3)
    if has_pattern:
        c += 0.05
    return round(max(0.05, min(0.99, c)), 2)


def _apply_pattern(raw: str, spec: FieldSpec) -> str | None:
    if not spec.pattern:
        return raw
    m = re.search(spec.pattern, raw)
    if not m:
        return None
    return m.group(1) if m.re.groups else m.group(0)


def _extract_table(doc: Document, spec: FieldSpec) -> FieldValue:
    wanted = {_fold(h): col for h, col in (spec.columns or {}).items()}
    rows_out: list[dict[str, Any]] = []
    first_page = None
    errors: list[str] = []
    prev: dict | None = None  # last matched table, to continue it on the next page
    for page in _pages(doc, spec):
        for t_index, table in enumerate(page.tables):
            header_idx, col_map = None, {}
            for r, row in enumerate(table.rows):
                mapping = {}
                for c, cell in enumerate(row):
                    for h, col in wanted.items():
                        if cell and h in _fold(cell) and col.key not in {v.key for v in mapping.values()}:
                            mapping[c] = col
                if len(mapping) == len(wanted):
                    header_idx, col_map = r, mapping
                    break
            start = header_idx + 1 if header_idx is not None else None
            if (
                header_idx is None
                and prev is not None
                and t_index == 0
                and page.number == prev["page"] + 1
                and table.n_cols == prev["n_cols"]
            ):
                col_map, start = prev["col_map"], 0  # same table continued without its header
            if start is None:
                continue
            first_page = first_page or page.number
            prev = {"col_map": col_map, "n_cols": table.n_cols, "page": page.number}
            header_text = {_fold(h) for h in wanted}
            for row in table.rows[start:]:
                if not any(row) or {_fold(c) for c in row if c} <= header_text:
                    continue  # empty, or the header repeated on a new page
                record: dict[str, Any] = {}
                for c, col in col_map.items():
                    cell = row[c] if c < len(row) else None
                    try:
                        record[col.key] = convert(cell, col.type) if cell else None
                    except ValueError as exc:
                        record[col.key] = None
                        errors.append(f"page {page.number}: {col.key}: {exc}")
                rows_out.append(record)
    if first_page is None:
        return FieldValue(name=spec.name, found=False, value=spec.default, error="no table with those headers")
    return FieldValue(
        name=spec.name,
        found=True,
        value=rows_out,
        page=first_page,
        method="table",
        error="; ".join(errors[:5]) or None,
    )


def _region_bbox(page: Any, r: Region) -> tuple[float, float, float, float]:
    x_off, y_off, x_end, y_end = page.bbox
    w, h = x_end - x_off, y_end - y_off
    return (x_off + r.x0 * w, y_off + r.top * h, x_off + r.x1 * w, y_off + r.bottom * h)


def _fold_text(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _shift_by_anchor(doc: Document, r: Region) -> tuple[Region, str]:
    """Move the region by however far its anchor text moved on this document."""
    if r.anchor is None:
        return r, "region"
    page = next((p for p in doc.pages if p.number == r.page), None)
    if page is None or not page.width or not page.height:
        return r, "region"
    target = _fold_text(r.anchor.text)
    best = None
    for b in page.blocks:
        if target not in _fold_text(b.text):
            continue
        bx, by = b.bbox.x0 / page.width, b.bbox.top / page.height
        dist = abs(bx - r.anchor.x0) + abs(by - r.anchor.top)
        if best is None or dist < best[0]:
            best = (dist, bx - r.anchor.x0, by - r.anchor.top)
    if best is None:
        return r, "region (anchor not found)"
    _, dx, dy = best
    if abs(dx) < 1e-4 and abs(dy) < 1e-4:
        return r, "region"
    dx = max(dx, -r.x0) if dx < 0 else min(dx, 1 - r.x1)
    dy = max(dy, -r.top) if dy < 0 else min(dy, 1 - r.bottom)
    moved = r.model_copy(update={"x0": r.x0 + dx, "x1": r.x1 + dx, "top": r.top + dy, "bottom": r.bottom + dy})
    return moved, "region (moved with anchor)"


def _extract_region(pdf: Any, spec: FieldSpec, doc: Document | None = None) -> FieldValue:
    r = spec.region
    method = "region"
    if doc is not None:
        r, method = _shift_by_anchor(doc, r)
    if pdf is None:
        return FieldValue(name=spec.name, found=False, value=spec.default, error="region fields need the PDF file")
    if r.page > len(pdf.pages):
        return FieldValue(name=spec.name, found=False, value=spec.default, error=f"document has no page {r.page}")
    page = pdf.pages[r.page - 1]
    box = _region_bbox(page, r)
    crop = page.crop(box)  # keeps characters that touch the box, so a loose box still works
    bbox = BBox.of(*box)

    if spec.type == "table":
        rows = crop.extract_table() or []
        rows = [[normalize_text(c) if c else None for c in row] for row in rows if any(row)]
        if len(rows) < 2:
            return FieldValue(name=spec.name, found=False, value=spec.default, page=r.page, bbox=bbox,
                              error="no table with a header row inside the region")  # fmt: skip
        header = [re.sub(r"[^0-9a-z]+", "_", (h or f"col{i + 1}").casefold()).strip("_") or f"col{i + 1}"
                  for i, h in enumerate(rows[0])]  # fmt: skip
        value = [dict(zip(header, row, strict=False)) for row in rows[1:]]
        return FieldValue(name=spec.name, found=True, value=value, page=r.page, method=method, bbox=bbox)

    text = normalize_text(crop.extract_text() or "")
    if not text:
        return FieldValue(name=spec.name, found=False, value=spec.default, page=r.page, bbox=bbox,
                          error="region is empty on this document")  # fmt: skip
    picked = _apply_pattern(text, spec)
    if picked is None:
        return FieldValue(name=spec.name, found=False, value=spec.default, raw=text, page=r.page, bbox=bbox,
                          error="text in region does not match pattern")  # fmt: skip
    try:
        value = convert(picked if spec.type != "string" else picked.replace("\n", " "), spec.type)
    except ValueError as exc:
        return FieldValue(name=spec.name, found=False, value=spec.default, raw=text, page=r.page, bbox=bbox,
                          error=str(exc))  # fmt: skip
    return FieldValue(name=spec.name, found=True, value=value, raw=picked, page=r.page, method=method, bbox=bbox)


def evaluate_field(doc: Document, spec: FieldSpec, pdf: Any = None) -> FieldValue:
    if spec.region:
        return _extract_region(pdf, spec, doc)
    if spec.type == "table":
        return _extract_table(doc, spec)

    if spec.source:
        try:
            raw_val = _resolve_source(doc, spec.source)
        except (KeyError, IndexError, ValueError):
            return FieldValue(name=spec.name, found=False, value=spec.default, error=f"no such path {spec.source}")
        if raw_val is None or raw_val == "":
            return FieldValue(name=spec.name, found=False, value=spec.default, error="empty")
        try:
            value = raw_val if not isinstance(raw_val, str) else convert(raw_val, spec.type)
        except ValueError as exc:
            return FieldValue(name=spec.name, found=False, value=spec.default, raw=str(raw_val), error=str(exc))
        return FieldValue(name=spec.name, found=True, value=value, raw=str(raw_val), method="source")

    candidates = _candidates_by_label(doc, spec) if spec.label else _candidates_by_regex(doc, spec)
    hits: list[FieldValue] = []
    last_error = "not found"
    for cand in candidates:
        raw, page_no, method = cand.raw, cand.page, cand.method
        picked = _apply_pattern(raw, spec)
        if picked is None:
            last_error = f"no value matching pattern near label (last tried {raw[:60]!r})"
            continue
        try:
            value = convert(picked, spec.type)
        except ValueError as exc:
            last_error = str(exc)
            continue
        hit = FieldValue(
            name=spec.name,
            found=True,
            value=value,
            raw=picked,
            page=page_no,
            method=method,
            bbox=BBox.of(*cand.bbox) if cand.bbox else None,
            confidence=_confidence(method, cand.quality, bool(spec.pattern)),
        )
        if not spec.all:
            return hit
        if all(h.value != hit.value or h.page != hit.page for h in hits):
            hits.append(hit)
    if spec.all and hits:
        return FieldValue(
            name=spec.name,
            found=True,
            value=[h.value for h in hits],
            raw=" | ".join(h.raw or "" for h in hits),
            page=hits[0].page,
            method=hits[0].method,
        )
    return FieldValue(name=spec.name, found=False, value=spec.default, error=last_error)


def apply_spec(doc: Document, spec: Spec, pdf_path: str | Path | None = None) -> Document:
    """Fill ``doc.fields`` from the spec. Never raises for a missing value.

    ``pdf_path`` is needed for ``region`` fields (they read the PDF directly).
    """
    pdf = None
    if pdf_path is not None and any(f.region for f in spec.fields):
        import pdfplumber

        try:
            pdf = pdfplumber.open(str(pdf_path))
        except Exception as exc:
            doc.warnings.append(f"region fields: cannot reopen PDF: {exc}")
    try:
        for f in spec.fields:
            try:
                result = evaluate_field(doc, f, pdf)
            except Exception as exc:  # a broken field must never break the document
                result = FieldValue(name=f.name, found=False, value=f.default, error=f"{type(exc).__name__}: {exc}")
            if result.found and result.confidence is None:
                result.confidence = _confidence(result.method or "")
            page = next((pg for pg in doc.pages if pg.number == result.page), None) if result.page else None
            if result.found and result.confidence is not None and page is not None and page.ocr_confidence is not None:
                result.confidence = round(result.confidence * max(0.4, page.ocr_confidence), 2)  # OCR may misread
            doc.fields[f.name] = result
            if f.required and not result.found:
                doc.extraction.missing_required_fields.append(f.name)
    finally:
        if pdf is not None:
            pdf.close()
    if doc.extraction.missing_required_fields:
        doc.warnings.append(f"required fields not found: {doc.extraction.missing_required_fields}")
    return doc


def filter_output(data: dict, include: list[str] | None) -> dict:
    """Drop parts of the JSON the spec did not ask for (schema_version/source/fields/extraction always stay)."""
    if not include:
        return data
    tops = {i.partition(".")[0] for i in include}
    page_subs = {i.partition(".")[2] for i in include if i.startswith("pages.")}
    for key in INCLUDE_KEYS - tops:
        data.pop(key, None)
    if "pages" in data and page_subs and "pages" not in include:
        for page in data["pages"]:
            for sub in PAGE_KEYS - page_subs:
                page.pop(sub, None)
    return data
