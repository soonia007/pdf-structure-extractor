"""Tables to Excel / CSV, the same way every time.

* Tables with the same header on consecutive pages (a statement split over
  pages, header repeated) become one table; a page that continues a table
  without repeating the header is appended too.
* Cells are typed by strict rules only: amounts like ``1,234.50`` or
  ``(500.00)`` become numbers with a number format; dates, times, account or
  tax numbers, anything with a leading zero or more than 15 digits stays text,
  so Excel never re-interprets them ("06-11-26" is not turned into a date,
  "0105556123456" keeps its zero).
* Text that Excel would run as a formula (starting with = + - @) is written as
  plain text: a PDF must not be able to plant a formula in a spreadsheet.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any

from .models import Document
from .prompts import field_values

_AMOUNT = re.compile(r"^(?P<open>\()?(?P<sign>[-+])?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?(?P<close>\))?$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")


def _fold(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").casefold()


def typed_cell(text: Any) -> tuple[Any, str | None]:
    """(value, number_format). Numbers only for unambiguous amounts; everything else stays text."""
    if text is None:
        return None, None
    if isinstance(text, bool):
        return text, None
    if isinstance(text, int | float):
        return text, None
    t = str(text).strip()
    if not t:
        return None, None
    m = _AMOUNT.match(t)
    if m and bool(m["open"]) == bool(m["close"]):
        digits = m["int"].replace(",", "")
        leading_zero = len(digits) > 1 and digits.startswith("0")
        if not leading_zero and len(digits) <= 15:
            dec = m["dec"] or ""
            value: float | int = float(digits + dec) if dec else int(digits)
            if m["open"] or m["sign"] == "-":
                value = -value
            places = len(dec) - 1 if dec else 0
            fmt = "#,##0" + ("." + "0" * places if places else "") if "," in m["int"] or places else None
            return value, fmt
    return t, None


def _safe_text(value: Any) -> Any:
    """For CSV: keep a cell from being run as a formula when opened in Excel."""
    if isinstance(value, str) and value.startswith(_FORMULA_START) and typed_cell(value)[1] is None:
        if not _AMOUNT.match(value.strip()):
            return "'" + value
    return value


def _is_header_like(row: list) -> bool:
    cells = [c for c in row if c]
    return bool(cells) and not any(_AMOUNT.match(str(c).strip()) for c in cells)


def merged_tables(doc: Document) -> list[dict]:
    """All tables in the document, with tables continued across pages joined into one."""
    groups: list[dict] = []
    for page in doc.pages:
        for t_index, t in enumerate(page.tables):
            rows = [list(r) for r in t.rows if any(c not in (None, "") for c in r)]
            if not rows:
                continue
            key = [_fold(c) for c in rows[0]]
            last = groups[-1] if groups else None
            same_width = last is not None and len(rows[0]) == len(last["header"])
            if same_width and key == last["key"]:
                last["rows"].extend(rows[1:])  # header repeated on the new page
            elif same_width and t_index == 0 and page.number == last["pages"][-1] + 1 and not _is_header_like(rows[0]):
                last["rows"].extend(rows)  # continued without a header
            else:
                groups.append(
                    {"header": rows[0], "key": key, "rows": rows[1:], "pages": [page.number], "strategy": t.strategy}
                )
                continue
            if page.number not in last["pages"]:
                last["pages"].append(page.number)
    out = []
    for i, g in enumerate(groups, 1):
        pages = g["pages"]
        span = f"หน้า {pages[0]}" if len(pages) == 1 else f"หน้า {pages[0]}–{pages[-1]}"
        out.append(
            {
                "index": i,
                "title": f"ตาราง {i}",
                "pages": pages,
                "span": span,
                "header": g["header"],
                "rows": g["rows"],
                "n_rows": len(g["rows"]),
                "n_cols": len(g["header"]),
                "strategy": g["strategy"],
            }
        )
    return out


def table_csv(table: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([_safe_text(c or "") for c in table["header"]])
    for row in table["rows"]:
        w.writerow([_safe_text("" if c is None else c) for c in row])
    return "﻿" + buf.getvalue()


def _sheet_name(name: str, used: set[str]) -> str:
    base = _BAD_SHEET.sub("_", name).strip("'")[:31] or "Sheet"
    candidate, n = base, 2
    while candidate.casefold() in used:
        suffix = f" ({n})"
        candidate = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.casefold())
    return candidate


def xlsx_export(doc: Document, edits: dict[str, Any] | None = None) -> bytes:
    """One workbook: document info, variables, every table (merged across pages), table variables."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    used: set[str] = set()
    head_font = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EAF0FD")

    def write_sheet(ws, header: list, rows: list[list]) -> None:
        widths = [len(str(h or "")) for h in header]
        for c, h in enumerate(header, 1):
            cell = ws.cell(row=1, column=c, value=str(h or f"col{c}"))
            cell.data_type = "s"
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        for r, row in enumerate(rows, 2):
            for c, raw in enumerate(row, 1):
                value, fmt = typed_cell(raw)
                cell = ws.cell(row=r, column=c, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"  # never a formula
                if fmt:
                    cell.number_format = fmt
                if c <= len(widths):
                    widths[c - 1] = max(widths[c - 1], len(str(raw or "")))
                else:
                    widths.append(len(str(raw or "")))
        for c, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 8), 60)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(max(len(header), 1))}{len(rows) + 1}"

    info = wb.active
    info.title = _sheet_name("ข้อมูลเอกสาร", used)
    meta = [
        ["ไฟล์", doc.source.filename],
        ["จำนวนหน้า", doc.source.page_count],
        ["Template", doc.extraction.template or ""],
        ["สถานะ", doc.extraction.status],
        ["sha256", doc.source.sha256],
        ["ส่งออกเมื่อ (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")],
    ]
    write_sheet(info, ["รายการ", "ค่า"], meta)

    values = field_values(doc, edits)
    scalar = [(k, v) for k, v in values.items() if not isinstance(v, list)]
    if scalar:
        ws = wb.create_sheet(_sheet_name("ตัวแปร", used))
        rows = []
        for name, value in scalar:
            f = doc.fields[name]
            rows.append([name, value, f.page, f.confidence, f.method or "", "แก้ไขแล้ว" if name in (edits or {}) else ""])
        write_sheet(ws, ["ตัวแปร", "ค่า", "หน้า", "ความมั่นใจ", "วิธีที่หาเจอ", "หมายเหตุ"], rows)

    for name, value in values.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            keys = list(dict.fromkeys(k for item in value for k in item))
            ws = wb.create_sheet(_sheet_name(name, used))
            write_sheet(ws, keys, [[item.get(k) for k in keys] for item in value])

    for t in merged_tables(doc):
        ws = wb.create_sheet(_sheet_name(f"{t['title']} ({t['span']})", used))
        write_sheet(ws, t["header"], t["rows"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
