"""Command line interface: ``pdfstruct input.pdf [-o out.json]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .errors import PDFStructError
from .extractor import extract, extract_isolated
from .fields import apply_spec, filter_output, load_spec
from .models import Document
from .prompts import PROMPT_IDS, build_prompts
from .templates import DEFAULT_STORE, TemplateStore, spec_yaml

EXIT_OK, EXIT_PARTIAL, EXIT_ERROR = 0, 1, 2


def _run_one(src: Path, args: argparse.Namespace) -> tuple[int, str]:
    kwargs = {
        "password": args.password,
        "tables": not args.no_tables,
        "max_pages": args.max_pages,
        "ocr": args.ocr,
        "spec": args.loaded_spec,
    }
    try:
        doc = extract_isolated(src, timeout=args.timeout, **kwargs) if args.isolate else extract(src, **kwargs)
        if args.auto_template and args.loaded_spec is None:
            found = TemplateStore(args.store).best(doc, args.threshold)
            if found is None:
                doc.warnings.append(f"no template matched (threshold {args.threshold})")
            else:
                template, result = found
                apply_spec(doc, template.spec, pdf_path=src, password=args.password)
                doc.extraction.template, doc.extraction.template_score = template.name, result.score
    except PDFStructError as exc:
        return EXIT_ERROR, json.dumps({"error": type(exc).__name__, "message": str(exc), "file": src.name})
    if args.excel:
        from .export import xlsx_export

        target = args.excel / f"{src.stem}.xlsx" if args.input.is_dir() else args.excel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(xlsx_export(doc))
    ok = doc.extraction.status == "ok" and not doc.extraction.missing_required_fields
    code = EXIT_OK if ok else EXIT_PARTIAL
    indent = 2 if args.pretty else None
    if args.prompt:
        return code, next(x.text for x in build_prompts(doc) if x.id == args.prompt)
    if args.fields_only:
        out = {
            "file": doc.source.filename,
            "template": doc.extraction.template,
            "fields": {k: v.value for k, v in doc.fields.items()},
            "missing_required_fields": doc.extraction.missing_required_fields,
            "details": {k: v.model_dump(exclude_none=True) for k, v in doc.fields.items()},
        }
        return code, json.dumps(out, indent=indent, ensure_ascii=False)
    data = doc.model_dump(mode="json", exclude_none=args.compact)
    include = args.loaded_spec.include if args.loaded_spec else None
    return code, json.dumps(filter_output(data, include), indent=indent, ensure_ascii=False)


def template_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pdfstruct template", description="Learn, store and match document templates.")
    p.add_argument("--store", type=Path, default=DEFAULT_STORE, help="template directory (default ./templates)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("learn", help="create a template from sample PDFs, or add samples to it")
    s.add_argument("name")
    s.add_argument("pdfs", nargs="+", type=Path)
    s.add_argument("--description")
    s.add_argument("--password")
    sub.add_parser("list", help="list templates")
    s = sub.add_parser("show", help="print a template's spec as YAML")
    s.add_argument("name")
    s = sub.add_parser("match", help="score a PDF against all templates")
    s.add_argument("pdf", type=Path)
    s = sub.add_parser("set-spec", help="replace a template's spec with a hand-edited YAML/JSON file")
    s.add_argument("name")
    s.add_argument("spec", type=Path)
    s = sub.add_parser("delete", help="delete a template")
    s.add_argument("name")
    args = p.parse_args(argv)
    store = TemplateStore(args.store)

    try:
        if args.cmd == "learn":
            docs = [extract(f, password=args.password) for f in args.pdfs]
            t = store.learn(args.name, docs, description=args.description)
            print(
                f"template '{t.name}': {len(t.samples)} samples, {len(t.spec.fields)} fields, "
                f"{len(t.anchors)} anchors -> {store._path(t.name)}",
                file=sys.stderr,
            )
            for st in t.field_stats:
                print(f"  {st.name:<24} found in {st.found_in}/{st.samples}", file=sys.stderr)
            print(spec_yaml(t.spec))
        elif args.cmd == "list":
            for t in store.list():
                print(f"{t.name}\t{len(t.samples)} samples\t{len(t.spec.fields)} fields\t{t.description or ''}")
        elif args.cmd == "show":
            print(spec_yaml(store.load(args.name).spec))
        elif args.cmd == "match":
            doc = extract(args.pdf)
            print(json.dumps([r.model_dump() for r in store.match(doc)], indent=2, ensure_ascii=False))
        elif args.cmd == "set-spec":
            store.set_spec(args.name, args.spec)
            print(f"spec of '{args.name}' replaced (marked manual)", file=sys.stderr)
        elif args.cmd == "delete":
            store.delete(args.name)
    except PDFStructError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "template":
        return template_main(argv[1:])
    p = argparse.ArgumentParser(
        prog="pdfstruct",
        description="Extract PDF structure to JSON. Templates: 'pdfstruct template --help'.",
    )
    p.add_argument("input", nargs="?", type=Path, help="PDF file or directory of PDFs")
    p.add_argument("-o", "--output", type=Path, help="output file (single input) or directory")
    p.add_argument("--password", help="password for encrypted PDFs")
    p.add_argument("--no-tables", action="store_true", help="skip table detection (faster)")
    p.add_argument(
        "--ocr",
        choices=("auto", "off", "force"),
        default="auto",
        help="OCR scanned/garbled pages with Tesseract: auto (default), off, or force on every page",
    )
    p.add_argument("--max-pages", type=int, help="only process the first N pages")
    p.add_argument("--isolate", action="store_true", help="run in a child process with a hard timeout")
    p.add_argument("--timeout", type=float, default=120.0, help="timeout in seconds for --isolate")
    p.add_argument("--spec", type=Path, help="YAML/JSON file listing the variables to extract (docs/FIELDS.md)")
    p.add_argument("--fields-only", action="store_true", help="output only the variables from --spec")
    p.add_argument("--auto-template", action="store_true", help="recognise the document and apply a learned template")
    p.add_argument("--store", type=Path, default=DEFAULT_STORE, help="template directory (default ./templates)")
    p.add_argument("--threshold", type=float, default=0.6, help="minimum match score for --auto-template")
    p.add_argument(
        "--prompt",
        choices=PROMPT_IDS,
        help="print a ready-made AI prompt with this document's data instead of JSON",
    )
    p.add_argument(
        "--excel",
        type=Path,
        metavar="PATH",
        help="also write an Excel workbook (tables merged across pages, typed numbers); a folder for batch input",
    )
    p.add_argument("--pretty", action="store_true", help="indent the JSON output")
    p.add_argument("--compact", action="store_true", help="omit null fields")
    p.add_argument("--schema", action="store_true", help="print the JSON Schema of the output and exit")
    p.add_argument("--version", action="version", version=f"pdfstruct {__version__}")
    args = p.parse_args(argv)

    if args.schema:
        print(json.dumps(Document.model_json_schema(), indent=2, ensure_ascii=False))
        return EXIT_OK
    if args.input is None:
        p.error("input is required")
    if args.fields_only and not (args.spec or args.auto_template):
        p.error("--fields-only needs --spec or --auto-template")
    try:
        args.loaded_spec = load_spec(args.spec) if args.spec else None
    except PDFStructError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR

    if args.input.is_dir():
        files = sorted(args.input.rglob("*.pdf"))
        out_dir = args.output or args.input
        out_dir.mkdir(parents=True, exist_ok=True)
        worst = EXIT_OK
        for f in files:
            code, payload = _run_one(f, args)
            (out_dir / f"{f.stem}.json").write_text(payload, encoding="utf-8")
            print(f"[{'ok' if code == 0 else 'partial' if code == 1 else 'error'}] {f}", file=sys.stderr)
            worst = max(worst, code)
        return worst

    code, payload = _run_one(args.input, args)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
    if code == EXIT_ERROR:
        print(payload, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
