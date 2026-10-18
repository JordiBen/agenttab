"""Video script (reviewer, 2026-10-19): filmed locally on the live site's sha with sandbox PayPal, vendor payouts on and
real Claude; the end card says "Your agent gets a budget, not your card."; the voiceover names the model only as Claude
and never quotes milliseconds (the backup tables keep eval timings)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIDEO = (ROOT / "docs" / "video-script.md").read_text(encoding="utf-8")
SLOGAN = "Your agent gets a budget, not your card."


def _voiceover(text: str) -> str:
    return "\n".join(re.findall(r"^> (.*)$", text, re.M))


def test_end_card_slogan_is_exact():
    card = VIDEO[VIDEO.index("## 5. End card"):VIDEO.index("### Open items")]
    on_screen = next(l for l in card.splitlines() if l.startswith("**On screen:**"))
    assert f'"AgentTab. {SLOGAN}"' in on_screen
    assert _voiceover(card) == f'"AgentTab. {SLOGAN}"'
    for p in [ROOT / "README.md", *ROOT.glob("docs/*.md"), *ROOT.glob("static/*.html"), *ROOT.glob("static/*.js")]:
        assert "a tab, not your card" not in p.read_text(encoding="utf-8").lower(), p.name


def test_voiceover_has_no_milliseconds_and_no_old_model_names():
    vo = _voiceover(VIDEO)
    assert vo.count("\n") > 10
    assert not re.search(r"\d\s*ms\b|\bms\b|millisecond", vo, re.I)
    assert not re.search(r"haiku|sonnet|opus|gemini|gpt|claude-\w|anthropic", vo, re.I)
    assert "Claude" in vo


def test_no_gemini_in_the_video_script():
    assert "GEMINI_" not in VIDEO


def test_recording_rules_name_the_recorded_build():
    rules = VIDEO[VIDEO.index("## Recording rules"):VIDEO.index("## Timeline")]
    assert "same sha as the live site" in rules
    for s in ("`PAYPAL_MODE=sandbox`", "`VENDOR_PAYOUTS=1`", "real Claude"):
        assert s in rules, s
    assert "use `PAYPAL_MODE=mock`" not in rules
