"""Build the desktop app with PyInstaller.

    pip install -e ".[build]"
    python packaging/build.py

Output: dist/pdfstruct/ (Windows/Linux folder with the executable) or
dist/pdfstruct.app (macOS). Run on each OS you want a build for; PyInstaller
does not cross-compile.
"""

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]

args = [
    str(ROOT / "packaging" / "launcher.py"),
    "--name=pdfstruct",
    "--noconfirm",
    "--clean",
    f"--distpath={ROOT / 'dist'}",
    f"--workpath={ROOT / 'build'}",
    f"--specpath={ROOT / 'build'}",
    "--collect-data=pdfstruct",  # the web UI (static files)
    "--collect-data=pdfminer",  # CMaps needed for CJK/Thai fonts
    "--collect-all=pypdfium2",
    "--collect-all=pypdfium2_raw",
    "--hidden-import=pdfstruct.app.server",
]
if sys.platform in ("win32", "darwin"):
    args.append("--windowed")  # no console window; on macOS this makes pdfstruct.app
if sys.platform == "darwin":
    args.append("--osx-bundle-identifier=io.github.pdfstruct")

PyInstaller.__main__.run(args)
