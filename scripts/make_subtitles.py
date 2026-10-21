"""Build docs/video-subtitles.srt from the voiceover in docs/video-script.md and its timing table (the edited 2:51 cut).

Each segment's voiceover is split into cues of at most 2 lines x 42 characters (sentence, then clause, then word
boundaries). Cue times are spread over the segment in proportion to their length, with a short gap between cues, and
clamped to 1-7 s. Usage: python -m scripts.make_subtitles [--print-cps]"""
from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "docs" / "video-script.md"
OUT = ROOT / "docs" / "video-subtitles.srt"
EDITED_S = 170.96          # agenttab_takeB2_68a7cf6_edit.mp4 as encoded
WIDTH, LEAD, GAP, TAIL = 42, 0.3, 0.12, 0.3


def _s(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def segments(text: str):
    out = []
    for sec in re.split(r"\n(?=#{2,3} \d)", text):
        m = re.match(r"#{2,3} [^\n]*\((\d:\d\d)–(\d:\d\d)\)", sec)
        vo = re.findall(r"^> (.*)$", sec, re.M)
        if m and vo:
            out.append((_s(m.group(1)), min(_s(m.group(2)), EDITED_S),
                        re.sub(r"\s+", " ", " ".join(vo)).strip().strip('"')))
    return out


def _fits(t: str) -> bool:
    return len(textwrap.wrap(t, WIDTH, break_on_hyphens=False)) <= 2


def _split(t: str) -> list[str]:
    if _fits(t):
        return [t]
    mid = len(t) / 2
    for pat in (r"(?<=[.!?])\s", r"(?<=[,;:])\s",               # sentence, clause, a conjunction, then any word boundary
                r"\s(?=(?:and|but|so|then|with|before|or|from|as)\b)", r"\s"):
        cuts = [m.start() for m in re.finditer(pat, t) if _fits(t[:m.start()].strip()) or _fits(t[m.start():].strip())]
        if cuts:
            c = min(cuts, key=lambda i: abs(i - mid))
            return _split(t[:c].strip()) + _split(t[c:].strip())
    return [t]


def chunks(vo: str) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", vo):
        out += _split(sent)
    return out


def cues():
    res = []
    for a, b, vo in segments(VIDEO.read_text(encoding="utf-8")):
        parts = chunks(vo)
        start, end = a + LEAD, b - TAIL
        avail = end - start - GAP * (len(parts) - 1)
        total = sum(len(p) for p in parts)
        t = start
        for i, p in enumerate(parts):
            d = max(1.0, min(7.0, avail * len(p) / total))
            e = min(t + d, end) if i < len(parts) - 1 else min(max(t + d, t + 1.0), end)
            res.append((round(t, 3), round(e, 3), textwrap.wrap(p, WIDTH, break_on_hyphens=False)))
            t = e + GAP
    return res


def _ts(x: float) -> str:
    ms = int(round(x * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def render() -> str:
    return "".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n" + "\n".join(lines) + "\n\n" for i, (a, b, lines) in enumerate(cues(), 1)).rstrip("\n") + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    if "--print-cps" in sys.argv:
        for a, b, lines in cues():
            n = len(" ".join(lines))
            print(f"{a:7.2f}-{b:7.2f} {b - a:4.1f}s {n / (b - a):5.1f} cps | {' / '.join(lines)}")
