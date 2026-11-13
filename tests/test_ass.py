"""ASS script generation for burned-in captions (pure text, no FFmpeg)."""

from __future__ import annotations

import re

from clipforge.core.captions import CaptionLine, CaptionStyle, Word, style_for_preset
from clipforge.media import ass


def dialogues(script: str) -> list[dict]:
    out = []
    for ln in script.splitlines():
        if ln.startswith("Dialogue:"):
            f = ln[len("Dialogue:"):].strip().split(",", 9)
            out.append({"start": f[1], "end": f[2], "style": f[3], "text": f[9]})
    return out


def style_fields(script: str) -> dict:
    fmt = next(ln for ln in script.splitlines() if ln.startswith("Format:") and "Fontname" in ln)
    names = [x.strip() for x in fmt[len("Format:"):].split(",")]
    row = next(ln for ln in script.splitlines() if ln.startswith("Style:"))
    return dict(zip(names, [x.strip() for x in row[len("Style:"):].split(",")]))


def test_colour_conversion():
    assert ass.ass_colour("#FF8000") == "&H000080FF"
    assert ass.ass_colour("#ffd400") == "&H0000D4FF"
    assert ass.ass_colour("#000", alpha=128) == "&H80000000"
    assert ass.ass_tag_colour("#FFD400") == "&H00D4FF&"
    assert ass.ass_colour("garbage") == "&H00FFFFFF"            # falls back to white


def test_time_format():
    assert ass.format_time(0) == "0:00:00.00"
    assert ass.format_time(65.456) == "0:01:05.46"
    assert ass.format_time(3600 + 61.5) == "1:01:01.50"
    assert ass.format_time(-3) == "0:00:00.00"


def test_header_and_style():
    st = CaptionStyle(font="Segoe UI Black", size_pct=5.0, outline_pct=0.5, primary="#112233",
                      outline="#445566", position="bottom", margin_pct=10)
    s = ass.build_ass([CaptionLine(0, 1, "hi")], st, 1080, 1920)
    assert "PlayResX: 1080" in s and "PlayResY: 1920" in s
    f = style_fields(s)
    assert f["Fontname"] == "Segoe UI Black"
    assert float(f["Fontsize"]) == 96.0
    assert f["PrimaryColour"] == "&H00332211" and f["OutlineColour"] == "&H00665544"
    assert f["BorderStyle"] == "1" and float(f["Outline"]) == 9.6
    assert f["Alignment"] == "2" and f["MarginV"] == "192"


def test_alignment_per_position():
    for pos, al in (("bottom", "2"), ("middle", "5"), ("top", "8")):
        f = style_fields(ass.build_ass([], CaptionStyle(position=pos, margin_pct=12), 1000, 1000))
        assert f["Alignment"] == al
        assert f["MarginV"] == ("0" if pos == "middle" else "120")


def test_boxed_style():
    f = style_fields(ass.build_ass([], style_for_preset("boxed"), 1080, 1920))
    assert f["BorderStyle"] == "3"
    assert f["OutlineColour"].startswith("&H") and f["OutlineColour"] != "&H00000000"
    assert float(f["Outline"]) > 0                                  # box padding


def test_escaping():
    t = ass.escape_text("a {b} c\\Nd\nline2")
    assert "\\{" in t and "\\}" in t
    assert "\n" not in t and "\\N" not in t.replace("\\{", "").replace("\\}", "")
    assert "\\" not in ass.escape_text("x\\y").replace("\\{", "")
    s = ass.build_ass([CaptionLine(0, 1, "x {\\an8} y\nz")], CaptionStyle(uppercase=False),
                      1080, 1920)
    body = dialogues(s)[0]["text"]
    assert "{\\an8}" not in body and "\\{" in body
    assert len(dialogues(s)) == 1 and "\r" not in body


def test_uppercase():
    on = dialogues(ass.build_ass([CaptionLine(0, 1, "hello World")], CaptionStyle(uppercase=True),
                                 1080, 1920))[0]["text"]
    off = dialogues(ass.build_ass([CaptionLine(0, 1, "hello World")], CaptionStyle(uppercase=False),
                                  1080, 1920))[0]["text"]
    assert "HELLO WORLD" in on and "hello World" in off


def test_wrap_max_two_rows():
    long = "this is a rather long caption line that cannot fit on one row"
    body = dialogues(ass.build_ass([CaptionLine(0, 1, long)], style_for_preset("classic"),
                                   400, 700))[0]["text"]
    assert body.count("\\N") == 1
    short = dialogues(ass.build_ass([CaptionLine(0, 1, "ok")], style_for_preset("classic"),
                                    1080, 1920))[0]["text"]
    assert "\\N" not in short


def _cs(t: str) -> int:
    h, m, rest = t.split(":")
    s, c = rest.split(".")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 100 + int(c)


def test_word_events_are_continuous():
    words = [Word(0.5, 0.9, "one"), Word(1.0, 1.4, "two"), Word(1.4, 2.2, "three")]
    st = CaptionStyle(highlight_words=True, highlight="#00FF00", uppercase=False, preset="classic")
    ev = dialogues(ass.build_ass([CaptionLine(0.4, 2.5, "one two three", words)], st, 1080, 1920))
    assert len(ev) == 3
    assert ev[0]["start"] == ass.format_time(0.4) and ev[-1]["end"] == ass.format_time(2.5)
    for a, b in zip(ev, ev[1:]):
        assert a["end"] == b["start"]                                # no gap, no overlap
    assert ev[1]["start"] == ass.format_time(1.0)
    for i, e in enumerate(ev):
        assert e["text"].count("{\\c&H00FF00&}") == 1
        assert re.sub(r"\{[^}]*\}", "", e["text"]).replace("\\N", " ") == "one two three"
    assert ev[1]["text"].startswith("one {\\c&H00FF00&}two")


def test_pop_animation_only_on_first_event():
    words = [Word(0, 0.5, "a"), Word(0.5, 1, "b")]
    ev = dialogues(ass.build_ass([CaptionLine(0, 1, "a b", words)], style_for_preset("pop"),
                                 1080, 1920))
    assert "\\t(0,120" in ev[0]["text"] and "\\t(" not in ev[1]["text"]


def test_no_words_single_event_and_highlight_off():
    st = CaptionStyle(highlight_words=True)
    assert len(dialogues(ass.build_ass([CaptionLine(0, 1, "plain text")], st, 1080, 1920))) == 1
    st2 = CaptionStyle(highlight_words=False)
    ln = CaptionLine(0, 2, "a b", [Word(0, 1, "a"), Word(1, 2, "b")])
    ev = dialogues(ass.build_ass([ln], st2, 1080, 1920))
    assert len(ev) == 1 and "\\c&H" not in ev[0]["text"]


def test_degenerate_lines_skipped():
    assert dialogues(ass.build_ass([CaptionLine(1, 1, "x"), CaptionLine(0, 1, "  ")],
                                   CaptionStyle(), 1080, 1920)) == []
