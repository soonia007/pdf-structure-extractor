"""Label / value detection using positions on the page.

Real documents put values in many places relative to their label:

    Invoice No: INV-001          same segment          "label:same-line"
    Invoice No:      INV-001     segment to the right  "label:right"
    PO Number                    segment below         "label:below"
    PO-7781
    ชื่อ-นามสกุล นายสมชาย          known label + text    "label:same-line"

Both the learner (templates.observe) and the extractor (fields, method
``label``) use this module, so whatever was learned is found again the same
way. Labels are matched by quality: an exact label beats a label at the start
of a longer one, which beats a label inside other text ("Date" must not take
the value of "Due Date").
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from .models import Page, Segment, TextLine

SEP = " \t:：=#-–—.|"
_DIGIT = re.compile(r"[0-9๐-๙]")
_LETTER = re.compile(r"[A-Za-zก-๛]")

# Labels that often appear without a colon (mostly Thai forms), with an English variable name.
KNOWN_LABELS: dict[str, str] = {
    # people / contact
    "ชื่อ-นามสกุล": "name", "ชื่อ - นามสกุล": "name", "ชื่อ-สกุล": "name", "ชื่อและนามสกุล": "name", "ชื่อ": "name",
    "นามสกุล": "surname", "เลขประจำตัวประชาชน": "citizen_id", "เลขบัตรประชาชน": "citizen_id",
    "เลขบัตรประจำตัวประชาชน": "citizen_id", "วันเกิด": "birth_date", "วันเดือนปีเกิด": "birth_date", "อายุ": "age",
    "เพศ": "gender", "สัญชาติ": "nationality", "อาชีพ": "occupation", "โทรศัพท์": "phone", "เบอร์โทร": "phone",
    "เบอร์โทรศัพท์": "phone", "โทร": "phone", "มือถือ": "mobile", "โทรสาร": "fax", "แฟกซ์": "fax", "อีเมล": "email",
    "อีเมล์": "email", "ที่อยู่": "address", "บ้านเลขที่": "house_no", "หมู่": "moo", "ถนน": "road",
    "ตำบล": "subdistrict", "แขวง": "subdistrict", "อำเภอ": "district", "เขต": "district", "จังหวัด": "province",
    "รหัสไปรษณีย์": "postal_code", "บริษัท": "company", "ตำแหน่ง": "position", "แผนก": "department",
    "ฝ่าย": "department",
    # documents
    "เลขที่": "doc_no", "เลขที่เอกสาร": "doc_no", "เลขที่ใบเสร็จ": "receipt_no", "เลขที่ใบเสร็จรับเงิน": "receipt_no",
    "เลขที่ใบกำกับภาษี": "tax_invoice_no", "เลขที่ใบแจ้งหนี้": "invoice_no", "เลขที่ใบสั่งซื้อ": "po_number",
    "ใบสั่งซื้อเลขที่": "po_number", "อ้างอิง": "reference", "เลขที่อ้างอิง": "reference", "วันที่": "date",
    "ลงวันที่": "date", "วันที่ออก": "issue_date", "วันครบกำหนด": "due_date", "ครบกำหนดชำระ": "due_date",
    "กำหนดชำระ": "due_date", "เลขประจำตัวผู้เสียภาษี": "tax_id", "เลขประจำตัวผู้เสียภาษีอากร": "tax_id",
    "เลขผู้เสียภาษี": "tax_id", "สาขา": "branch", "ผู้ขาย": "seller", "ผู้ซื้อ": "buyer", "ลูกค้า": "customer",
    "ชื่อลูกค้า": "customer", "ผู้ติดต่อ": "contact", "พนักงานขาย": "sales_rep",
    "เงื่อนไขการชำระเงิน": "payment_terms", "เงื่อนไขการชำระ": "payment_terms",
    # money
    "รวมเงิน": "subtotal", "รวม": "subtotal", "ยอดรวม": "total", "ยอดรวมทั้งสิ้น": "grand_total",
    "รวมทั้งสิ้น": "grand_total", "ยอดสุทธิ": "net_total", "จำนวนเงินรวม": "total", "จำนวนเงิน": "amount",
    "ภาษีมูลค่าเพิ่ม": "vat", "ภาษี": "tax", "ส่วนลด": "discount", "หัก ณ ที่จ่าย": "withholding_tax",
    "ภาษีหัก ณ ที่จ่าย": "withholding_tax", "ค่าขนส่ง": "shipping", "มัดจำ": "deposit", "ธนาคาร": "bank",
    "เลขที่บัญชี": "account_no", "ชื่อบัญชี": "account_name",
    # items / misc
    "จำนวน": "qty", "ราคา": "price", "ราคาต่อหน่วย": "unit_price", "รายการ": "item", "หน่วย": "unit",
    "หมายเหตุ": "note", "ผู้อนุมัติ": "approver", "ผู้รับเงิน": "receiver", "ผู้จ่ายเงิน": "payer",
}  # fmt: skip
_KNOWN_SORTED = sorted(KNOWN_LABELS, key=len, reverse=True)


def fold(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def has_digit(s: str) -> bool:
    return bool(_DIGIT.search(s))


def known_prefix(text: str) -> str | None:
    """The longest known label that ``text`` starts with (followed by a space, ':' or end)."""
    t = text.strip()
    for lb in _KNOWN_SORTED:
        if t.startswith(lb) and (len(t) == len(lb) or t[len(lb)] in " :：\t"):
            return lb
    return None


def is_label_like(text: str) -> bool:
    """Short text that reads like a field label (no sentence, few digits)."""
    t = text.strip().rstrip(":：").strip()
    if not 2 <= len(t) <= 40 or not _LETTER.search(t):
        return False
    if len(t.split()) > 6 or t.endswith((".", "!", "?", ",")):
        return False
    digits = len(_DIGIT.findall(t))
    return digits <= 3 and digits < len(t) / 3  # "VAT 7%" ok, "INV-2026-001" not


def looks_like_table_row(segments: list[Segment]) -> bool:
    numeric = sum(1 for s in segments if has_digit(s.text) and not _LETTER.search(s.text.replace("%", "")))
    return len(segments) >= 3 and numeric >= 2


@dataclass
class Match:
    quality: int  # 3 exact label, 2 label starts a longer label, 1 label inside other text
    rest: str  # text after the label (and its ':') in the same segment


def match_label(text: str, label: str) -> Match | None:
    words = label.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(w) for w in words)
    if re.match(r"[A-Za-z0-9]", label[-1]):
        pattern += r"(?![A-Za-z])"  # "No" must not match the start of "Notes"
    m = re.search(pattern, text, re.IGNORECASE)
    if m is None:
        return None
    before = text[: m.start()].strip(" •-*\t")
    after = text[m.end() :]
    stripped = after.lstrip(" \t")
    if before:
        quality = 1
        rest = re.split(r"[:：]", stripped, maxsplit=1)[1] if re.search(r"[:：]", stripped) else stripped
    elif not stripped or stripped[0] in ":：=#|":
        quality, rest = 3, stripped[1:] if stripped else ""
    elif _DIGIT.match(stripped[0]) or not re.search(r"[:：]", stripped):
        quality, rest = 3, stripped  # "Tax ID 0105556123456", "ชื่อ-นามสกุล นายสมชาย"
    else:
        # "Date of birth: 1990" for label "Date": the label continues with more words
        head, rest = re.split(r"[:：]", stripped, maxsplit=1)
        quality = 2 if _LETTER.search(head) else 3
    return Match(quality, rest.strip(SEP).strip())


@dataclass
class Candidate:
    raw: str
    page: int
    method: str
    quality: int
    rank: int  # method preference within the same quality
    order: int  # reading order
    bbox: tuple[float, float, float, float] | None = None


def segment_below(lines: list[TextLine], li: int, seg: Segment, max_lines: int = 2) -> Segment | None:
    """The first segment under ``seg`` (horizontally overlapping) within the next lines."""
    height = max(seg.bbox.bottom - seg.bbox.top, 4.0)
    for nxt in lines[li + 1 : li + 1 + max_lines]:
        if nxt.bbox.top - seg.bbox.bottom > 3 * height:
            return None
        for s in nxt.segments:
            if s.bbox.x0 <= seg.bbox.x1 + height and seg.bbox.x0 <= s.bbox.x1 + height:
                return s
    return None


def label_candidates(page: Page, labels: list[str], start_order: int = 0) -> Iterator[Candidate]:
    """Every place on the page where one of ``labels`` might have its value."""
    order = start_order
    for li, line in enumerate(page.lines):
        for si, seg in enumerate(line.segments):
            best = None
            for lb in labels:
                m = match_label(seg.text, lb)
                if m and (best is None or m.quality > best.quality):
                    best = m
            if best is None:
                continue
            order += 1
            if best.rest:
                yield Candidate(best.rest, page.number, "label:same-line", best.quality, 0, order, _bb(seg))
            if si + 1 < len(line.segments):
                nxt = line.segments[si + 1]
                yield Candidate(nxt.text, page.number, "label:right", best.quality, 1, order, _bb(nxt))
            below = segment_below(page.lines, li, seg)
            if below is not None:
                yield Candidate(below.text, page.number, "label:below", best.quality, 2, order, _bb(below))


def _bb(seg: Segment) -> tuple[float, float, float, float]:
    return (seg.bbox.x0, seg.bbox.top, seg.bbox.x1, seg.bbox.bottom)


# --------------------------------------------------------------------------- #
# Tables without ruling lines
# --------------------------------------------------------------------------- #
def _numeric(text: str) -> bool:
    return has_digit(text) and not _LETTER.search(text.replace("%", ""))


def _column_of(seg: Segment, header: list[Segment]) -> int | None:
    """Header column a cell belongs to: most horizontal overlap, or the nearest centre if close."""
    best, best_ov = None, 0.0
    for i, h in enumerate(header):
        ov = min(seg.bbox.x1, h.bbox.x1 + 6) - max(seg.bbox.x0, h.bbox.x0 - 6)
        if ov > best_ov:
            best, best_ov = i, ov
    if best is not None:
        return best
    centre = (seg.bbox.x0 + seg.bbox.x1) / 2
    dist, idx = min((abs(centre - (h.bbox.x0 + h.bbox.x1) / 2), i) for i, h in enumerate(header))
    return idx if dist < 40 else None


def find_text_tables(lines: list[TextLine], avoid: list[tuple[float, float, float, float]] = ()) -> list[tuple]:
    """Tables drawn without lines: a header row of labels, then aligned rows that contain numbers.

    Returns (rows, bbox) per table. Conservative on purpose: a table must have
    >= 3 columns and >= 1 data row, each row must have a number and fit the
    header's columns; it ends at the first line that does not (e.g. "Total").
    """

    def inside(line: TextLine) -> bool:
        cy = (line.bbox.top + line.bbox.bottom) / 2
        return any(x0 <= line.bbox.x0 <= x1 and t <= cy <= b for x0, t, x1, b in avoid)

    tables = []
    i = 0
    while i < len(lines):
        header = lines[i].segments
        if (
            len(header) < 3
            or inside(lines[i])
            or not all(is_label_like(s.text) and not _numeric(s.text) for s in header)
        ):
            i += 1
            continue
        rows: list[list[str | None]] = []
        last = lines[i]
        step = None  # usual distance between rows
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            height = max(last.bbox.bottom - last.bbox.top, 4.0)
            gap = ln.bbox.top - last.bbox.top
            if ln.bbox.top - last.bbox.bottom > 3 * height or inside(ln):
                break
            if step is not None and gap > step * 1.6:
                break  # a bigger gap: the table has ended (totals, notes)
            cells: list[str | None] = [None] * len(header)
            ok = len(ln.segments) >= max(2, (len(header) + 1) // 2) and any(_numeric(s.text) for s in ln.segments)
            for s in ln.segments if ok else []:
                col = _column_of(s, header)
                if col is None:
                    ok = False
                    break
                cells[col] = s.text if cells[col] is None else f"{cells[col]} {s.text}"
            if ok and rows:
                filled = {c for c in range(len(header)) if any(r[c] for r in rows)}
                numeric_cols = {c for c in filled if all(r[c] is None or _numeric(r[c]) for r in rows)}
                if any(cells[c] and not _numeric(cells[c]) for c in numeric_cols):
                    ok = False  # words in a number column ("Subtotal"): not an item row
            if not ok:
                break
            if rows:
                step = gap if step is None else min(step, gap)
            rows.append(cells)
            last = ln
            j += 1
        if rows:
            x0 = min(s.bbox.x0 for s in header)
            x1 = max(max(s.bbox.x1 for s in header), max(ln.bbox.x1 for ln in lines[i + 1 : j]))
            bbox = (x0, lines[i].bbox.top, x1, lines[j - 1].bbox.bottom)
            tables.append(([[s.text for s in header], *rows], bbox))
            i = j
        else:
            i += 1
    return tables
