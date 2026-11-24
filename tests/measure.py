"""Measure template learning on the realistic corpus: learn from 2 samples, read the 3rd.

    python tests/measure.py            # prints a score card
Used by tests/test_learning_quality.py to stop regressions.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import corpus  # noqa: E402

from pdfstruct import TemplateStore, extract, extract_auto  # noqa: E402


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(a).strip() == str(b).strip()


def score(builder, data: list[dict], name: str, work: Path, **extract_kw) -> dict:
    paths = []
    truths = []
    for i, d in enumerate(data):
        p, t = builder(work / f"{name}_{i}.pdf", d)
        paths.append(p)
        truths.append(t)
    store = TemplateStore(work / f"tpl_{name}")
    store.learn(name, [extract(p, **extract_kw) for p in paths[:2]])
    doc = extract_auto(paths[2], store, **extract_kw)
    truth = truths[2]
    values = {k: f.value for k, f in doc.fields.items() if f.found}
    by_value = {k: any(_same(v, truth[k]) for v in values.values()) for k in truth}
    by_name = {k: k in values and _same(values[k], truth[k]) for k in truth}
    return {
        "doc": name,
        "matched_template": doc.extraction.template,
        "found": sum(by_value.values()),
        "named": sum(by_name.values()),
        "total": len(truth),
        "missing": [k for k, ok in by_value.items() if not ok],
        "fields": values,
    }


def run(work: Path, scanned: bool = False) -> list[dict]:
    results = [score(corpus.kv_invoice, corpus.KV_INVOICES, "kv_invoice", work)]
    if corpus.THAI_FONT.exists():
        results.append(score(corpus.thai_form, corpus.THAI_FORMS, "thai_form", work))
    if scanned:

        def scanned_invoice(path: Path, d: dict):
            src, truth = corpus.kv_invoice(path.with_suffix(".src.pdf"), d)
            return corpus.scanned_copy(src, path), truth

        results.append(score(scanned_invoice, corpus.KV_INVOICES, "scanned_invoice", work))
    return results


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        for r in run(Path(tmp), scanned="--scanned" in sys.argv):
            print(f"{r['doc']:<16} template={r['matched_template']!s:<16} found {r['found']}/{r['total']}  "
                  f"named {r['named']}/{r['total']}  missing={r['missing']}")  # fmt: skip
            if "-v" in sys.argv:
                print("   ", r["fields"])
