"""Unit tests for emote_danmaku — stdlib only (no pytest, no network, no ffmpeg).

Run with::

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emote_danmaku.ass import (  # noqa: E402
    AssDocument,
    RenderOptions,
    TextMetrics,
    transform,
)
from emote_danmaku.library import EmoteLibrary, LibraryError, normalize_key  # noqa: E402
from emote_danmaku.vectorize import TraceError, trace_image  # noqa: E402
from emote_danmaku.xmlfile import harvest_emotes, parse_xml  # noqa: E402

METRICS = TextMetrics()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_emote(path: Path, size=(64, 32), colors=((240, 200, 60), (200, 40, 40))):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, h = size
    d.ellipse((1, 1, w - 2, h - 2), fill=colors[0] + (255,))
    d.rectangle((w // 3, h // 3, 2 * w // 3, 2 * h // 3), fill=colors[1] + (255,))
    im.save(path)
    return path


def make_library(root: Path):
    lib = EmoteLibrary(root / "emotes")
    # distinct artwork per name, otherwise identical bytes are deduplicated
    lib.add_file(make_emote(root / "a.png", size=(64, 32),
                            colors=((240, 200, 60), (200, 40, 40))), name="妙啊")
    lib.add_file(make_emote(root / "b.png", size=(48, 48),
                            colors=((60, 200, 240), (40, 40, 200))), name="热")
    return lib


def ass_with(events, styles=None):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
            "WrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
    for name, fs, align in (styles or [("R2L", 38, 8), ("TOP", 38, 8)]):
        head += (f"Style: {name},Microsoft YaHei,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,"
                 f"&H00000000,0,0,0,0,100,100,0,0,1,1,0,{align},0,0,0,1\n")
    head += ("\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text\n")
    return head + "\n".join(events) + "\n"


RAW = json.dumps({
    "cmd": "DANMU_MSG",
    "info": [
        [0, 1, 25, 16777215, 1690000000000, 0, 0, "0", 0, 0, 0, "", 0, "{}", "{}",
         {"extra": json.dumps({
             "content": "好啊[妙啊]",
             "emots": {"[妙啊]": {
                 "emoji": "[妙啊]", "emoticon_unique": "official_109",
                 "url": "http://i0.hdslb.com/bfs/live/abc.png",
                 "width": 138, "height": 60}},
         })},
         0],
        "好啊[妙啊]",
        [10001, "观众", 0, 0, 0, 10000, 1, ""],
    ],
})


def write_xml(path: Path, raw: bool = True):
    attr = f' raw="{RAW.replace(chr(34), "&quot;")}"' if raw else ""
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<i>\n'
        '  <chatserver>chat.bilibili.com</chatserver>\n'
        f'  <d p="1.500,1,25,16777215,1690000000000,0,10001,0" user="观众"{attr}>'
        '好啊[妙啊]</d>\n'
        '  <d p="2.500,4,25,16777215,1690000001000,0,10002,0" user="路人">底部[热]</d>\n'
        '</i>\n', "utf-8")
    return path


class TempDirTest(unittest.TestCase):
    def setUp(self):
        # Plain mkdir with an ordinary mode: tempfile.mkdtemp creates 0700 dirs
        # that some sandboxes refuse to write into.
        base = ROOT / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.tmp = base / f"t{uuid.uuid4().hex[:12]}"
        self.tmp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------

class TestLibrary(TempDirTest):
    def test_normalize_key(self):
        self.assertEqual(normalize_key("[妙啊]"), normalize_key("妙啊"))
        self.assertEqual(normalize_key("[AWSL]"), "awsl")

    def test_add_and_resolve(self):
        lib = make_library(self.tmp)
        self.assertEqual(len(lib), 2)
        self.assertEqual(lib.find("[妙啊]")["name"], "妙啊")
        self.assertIsNotNone(lib.find("妙啊"))
        self.assertIsNone(lib.find("[不存在]"))

    def test_index_survives_reload(self):
        lib = make_library(self.tmp)
        again = EmoteLibrary(lib.root)
        self.assertEqual(len(again), 2)
        self.assertIsNotNone(again.find("[热]"))

    def test_identical_file_is_deduped(self):
        lib = EmoteLibrary(self.tmp / "emotes")
        p = make_emote(self.tmp / "a.png")
        first = lib.add_file(p, name="妙啊")
        second = lib.add_file(p, name="妙啊2")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(lib), 1)

    def test_unknown_suffix_rejected(self):
        lib = EmoteLibrary(self.tmp / "emotes")
        bad = self.tmp / "x.txt"
        bad.write_text("hi", "utf-8")
        with self.assertRaises(LibraryError):
            lib.add_file(bad)

    def test_vector_cache_is_reused(self):
        lib = make_library(self.tmp)
        entry = lib.find("[妙啊]")
        a = lib.vector(entry, height=32)
        b = lib.vector(entry, height=32)
        self.assertIs(a, b)

    def test_images_dropped_in_root_are_indexed(self):
        """Users copy ``[示例主播_偷笑].png`` straight into emotes/; it must resolve."""
        lib = EmoteLibrary(self.tmp / "emotes")
        lib.add_file(make_emote(self.tmp / "seed.png"), name="seed")
        dropped = self.tmp / "emotes" / "[示例主播_偷笑].png"
        make_emote(self.tmp / "x.png", size=(40, 40)).replace(dropped)
        again = EmoteLibrary(self.tmp / "emotes")
        self.assertIsNotNone(again.find("[示例主播_偷笑]"))
        self.assertIsNotNone(again.find("示例主播_偷笑"))
        self.assertEqual(len(again), 2)

    def test_root_image_without_brackets_is_indexed(self):
        lib = EmoteLibrary(self.tmp / "emotes")
        make_emote(self.tmp / "y.png", size=(36, 36)).replace(
            self.tmp / "emotes" / "dog.png")
        again = EmoteLibrary(self.tmp / "emotes")
        self.assertIsNotNone(again.find("[dog]"))


# --------------------------------------------------------------------------
# vectorizer
# --------------------------------------------------------------------------

class TestVectorizer(TempDirTest):
    def test_trace_produces_colour_layers(self):
        p = make_emote(self.tmp / "e.png", size=(40, 40))
        vec = trace_image(p, height=40)
        self.assertEqual((vec.width, vec.height), (40, 40))
        self.assertGreaterEqual(len(vec.shapes), 2)
        self.assertTrue(all(s.loops for s in vec.shapes))

    def test_blank_image_rejected(self):
        p = self.tmp / "blank.png"
        Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(p)
        with self.assertRaises(TraceError):
            trace_image(p, height=16)

    def test_drawing_paths_are_single_line_and_absolute(self):
        p = make_emote(self.tmp / "e.png", size=(48, 32))
        vec = trace_image(p, height=32)
        for _, path in vec.layer_drawings():
            self.assertNotIn("\n", path)
            self.assertNotIn("\r", path)
            self.assertTrue(path.startswith("m "))
            nums = [float(t) for t in path.replace("m", " ").replace("l", " ").split()]
            self.assertGreaterEqual(min(nums), 0.0)

    def test_each_run_opens_its_own_drawing_mode(self):
        # Regression: sharing one \p1 across colour blocks makes libass advance
        # the pen, smearing the emote to the right (see README).
        p = make_emote(self.tmp / "e.png")
        vec = trace_image(p, height=32)
        runs = vec.layer_drawings()
        self.assertTrue(runs)
        for tags, path in runs:
            self.assertIn("\\p1", tags)
            self.assertNotIn("\\p0", path)

    def test_transparent_margin_is_trimmed(self):
        p = self.tmp / "padded.png"
        im = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle((20, 20, 39, 39), fill=(10, 200, 30, 255))
        im.save(p)
        # trace at the artwork's own size so no rescaling hides the trim
        vec = trace_image(p, height=20)
        self.assertEqual(vec.width, 20)
        self.assertEqual(vec.height, 20)
        xs = [x for s in vec.shapes for loop in s.loops for x, _ in loop]
        self.assertGreaterEqual(min(xs), 0.0)
        self.assertLessEqual(max(xs), 21.0)


# --------------------------------------------------------------------------
# ASS transform
# --------------------------------------------------------------------------

class TestTransform(TempDirTest):
    def _run(self, events, lib, styles=None, opts=None):
        doc = AssDocument.parse(ass_with(events, styles))
        doc, stats = transform(doc, lib, METRICS, opts or RenderOptions())
        return doc, stats

    def test_text_line_reserves_width_with_transparent_run(self):
        """The text keeps a transparent drawing so libass advances the pen by it."""
        lib = make_library(self.tmp)
        doc, stats = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,0,-100,0)}[妙啊]"], lib)
        self.assertEqual(stats.emotes, 1)
        line = next(e for e in doc.events if e.style != "EMOTE")
        self.assertNotIn("[妙啊]", line.text)
        self.assertIn("\\p1", line.text)
        self.assertIn("\\1a&HFF&", line.text, "占位必须是透明的")
        reserved = float(re.search(r"m 0 0 l ([\d.]+) 0", line.text).group(1))
        self.assertTrue(10 < reserved < 200, reserved)

    def test_colour_layers_share_the_text_trajectory(self):
        """Every colour layer uses the same \move as the text, so it cannot drift."""
        lib = make_library(self.tmp)
        doc, _ = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,0,-100,0)}[妙啊]"], lib)
        emote_events = [e for e in doc.events if e.style == "EMOTE"]
        self.assertTrue(emote_events)
        text_line = next(e for e in doc.events if e.style != "EMOTE")
        text_move = re.search(r"\\move\([^)]*\)", text_line.text).group(0)
        # same vertical band and same travel distance as the text
        for ev in emote_events:
            m = re.search(r"\\move\(([-\d.]+),([-\d.]+),([-\d.]+),", ev.text)
            self.assertIsNotNone(m, ev.text[:60])
            travel = float(m.group(1)) - float(m.group(3))
            ref = re.search(r"\\move\(([-\d.]+),([-\d.]+),([-\d.]+),", text_move)
            self.assertAlmostEqual(travel, float(ref.group(1)) - float(ref.group(3)), delta=1)

    def test_text_around_emote_is_preserved_in_order(self):
        lib = make_library(self.tmp)
        doc, _ = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,100,-100,100)}前面[妙啊]后面"], lib)
        line = next(e for e in doc.events if e.style != "EMOTE").text
        self.assertNotIn("[妙啊]", line)
        self.assertIn("前面", line)
        self.assertIn("后面", line)
        self.assertLess(line.index("前面"), line.index("\\p1"))
        self.assertLess(line.index("\\p1"), line.index("后面"))

    def test_two_emotes_both_drawn(self):
        lib = make_library(self.tmp)
        doc, stats = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,0,-100,0)}[妙啊][热]"], lib)
        self.assertEqual(stats.emotes, 2)
        line = next(e for e in doc.events if e.style != "EMOTE").text
        self.assertEqual(line.count("\\p1"), 2)

    def test_skip_flag_honoured(self):
        lib = make_library(self.tmp)
        doc, stats = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,0,-100,0)\--emote-skip}[妙啊]"], lib)
        self.assertEqual(stats.emotes, 0)
        self.assertIn("[妙啊]", doc.events[0].text)

    def test_comment_without_anchor_is_untouched(self):
        lib = make_library(self.tmp)
        doc, stats = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,没有定位标签[妙啊]"], lib)
        self.assertEqual(stats.emotes, 0)
        self.assertIn("[妙啊]", doc.events[0].text)

    def test_document_roundtrip(self):
        lib = make_library(self.tmp)
        doc, _ = self._run(
            [r"Dialogue: 0,0:00:01.00,0:00:09.00,R2L,,0,0,0,,"
             r"{\move(2000,0,-100,0)}[妙啊]"], lib)
        again = AssDocument.parse(doc.to_text())
        self.assertEqual(len(again.events), len(doc.events))
        self.assertEqual(again.play_res(), (1920, 1080))


# --------------------------------------------------------------------------
# XML harvest
# --------------------------------------------------------------------------

class TestXml(TempDirTest):
    def test_parse_reads_comments(self):
        items = parse_xml(write_xml(self.tmp / "d.xml"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].text, "好啊[妙啊]")
        self.assertEqual(items[0].tokens(), ["[妙啊]"])
        self.assertEqual(items[1].mode, 4)

    def test_harvest_extracts_metadata(self):
        res = harvest_emotes(write_xml(self.tmp / "d.xml"))
        self.assertEqual(res.total_comments, 2)
        self.assertEqual(res.raw_comments, 1)
        self.assertIn("[妙啊]", res.emotes)
        info = res.emotes["[妙啊]"]
        self.assertEqual(info.unique, "official_109")
        self.assertTrue(info.url.endswith("/abc.png"))
        self.assertEqual(res.tokens["[热]"], 1)

    def test_harvest_without_raw(self):
        res = harvest_emotes(write_xml(self.tmp / "d.xml", raw=False))
        self.assertEqual(res.raw_comments, 0)
        self.assertFalse(res.emotes)

    def test_harvest_recovers_truncated_file(self):
        p = write_xml(self.tmp / "d.xml")
        text = p.read_text("utf-8")
        # cut inside the *second* comment so exactly one <d> survives
        p.write_text(text[: text.rindex("底部")], "utf-8")
        res = harvest_emotes(p)
        self.assertEqual(res.total_comments, 1)
        self.assertIn("[妙啊]", res.emotes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
