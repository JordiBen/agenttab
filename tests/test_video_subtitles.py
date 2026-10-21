"""Reviewer t85u (2026-10-21): docs/video-subtitles.srt carries the voiceover of docs/video-script.md as subtitles for
the edited 2:51 cut (agenttab_takeB2_68a7cf6_edit.mp4, 170.96 s). Cues are ordered, don't overlap, stay inside the
video and inside their segment of the timing table, are readable (at most 2 lines of 42 characters, 1 to 7 s each),
and their text is exactly the voiceover, so the two can't drift. Not burned into the video."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRT = ROOT / "docs" / "video-subtitles.srt"
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
EDITED_S = 170.96
TS = re.compile(r"^(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)$")


def _s(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("\u2019", "'")).strip()


def _segments():
    """(start, end, voiceover) for every section/subsection that has a voiceover, in document order."""
    out = []
    for sec in re.split(r"\n(?=#{2,3} \d)", VIDEO):
        m = re.match(r"#{2,3} [^\n]*\((\d:\d\d)–(\d:\d\d)\)", sec)
        vo = re.findall(r"^> (.*)$", sec, re.M)
        if m and vo:
            out.append((_s(m.group(1)), _s(m.group(2)), _norm(" ".join(vo)).strip('"')))
    return out


def _cues():
    assert SRT.exists(), "docs/video-subtitles.srt is missing"
    raw = SRT.read_text(encoding="utf-8")
    assert "\r" not in raw and raw.endswith("\n")
    cues = []
    for i, block in enumerate(raw.strip("\n").split("\n\n"), 1):
        lines = block.split("\n")
        assert lines[0] == str(i), block
        m = TS.match(lines[1])
        assert m, block
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        assert len(lines) in (3, 4), block
        cues.append((start, end, lines[2:]))
    return cues


def test_srt_parses_and_cues_are_ordered_without_overlap_inside_the_video():
    cues = _cues()
    assert cues
    prev_end = 0.0
    for start, end, _ in cues:
        assert 0 <= start < end <= EDITED_S, (start, end)
        assert start >= prev_end, (start, prev_end)
        prev_end = end


def test_cues_are_readable():
    for start, end, lines in _cues():
        assert 1.0 <= end - start <= 7.0, (start, end, lines)
        assert 1 <= len(lines) <= 2 and all(0 < len(l) <= 42 for l in lines), lines


def test_cue_text_is_exactly_the_voiceover():
    cues = _cues()
    text = _norm(" ".join(" ".join(l) for _, _, l in cues))
    assert text == _norm(" ".join(v for _, _, v in _segments()))


def test_each_segment_voiceover_sits_inside_its_segment():
    cues = _cues()
    for a, b, vo in _segments():
        inside = [c for c in cues if c[0] >= a and c[1] <= min(b, EDITED_S)]
        assert _norm(" ".join(" ".join(l) for _, _, l in inside)) == vo, (a, b)
    assert sum(1 for c in cues if not any(c[0] >= a and c[1] <= b for a, b, _ in _segments())) == 0


def test_copy_matches_the_timing_table_length():
    rows = re.findall(r"^\| \d \| [^|]+ \| (\d:\d\d)–(\d:\d\d) \| (\d+) s \|$", VIDEO, re.M)
    assert _s(rows[-1][1]) - 1 < EDITED_S <= _s(rows[-1][1])
