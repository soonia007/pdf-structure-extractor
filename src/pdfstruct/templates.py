"""Templates: learn a spec from sample PDFs, store it, and recognise new PDFs.

Flow
----
1. ``learn``   - read 1..n sample PDFs of the same kind (e.g. invoices from one
                 supplier), detect label/value pairs and tables, infer types,
                 and build a :class:`~pdfstruct.fields.Spec` automatically.
2. ``store``   - save it as ``<name>.template.json`` (plain JSON, reviewable,
                 can be committed to git).
3. ``learn more`` - add samples later; the template is re-derived from *all*
                 observations, so it gets more accurate with every sample.
4. ``match``   - score a new PDF against every template by its anchors
                 (labels / table headers seen in every sample) and apply the
                 best template's spec.

Only value *hashes* and types are stored for samples, never the values
themselves, so a template file does not leak document contents.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import kv
from .errors import PDFStructError
from .fields import FieldSpec, RegionAnchor, Spec, convert, load_spec
from .models import Document

TEMPLATE_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_STORE = Path(os.environ.get("PDFSTRUCT_TEMPLATES", "templates"))

ValueType = Literal["string", "number", "integer", "date", "boolean"]


class TemplateError(PDFStructError):
    pass


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Observation(BaseModel):
    """One label/value pair seen in one sample."""

    label: str
    method: str
    type: ValueType
    pattern: str | None = None
    value_hash: str
    page: int


class TableObservation(BaseModel):
    headers: list[str]
    column_types: list[ValueType]
    page: int


class Sample(BaseModel):
    filename: str
    sha256: str
    page_count: int
    added_at: str
    observations: list[Observation] = []
    tables: list[TableObservation] = []
    headings: list[str] = []
    text_hashes: list[str] = Field(default=[], description="hashes of each text piece: finds fixed text across samples")


class FieldStat(BaseModel):
    name: str
    found_in: int
    samples: int

    @property
    def rate(self) -> float:
        return self.found_in / self.samples if self.samples else 0.0


class Template(BaseModel):
    template_version: int = TEMPLATE_VERSION
    name: str
    description: str | None = None
    created_at: str
    updated_at: str
    manual_spec: bool = Field(default=False, description="spec was edited by hand; learning keeps it as is")
    anchors: list[str] = Field(default=[], description="text present in every sample, used to recognise documents")
    page_count_range: tuple[int, int] = (0, 0)
    spec: Spec = Spec()
    samples: list[Sample] = []
    field_stats: list[FieldStat] = Field(default=[], description="how often each field was found in the samples")
    custom_fields: list[FieldSpec] = Field(default=[], description="fields the user defined (e.g. cropped regions)")
    auto_fields: bool = Field(default=True, description="False: only the user's fields; samples still teach anchors")
    excluded_fields: list[str] = Field(default=[], description="learned fields the user removed")


class MatchResult(BaseModel):
    template: str
    score: float
    anchors_found: int
    anchors_total: int


# --------------------------------------------------------------------------- #
# Observation from one Document
# --------------------------------------------------------------------------- #
_LABEL_COLON = re.compile(r"^(?P<label>[^\d:：|]{2,40}?)\s*[:：]\s*(?P<value>.*)$")
_LABEL_SPACE = re.compile(r"^(?P<label>[^\d:：|]{2,40}?)\s+(?P<value>[\d๐-๙(฿$€-].*)$")
_MAX_LINE = 90


def _hash(v: str) -> str:
    return hashlib.sha256(v.strip().casefold().encode()).hexdigest()[:16]


def infer_type(raw: str) -> ValueType:
    raw = raw.strip()
    compact = re.sub(r"[\s,]", "", raw)
    if re.fullmatch(r"[\d๐-๙]{10,}", compact):
        return "string"  # ids / tax numbers / phone numbers are not quantities
    for typ in ("date", "boolean"):
        try:
            convert(raw, typ)
            if typ == "boolean" and raw.lower() in ("0", "1", ""):
                continue
            return typ
        except ValueError:
            pass
    if re.fullmatch(r"[(\-]?[฿$€]?\s*[\d๐-๙][\d๐-๙,]*(\.\d+)?\)?\s*(บาท|THB|USD|EUR|฿|%)?", raw, re.IGNORECASE):
        n = convert(raw, "number")
        return "integer" if n == int(n) and "." not in raw else "number"
    return "string"


def generalise(value: str) -> str | None:
    """INV-2026-001 -> [A-Z]+-\\d+-\\d+ ; only for code-like values (no spaces, letters+digits)."""
    v = value.strip()
    if " " in v or not re.search(r"\d", v) or not re.search(r"[A-Za-z]", v) or len(v) > 40:
        return None
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c.isdigit():
            j = i
            while j < len(v) and v[j].isdigit():
                j += 1
            out.append(r"\d+")
        elif c.isalpha() and c.isascii():
            j = i
            while j < len(v) and v[j].isalpha() and v[j].isascii():
                j += 1
            out.append("[A-Z]+" if v[i:j].isupper() else "[A-Za-z]+")
        else:
            j = i + 1
            out.append(re.escape(c))
        i = j
    return "".join(out)


def _clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip(" .:-–—#")


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_PATTERN = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"


def value_pattern(value: str, typ: str) -> str | None:
    """A regex the value of this field should match, so a wrong value is not picked."""
    v = value.strip()
    if typ != "string":
        return None
    if _EMAIL.fullmatch(v):
        return EMAIL_PATTERN
    if re.fullmatch(r"[\d๐-๙]+(?:[-/. ][\d๐-๙]+)*", v) and len(re.findall(r"\d", v)) >= 6:
        # ids, phone numbers: keep the digit groups ("1-1001-00123-45-6" -> \d-\d{4}-\d{5}-\d{2}-\d)
        return "".join(
            (rf"\d{{{len(part)}}}" if len(part) > 1 else r"\d") if part.isdigit() else re.escape(part)
            for part in re.findall(r"\d+|\D", v)
        )
    return generalise(v)


def text_hashes(doc: Document) -> set[str]:
    return {_hash(sg.text) for p in doc.pages for ln in p.lines for sg in ln.segments if sg.text.strip()}


def observe(doc: Document, constants: set[str] | None = None) -> Sample:
    """Find label/value pairs in one sample.

    ``constants`` (hashes of text that is identical in every sample) tells
    labels from values without relying on bold fonts or colons, which also
    makes scanned (OCR) samples learnable.
    """

    def is_const(text: str) -> bool | None:
        return None if constants is None else _hash(text) in constants

    obs: list[Observation] = []
    seen: set[str] = set()

    def add(label: str, value: str, method: str, page_no: int) -> None:
        label, value = _clean_label(label), value.strip()
        if not value or not kv.is_label_like(label) or label.casefold() in seen or kv.fold(value) == kv.fold(label):
            return
        seen.add(label.casefold())
        typ = infer_type(value)
        obs.append(
            Observation(
                label=label,
                method=method,
                type=typ,
                pattern=value_pattern(value, typ),
                value_hash=_hash(value),
                page=page_no,
            )
        )

    for page in doc.pages:
        if page.lines:
            _observe_segments(page, add, is_const)
        else:
            _observe_text_lines(page, add)
        _observe_kv_tables(page, add)

    tables: list[TableObservation] = []
    for page in doc.pages:
        for t in page.tables:
            if len(t.rows) < 2 or _is_kv_table(t):
                continue
            header = [c or "" for c in t.rows[0]]
            if not all(header) or any(infer_type(h) in ("number", "integer") for h in header):
                continue
            col_types: list[ValueType] = []
            for c in range(len(header)):
                cells = [r[c] for r in t.rows[1:] if c < len(r) and r[c]]
                types = {infer_type(x) for x in cells}
                if types and types <= {"integer"}:
                    col_types.append("integer")
                elif types and types <= {"integer", "number"}:
                    col_types.append("number")
                elif types == {"date"}:
                    col_types.append("date")
                else:
                    col_types.append("string")
            tables.append(TableObservation(headers=header, column_types=col_types, page=page.number))

    headings = [b.text for p in doc.pages for b in p.blocks if b.type == "heading" and len(b.text) <= 80]
    return Sample(
        filename=doc.source.filename,
        sha256=doc.source.sha256,
        page_count=doc.source.page_count,
        added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        observations=obs,
        tables=tables,
        headings=headings,
        text_hashes=sorted(text_hashes(doc)),
    )


# --------------------------------------------------------------------------- #
# Building a spec from all samples
# --------------------------------------------------------------------------- #
_THAI_NAMES = {
    "เลขที่ใบกำกับภาษี": "tax_invoice_no",
    "เลขที่ใบแจ้งหนี้": "invoice_no",
    "เลขประจำตัวผู้เสียภาษี": "tax_id",
    "เลขผู้เสียภาษี": "tax_id",
    "ยอดรวมทั้งสิ้น": "grand_total",
    "ยอดรวม": "total",
    "รวมเงิน": "subtotal",
    "ภาษีมูลค่าเพิ่ม": "vat",
    "ส่วนลด": "discount",
    "วันที่": "date",
    "วันครบกำหนด": "due_date",
    "เลขที่": "doc_no",
    "ลูกค้า": "customer",
    "ชื่อลูกค้า": "customer_name",
    "ผู้ขาย": "seller",
    "ที่อยู่": "address",
    "โทรศัพท์": "phone",
    "อีเมล": "email",
    "ชื่อ": "name",
    "หมายเหตุ": "note",
    "จำนวน": "qty",
    "ราคา": "price",
    "รายการ": "item",
    "จำนวนเงิน": "amount",
    "หน่วย": "unit",
}


def field_name(label: str, used: set[str]) -> str:
    label = label.strip()
    base = kv.KNOWN_LABELS.get(label) or _THAI_NAMES.get(label)
    if base is None and (known := kv.known_prefix(label)):
        suffix = re.sub(r"[^a-z0-9]+", "_", label[len(known) :].casefold()).strip("_")
        base = kv.KNOWN_LABELS[known] + (f"_{suffix}" if suffix else "")
    if base is None:
        ascii_part = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
        base = ascii_part if ascii_part and ascii_part[0].isalpha() else "field"
    base = base[:40]
    name, n = base, 2
    while name in used or name == "field":
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _majority(values: list[str]) -> str:
    return Counter(values).most_common(1)[0][0]


def _observe_segments(page, add, is_const=lambda _t: None) -> None:
    """Label/value pairs from line segments (positions): the main source for learning."""
    lines = page.lines
    used: set[tuple[int, int]] = set()
    for li, line in enumerate(lines):
        segs = line.segments
        if kv.looks_like_table_row(segs):
            continue
        if len(segs) >= 2 and li + 1 < len(lines) and _label_row(line, lines[li + 1], is_const):
            nxt = lines[li + 1]
            for seg in segs:
                vi = next(i for i, v in enumerate(nxt.segments) if _aligned(seg, v))
                add(seg.text, nxt.segments[vi].text, "label:below", page.number)
                used.add((li + 1, vi))
            continue
        for si, seg in enumerate(segs):
            if (li, si) in used:
                continue
            t = seg.text.strip()
            m = _LABEL_COLON.match(t)
            if m:
                if m["value"].strip():
                    add(m["label"], m["value"], "label:same-line", page.number)
                elif si + 1 < len(segs):
                    add(m["label"], segs[si + 1].text, "label:right", page.number)
                    used.add((li, si + 1))
                else:
                    below = kv.segment_below(lines, li, seg)
                    if below is not None:
                        add(m["label"], below.text, "label:below", page.number)
                        for lj in range(li + 1, min(li + 3, len(lines))):
                            for bi, b in enumerate(lines[lj].segments):
                                if b is below:
                                    used.add((lj, bi))
                continue
            known = kv.known_prefix(t)
            if known and len(t) > len(known):
                add(known, t[len(known) :], "label:same-line", page.number)  # "ชื่อ-นามสกุล นายสมชาย"
                continue
            if si + 1 < len(segs) and (li, si + 1) not in used and kv.is_label_like(t):
                right = segs[si + 1]
                looks_value = kv.has_digit(right.text) or "@" in right.text or (seg.bold and not right.bold)
                varies = is_const(t) is True and is_const(right.text) is False  # fixed label, changing value
                if (known == t or looks_value or varies) and not _LABEL_COLON.match(right.text.strip()):
                    add(t, right.text, "label:right", page.number)  # "VAT 7%   95.90"
                    used.add((li, si + 1))
                    continue
            m2 = _LABEL_SPACE.match(t)
            if m2 and kv.is_label_like(m2["label"]):
                add(m2["label"], m2["value"], "label:same-line", page.number)  # "Tax ID 0105556123456"


def _aligned(a, b) -> bool:
    slack = 12.0
    return a.bbox.x0 <= b.bbox.x1 + slack and b.bbox.x0 <= a.bbox.x1 + slack


def _label_row(line, nxt, is_const=lambda _t: None) -> bool:
    """A row of labels with their values in the row below (e.g. PO Number | Payment Terms)."""
    segs, vals = line.segments, nxt.segments
    if len(vals) != len(segs) or kv.looks_like_table_row(vals):
        return False
    if nxt.bbox.top - line.bbox.bottom > 2.5 * max(line.bbox.bottom - line.bbox.top, 4.0):
        return False
    if not all(kv.is_label_like(sg.text) for sg in segs):
        return False
    if not all(any(_aligned(sg, v) for v in vals) for sg in segs):
        return False
    bold_labels = all(sg.bold for sg in segs) and not any(v.bold for v in vals)
    colon_labels = all(sg.text.rstrip().endswith((":", "：")) for sg in segs)
    fixed_labels = all(is_const(sg.text) is True for sg in segs) and all(is_const(v.text) is False for v in vals)
    return bold_labels or colon_labels or fixed_labels


def _observe_text_lines(page, add) -> None:
    """Fallback for pages without line geometry (older outputs)."""
    lines = [ln.strip() for ln in page.text.splitlines() if ln.strip()]
    table_text = {_fold(c) for t in page.tables for row in t.rows for c in row if c}
    table_text |= {_fold(" ".join(c for c in row if c)) for t in page.tables for row in t.rows}
    for i, line in enumerate(lines):
        if len(line) > _MAX_LINE or _fold(line) in table_text:
            continue
        m = _LABEL_COLON.match(line) or _LABEL_SPACE.match(line)
        if not m:
            continue
        value = m["value"].strip()
        if value:
            add(m["label"], value, "label:same-line", page.number)
        elif i + 1 < len(lines) and not _LABEL_COLON.match(lines[i + 1]):
            add(m["label"], lines[i + 1], "label:next-line", page.number)


def _numeric_cells(cells) -> int:
    return sum(1 for c in cells if c and kv.has_digit(c) and not re.search(r"[A-Za-zก-๛]", c.replace("%", "")))


def _is_kv_table(t) -> bool:
    """Header + one row of values (a box of labels), not an item list with a single item."""
    rows = [[c or "" for c in r] for r in t.rows]
    return (
        len(rows) == 2
        and all(kv.is_label_like(h) for h in rows[0] if h)
        and not (len(rows[1]) >= 3 and _numeric_cells(rows[1]) >= 2)
    )


def _observe_kv_tables(page, add) -> None:
    """Small ruled tables that are label/value boxes, not item lists."""
    for t in page.tables:
        rows = [[c or "" for c in r] for r in t.rows]
        if _is_kv_table(t):
            for label, value in zip(rows[0], rows[1], strict=False):  # labels over values
                if label and value:
                    add(label, value, "label:table-below", page.number)
        elif t.n_cols == 2 and 2 <= len(rows) <= 12 and all(kv.is_label_like(r[0]) for r in rows if r[0]):
            for r in rows:  # label | value per row
                if r[0] and len(r) > 1 and r[1]:
                    add(r[0], r[1], "label:table-right", page.number)


def build_template(name: str, samples: list[Sample], *, description: str | None = None, old: Template | None = None):
    n = len(samples)
    min_support = 1 if n == 1 else max(2, math.ceil(n * 0.5))

    by_label: dict[str, list[Observation]] = defaultdict(list)
    display: dict[str, str] = {}
    for s in samples:
        for o in s.observations:
            key = o.label.casefold()
            by_label[key].append(o)
            display.setdefault(key, o.label)

    custom = list(old.custom_fields) if old else []
    excluded = set(old.excluded_fields) if old else set()
    used: set[str] = {f.name for f in custom} | excluded  # learned names never shadow the user's
    fields: list[FieldSpec] = []
    stats: list[FieldStat] = []
    anchors: list[str] = []
    for key in by_label:  # insertion order = reading order of the first sample containing it
        group = by_label[key]
        support = len(group)
        if support == n:
            anchors.append(display[key])
        if support < min_support:
            continue
        if n >= 2 and len({o.value_hash for o in group}) == 1:
            continue  # same value in every sample -> fixed text, not a variable
        types = {o.type for o in group}
        typ = types.pop() if len(types) == 1 else ("number" if types <= {"number", "integer"} else "string")
        patterns = {o.pattern for o in group}
        pattern = patterns.pop() if len(patterns) == 1 and typ == "string" else None
        fname = field_name(display[key], used)
        position = Counter(o.method for o in group).most_common(1)[0][0].removeprefix("label:")
        fields.append(
            FieldSpec(
                name=fname,
                type=typ,
                label=[display[key]],
                position=position if position in ("same-line", "right", "below") else None,
                pattern=pattern,
                required=support == n and n >= 2,
                description=f"learned from label '{display[key]}' ({support}/{n} samples)",
            )
        )
        stats.append(FieldStat(name=fname, found_in=support, samples=n))

    by_header: dict[tuple[str, ...], list[TableObservation]] = defaultdict(list)
    for s in samples:
        for t in {tuple(t.headers): t for t in s.tables}.values():
            by_header[tuple(h.casefold() for h in t.headers)].append(t)
    for group in by_header.values():
        support = len(group)
        if support == n:
            anchors.extend(group[0].headers)
        if support < min_support:
            continue
        headers = group[0].headers
        col_types = [_majority([g.column_types[i] for g in group]) for i in range(len(headers))]
        col_used: set[str] = set()
        columns = {
            h: {"key": field_name(h, col_used), "type": col_types[i]} for i, h in enumerate(headers) if h.strip()
        }
        tname = field_name("items" if "items" not in used else "table", used)
        fields.append(
            FieldSpec(
                name=tname,
                type="table",
                columns=columns,
                description=f"learned table ({support}/{n} samples)",
            )
        )
        stats.append(FieldStat(name=tname, found_in=support, samples=n))

    heading_sets = [set(s.headings) for s in samples]
    common_headings = set.intersection(*heading_sets) if heading_sets else set()
    anchors.extend(sorted(common_headings))
    anchors = list(dict.fromkeys(a for a in anchors if len(a.strip()) >= 2))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    auto = old.auto_fields if old is not None else True
    if not auto:
        fields, stats = [], []
    base = old.spec.fields if old is not None and old.manual_spec else fields
    if old is not None and old.manual_spec:
        stats = old.field_stats
    spec = Spec(fields=merge_fields(base, custom, excluded))
    return Template(
        name=name,
        description=description if description is not None else (old.description if old else None),
        created_at=old.created_at if old else now,
        updated_at=now,
        manual_spec=bool(old and old.manual_spec),
        anchors=anchors,
        page_count_range=(min(s.page_count for s in samples), max(s.page_count for s in samples)),
        spec=spec,
        samples=samples,
        field_stats=[st for st in stats if st.name not in excluded],
        custom_fields=custom,
        excluded_fields=sorted(excluded),
        auto_fields=auto,
    )


def attach_anchor(field: FieldSpec, doc: Document, anchors: list[str]) -> FieldSpec:
    """Tie a drawn box to the nearest fixed text (a label, header, heading).

    Layouts flow: an extra table row pushes "Grand Total" down. Remembering
    where the label was when the box was drawn lets extraction move the box by
    the same amount on later documents.
    """
    r = field.region
    page = next((p for p in doc.pages if p.number == r.page), None)
    if page is None or not page.width or not page.height or not anchors:
        return field
    folded = sorted(((_fold(a), a) for a in anchors if len(_fold(a)) >= 2), key=lambda x: -len(x[0]))
    best = None
    for b in page.blocks:
        bx0, btop = b.bbox.x0 / page.width, b.bbox.top / page.height
        bx1, bbot = b.bbox.x1 / page.width, b.bbox.bottom / page.height
        near = bx1 >= r.x0 - 0.35 and bx0 <= r.x1 and bbot >= r.top - 0.06 and btop <= r.bottom
        if not near:
            continue
        text = _fold(b.text)
        hit = next((orig for f, orig in folded if f in text), None)
        if hit is None:
            continue
        dist = ((bx0 - r.x0) ** 2 + (btop - r.top) ** 2) ** 0.5
        if best is None or dist < best[0]:
            best = (dist, hit, bx0, btop)
    if best is None:
        return field
    _, text, ax, ay = best
    region = r.model_copy(update={"anchor": RegionAnchor(text=text, x0=round(ax, 4), top=round(ay, 4))})
    return field.model_copy(update={"region": region})


def merge_fields(learned: list[FieldSpec], custom: list[FieldSpec], excluded: set[str]) -> list[FieldSpec]:
    """User-defined fields replace learned ones of the same name; removed names are dropped."""
    custom_names = {f.name for f in custom}
    out = [f for f in learned if f.name not in excluded and f.name not in custom_names]
    return out + list(custom)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _fold(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def score(doc: Document, template: Template) -> MatchResult:
    haystack = _fold("\n".join(p.text for p in doc.pages))
    found = sum(1 for a in template.anchors if _fold(a) in haystack)
    total = len(template.anchors)
    s = found / total if total else 0.0
    lo, hi = template.page_count_range
    if lo and not (lo <= doc.source.page_count <= max(hi, lo) * 2):
        s *= 0.8
    return MatchResult(template=template.name, score=round(s, 3), anchors_found=found, anchors_total=total)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class TemplateStore:
    def __init__(self, root: str | Path = DEFAULT_STORE):
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise TemplateError(f"invalid template name {name!r} (letters, digits, _ . - ; max 64)")
        return self.root / f"{name}.template.json"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> Template:
        path = self._path(name)
        if not path.exists():
            raise TemplateError(f"template {name!r} not found in {self.root}")
        try:
            return Template.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TemplateError(f"template {name!r} is corrupt: {exc}") from exc

    def save(self, template: Template) -> Path:
        path = self._path(template.name)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(template.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves a half-written template
        return path

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def list(self) -> list[Template]:
        out = []
        for p in sorted(self.root.glob("*.template.json")):
            try:
                out.append(Template.model_validate_json(p.read_text(encoding="utf-8")))
            except ValueError:
                continue  # skip a corrupt file rather than failing every lookup
        return out

    # -- high level --------------------------------------------------------- #
    def learn(self, name: str, docs: list[Document], *, description: str | None = None) -> Template:
        """Create the template, or add samples to an existing one and re-learn."""
        if not docs:
            raise TemplateError("need at least one sample document")
        old = self.load(name) if self.exists(name) else None
        samples = list(old.samples) if old else []
        known = {s.sha256 for s in samples}
        fresh = []
        for d in docs:
            if d.source.sha256 not in known:  # same file twice (stored or in this batch) counts once
                known.add(d.source.sha256)
                fresh.append(d)
        sets = [set(s.text_hashes) for s in samples if s.text_hashes] + [text_hashes(d) for d in fresh]
        constants = set.intersection(*sets) if len(sets) >= 2 else None
        samples += [observe(d, constants) for d in fresh]
        template = build_template(name, samples, description=description, old=old)
        self.save(template)
        return template

    def add_field(self, name: str, field: FieldSpec | dict, sample: Document | None = None) -> Template:
        """Add or replace a user-defined field (e.g. a cropped region).

        Creates the template from ``sample`` first when it does not exist yet,
        so a user can start a template just by drawing boxes on one document.
        """
        f = field if isinstance(field, FieldSpec) else FieldSpec.model_validate(field)
        if not self.exists(name):
            if sample is None:
                raise TemplateError(f"template {name!r} does not exist; a sample document is needed to create it")
            self.learn(name, [sample])
            created = self.load(name)
            created.auto_fields = False  # started by hand: keep only the fields the user draws
            self.save(build_template(name, created.samples, old=created))
        t = self.load(name)
        if f.region is not None and f.region.anchor is None and sample is not None:
            f = attach_anchor(f, sample, t.anchors)
        t.custom_fields = [c for c in t.custom_fields if c.name != f.name] + [f]
        t.excluded_fields = [e for e in t.excluded_fields if e != f.name]
        if sample is not None and sample.source.sha256 not in {s.sha256 for s in t.samples}:
            t.samples.append(observe(sample))
        rebuilt = build_template(name, t.samples, old=t)
        self.save(rebuilt)
        return rebuilt

    def remove_field(self, name: str, field_name: str) -> Template:
        t = self.load(name)
        if field_name not in {f.name for f in t.spec.fields}:
            raise TemplateError(f"template {name!r} has no field {field_name!r}")
        if any(c.name == field_name for c in t.custom_fields):
            t.custom_fields = [c for c in t.custom_fields if c.name != field_name]
        else:  # a learned field: remember the removal so re-learning does not bring it back
            t.excluded_fields = sorted(set(t.excluded_fields) | {field_name})
        if t.manual_spec:
            t.spec = Spec(fields=[f for f in t.spec.fields if f.name != field_name])
        rebuilt = build_template(name, t.samples, old=t)
        self.save(rebuilt)
        return rebuilt

    def set_spec(self, name: str, spec: str | Path | dict | Spec) -> Template:
        """Replace the learned spec with a hand-edited one (kept on later learning)."""
        template = self.load(name)
        template.spec = load_spec(spec)
        template.manual_spec = True
        template.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save(template)
        return template

    def match(self, doc: Document) -> list[MatchResult]:
        return sorted((score(doc, t) for t in self.list()), key=lambda r: (-r.score, r.template))

    def best(self, doc: Document, threshold: float = 0.6) -> tuple[Template, MatchResult] | None:
        results = self.match(doc)
        if not results or results[0].score < threshold:
            return None
        return self.load(results[0].template), results[0]


def spec_yaml(spec: Spec) -> str:
    import yaml

    data = json.loads(spec.model_dump_json(exclude_none=True, exclude_defaults=True))
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def extract_auto(path: str | Path, store: TemplateStore, *, threshold: float = 0.6, **kwargs) -> Document:
    """Extract, recognise the document type from the store, and apply that template's spec."""
    from .extractor import extract
    from .fields import apply_spec

    doc = extract(path, **kwargs)
    found = store.best(doc, threshold)
    if found is None:
        doc.warnings.append(f"no template matched (threshold {threshold})")
        return doc
    template, result = found
    apply_spec(doc, template.spec, pdf_path=path, password=kwargs.get("password"))
    doc.extraction.template = template.name
    doc.extraction.template_score = result.score
    return doc
