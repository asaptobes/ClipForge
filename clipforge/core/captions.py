"""Caption data: timed lines made of timed words, plus a style, stored in the project.

Captions live in *timeline time* (seconds on the edited timeline), not source time, and are
generated from the timeline's audio. Editing the timeline afterwards makes them stale (see
:func:`timeline_stamp`); the UI then offers to regenerate. Nothing here touches media files.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Optional

# Style presets shown in the UI. Each maps to a CaptionStyle (see style_for_preset).
PRESETS = ("pop", "classic", "boxed", "minimal")
POSITIONS = ("top", "middle", "bottom")


@dataclass
class Word:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]))


@dataclass
class CaptionLine:
    """One on-screen caption: a short phrase shown from ``start`` to ``end``."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)   # empty => no word-by-word highlight

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text,
                "words": [w.to_dict() for w in self.words]}

    @classmethod
    def from_dict(cls, d: dict) -> "CaptionLine":
        return cls(float(d["start"]), float(d["end"]), str(d.get("text", "")),
                   [Word.from_dict(w) for w in d.get("words", [])])

    def copy(self) -> "CaptionLine":
        return CaptionLine(self.start, self.end, self.text, [replace(w) for w in self.words])


@dataclass
class CaptionStyle:
    preset: str = "pop"
    font: str = "Segoe UI Black"
    size_pct: float = 5.2            # font size as a percentage of the canvas height
    position: str = "bottom"         # "top" | "middle" | "bottom"
    margin_pct: float = 16.0         # distance from that edge, % of canvas height
    primary: str = "#FFFFFF"
    highlight: str = "#FFD400"       # colour of the word being spoken (pop / boxed presets)
    outline: str = "#000000"
    outline_pct: float = 0.45        # outline width, % of canvas height (0 = none)
    box: bool = False                # draw a rounded box behind the text
    uppercase: bool = True
    words_per_line: int = 3          # when regenerating: max words shown at once
    highlight_words: bool = True     # colour the active word

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "CaptionStyle":
        known = set(cls.__dataclass_fields__)
        style = cls(**{k: v for k, v in (d or {}).items() if k in known})
        if style.position not in POSITIONS:
            style.position = "bottom"
        return style


def style_for_preset(name: str) -> CaptionStyle:
    """Ready-made looks. Unknown names give the default ("pop")."""
    if name == "classic":
        return CaptionStyle(preset="classic", font="Segoe UI Semibold", size_pct=4.2,
                            uppercase=False, words_per_line=7, highlight_words=False)
    if name == "boxed":
        return CaptionStyle(preset="boxed", size_pct=4.6, box=True, outline_pct=0.0,
                            highlight="#FFFFFF", highlight_words=False, words_per_line=5)
    if name == "minimal":
        return CaptionStyle(preset="minimal", font="Segoe UI", size_pct=3.8, uppercase=False,
                            outline_pct=0.25, highlight_words=False, words_per_line=8,
                            position="bottom", margin_pct=9.0)
    return CaptionStyle(preset="pop")


def timeline_stamp(segments: Iterable) -> str:
    """A short fingerprint of what the captions were generated from: every clip's source,
    source range, position and mute state on both lanes (not crop or selection)."""
    h = hashlib.sha1()
    for s in segments:
        h.update(f"{s.track}|{s.source_id}|{s.source_in:.4f}|{s.source_out:.4f}|"
                 f"{s.timeline_start:.4f}|{int(s.muted)};".encode())
    return h.hexdigest()[:16]


def lines_to_dicts(lines: Iterable[CaptionLine]) -> list[dict]:
    return [ln.to_dict() for ln in lines]


def lines_from_dicts(items: Optional[Iterable[dict]]) -> list[CaptionLine]:
    out = []
    for d in items or []:
        try:
            ln = CaptionLine.from_dict(d)
        except (KeyError, TypeError, ValueError):
            continue
        if ln.end > ln.start:
            out.append(ln)
    out.sort(key=lambda ln: ln.start)
    return out


def chunk_words(words: list[Word], max_words: int, max_gap: float = 0.8,
                max_chars: int = 28) -> list[CaptionLine]:
    """Group a flat list of timed words into caption lines of at most ``max_words`` words,
    breaking early at long pauses, after sentence-ending punctuation or at ``max_chars``."""
    lines: list[CaptionLine] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            lines.append(CaptionLine(cur[0].start, cur[-1].end,
                                     " ".join(w.text.strip() for w in cur), list(cur)))
            cur.clear()

    for w in words:
        if not w.text.strip():
            continue
        if cur and (w.start - cur[-1].end > max_gap or len(cur) >= max(1, max_words)
                    or sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars):
            flush()
        cur.append(w)
        if w.text.rstrip().endswith((".", "?", "!")):
            flush()
    flush()
    return lines
