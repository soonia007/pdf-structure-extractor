"""OCR for scanned pages, using the Tesseract command-line program.

Tesseract is called as an external program (no Python binding needed) and
asked for TSV output, which gives every word with its box and confidence.
The words then go through the same layout code as a normal PDF, so blocks,
lines, label/value detection and templates all work on scanned pages too.

Where Tesseract is looked for, in order:
1. ``PDFSTRUCT_TESSERACT`` (full path to the program)
2. a ``tesseract`` folder next to the packaged app (the Windows build ships one)
3. ``tesseract`` on PATH
4. the usual install folders on Windows and macOS
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .layout import Word
from .normalize import normalize_text

DEFAULT_LANGS = ("tha", "eng")
OCR_FONT = "ocr"
DPI = 300
TIMEOUT_S = 120


@dataclass(frozen=True)
class OcrStatus:
    available: bool
    path: str | None
    version: str | None
    languages: tuple[str, ...]
    message: str

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "languages": list(self.languages),
            "thai": "tha" in self.languages,
            "message": self.message,
        }


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("PDFSTRUCT_TESSERACT")
    if env:
        out.append(Path(env))
    exe = "tesseract.exe" if sys.platform == "win32" else "tesseract"
    bases = [Path(sys.executable).parent]
    if getattr(sys, "_MEIPASS", None):
        bases.append(Path(sys._MEIPASS))  # PyInstaller one-folder build
    out += [b / "tesseract" / exe for b in bases]
    found = shutil.which("tesseract")
    if found:
        out.append(Path(found))
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            if root:
                out.append(Path(root) / "Tesseract-OCR" / exe)
    elif sys.platform == "darwin":
        out += [Path("/opt/homebrew/bin/tesseract"), Path("/usr/local/bin/tesseract")]
    return out


def _env_for(exe: Path) -> dict[str, str]:
    env = dict(os.environ)
    tessdata = exe.parent / "tessdata"
    if tessdata.is_dir() and "TESSDATA_PREFIX" not in os.environ:
        env["TESSDATA_PREFIX"] = str(tessdata)  # bundled copy: use its own language files
    return env


def _run(exe: Path, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        timeout=timeout,
        env=_env_for(exe),
        creationflags=flags,
        check=False,
    )


@lru_cache(maxsize=1)
def status() -> OcrStatus:
    for exe in _candidates():
        if not exe.is_file():
            continue
        try:
            ver = _run(exe, "--version")
            langs = _run(exe, "--list-langs")
        except (OSError, subprocess.SubprocessError):
            continue
        version = (ver.stdout or ver.stderr).decode("utf-8", "replace").splitlines()[0:1]
        listed = langs.stdout.decode("utf-8", "replace").splitlines()[1:]
        languages = tuple(sorted(x.strip() for x in listed if x.strip()))
        thai = "tha" in languages
        msg = "พร้อมใช้งาน" + ("" if thai else " (ยังไม่มีภาษาไทย: ติดตั้งไฟล์ tha.traineddata)")
        return OcrStatus(True, str(exe), version[0] if version else None, languages, msg)
    return OcrStatus(False, None, None, (), "ไม่พบโปรแกรม Tesseract OCR: หน้าที่เป็นภาพสแกนจะอ่านข้อความไม่ได้")


def available() -> bool:
    return status().available


@dataclass
class OcrResult:
    words: list[Word]
    confidence: float  # mean word confidence, 0..1
    languages: str


def _is_thai(ch: str) -> bool:
    return "\u0e00" <= ch <= "\u0e7f"


def _spaces_from_text(entries: list[dict], text: str | None) -> list[bool] | None:
    """For each entry, whether Tesseract's text puts a space before it (None if they do not match)."""
    if text is None or text.replace(" ", "") != "".join(e["text"] for e in entries):
        return None
    flags, pos = [], 0
    for e in entries:
        space = False
        while pos < len(text) and text[pos] == " ":
            space, pos = True, pos + 1
        flags.append(space)
        pos += len(e["text"])
    return flags


def _line_words(entries: list[dict], text: str | None = None) -> list[Word]:
    """Rebuild one OCR line in Tesseract's own order.

    For Thai, Tesseract returns characters and vowel/tone marks as separate
    "words" (marks sit above or below the line), so they are glued back in
    order; a space is kept only for a real gap, and a wide gap starts a new piece.
    """
    heights = sorted(e["bottom"] - e["top"] for e in entries)
    h = max(heights[len(heights) // 2], 1.0)
    spaces = _spaces_from_text(entries, text)
    groups: list[dict] = []
    for k, e in enumerate(entries):
        if groups:
            g = groups[-1]
            gap = e["x0"] - g["x1"]
            if gap <= 1.2 * h:
                if spaces is not None:
                    space = spaces[k]
                else:
                    both_thai = _is_thai(g["text"][-1]) and _is_thai(e["text"][0])
                    space = gap > (0.35 if both_thai else 0.12) * h
                if space:
                    g["text"] += " "
                g["text"] += e["text"]
                g["x0"], g["x1"] = min(g["x0"], e["x0"]), max(g["x1"], e["x1"])
                g["top"], g["bottom"] = min(g["top"], e["top"]), max(g["bottom"], e["bottom"])
                continue
        groups.append(dict(e))
    return [
        Word(
            text=normalize_text(g["text"]),
            x0=g["x0"],
            top=g["top"],
            x1=g["x1"],
            bottom=g["bottom"],
            font=OCR_FONT,
            size=h * 0.8,
        )
        for g in groups
        if normalize_text(g["text"])
    ]


def ocr_page(pdfium_page, langs: tuple[str, ...] = DEFAULT_LANGS, dpi: int = DPI) -> OcrResult:
    """Render one pypdfium2 page and OCR it. Word boxes come back in PDF points (top-left origin)."""
    st = status()
    if not st.available:
        raise RuntimeError(st.message)
    use = [lang for lang in langs if lang in st.languages] or ["eng"]
    scale = dpi / 72
    image = pdfium_page.render(scale=scale, grayscale=True).to_pil()
    from PIL import ImageOps

    image = ImageOps.autocontrast(image)
    with tempfile.TemporaryDirectory(prefix="pdfstruct-ocr-") as tmp:
        img_path = Path(tmp) / "page.png"
        image.save(img_path)
        out = Path(tmp) / "ocr"
        proc = _run(
            Path(st.path), str(img_path), str(out), "-l", "+".join(use), "--psm", "3", "--dpi", str(dpi), "tsv", "txt",
            timeout=TIMEOUT_S,
        )  # fmt: skip
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300] or "tesseract failed")
        tsv = out.with_suffix(".tsv").read_text(encoding="utf-8", errors="replace")
        txt_path = out.with_suffix(".txt")
        txt_lines = [ln for ln in txt_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    entries: dict[tuple[str, str, str], list[dict]] = {}
    confs: list[float] = []
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            continue
        if row.get("level") != "5" or not text or conf < 0:
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        left, top = int(row["left"]) / scale, int(row["top"]) / scale
        entries.setdefault(key, []).append(
            {"text": text, "x0": left, "top": top, "x1": left + int(row["width"]) / scale,
             "bottom": top + int(row["height"]) / scale}
        )  # fmt: skip
        confs.append(conf / 100)
    words: list[Word] = []
    lines = list(entries.values())
    # Tesseract's plain text has the right spaces (Thai has none between letters); use it when it lines up
    texts = txt_lines if len(txt_lines) == len(lines) else [None] * len(lines)
    for line, text in zip(lines, texts, strict=True):
        words.extend(_line_words(line, text))
    mean = sum(confs) / len(confs) if confs else 0.0
    return OcrResult(words=words, confidence=round(mean, 3), languages="+".join(use))
