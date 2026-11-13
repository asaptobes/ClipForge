from clipforge.core.captions import (CaptionLine, CaptionStyle, Word, chunk_words, lines_from_dicts,
                                     lines_to_dicts, style_for_preset, timeline_stamp)
from clipforge.core.model import MediaSource
from clipforge.core.project import Project
from clipforge.document import Document


def _words(text, step=0.4):
    return [Word(i * step, i * step + 0.3, w) for i, w in enumerate(text.split())]


def test_chunk_words_respects_limits_and_punctuation():
    lines = chunk_words(_words("one two three four five six seven"), max_words=3)
    assert [ln.text for ln in lines] == ["one two three", "four five six", "seven"]
    lines = chunk_words(_words("hello there. how are you"), max_words=5)
    assert [ln.text for ln in lines] == ["hello there.", "how are you"]
    gap = [Word(0, 0.3, "a"), Word(5, 5.3, "b")]
    assert len(chunk_words(gap, max_words=5)) == 2


def test_lines_round_trip_and_bad_data_dropped():
    lines = chunk_words(_words("a b c"), 3)
    back = lines_from_dicts(lines_to_dicts(lines) + [{"start": "x"}, {"start": 3, "end": 2, "text": "z"}])
    assert [ln.text for ln in back] == ["a b c"] and back[0].words[1].text == "b"


def test_style_presets_and_tolerant_loading():
    assert style_for_preset("boxed").box is True
    assert style_for_preset("nope").preset == "pop"
    s = CaptionStyle.from_dict({"preset": "x", "position": "sideways", "future_field": 1})
    assert s.position == "bottom"


def test_document_captions_stale_when_timeline_changes(tmp_path):
    doc = Document()
    src = MediaSource(id="s", path="x.mp4", duration=10.0, fps=25.0)
    doc.add_source(src)
    doc.set_captions(chunk_words(_words("hello world"), 3))
    assert not doc.captions_stale()
    doc.seek(4.0)
    doc.split_at_playhead()
    assert doc.captions_stale()
    doc.set_captions(doc.captions)
    assert not doc.captions_stale()
    assert timeline_stamp(doc.timeline.all_segments())


def test_project_saves_captions_canvas_and_crop(tmp_path):
    doc = Document()
    src = MediaSource(id="s", path=str(tmp_path / "x.mp4"), duration=6.0, fps=25.0)
    (tmp_path / "x.mp4").write_bytes(b"x")
    doc.add_source(src)
    doc.set_canvas("9:16", "blur")
    doc.set_crop([doc.timeline.segments[0].id], 0.3, 0.7)
    doc.set_captions(chunk_words(_words("save me please"), 3), style_for_preset("boxed"))
    path = str(tmp_path / "p.clipforge")
    doc.save(path)
    doc2 = Document()
    doc2.load(path)
    assert (doc2.aspect, doc2.reframe_mode) == ("9:16", "blur")
    seg = doc2.timeline.segments[0]
    assert (seg.crop_x, seg.crop_x_end) == (0.3, 0.7)
    assert [ln.text for ln in doc2.captions] == ["save me please"]
    assert doc2.caption_style.preset == "boxed" and not doc2.captions_stale()


def test_document_set_crop_is_one_undo_step():
    doc = Document()
    doc.add_source(MediaSource(id="s", path="x.mp4", duration=6.0))
    seg_id = doc.timeline.segments[0].id
    assert doc.set_crop([seg_id], 0.1)
    assert doc.timeline.segments[0].crop_x == 0.1
    doc.undo()
    assert doc.timeline.segments[0].crop_x == 0.5
