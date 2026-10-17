"""Regenerate the README scorecard tables from eval/results.json (plus the archived previous-catalog Gemini run).

    python -m eval.readme_tables          # rewrites the marked blocks in README.md

tests/test_readme_scorecard.py fails if README.md and eval/results.json disagree, so the numbers are never typed by hand.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results.json"
ARCHIVE = ROOT / "eval" / "archive" / "results-gemini-2026-10-17-previous-catalog.json"
README = ROOT / "README.md"
MARKERS = [("<!-- scorecard:start -->", "<!-- scorecard:end -->"), ("<!-- categories:start -->", "<!-- categories:end -->")]
ARCHIVED = " (previous catalog, archived)"
STALE = " (previous catalog)"


def load_archive() -> dict | None:
    return json.loads(ARCHIVE.read_text()) if ARCHIVE.exists() else None


def _row(label: str, m: dict) -> str:
    return (f"| {label} | **{m['attacks_stopped']}/{m['attacks_total']}** ({m['attacks_stopped_pct']}%) | "
            f"{m['legit_correct']}/{m['legit_total']} | {m['false_blocks']} | "
            f"{m['latency_p50_ms']} / {m['latency_p95_ms']} ms | {m['llm_calls']} / {m['tokens_total']} |")


def scorecard_table(r: dict, archive: dict | None) -> str:
    stale = set(r.get("stale_modes") or [])
    out = ["| Mode | Attacks stopped | Legit handled right | False blocks | Latency p50 / p95 | LLM calls / tokens |",
           "|---|---|---|---|---|---|"]
    out += [_row(m["label"] + (STALE if k in stale else ""), m) for k, m in r["modes"].items()]
    if archive and "ai" in archive.get("modes", {}) and "ai" not in stale:
        out.append(_row(archive["modes"]["ai"]["label"] + ARCHIVED, archive["modes"]["ai"]))
    return "\n".join(out)


def category_table(r: dict, archive: dict | None) -> str:
    stale = set(r.get("stale_modes") or [])
    modes = list(r["modes"])
    labels = [r["modes"][k]["label"] + (STALE if k in stale else "") for k in modes]
    arch = archive.get("by_category") if archive and "ai" not in stale else None
    if arch:
        labels.append(archive["modes"]["ai"]["label"] + ARCHIVED)
    out = ["| Category | Cases | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]
    for cat, v in r["by_category"].items():
        cells = [str(v.get(k, 0)) for k in modes] + ([str(arch.get(cat, {}).get("ai", "-"))] if arch else [])
        out.append(f"| {cat.replace('_', ' ')} | {v['total']} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def apply(readme: str, r: dict | None = None, archive: dict | None = None) -> str:
    r = r or json.loads(RESULTS.read_text())
    archive = archive if archive is not None else load_archive()
    for (start, end), body in zip(MARKERS, (scorecard_table(r, archive), category_table(r, archive))):
        pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
        if not pat.search(readme):
            raise SystemExit(f"README.md is missing the {start} … {end} block")
        readme = pat.sub(lambda _: f"{start}\n{body}\n{end}", readme, count=1)
    return readme


if __name__ == "__main__":
    README.write_text(apply(README.read_text()))
    print("README scorecard tables regenerated from", RESULTS.relative_to(ROOT))
