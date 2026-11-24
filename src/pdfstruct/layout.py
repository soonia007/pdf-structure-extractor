"""Turn positioned words into lines, blocks and typed blocks (heading / list / ...).

The heuristics are deliberately simple and deterministic so the output is
stable across runs and versions; anything fancier (ML layout models) belongs
to a later phase and must be opt-in.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_LIST_RE = re.compile(r"^\s*(?:[\ufffd•●▪–—\-\*•▪◦]|\(?\d{1,3}[.)]|\(?[a-zA-Zก-ฮ][.)])\s+")
_THAI_END = re.compile(r"[\u0e00-\u0e7f]$")
_THAI_START = re.compile(r"[\u0e00-\u0e7f]")
_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi", re.I)


@dataclass
class Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    font: str | None
    size: float


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def size(self) -> float:
        return Counter(round(w.size, 1) for w in self.words).most_common(1)[0][0]

    @property
    def font(self) -> str | None:
        return Counter(w.font for w in self.words).most_common(1)[0][0]

    @property
    def text(self) -> str:
        out: list[str] = []
        prev: Word | None = None
        for w in self.words:
            gap = w.x0 - prev.x1 if prev is not None else 0
            # OCR splits Thai (written without spaces) into pieces: glue them back unless the gap is a real space
            thai_join = (
                prev is not None and w.font == "ocr" and _THAI_END.search(prev.text) and _THAI_START.match(w.text)
            )
            if prev is not None and gap > (0.45 if thai_join else 0.15) * max(w.size, 1):
                out.append(" ")
            out.append(w.text)
            prev = w
        text = "".join(out)
        # an unmapped glyph opening a line is a bullet marker
        if text.startswith("\ufffd "):
            text = "\u2022" + text[1:]
        return text


@dataclass
class RawBlock:
    lines: list[Line]

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(ln.x0 for ln in self.lines),
            min(ln.top for ln in self.lines),
            max(ln.x1 for ln in self.lines),
            max(ln.bottom for ln in self.lines),
        )

    @property
    def size(self) -> float:
        return Counter(ln.size for ln in self.lines).most_common(1)[0][0]

    @property
    def font(self) -> str | None:
        return Counter(ln.font for ln in self.lines).most_common(1)[0][0]

    @property
    def bold(self) -> bool:
        return bool(self.font and _BOLD_RE.search(self.font))


def group_lines(words: list[Word]) -> list[Line]:
    """Cluster words whose vertical centres are close into lines (top-to-bottom, left-to-right)."""
    lines: list[Line] = []
    for w in sorted(words, key=lambda w: (round(w.top, 1), w.x0)):
        centre = (w.top + w.bottom) / 2
        tol = max(w.size * 0.5, 2.0)
        for ln in reversed(lines[-3:]):
            lc = (ln.top + ln.bottom) / 2
            if abs(lc - centre) <= tol:
                ln.words.append(w)
                break
        else:
            lines.append(Line([w]))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: (round(ln.top, 1), ln.x0))
    return lines


def split_segments(line: Line) -> list[Line]:
    """Split a visual line where the horizontal gap is wide (columns, label ... value, table cells).

    Ordinary word spacing is ~0.25-0.35 em; a gap of more than 1.5 em almost
    always separates different pieces of information.
    """
    limit = max(1.5 * line.size, 8.0)
    segments = [Line([line.words[0]])]
    for prev, w in zip(line.words, line.words[1:], strict=False):
        if w.x0 - prev.x1 > limit:
            segments.append(Line([w]))
        else:
            segments[-1].words.append(w)
    # a bullet or list number sits apart from its text but belongs to it
    if len(segments) > 1 and len(segments[0].words) == 1 and _LIST_RE.match(segments[0].words[0].text + " x"):
        segments[1].words[:0] = segments[0].words
        segments.pop(0)
    return segments


def _overlaps(a: Line, b: Line, slack: float) -> bool:
    return a.x0 <= b.x1 + slack and b.x0 <= a.x1 + slack


def group_blocks(lines: list[Line]) -> list[RawBlock]:
    """Group line pieces into blocks, column by column.

    Pieces stack into the same block only when they are vertically adjacent,
    horizontally overlapping and in the same style, so text in a left and a
    right column (or label | value pairs) is not merged together.
    """
    blocks: list[RawBlock] = []
    open_blocks: list[RawBlock] = []
    for ln in lines:
        for piece in split_segments(ln):
            target = None
            starts_list = bool(_LIST_RE.match(piece.text))
            for blk in reversed(open_blocks):
                prev = blk.lines[-1]
                gap = piece.top - prev.bottom
                if gap > prev.size * 0.8 or gap < -prev.size * 0.3:
                    continue
                same_style = abs(piece.size - prev.size) < 0.6 and piece.font == prev.font
                if same_style and not starts_list and _overlaps(piece, prev, prev.size):
                    target = blk
                    break
            if target is None:
                target = RawBlock([piece])
                blocks.append(target)
                open_blocks.append(target)
            else:
                target.lines.append(piece)
        # blocks whose last line is far above can no longer grow
        open_blocks = [b for b in open_blocks if ln.bottom - b.lines[-1].bottom <= b.lines[-1].size * 2.5]
    return blocks


def body_font_size(blocks: list[RawBlock]) -> float:
    counter: Counter[float] = Counter()
    for b in blocks:
        for ln in b.lines:
            counter[ln.size] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else 10.0


def classify(block: RawBlock, body_size: float) -> str:
    text = block.text.strip()
    if _LIST_RE.match(text):
        return "list_item"
    short = len(text) <= 150 and len(block.lines) <= 3
    if short and block.size >= body_size * 1.15:
        return "heading"
    if short and block.bold and not text.endswith((".", ":", ",")) and len(block.lines) == 1:
        return "heading"
    return "paragraph"


def heading_levels(sizes: list[float]) -> dict[float, int]:
    """Largest heading size -> level 1, next -> 2, ... (max 6)."""
    distinct = sorted({round(s, 1) for s in sizes}, reverse=True)
    return {s: min(i + 1, 6) for i, s in enumerate(distinct)}


def digits_stripped(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())
