"""Every eval mode records the git commit it ran on (and whether the deciding code was dirty)."""
import json, re, shutil, subprocess
from pathlib import Path
import pytest
from eval import run_eval

ROOT = Path(__file__).resolve().parent.parent
HAS_GIT = shutil.which("git") and (ROOT / ".git").exists()


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(not HAS_GIT, reason="needs the git checkout")
def test_runner_records_the_commit_of_each_mode(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "r.json"
    run_eval.main(["--modes", "none,keyword", "--out", str(out)])
    res = json.loads(out.read_text(encoding="utf-8"))
    for mode in ("none", "keyword"):
        assert res["modes"][mode]["git_sha"] == _git("rev-parse", "HEAD")
        assert isinstance(res["modes"][mode]["git_dirty"], bool)


def test_git_state_without_git_is_explicitly_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(run_eval.subprocess, "run", boom)
    assert run_eval.git_state() == {"git_sha": "unknown", "git_dirty": None}


def test_readme_scorecard_names_the_commit_of_each_run():
    from eval import readme_tables
    r = {"modes": {"ai": {"label": "Rules + Claude judge", "attacks_stopped": 1, "attacks_total": 1, "attacks_stopped_pct": 100.0,
                          "legit_correct": 1, "legit_total": 1, "false_blocks": 0, "latency_p50_ms": 1, "latency_p95_ms": 1,
                          "llm_calls": 1, "tokens_total": 1, "git_sha": "a" * 40, "git_dirty": True}}}
    t = readme_tables.scorecard_table(r, None)
    assert "Rules + Claude judge @ `aaaaaaa` (uncommitted changes)" in t


def test_committed_results_name_a_clean_commit_for_every_mode():
    res = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
    for mode, m in res["modes"].items():
        assert re.fullmatch(r"[0-9a-f]{40}", m.get("git_sha", "")), mode
        assert m["git_dirty"] is False, mode


@pytest.mark.skipif(not HAS_GIT, reason="needs the git checkout")
def test_committed_results_commit_is_in_this_history():
    res = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
    for mode, m in res["modes"].items():
        r = subprocess.run(["git", "merge-base", "--is-ancestor", m["git_sha"], "HEAD"], cwd=ROOT)
        assert r.returncode == 0, f"{mode} ran on {m['git_sha'][:7]}, not an ancestor of HEAD"
