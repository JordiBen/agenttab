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
    res = json.loads(out.read_text())
    for mode in ("none", "keyword"):
        assert res["modes"][mode]["git_sha"] == _git("rev-parse", "HEAD")
        assert isinstance(res["modes"][mode]["git_dirty"], bool)


def test_git_state_without_git_is_explicitly_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(run_eval.subprocess, "run", boom)
    assert run_eval.git_state() == {"git_sha": "unknown", "git_dirty": None}
