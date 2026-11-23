"""Start the app: ``pdfstruct-app`` (native window) or ``pdfstruct-app --browser``."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .. import __version__


def default_store() -> Path:
    env = os.environ.get("PDFSTRUCT_TEMPLATES")
    return Path(env) if env else Path.home() / ".pdfstruct" / "templates"


def _open_window(url: str) -> bool:
    """Show the native window; False if no GUI backend is available (caller falls back to the browser)."""
    try:
        import webview  # pywebview
    except ImportError:
        print("pywebview is not installed; opening in the browser instead.", file=sys.stderr)
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("pdfstruct", url, width=1360, height=900, min_size=(860, 600))
        webview.start()
        return True
    except Exception as exc:  # e.g. Linux without GTK/Qt
        print(f"native window unavailable ({type(exc).__name__}: {exc}); opening in the browser.", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()  # required for --isolate workers inside a PyInstaller build
    p = argparse.ArgumentParser(prog="pdfstruct-app", description="pdfstruct desktop app")
    p.add_argument("--browser", action="store_true", help="open in the default web browser instead of a window")
    p.add_argument("--port", type=int, default=0, help="local port (default: a free one)")
    p.add_argument("--store", type=Path, default=default_store(), help="template folder")
    p.add_argument("--no-isolate", action="store_true", help="read PDFs in-process (faster, less safe)")
    p.add_argument("--version", action="version", version=f"pdfstruct {__version__}")
    args = p.parse_args(argv)

    from .server import AppState, make_server

    state = AppState(args.store, isolate=not args.no_isolate)
    server = make_server(state, args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={state.token}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        if args.browser or not _open_window(url):
            print(f"pdfstruct is running. Open: {url}\n(Ctrl+C to quit)", file=sys.stderr, flush=True)
            webbrowser.open(url)
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        state.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
