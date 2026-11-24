"""Entry point for the PyInstaller build of the desktop app."""

import multiprocessing

from pdfstruct.app.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # child workers of extract_isolated re-enter here
    raise SystemExit(main())
