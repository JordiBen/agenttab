"""Reviewer t84u (2026-10-19): docs/devpost.md is the Devpost draft. It may only say what code, tests and the result
files prove:
- every sentence carries a source comment `<!-- source: ref | ref -->`;
- every referenced file (and the function or test it names) exists;
- every number comes from eval/results.json or eval/heldout-results.json, apart from one config value that is checked
  against the code, and every "X of Y" pair is a real pair from those files;
- one explicit DEMAND-DATA placeholder in Inspiration is allowed;
- the prose says "Claude" (no model ids), never the builder's real name, and uses the WebIntel Tools brand."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "docs" / "devpost.md"
RESULTS = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
HELDOUT = json.loads((ROOT / "eval" / "heldout-results.json").read_text(encoding="utf-8"))
SECTIONS = ["Inspiration", "What it does", "How we built it", "Challenges we ran into",
            "Accomplishments that we're proud of", "What we learned", "What's next", "Built with"]
PLACEHOLDER = "<!-- DEMAND-DATA: pending from reviewer -->"
SOURCE = re.compile(r"<!--\s*source:(.*?)-->", re.S)
# numbers that are not results: each one is checked against the code that defines it
CONFIG = {"60": ("app/procurement.py", r'SHOP_APPROVAL_THRESHOLD = Decimal\("60\.00"\)')}
VENDOR_STORY = "AgentTab never touches the money; the vendor is paid by PayPal only after delivery."
TAB_SENTENCE = "Agents created in the owner console use a prepaid tab with authorize-then-capture holds."


def _doc() -> str:
    assert DOC_PATH.exists(), "docs/devpost.md is missing"
    return DOC_PATH.read_text(encoding="utf-8")


def _prose(text: str) -> str:
    return re.sub(r"<!--.*?-->", " ", text, flags=re.S)


def _sections(doc: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", doc, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def _blocks(doc: str):
    for name, body in _sections(doc).items():
        cur: list[str] = []
        for line in body.splitlines() + [""]:
            if not line.strip() or line.lstrip().startswith("- "):
                if cur:
                    yield name, "\n".join(cur)
                cur = [line] if line.strip() else []
            else:
                cur.append(line)


def _summaries():
    for mode, d in RESULTS["modes"].items():
        yield ("results", mode), d
    for mode, d in HELDOUT.items():
        yield ("heldout", mode), d["summary"]


def _allowed_numbers() -> set[str]:
    vals: set[str] = set()

    def add(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            vals.add(str(v))
            if isinstance(v, float) and v.is_integer():
                vals.add(str(int(v)))
        elif isinstance(v, dict):
            for x in v.values():
                add(x)

    for _, s in _summaries():
        add(s)
    add(RESULTS["by_category"])
    for k in ("cases", "attacks", "legit"):
        add(RESULTS[k])
    s = HELDOUT["ai"]["summary"]
    vals.add(str(s["attacks_total"] + s["legit_total"]))            # held-out set size (40)
    return vals


def _allowed_pairs() -> set[tuple[int, int]]:
    pairs = set()
    for _, s in _summaries():
        pairs.add((s["attacks_stopped"], s["attacks_total"]))
        pairs.add((s["legit_correct"], s["legit_total"]))
    for cat in RESULTS["by_category"].values():
        for m in ("none", "keyword", "ai"):
            pairs.add((cat[m], cat["total"]))
    return pairs


def test_sections_are_the_devpost_sections_in_order():
    assert list(_sections(_doc())) == SECTIONS


def test_inspiration_keeps_the_demand_data_placeholder():
    assert _doc().count(PLACEHOLDER) == 1
    assert PLACEHOLDER in _sections(_doc())["Inspiration"]


def test_every_sentence_has_a_source_comment():
    for name, block in _blocks(_doc()):
        if block.lstrip().startswith("#") or block.strip() == PLACEHOLDER:
            continue
        rest = block.replace(PLACEHOLDER, " ")
        segs = SOURCE.split(rest)[0::2]                  # text between source comments
        assert len(segs) > 1, (name, block[:120])
        assert not _prose(segs[-1]).strip(" \n-"), ("text after the last source", name, segs[-1][:120])
        for seg in segs[:-1]:
            text = _prose(seg).strip(" \n-")
            assert text, ("source comment with no sentence", name, block[:120])
            assert len(re.findall(r"[.!?](?=\s|$)", text)) <= 1, ("one source per sentence", name, text[:160])


def _check_ref(ref: str):
    ref = ref.strip()
    if "::" in ref:
        path, test = ref.split("::", 1)
        p = ROOT / path
        assert p.is_file(), ref
        assert re.search(rf"^def {re.escape(test)}\b", p.read_text(encoding="utf-8"), re.M), ref
    elif "#" in ref:
        path, keys = ref.split("#", 1)
        d = json.loads((ROOT / path).read_text(encoding="utf-8"))
        for k in keys.split("."):
            assert isinstance(d, dict) and k in d, ref
            d = d[k]
    elif re.match(r"^[\w./-]+:\w+$", ref):
        path, sym = ref.split(":", 1)
        p = ROOT / path
        assert p.is_file(), ref
        assert re.search(rf"\bdef {re.escape(sym)}\b|^{re.escape(sym)}\s*=|\b{re.escape(sym)}\b\s*=", p.read_text(encoding="utf-8"), re.M), ref
    else:
        assert (ROOT / ref).is_file(), ref


def test_every_source_reference_exists():
    refs = [r for m in SOURCE.finditer(_doc()) for r in m.group(1).split("|")]
    assert len(refs) >= 20
    for r in refs:
        _check_ref(r)


def test_every_number_comes_from_the_result_files():
    prose = _prose(_doc())
    allowed = _allowed_numbers()
    nums = re.findall(r"(?<![A-Za-z_\d.])\d+(?:\.\d+)?(?![A-Za-z_\d])", prose)
    assert nums, "the draft should report measured numbers"
    for n in nums:
        if n in CONFIG:
            path, pat = CONFIG[n]
            assert re.search(pat, (ROOT / path).read_text(encoding="utf-8")), n
        else:
            assert n in allowed, f"{n} is not in eval/results.json or eval/heldout-results.json"


def test_every_x_of_y_pair_is_a_real_pair():
    pairs = _allowed_pairs()
    found = [(int(a), int(b)) for a, b in re.findall(r"\b(\d+) of (\d+)\b", _prose(_doc()))]
    assert found
    for p in found:
        assert p in pairs, p


def test_headline_numbers_match_the_files():
    prose = re.sub(r"\s+", " ", _prose(_doc()))
    ai, kw, h_ai, h_kw = (RESULTS["modes"]["ai"], RESULTS["modes"]["keyword"], HELDOUT["ai"]["summary"], HELDOUT["keyword"]["summary"])
    for s in (f"{ai['attacks_stopped']} of {ai['attacks_total']} attacks", f"{ai['legit_correct']} of {ai['legit_total']} legitimate",
              f"{kw['attacks_stopped']} of {kw['attacks_total']}", f"{h_ai['attacks_stopped']} of {h_ai['attacks_total']} attacks",
              f"{h_ai['legit_correct']} of {h_ai['legit_total']} legitimate", f"{h_kw['attacks_stopped']} of {h_kw['attacks_total']}"):
        assert s in prose, s


def test_required_wording():
    prose = re.sub(r"\s+", " ", _prose(_doc()))
    assert VENDOR_STORY in prose and TAB_SENTENCE in prose
    assert "PayPal sandbox" in prose and "Claude" in prose and "WebIntel Tools" in prose


def test_no_model_ids_and_no_personal_names_in_prose():
    prose = _prose(_doc())
    assert not re.search(r"haiku|sonnet|opus|gemini|gpt|claude-\w", prose, re.I)   # "Claude" only; Built with may name the SDK
    assert not re.search(r"yarden|shushan|jordiben", prose, re.I)
