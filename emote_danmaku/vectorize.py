"""Convert raster emote images into ASS vector drawing commands.

Why vector drawings instead of ``\\1img``?
-----------------------------------------
ffmpeg/libass does not implement the ``\\1img`` picture tag (probed: it renders
nothing), so an ASS that references PNG files only works in VSFilter-style
players, not in the ffmpeg burn-in pipeline.  Inline ``{\\p1}`` drawings are
ordinary subtitle content: no external files, no extra video pass, and they
survive trimming/remuxing.

How colour is handled
---------------------
Measurement (see ``probe``/``rule`` notes in the README) shows that inside one
event a ``{...}`` override block ends the current drawing and restarts the next
subpath's coordinate system at the *block* origin.  Colours therefore have to be
applied per drawing run, and every run must start at the emote's origin.  The
tracer emits exactly that: one ``{\\p1\\1c..}path`` run per colour, path
coordinates relative to the emote's top-left corner.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

try:  # Pillow is only needed for tracing.
    from PIL import Image
except Exception:  # pragma: no cover - reported to the user by the CLI
    Image = None  # type: ignore[assignment]


DEFAULT_MAX_COLORS = 10
DEFAULT_TOLERANCE = 0.5
MIN_REGION_AREA = 1.5


class TraceError(RuntimeError):
    """Raised when an image cannot be turned into a vector drawing."""


@dataclasses.dataclass
class Shape:
    color: tuple[int, int, int]
    alpha: int
    loops: list[list[tuple[float, float]]]
    area: float = 0.0

    def drawing(self, dx: float = 0.0, dy: float = 0.0) -> str:
        """ASS path for this colour layer, optionally shifted by (dx, dy).

        ``dx`` is used to cancel the picture's transparent left margin so its
        ink starts exactly at the pen position when drawn inline.
        """
        # A newline inside an ASS event is not a drawing separator; keep the
        # whole run on one physical line.
        parts: list[str] = []
        for loop in self.loops:
            if len(loop) < 3:
                continue
            x0, y0 = loop[0]
            parts.append(f"m {x0 + dx:.1f} {y0 + dy:.1f}")
            for x, y in loop[1:]:
                parts.append(f"l {x + dx:.1f} {y + dy:.1f}")
        return " ".join(parts).replace("\n", " ").replace("\r", " ")


@dataclasses.dataclass
class VectorEmote:
    width: int
    height: int
    shapes: list[Shape]

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0

    def layer_drawings(self) -> list[tuple[str, str]]:
        """Return ``[(override_tags, path)]`` — one self-contained run per colour.

        Measured libass behaviour: any override block that follows a drawing run
        advances the pen past that run's bounding box, and neither ``\\p0``,
        re-opening ``\\p1`` nor an explicit ``\\pos`` resets it.  The only way to
        get several colours back onto the same origin is to give each colour its
        own **event**, because the pen resets at every event start.
        """
        runs: list[tuple[str, str]] = []
        for shape in self.shapes:
            path = shape.drawing()
            if not path:
                continue
            # ASS colours are &HBBGGRR, i.e. bytes are written B, G, R
            r, g, b = shape.color
            tags = f"\\p1\\bord0\\shad0\\1c&H{b:02X}{g:02X}{r:02X}&"
            if shape.alpha < 255:
                tags += f"\\1a&H{255 - shape.alpha:02X}&"
            runs.append((tags, path))
        return runs

    def ass_blocks(self, scale: float = 1.0) -> list[str]:
        """Render every colour as ``{tags}path{/p0}`` (caller must use one event
        per block, otherwise the pen advances between blocks)."""
        runs = self.layer_drawings()
        if not runs:
            return []
        scale_tag = (f"\\fscx{scale * 100:.2f}\\fscy{scale * 100:.2f}"
                     if abs(scale - 1.0) > 1e-6 else "")
        return ["{" + tags + scale_tag + "}" + path + "{\\p0}" for tags, path in runs]


# --------------------------------------------------------------------------
# image loading
# --------------------------------------------------------------------------

def _load_rgba(path: Path):
    if Image is None:
        raise TraceError("需要 Pillow 才能把表情包转成 ASS 矢量图形（pip install pillow）")
    try:
        im = Image.open(path)
    except Exception as exc:  # pragma: no cover
        raise TraceError(f"无法读取图片 {path}: {exc}") from exc
    try:
        im.seek(0)  # animated gif/webp -> first frame
    except Exception:
        pass
    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    rgba = im.convert("RGBA")
    if not has_alpha:
        bg = rgba.getpixel((0, 0))
        if bg[3] == 255 and sum(bg[:3]) > 3 * 235:
            _erase_border_background(rgba, bg)
    return rgba


def _erase_border_background(im, bg, tolerance: int = 24) -> None:
    w, h = im.size
    px = im.load()
    stack = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    seen = bytearray(w * h)
    while stack:
        x, y = stack.pop()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        idx = y * w + x
        if seen[idx]:
            continue
        seen[idx] = 1
        c = px[x, y]
        if c[3] == 0 or any(abs(c[i] - bg[i]) > tolerance for i in range(3)):
            continue
        px[x, y] = (c[0], c[1], c[2], 0)
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))


# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------

def _palette_index(rgba, max_colors: int, alpha_threshold: int):
    """Return (grid, palette, alpha_bytes, width, height).

    ``grid`` holds a palette index per pixel, 255 meaning "background".
    """
    w, h = rgba.size
    alpha_data = rgba.getchannel("A").tobytes()
    rgb = rgba.convert("RGB").tobytes()

    counts: dict[tuple[int, int, int], int] = {}
    for i in range(w * h):
        if alpha_data[i] < alpha_threshold:
            continue
        o = i * 3
        key = (rgb[o], rgb[o + 1], rgb[o + 2])
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        raise TraceError("图片是空白的（所有像素都透明）")

    if len(counts) <= max_colors:
        palette = sorted(counts, key=counts.get, reverse=True)
    else:
        palette = _median_cut(list(counts.items()), max_colors)
    palette = _merge_similar(palette)
    if not palette:
        palette = [max(counts, key=counts.get)]

    # map colours -> palette
    remap: dict[tuple[int, int, int], int] = {}
    grid = bytearray(w * h)
    for i in range(w * h):
        if alpha_data[i] < alpha_threshold:
            grid[i] = 255
            continue
        o = i * 3
        key = (rgb[o], rgb[o + 1], rgb[o + 2])
        idx = remap.get(key)
        if idx is None:
            idx = _nearest(palette, key)
            remap[key] = idx
        grid[i] = idx
    return grid, palette, alpha_data, w, h


def _median_cut(items, k: int):
    boxes = [items]
    while len(boxes) < k:
        best = None
        for bi, box in enumerate(boxes):
            if len(box) < 2:
                continue
            score, channel = _spread(box)
            if best is None or score > best[0]:
                best = (score, bi, channel)
        if best is None:
            break
        _, bi, channel = best
        box = boxes.pop(bi)
        box.sort(key=lambda it: it[0][channel])
        total = sum(c for _, c in box)
        acc, cut = 0, 1
        for i, (_, c) in enumerate(box):
            acc += c
            if acc >= total / 2:
                cut = max(1, min(i + 1, len(box) - 1))
                break
        boxes.append(box[:cut])
        boxes.append(box[cut:])
    palette = []
    for box in boxes:
        total = sum(c for _, c in box)
        if total:
            palette.append((round(sum(col[0] * c for col, c in box) / total),
                            round(sum(col[1] * c for col, c in box) / total),
                            round(sum(col[2] * c for col, c in box) / total)))
    return palette


def _spread(box):
    best = (0.0, 2)
    for ch in range(3):
        vals = [col[ch] for col, _ in box]
        weight = (0.299, 0.587, 0.114)[ch]
        score = (max(vals) - min(vals)) * (0.35 + weight)
        if score > best[0]:
            best = (score, ch)
    return best


def _merge_similar(palette, tol: int = 20):
    merged: list[tuple[int, int, int]] = []
    for c in palette:
        if not any(all(abs(c[i] - m[i]) <= tol for i in range(3)) for m in merged):
            merged.append(c)
    return merged


def _nearest(palette, color):
    best, bi = None, 0
    r, g, b = color
    for i, (pr, pg, pb) in enumerate(palette):
        d = (r - pr) ** 2 * 0.30 + (g - pg) ** 2 * 0.59 + (b - pb) ** 2 * 0.11
        if best is None or d < best:
            best, bi = d, i
    return bi


# --------------------------------------------------------------------------
# boundary tracing (crack following on the pixel grid)
# --------------------------------------------------------------------------

def trace_loops(grid: bytearray, w: int, h: int, index: int) -> list[list[tuple[int, int]]]:
    """Closed pixel-boundary loops of every 4-connected region of colour ``index``.

    Outer boundaries run clockwise, holes counter-clockwise (screen coordinates),
    which is what libass' non-zero fill rule needs for holes to be cut out.
    """
    def inside(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and grid[y * w + x] == index

    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a, b):
        edges.setdefault(a, []).append(b)

    for y in range(h):
        row = y * w
        for x in range(w):
            if grid[row + x] != index:
                continue
            if not inside(x, y - 1):
                add((x, y), (x + 1, y))
            if not inside(x + 1, y):
                add((x + 1, y), (x + 1, y + 1))
            if not inside(x, y + 1):
                add((x + 1, y + 1), (x, y + 1))
            if not inside(x - 1, y):
                add((x, y + 1), (x, y))

    loops: list[list[tuple[int, int]]] = []
    while edges:
        start = next(iter(edges))
        targets = edges[start]
        if not targets:
            del edges[start]
            continue
        loop = [start]
        target = targets.pop()
        if not targets:
            del edges[start]
        while target != start:
            loop.append(target)
            nxt = edges.get(target)
            if not nxt:
                break
            nxt_target = nxt.pop()
            if not nxt:
                del edges[target]
            target = nxt_target
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def _rdp(points, eps: float):
    if len(points) < 3:
        return points
    first, last = points[0], points[-1]
    dx, dy = last[0] - first[0], last[1] - first[1]
    denom = math.hypot(dx, dy)
    best, index = -1.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if denom == 0:
            dist = math.hypot(px - first[0], py - first[1])
        else:
            dist = abs(dy * px - dx * py + last[0] * first[1] - last[1] * first[0]) / denom
        if dist > best:
            best, index = dist, i
    if best <= eps:
        return [first, last]
    return _rdp(points[: index + 1], eps)[:-1] + _rdp(points[index:], eps)


def _simplify(loop, eps: float):
    if len(loop) < 4:
        return [(float(x), float(y)) for x, y in loop]
    far = max(range(len(loop)),
              key=lambda i: (loop[i][0] - loop[0][0]) ** 2 + (loop[i][1] - loop[0][1]) ** 2)
    a = _rdp(loop[: far + 1], eps)
    b = _rdp(loop[far:] + [loop[0]], eps)
    out = a[:-1] + b[:-1]
    return [(float(x), float(y)) for x, y in out]


def _area(loop) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def trace_image(
    path: str | Path,
    height: int | None = None,
    max_colors: int = DEFAULT_MAX_COLORS,
    tolerance: float = DEFAULT_TOLERANCE,
    alpha_threshold: int = 110,
) -> VectorEmote:
    """Vectorise ``path`` so its emote box is ``height`` device pixels tall."""
    rgba = _load_rgba(Path(path))
    bbox = rgba.getchannel("A").getbbox()
    if bbox:
        rgba = rgba.crop(bbox)
    src_w, src_h = rgba.size
    if src_w < 2 or src_h < 2:
        raise TraceError(f"图片尺寸太小：{src_w}x{src_h}")
    if height and height != src_h:
        width = max(1, round(src_w * height / src_h))
        rgba = rgba.resize((width, height), Image.LANCZOS)

    grid, palette, alpha_data, w, h = _palette_index(rgba, max_colors, alpha_threshold)
    shapes: list[Shape] = []
    for index, color in enumerate(palette):
        loops = trace_loops(grid, w, h, index)
        if not loops:
            continue
        simplified = [_simplify(loop, tolerance) for loop in loops]
        simplified = [l for l in simplified if len(l) >= 3 and _area(l) >= MIN_REGION_AREA]
        if not simplified:
            continue
        alphas = [alpha_data[i] for i in range(w * h) if grid[i] == index]
        alpha = round(sum(alphas) / len(alphas)) if alphas else 255
        shapes.append(Shape(color=color, alpha=alpha, loops=simplified,
                            area=sum(_area(l) for l in simplified)))
    if not shapes:
        raise TraceError("没有可描绘的图形（可能整张图都被判为背景）")
    shapes.sort(key=lambda s: s.area, reverse=True)
    return VectorEmote(width=w, height=h, shapes=shapes)


def box_emote(width: int, height: int, color=(120, 120, 120)) -> VectorEmote:
    """Fallback placeholder used when an image cannot be traced."""
    r = max(2.0, height * 0.18)
    w, h = float(width), float(height)
    loop: list[tuple[float, float]] = []
    for cx, cy, start in ((w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)):
        for i in range(7):
            ang = math.radians(start + 90 * i / 6)
            loop.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return VectorEmote(width=width, height=height,
                       shapes=[Shape(color=color, alpha=255, loops=[loop], area=w * h)])
