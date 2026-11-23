"""``python -m pdfstruct.app`` — works even when the Scripts folder is not on PATH."""

from .desktop import main

# The guard matters: extract_isolated starts worker processes with "spawn"
# (the only method on Windows/macOS), which re-imports this module as
# __mp_main__. Without it every PDF would launch another copy of the app.
if __name__ == "__main__":
    raise SystemExit(main())
