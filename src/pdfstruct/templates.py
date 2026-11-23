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
    method: Literal["label:same-line", "label:next-line"]
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


def observe(doc: Document) -> Sample:
    obs: list[Observation] = []
    seen: set[str] = set()
    for page in doc.pages:
        lines = [ln.strip() for ln in page.text.splitlines() if ln.strip()]
        table_text = {_fold(c) for t in page.tables for row in t.rows for c in row if c}
        table_text |= {_fold(" ".join(c for c in row if c)) for t in page.tables for row in t.rows}
        for i, line in enumerate(lines):
            if len(line) > _MAX_LINE or _fold(line) in table_text:  # table rows are not label: value pairs
                continue
            m = _LABEL_COLON.match(line) or _LABEL_SPACE.match(line)
            if not m:
                continue
            label = _clean_label(m["label"])
            if len(label) < 2 or len(label.split()) > 6 or label.casefold() in seen:
                continue
            value, method = m["value"].strip(), "label:same-line"
            if not value:
                if i + 1 >= len(lines) or _LABEL_COLON.match(lines[i + 1]):
                    continue
                value, method = lines[i + 1], "label:next-line"
            seen.add(label.casefold())
            typ = infer_type(value)
            obs.append(
                Observation(
                    label=label,
                    method=method,
                    type=typ,
                    pattern=generalise(value) if typ == "string" else None,
                    value_hash=_hash(value),
                    page=page.number,
                )
            )

    tables: list[TableObservation] = []
    for page in doc.pages:
        for t in page.tables:
            if len(t.rows) < 2:
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
    base = _THAI_NAMES.get(label.strip())
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
        fields.append(
            FieldSpec(
                name=fname,
                type=typ,
                label=[display[key]],
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
        for d in docs:
            if d.source.sha256 not in known:  # same file twice (stored or in this batch) counts once
                known.add(d.source.sha256)
                samples.append(observe(d))
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
    apply_spec(doc, template.spec, pdf_path=path)
    doc.extraction.template = template.name
    doc.extraction.template_score = result.score
    return doc
