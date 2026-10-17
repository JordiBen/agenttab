"""The README scorecard tables are generated from eval/results.json (python -m eval.readme_tables), never typed by hand."""
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_readme_scorecard_tables_match_results_json():
    from eval import readme_tables
    readme = (ROOT / "README.md").read_text()
    assert readme == readme_tables.apply(readme), "README scorecard is out of date: run python -m eval.readme_tables"
    for start, _ in readme_tables.MARKERS:
        assert start in readme


def test_readme_ai_row_is_the_committed_claude_run():
    import json
    from eval import readme_tables
    r = json.loads((ROOT / "eval" / "results.json").read_text())
    ai = r["modes"]["ai"]
    table = readme_tables.scorecard_table(r, readme_tables.load_archive())
    row = next(l for l in table.splitlines() if l.startswith(f"| {ai['label']} |"))
    assert f"**{ai['attacks_stopped']}/{ai['attacks_total']}**" in row and f"{ai['legit_correct']}/{ai['legit_total']}" in row
    if "ai" not in r.get("stale_modes", []):
        assert "previous catalog" not in row
