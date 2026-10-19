"""Reviewer t83u (2026-10-19): Devpost needs the video under 3 minutes. Take B take 2 is edited to 2:51, which leaves
room under the 2:55 (175 s) target for the voiceover. The timing table in docs/video-script.md matches the edited cut:
rows are contiguous, they add up to the documented edited length, and every section/subsection heading uses the same
boundaries. The voiceover rule (2.7 words/s) is checked by tests/test_live_copy.py::test_video_stays_under_three_minutes."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
TARGET_S = 175
ROW = re.compile(r"^\| (\d) \| [^|]+ \| (\d:\d\d)–(\d:\d\d) \| (\d+) s \|$", re.M)


def _s(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def _edited():
    m = re.search(r"^Edited length: \*\*(\d:\d\d) \((\d+) s\)\*\*", VIDEO, re.M)
    assert m, "docs/video-script.md must state 'Edited length: **m:ss (N s)**' under ## Timeline"
    assert _s(m.group(1)) == int(m.group(2))
    return int(m.group(2))


def _heading(prefix: str):
    m = re.search(rf"^#{{2,3}} {re.escape(prefix)}[^\n]*\((\d:\d\d)–(\d:\d\d)\)", VIDEO, re.M)
    assert m, prefix
    return _s(m.group(1)), _s(m.group(2))


def test_edited_length_is_at_most_175_seconds():
    assert _edited() <= TARGET_S


def test_timing_table_sums_to_the_edited_length_and_is_contiguous():
    rows = ROW.findall(VIDEO)
    assert [r[0] for r in rows] == ["1", "2", "3", "4", "5"]
    assert _s(rows[0][1]) == 0
    for a, b in zip(rows, rows[1:]):
        assert a[2] == b[1], (a, b)
    for r in rows:
        assert _s(r[2]) - _s(r[1]) == int(r[3]), r
    assert sum(int(r[3]) for r in rows) == _edited() == _s(rows[-1][2])


def test_headings_use_the_table_boundaries():
    rows = {r[0]: (_s(r[1]), _s(r[2])) for r in ROW.findall(VIDEO)}
    for n, prefix in (("1", "1. Problem"), ("2", "2. Live demo"), ("3", "3. Proof"), ("4", "4. PayPal"), ("5", "5. End card")):
        assert _heading(prefix) == rows[n], prefix
    subs = [_heading(p) for p in ("2a.", "2b.", "2c.", "2d.", "2e.")]
    assert subs[0][0] == rows["2"][0] and subs[-1][1] == rows["2"][1]
    for a, b in zip(subs, subs[1:]):
        assert a[1] == b[0], (a, b)


def test_title_states_the_edited_length():
    first = VIDEO.splitlines()[0]
    assert first.startswith("# AgentTab demo video script")
    assert "3:00" not in first and re.search(r"\((\d:\d\d)\)", first).group(1) == next(
        m.group(1) for m in [re.search(r"^Edited length: \*\*(\d:\d\d)", VIDEO, re.M)] if m)


@pytest.mark.skipif(not os.environ.get("AGENTTAB_EDITED_VIDEO") or not shutil.which("ffprobe"),
                    reason="set AGENTTAB_EDITED_VIDEO to the edited MP4 (not in the repo) and have ffprobe")
def test_edited_video_file_matches_the_documented_length():
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,pix_fmt,codec_name",
                          "-of", "json", os.environ["AGENTTAB_EDITED_VIDEO"]], capture_output=True, text=True, check=True).stdout
    info = json.loads(out); dur = float(info["format"]["duration"]); st = info["streams"][0]
    assert dur <= TARGET_S and abs(dur - _edited()) < 0.5, dur
    assert (st["codec_name"], st["width"], st["height"], st["pix_fmt"]) == ("h264", 1440, 900, "yuv420p")
