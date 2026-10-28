"""Rewrite a DanmakuFactory ASS file so ``[表情]`` tokens render as pictures.

Why this shape of solution
--------------------------
``\\1img`` (picture tags) are not rendered by libass/ffmpeg, and a chained
``overlay`` filter graph costs minutes of encode time per video.  Instead every
emote becomes an inline ASS **vector drawing** (``\\p1``), which:

* needs no external files, so a single ``ffmpeg -vf ass=...`` encode is enough;
* is ordinary subtitle content, so trimming/remuxing keeps it intact;
* is resolution independent (drawn in script coordinates).

Layout preservation
-------------------
DanmakuFactory decides the row and the scroll speed from the width of the
original plain text ``[妙啊]``.  Replacing that text with a picture would change
the line width and make comments pop in mid-screen.  Two probed facts fix this:

* ``{\\p1}m 0 0 l W 0{\\p0}`` is degenerate (no ink, no vertical growth) yet
  still advances the pen by exactly ``W`` — so the *text* row keeps the width
  and the row position DanmakuFactory chose;
* a separate event with ``{\\an7\\pos(x,y)}`` places a drawing's top-left corner
  exactly at ``(x, y)`` (and ``\\move`` translates it exactly), so the visible
  emote can be placed absolutely over that reserved space.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from PIL import ImageFont

TOKEN_RE = re.compile(r"\[([^\[\]\n\r]{1,40})\]")
TAG_BLOCK_RE = re.compile(r"\{[^}]*\}")
POS_RE = re.compile(r"\\pos\(([-\d.]+)\s*,\s*([-\d.]+)\)")
MOVE_RE = re.compile(
    r"\\move\(([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)(?:\s*,[^)]*)?\)"
)
CLIP_RE = re.compile(r"\\clip\([^)]*\)")
FONT_RE = re.compile(r"\\fn([^\\}]*)")
FS_RE = re.compile(r"\\fs([-\d.]+)")
AN_RE = re.compile(r"\\an([1-9])")
LINEBREAK_RE = re.compile(r"\\[Nn]")

EMOTE_STYLE = "EMOTE"
DEFAULT_ESTIMATED_EM_WIDTH = 1.30  # em width of "[name]" when no font is usable


# --------------------------------------------------------------------------
# font metrics
# --------------------------------------------------------------------------

class TextMetrics:
    """Measure text the way libass lays it out (same face, same pixel size)."""

    _ALIASES = {
        "microsoft yahei": ("msyh.ttc", "msyhbd.ttc"),
        "微软雅黑": ("msyh.ttc",),
        "msyh": ("msyh.ttc",),
        "simhei": ("simhei.ttf",),
        "黑体": ("simhei.ttf",),
        "simsun": ("simsun.ttc",),
        "宋体": ("simsun.ttc",),
        "arial": ("arial.ttf", "arialbd.ttf"),
        "segoe ui": ("segoeui.ttf",),
        "sarasa ui sc": ("SarasaUiSC-Regular.ttf",),
        "sarasa gothic sc": ("SarasaGothicSC-Regular.ttf",),
        "noto sans cjk sc": ("NotoSansCJKsc-Regular.otf",),
    }

    def __init__(self, font_dir: str | Path | None = None):
        self.font_dir = Path(font_dir) if font_dir else Path("C:/Windows/Fonts")
        self._cache: dict[tuple[str, int], object] = {}

    def _candidates(self, name: str) -> list[Path]:
        key = (name or "").strip().casefold()
        out = [self.font_dir / n for n in self._ALIASES.get(key, ())]
        if name:
            out.append(self.font_dir / f"{name}.ttf")
            out.append(self.font_dir / f"{name}.ttc")
        out.append(self.font_dir / "msyh.ttc")
        out.append(self.font_dir / "arial.ttf")
        return out

    def font(self, name: str, size: int):
        key = ((name or "").casefold(), int(size))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        for path in self._candidates(name):
            if not path.exists():
                continue
            try:
                f = ImageFont.truetype(str(path), max(1, int(size)))
                self._cache[key] = f
                return f
            except Exception:
                continue
        f = ImageFont.load_default()
        self._cache[key] = f
        return f

    def width(self, text: str, name: str, size: int) -> float:
        if not text:
            return 0.0
        # Strip inline override blocks first: DanmakuFactory emits colour changes
        # like ``{\c&HFFFFFF}`` mid-line, and measuring them as literal characters
        # inflates the width by hundreds of pixels, which then misplaces every
        # emote anchor derived from it.
        text = TAG_BLOCK_RE.sub("", text)
        # ``\h`` is ASS's hard space (one space advance); treating it as the
        # literal characters "\" + "h" also overstates the width.
        text = text.replace("\\h", " ")
        if not text:
            return 0.0
        try:
            return float(self.font(name, size).getlength(text))
        except Exception:
            return float(len(text) * size * 0.5)

    def ascent(self, name: str, size: int) -> float:
        try:
            return float(self.font(name, size).getmetrics()[0])
        except Exception:
            return size * 0.78

# --------------------------------------------------------------------------
# ASS model
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Style:
    name: str
    fontname: str = "Microsoft YaHei"
    fontsize: int = 38
    alignment: int = 7
    outline: float = 0.0
    shadow: float = 0.0

    @classmethod
    def parse(cls, line: str) -> "Style | None":
        body = line.split(":", 1)[1] if ":" in line else ""
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 23:
            return None
        return cls(name=parts[0], fontname=parts[1] or "Microsoft YaHei",
                   fontsize=int(_num(parts[2], 38)) or 38,
                   alignment=int(_num(parts[18], 7)) or 7,
                   outline=_num(parts[16], 0.0), shadow=_num(parts[17], 0.0))

    def format(self) -> str:
        return (f"Style: {self.name},{self.fontname},{self.fontsize},&H00FFFFFF,&H00FFFFFF,"
                "&H00000000,&H00000000,0,0,0,0,100.00,100.00,0.00,0.00,1,"
                f"{self.outline:g},{self.shadow:g},{self.alignment},0,0,0,1")


def _num(text, default: float) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass
class Event:
    layer: int
    start: float
    end: float
    style: str
    name: str
    margin_l: str
    margin_r: str
    margin_v: str
    effect: str
    text: str
    prefix: str = "Dialogue"

    @property
    def tags(self) -> str:
        m = TAG_BLOCK_RE.match(self.text)
        return m.group(0) if m else ""

    @property
    def body(self) -> str:
        return self.text[len(self.tags):]

    def format(self) -> str:
        return (f"{self.prefix}: {self.layer},{_fmt_time(self.start)},{_fmt_time(self.end)},"
                f"{self.style},{self.name},{self.margin_l},{self.margin_r},{self.margin_v},"
                f"{self.effect},{self.text}")


@dataclasses.dataclass
class AssDocument:
    script_info: list[str]
    styles: list[Style]
    events: list[Event]
    style_header: list[str]

    @classmethod
    def parse(cls, text: str) -> "AssDocument":
        script_info: list[str] = []
        styles: list[Style] = []
        style_header: list[str] = []
        events: list[Event] = []
        section = ""
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
                continue
            if not stripped:
                continue
            if section == "[script info]":
                script_info.append(stripped)
            elif section.startswith("[v4"):
                if stripped.lower().startswith("format:"):
                    style_header = [stripped]
                elif stripped.lower().startswith("style:"):
                    st = Style.parse(stripped)
                    if st:
                        styles.append(st)
            elif section == "[events]" and stripped.lower().startswith(("dialogue:", "comment:")):
                ev = _parse_event(stripped)
                if ev:
                    events.append(ev)
        return cls(script_info=script_info, styles=styles, events=events,
                   style_header=style_header)

    def style(self, name: str) -> Style:
        for st in self.styles:
            if st.name == name:
                return st
        return self.styles[0] if self.styles else Style(name="Default")

    def play_res(self) -> tuple[int, int]:
        x = y = None
        for line in self.script_info:
            low = line.lower()
            if low.startswith("playresx:"):
                x = _int(line.split(":", 1)[1])
            elif low.startswith("playresy:"):
                y = _int(line.split(":", 1)[1])
        return (x or 1920, y or 1080)

    def to_text(self) -> str:
        out: list[str] = ["[Script Info]"]
        out.extend(self.script_info)
        out.append("")
        out.append("[V4+ Styles]")
        if self.style_header:
            out.extend(self.style_header)
        else:
            out.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                       "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                       "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                       "MarginL, MarginR, MarginV, Encoding")
        out.extend(st.format() for st in self.styles)
        out.append("")
        out.append("[Events]")
        out.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
        out.extend(ev.format() for ev in self.events)
        out.append("")
        return "\n".join(out)


def _int(text: str) -> int | None:
    try:
        return int(float(text.strip()))
    except ValueError:
        return None


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{seconds % 60:05.2f}"


def _parse_time(text: str) -> float:
    parts = text.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def _parse_event(line: str) -> Event | None:
    prefix, rest = line.split(":", 1)
    fields = rest.split(",", 9)
    if len(fields) < 10:
        return None
    return Event(
        layer=int(_num(fields[0], 0)),
        start=_parse_time(fields[1]),
        end=_parse_time(fields[2]),
        style=fields[3].strip(),
        name=fields[4].strip(),
        margin_l=fields[5].strip(),
        margin_r=fields[6].strip(),
        margin_v=fields[7].strip(),
        effect=fields[8].strip(),
        text=fields[9],
        prefix="Comment" if prefix.strip().lower() == "comment" else "Dialogue",
    )


# --------------------------------------------------------------------------
# options / stats
# --------------------------------------------------------------------------

#: Fraction of the preceding text's advance that libass does *not* draw, used to
#: pull an emote back onto the last visible glyph.  0.245 is the true shortfall,
#: but pulling that far makes the picture touch the text in some comments; 0.12
#: keeps a 1-4px gap everywhere, which is what Bilibili's player looks like.
_INK_RATIO = 0.12


@dataclasses.dataclass
class RenderOptions:
    #: Emote height as a multiple of the style's font size.  Changing the
    #: configured font size therefore scales every emote with the text.
    height_ratio: float = 1.30
    max_colors: int = 10
    tolerance: float = 0.5
    layer_boost: int = 1
    unknown: str = "text"          # "text" keeps [表情] as text, "drop" removes it
    min_emote_px: int = 8
    #: Hard ceiling only to catch absurd font sizes; high enough that the emote
    #: keeps tracking the configured font size (e.g. fs=120 -> 156px).
    max_emote_px: int = 400
    #: Pull each emote left by this fraction of the font size to cancel the
    #: preceding glyph's right side bearing, so the picture touches the word.
    side_bearing: float = 0.28
    #: Shrink the invisible placeholder by this fraction of the font size.  A
    #: placeholder exactly as wide as the picture still leaves a hair of space
    #: after it, because libass rounds the advance up.
    slot_trim: float = 0.0

    def emote_height(self, font_size: int) -> int:
        h = int(round(font_size * self.height_ratio))
        return max(self.min_emote_px, min(self.max_emote_px, h))

    def emote_height(self, font_size: int) -> int:
        h = int(round(font_size * self.height_ratio))
        return max(self.min_emote_px, min(self.max_emote_px, h))


@dataclasses.dataclass
class TransformStats:
    events: int = 0
    emotes: int = 0
    layers: int = 0
    missing: dict[str, int] = dataclasses.field(default_factory=dict)
    replaced: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def missing_total(self) -> int:
        return sum(self.missing.values())


# --------------------------------------------------------------------------
# the transformation
# --------------------------------------------------------------------------

def transform(doc: AssDocument, library, metrics: TextMetrics,
              options: RenderOptions | None = None) -> tuple[AssDocument, TransformStats]:
    opts = options or RenderOptions()
    stats = TransformStats()
    if not any(s.name == EMOTE_STYLE for s in doc.styles):
        doc.styles.append(Style(name=EMOTE_STYLE, fontname="Microsoft YaHei",
                                fontsize=25, alignment=7, outline=0.0, shadow=0.0))
    kept: list[Event] = []
    added: list[Event] = []
    for ev in doc.events:
        if ev.style == EMOTE_STYLE or "[" not in ev.text:
            kept.append(ev)
            continue
        try:
            kept.extend(_transform_event(ev, doc, library, metrics, opts, stats, added))
        except Exception:
            kept.append(ev)
    doc.events = kept + added
    return doc, stats


@dataclasses.dataclass
class _Slot:
    """One emote occurrence inside a line."""
    line: int
    x: float          # pen offset of its left edge inside the line
    width: float      # reserved width == the width the "[name]" text would have
    height: int
    entry: dict


def _transform_event(ev: Event, doc: AssDocument, library, metrics: TextMetrics,
                     opts: RenderOptions, stats: TransformStats,
                     added: list[Event]) -> list[Event]:
    style = doc.style(ev.style)
    tags = ev.tags
    tag_text = TAG_BLOCK_RE.sub("", tags)
    # the opt-out flag lives *inside* the tag block, so test the raw text
    if "--emote-skip" in tags or "--emote-skip" in tag_text:
        return [ev]

    fontname = style.fontname
    m = FONT_RE.search(tag_text)
    if m and m.group(1).strip():
        fontname = m.group(1).strip()
    fs = style.fontsize
    m = FS_RE.search(tag_text)
    if m:
        fs = max(6, int(_num(m.group(1), fs)))

    # position overrides live *inside* the leading tag block, so search `tags`
    pos_m = POS_RE.search(tags)
    move_m = MOVE_RE.search(tags)
    if not (pos_m or move_m):
        return [ev]  # no explicit anchor: leave DanmakuFactory's text alone

    # 1. resolve tokens; split into per-line piece lists
    lines: list[list[tuple[str, str, dict | None]]] = [[]]
    pos = 0
    body = ev.body
    tokens: list[tuple[int, int, str, dict | None]] = []
    for match in TOKEN_RE.finditer(body):
        token = match.group(0)
        entry = library.find(token)
        tokens.append((match.start(), match.end(), token, entry))
        if entry is None:
            stats.missing[token] = stats.missing.get(token, 0) + 1
        else:
            stats.replaced[token] = stats.replaced.get(token, 0) + 1
    if not any(t[3] for t in tokens):
        return [ev]

    token_iter = iter(tokens)
    pending = next(token_iter, None)
    cursor = 0
    while cursor < len(body):
        if pending and pending[0] == cursor:
            start, end, token, entry = pending
            lines[-1].append(("emote", token, entry))
            cursor = end
            pending = next(token_iter, None)
            continue
        stop = pending[0] if pending else len(body)
        chunk = body[cursor:stop]
        parts = LINEBREAK_RE.split(chunk)
        for i, part in enumerate(parts):
            if i:
                lines.append([])
            if part:
                lines[-1].append(("text", part, None))
        cursor = stop

    # 2. lay out every line.
    #
    # The emote is drawn inline, so its height only has to follow the configured
    # font size — ``emote_scale`` times the style's fontsize.  Nothing here needs
    # the text width to be exact any more; libass advances the pen itself.
    emote_h = opts.emote_height(fs)
    slots: list[_Slot] = []
    for li, pieces in enumerate(lines):
        for kind, value, entry in pieces:
            if kind != "emote":
                continue
            try:
                _vector(library, entry, emote_h, opts)
            except Exception:
                stats.missing[value] = stats.missing.get(value, 0) + 1
                continue
            slots.append(_Slot(line=li, x=0.0, width=0.0, height=emote_h,
                               entry=entry))

    # 3. rebuild the text line, reserving each emote's width with an invisible run.
    #
    # A drawing run can only hold one override block without advancing the pen,
    # so a multi-colour emote cannot be laid inline in a single event.  Instead
    # the *text* line carries a transparent drawing whose bbox is exactly the
    # emote's ink width: libass advances the pen by that width itself, so the
    # following words sit right after the emote with a normal glyph gap.
    # The coloured layers are then drawn by absolute events (step 4) that share
    # the same \move, which keeps them locked to the scrolling text.
    restore = f"\\bord{style.outline:g}\\shad{style.shadow:g}"
    out: list[str] = [tags]
    slot_idx = 0
    placed: list[tuple[_Slot, object, float, float]] = []   # slot, vec, scale, adv
    cursor = 0.0
    written_text = ""
    written_prefixes: list[str] = []
    offsets: list[float] = []
    bearings: list[float] = []
    for li, pieces in enumerate(lines):
        if li:
            out.append("\\N")
        tail_text = ""
        written_text = ""
        for pi, (kind, value, entry) in enumerate(pieces):
            if kind != "emote":
                # A hard space that directly precedes an emote only produces a
                # stray gap between the username and the picture, so drop it.  The
                # words stay identical; only the hole disappears.  The check has
                # to look through trailing tag blocks such as ``{\c&HFFFFFF}``,
                # which is where the \h actually hides in DanmakuFactory output.
                nxt = pieces[pi + 1] if pi + 1 < len(pieces) else None
                if nxt and nxt[0] == "emote" and _strip_trailing_tags(value) \
                        .rstrip(" ").endswith("\\h"):
                    value = _drop_trailing_hard_space(value)
                out.append(value)
                cursor += metrics.width(value, fontname, fs)
                tail_text += value
                written_text += value
                continue
            if slot_idx >= len(slots) or slots[slot_idx].entry is not entry:
                out.append(value)
                cursor += metrics.width(value, fontname, fs)
                tail_text += value
                written_text += value
                continue
            slot = slots[slot_idx]
            slot_idx += 1
            try:
                vec = _vector(library, slot.entry, slot.height, opts)
            except Exception:
                out.append(value)
                cursor += metrics.width(value, fontname, fs)
                tail_text += value
                written_text += value
                continue
            scale = slot.height / vec.height if vec.height else 1.0
            lo, hi = _ink_bounds(vec)
            adv = (hi - lo) * scale
            # The picture is drawn at the placeholder's left edge and its ink is
            # ``adv`` wide, so a placeholder of exactly ``adv`` makes the words
            # after it start right at the art's edge.  Keeping the emote's own
            # transparent right margin in the placeholder would push them away
            # again — that is the gap seen when an emote sits mid-sentence.
            pad_right = max(0.0, vec.width - hi) * scale
            reserved = max(1.0, adv - pad_right)
            offsets.append(cursor)
            # remember the text that really precedes this emote (the \h has been
            # removed above), so the width used for correction matches the ASS
            written_prefixes.append(written_text)
            bearings.append(0.0)   # filled in below, once the line width is known
            tail_text = ""
            cursor += reserved
            out.append(f"{{\\p1\\bord0\\shad0\\1a&HFF&}}m 0 0 l {reserved:.1f} 0"
                       f"{{\\p0\\1a&H00&{restore}}}")
            placed.append((slot, vec, scale, adv))
    ev.text = "".join(out)
    if not placed:
        return [ev]

    # 4. coloured layers, absolutely positioned over the reserved space.
    align = int(_num(AN_RE.search(tags).group(1), style.alignment)) \
        if AN_RE.search(tags) else style.alignment
    if move_m:
        x0, y0, x1, y1 = (float(g) for g in move_m.groups())
    else:
        x0, y0 = (float(g) for g in pos_m.groups())
        x1, y1 = x0, y0
    # libass centres the line on the width *it* renders, which is the text plus
    # the placeholders — the same quantity as ``cursor``.  ``\h`` and the glyph
    # side bearings are shared by both, so this matches within a pixel.
    left0 = x0 if align in (1, 4, 7) else x0 - cursor / 2.0
    left1 = x1 if align in (1, 4, 7) else x1 - cursor / 2.0
    if align in (1, 2, 3):
        top = y0 - fs
    elif align in (4, 5, 6):
        top = y0 - fs / 2.0
    else:
        top = y0

    ascent = metrics.ascent(fontname, fs)
    text_centre = ascent - fs / 2.0 + fs * 0.06
    clip = CLIP_RE.search(tags)
    clip_tag = ("\\" + clip.group(0)[1:]) if clip else ""

    # libass draws every glyph narrower than Pillow's advance (measured: ink ends
    # at 75.5% of it, within a pixel, for CJK/Latin/mixed).  The text before an
    # emote is therefore narrower on screen than our arithmetic assumed, so the
    # picture sits too far right by that shortfall.
    #
    # The correction uses the text actually written before each emote (the \h
    # separator has already been removed there), so the width matches the ASS.
    resolved: list[float] = []
    for prefix in written_prefixes[: len(placed)]:
        plain = TAG_BLOCK_RE.sub("", prefix).replace("\\h", " ").rstrip()
        advance = metrics.width(plain, fontname, fs) if plain else 0.0
        # libass draws the text narrower than Pillow's advance, so the picture
        # sits too far right; pull it back by that shortfall.
        #
        # The username separator ``\h`` is *already removed* from the text layer
        # (see _drop_trailing_hard_space), so it must not be compensated here as
        # well: doing both pulled the picture onto the preceding glyph, which is
        # what caused the overlap.
        resolved.append(advance * _INK_RATIO + fs * opts.side_bearing)
    if len(resolved) < len(placed):
        resolved += [fs * opts.side_bearing] * (len(placed) - len(resolved))

    for (slot, vec, scale, adv), off, bearing in zip(placed, offsets, resolved):
        art_h = vec.height * scale
        py = top + text_centre - art_h / 2.0
        # ``bearing`` pulls the picture back onto the last visible glyph; the
        # picture's own transparent left margin is skipped so its ink starts there.
        px0 = left0 + off - bearing
        px1 = left1 + off - bearing
        head = "\\an7\\bord0\\shad0" + clip_tag
        anchor = (f"\\move({px0:.1f},{py:.1f},{px1:.1f},{py:.1f})" if move_m
                  else f"\\pos({px0:.1f},{py:.1f})")
        scale_tag = (f"\\fscx{scale * 100:.2f}\\fscy{scale * 100:.2f}"
                     if abs(scale - 1.0) > 1e-6 else "")
        for depth, (ctags, path) in enumerate(vec.layer_drawings()):
            added.append(dataclasses.replace(
                ev, layer=ev.layer + opts.layer_boost + depth, style=EMOTE_STYLE,
                text="{" + head + anchor + ctags + scale_tag + "}" + path + "{\\p0}"))
            stats.layers += 1

    stats.emotes += len(placed)
    stats.events += 1
    return [ev]


def _shape_bounds(shape) -> tuple[float, float]:
    xs = [x for loop in shape.loops for x, _ in loop]
    return (min(xs), max(xs)) if xs else (0.0, 0.0)


def _strip_trailing_tags(text: str) -> str:
    """``text`` with any trailing override blocks removed."""
    stripped = text.rstrip()
    while stripped.endswith("}"):
        i = stripped.rfind("{")
        if i < 0:
            break
        stripped = stripped[:i].rstrip()
    return stripped


#: ``\h`` followed only by override blocks — DanmakuFactory's username separator.
#: Written as a raw string, so ``\\\\h`` here matches a backslash followed by ``h``.
_TRAILING_H_RE = re.compile("\\\\h(?=\\s*(?:\\{[^}]*\\}\\s*)*$)")


def _drop_trailing_hard_space(text: str) -> str:
    """Remove a ``\\h`` that is followed only by override blocks.

    DanmakuFactory writes ``用户名:\\h{\\c&HFFFFFF}评论``, so the space to drop is
    not at the end of the string but just before the colour block.
    """
    return _TRAILING_H_RE.sub("", text, count=1)

def _vector(library, entry, height: int, opts: RenderOptions):
    return library.vector(entry, height=height, max_colors=opts.max_colors,
                          tolerance=opts.tolerance)


def _ink_bounds(vec) -> tuple[float, float]:
    """Left and right edge of the drawn art, ignoring transparent margins."""
    lo, hi = None, None
    for shape in vec.shapes:
        for loop in shape.loops:
            for x, _ in loop:
                lo = x if lo is None else min(lo, x)
                hi = x if hi is None else max(hi, x)
    if lo is None or hi is None or hi <= lo:
        return 0.0, float(vec.width)
    return float(lo), float(hi)


def _ink_width(vec) -> float:
    """Width of the drawn art, ignoring transparent margins on either side."""
    lo, hi = _ink_bounds(vec)
    return hi - lo


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def write_ass(doc: AssDocument, path: str | Path) -> None:
    Path(path).write_text(doc.to_text(), encoding="utf-8-sig")


def read_ass(path: str | Path) -> AssDocument:
    return AssDocument.parse(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
