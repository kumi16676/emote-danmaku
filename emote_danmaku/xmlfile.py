"""Read 录播姬 (BililiveRecorder) danmaku XML and harvest its emote metadata.

录播姬 writes one ``<d>`` element per comment::

    <d p="12.345,1,25,16777215,1690000000000,0,10001,0" user="观众">[妙啊]</d>

With "录制弹幕原始数据" enabled it also stores the raw JSON of the message in a
``raw`` attribute, and *that* is where Bilibili puts the emote table::

    info[0][15].extra = "{\\"emots\\":{\\"[妙啊]\\":{\\"emoticon_unique\\":\\"official_109\\",
                        \\"url\\":\\"https://i0.hdslb.com/bfs/live/....png\\", ...}}}"

So a recording already contains everything needed to build the emote library,
as long as raw recording is on.  ``harvest`` extracts those entries; the CLI can
then download exactly the images that were used in that recording.
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

INVALID_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufeff\ufffe\uffff]"
)


@dataclasses.dataclass
class Danmaku:
    time: float
    mode: int
    size: int
    color: int
    user: str
    text: str

    @property
    def is_scroll(self) -> bool:
        return self.mode in (1, 2, 3)

    @property
    def is_top(self) -> bool:
        return self.mode in (4, 5)

    @property
    def is_bottom(self) -> bool:
        return self.mode == 4 and self.mode != 5

    def tokens(self) -> list[str]:
        return re.findall(r"\[[^\[\]\n]{1,40}\]", self.text)


@dataclasses.dataclass
class EmoteInfo:
    code: str            # "[妙啊]"
    unique: str          # "official_109"
    url: str
    width: int = 0
    height: int = 0


@dataclasses.dataclass
class HarvestResult:
    emotes: dict[str, EmoteInfo]
    raw_comments: int
    total_comments: int
    tokens: dict[str, int]


def parse_xml(path: str | Path) -> list[Danmaku]:
    """Parse a 录播姬/主站 XML file into danmaku records (text only)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    text = INVALID_XML.sub("", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 录播姬 files are not always well-formed after a crash; recover what we can
        root = _recover(text)
        if root is None:
            raise
    out: list[Danmaku] = []
    for node in root.iter("d"):
        p = (node.get("p") or "").split(",")
        if len(p) < 4:
            continue
        try:
            t = float(p[0])
        except ValueError:
            continue
        out.append(Danmaku(
            time=t,
            mode=int(float(p[1] or 1)) if p[1] else 1,
            size=int(float(p[2] or 25)) if p[2] else 25,
            color=int(float(p[3] or 16777215)) if p[3] else 16777215,
            user=node.get("user") or "",
            text=(node.text or "").strip(),
        ))
    return out


def _recover(text: str):
    """Best-effort parse of a truncated XML file: keep complete ``<d>`` nodes."""
    body = text[text.find("<i"):] if "<i" in text else text
    close = body.rfind("</i>")
    if close != -1:
        body = body[: close + 4]
    else:
        body += "</i>"
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        pass
    # drop the tail element that was cut mid-write and retry
    cut = body.rfind("<d ")
    if cut == -1:
        return None
    try:
        return ET.fromstring(body[:cut] + "</i>")
    except ET.ParseError:
        return None


def harvest_emotes(path: str | Path) -> HarvestResult:
    """Collect emote metadata from the ``raw`` attributes of a 录播姬 XML."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    text = INVALID_XML.sub("", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = _recover(text)
    emotes: dict[str, EmoteInfo] = {}
    tokens: dict[str, int] = {}
    raw_comments = 0
    total = 0
    if root is None:
        return HarvestResult(emotes, 0, 0, tokens)
    for node in root.iter("d"):
        total += 1
        body = (node.text or "").strip()
        for token in re.findall(r"\[[^\[\]\n]{1,40}\]", body):
            tokens[token] = tokens.get(token, 0) + 1
        raw = node.get("raw")
        if not raw:
            continue
        raw_comments += 1
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for code, info in _iter_emots(payload).items():
            if code not in emotes:
                emotes[code] = info
    return HarvestResult(emotes, raw_comments, total, tokens)


def _iter_emots(payload) -> dict[str, EmoteInfo]:
    """Yield ``[code] -> EmoteInfo`` from one DANMU_MSG raw payload.

    录播姬 writes the ``p``-tag source as ``info[0][15]`` (see its
    BasicDanmakuWriter), so the emote table arrives as a *string* inside
    ``extra``; handle both the string and the object form.
    """
    found: dict[str, EmoteInfo] = {}
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, list) or not info or not isinstance(info[0], list):
        return found
    slot = info[0][15] if len(info[0]) > 15 else None
    extra = None
    if isinstance(slot, dict):
        extra = slot.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = None
    emots = extra.get("emots") if isinstance(extra, dict) else None
    if not isinstance(emots, dict):
        return found
    for code, item in emots.items():
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        found[code] = EmoteInfo(
            code=code,
            unique=item.get("emoticon_unique") or "",
            url=url,
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
        )
    return found


def image_url_for(code: str, harvest: HarvestResult) -> str | None:
    info = harvest.emotes.get(code)
    return info.url if info else None


def unique_for(code: str, harvest: HarvestResult) -> str | None:
    info = harvest.emotes.get(code)
    return info.unique if info else None
