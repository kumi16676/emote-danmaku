"""Emote library: download from Bilibili, import local files, index by code.

The library is a plain directory the user can inspect and edit::

    emotes/
      index.json            # {"emotes": [{...}, ...], "version": 1}
      images/<sha1>.png     # original downloads (kept for re-tracing)
      vector/<sha1>.json    # cached vector trace

Lookup keys (all case/space insensitive) are, in priority order:

* ``emoticon_unique``  e.g. ``official_109`` (globally unique, preferred)
* the display name    e.g. ``妙啊``        (what ``[妙啊]`` in danmaku means)
* the file stem        for hand-imported images, e.g. ``猫猫.png`` -> ``猫猫``
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EMOTICON_API = "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
INDEX_VERSION = 1
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class LibraryError(RuntimeError):
    pass


def normalize_key(text: str) -> str:
    """Keys are compared after stripping brackets, whitespace and case."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return re.sub(r"\s+", "", text).casefold()


class EmoteLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.images_dir = self.root / "images"
        self.vector_dir = self.root / "vector"
        self.index_path = self.root / "index.json"
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._by_unique: dict[str, str] = {}
        self._by_name: dict[str, str] = {}
        self._vector_cache: dict[str, object] = {}
        self.load()

    # -- index ------------------------------------------------------------
    def load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(exist_ok=True)
        self.vector_dir.mkdir(exist_ok=True)
        self._entries.clear()
        self._by_unique.clear()
        self._by_name.clear()
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text("utf-8"))
            except Exception as exc:
                raise LibraryError(f"表情包索引损坏：{self.index_path}（{exc}）") from exc
            for entry in data.get("emotes", []):
                self._register(entry, save=False)
        self._scan_loose_images()

    def _scan_loose_images(self) -> None:
        """Pick up images the user simply dropped into the library folder.

        Recurses into sub-folders too, so a downloaded pack can be unzipped as
        ``emotes/某个包/xxx.png`` and still resolve.  The file name (with or
        without brackets) becomes the emote name; ``images/`` and ``vector/``
        are the library's own storage and are skipped.
        """
        added = False
        skip_dirs = {self.images_dir.resolve(), self.vector_dir.resolve()}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            if path.parent.resolve() in skip_dirs:
                continue
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            try:
                before = len(self._entries)
                self.add_file(path, name=path.stem, source="loose")
                if len(self._entries) != before:
                    added = True
            except Exception:
                continue
        if added:
            self.save()

    def save(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "emotes": sorted(self._entries.values(), key=lambda e: (e.get("name", ""), e["id"])),
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.index_path)

    def _register(self, entry: dict, save: bool = True) -> None:
        key = entry["id"]
        if key in self._entries:
            return
        self._entries[key] = entry
        for raw in (entry.get("name"), entry.get("stem")):
            if raw:
                self._by_name.setdefault(normalize_key(raw), key)
        for raw in entry.get("aliases", []):
            if raw:
                self._by_name.setdefault(normalize_key(raw), key)
        unique = entry.get("unique")
        if unique:
            self._by_unique.setdefault(normalize_key(unique), key)
        if save:
            self.save()

    # -- lookup -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self.find(key) is not None

    def find(self, code: str) -> dict | None:
        """Resolve a danmaku token like ``[妙啊]`` / ``official_109`` to an entry."""
        if not code:
            return None
        k = normalize_key(code)
        key = self._by_unique.get(k) or self._by_name.get(k)
        return self._entries.get(key) if key else None

    def entries(self) -> list[dict]:
        return sorted(self._entries.values(), key=lambda e: (e.get("name") or e["id"]))

    def image_path(self, entry: dict) -> Path:
        return self.images_dir / entry["file"]

    # -- vector cache -----------------------------------------------------
    def vector(self, entry: dict, height: int, max_colors: int = 12, tolerance: float = 0.45):
        from .vectorize import trace_image

        key = f"{entry['id']}@{height}c{max_colors}t{tolerance}"
        cached = self._vector_cache.get(key)
        if cached is not None:
            return cached
        cache_file = self.vector_dir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")
        if cache_file.exists():
            try:
                obj = _vector_from_json(json.loads(cache_file.read_text("utf-8")))
                self._vector_cache[key] = obj
                return obj
            except Exception:
                pass
        obj = trace_image(self.image_path(entry), height=height,
                          max_colors=max_colors, tolerance=tolerance)
        try:
            cache_file.write_text(json.dumps(_vector_to_json(obj), ensure_ascii=False), "utf-8")
        except Exception:
            pass
        self._vector_cache[key] = obj
        return obj

    # -- writing ----------------------------------------------------------
    def add_file(self, path: str | Path, name: str | None = None, *,
                 source: str = "local", unique: str | None = None,
                 aliases: list[str] | None = None, overwrite: bool = False) -> dict:
        path = Path(path)
        if not path.is_file():
            raise LibraryError(f"找不到图片：{path}")
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        existing = self._entries.get(digest)
        stem = Path(name).stem if name else path.stem
        if existing and not overwrite:
            # same bytes already known: only add the new name as an alias
            changed = False
            if stem and existing.get("name") != stem and normalize_key(stem) not in self._by_name:
                existing.setdefault("aliases", [])
                if stem not in existing["aliases"]:
                    existing["aliases"].append(stem)
                    changed = True
            if changed:
                self._register_aliases(existing)
                self.save()
            return existing

        suffix = path.suffix.lower() or ".png"
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            raise LibraryError(f"不支持的图片格式：{suffix}")
        target = self.images_dir / f"{digest}{suffix}"
        if not target.exists():
            shutil.copyfile(path, target)
        entry = {
            "id": digest,
            "file": target.name,
            "name": stem,
            "unique": unique or f"local_{stem}",
            "source": source,
            "aliases": aliases or [],
        }
        with self._lock:
            self._register(entry)
        return entry

    def _register_aliases(self, entry: dict) -> None:
        for raw in entry.get("aliases", []):
            self._by_name.setdefault(normalize_key(raw), entry["id"])

    # -- remote -----------------------------------------------------------
    def fetch_room_pack(
        self,
        room_id: int,
        *,
        cookie: str = "",
        platform: str = "pc",
        progress=None,
        timeout: float = 20.0,
    ) -> dict:
        """Download the emotes a live room offers (official + room-specific)."""
        query = urllib.parse.urlencode({"platform": platform, "room_id": room_id})
        url = f"{EMOTICON_API}?{query}"
        payload = self._http_json(url, cookie=cookie, timeout=timeout)
        if payload.get("code") != 0:
            raise LibraryError(
                f"获取表情包失败：code={payload.get('code')} message={payload.get('message')}"
            )
        packages = (payload.get("data") or {}).get("data") or []
        added = updated = failed = 0
        total = sum(len(p.get("emoticons") or []) for p in packages)
        done = 0
        for package in packages:
            for emote in package.get("emoticons") or []:
                done += 1
                name = (emote.get("emoji") or emote.get("descript") or "").strip()
                image_url = (emote.get("url") or "").strip()
                unique = (emote.get("emoticon_unique") or "").strip()
                if not image_url or not name:
                    failed += 1
                    continue
                if progress:
                    progress(done, total, name)
                try:
                    result = self._download_emote(image_url, name, unique, cookie, timeout)
                except Exception:
                    failed += 1
                    continue
                if result == "added":
                    added += 1
                else:
                    updated += 1
        return {"total": total, "added": added, "updated": updated, "failed": failed}

    def _download_emote(self, url: str, name: str, unique: str, cookie: str, timeout: float) -> str:
        existing = self._by_unique.get(normalize_key(unique)) if unique else None
        entry = self._entries.get(existing) if existing else None
        if entry and (self.images_dir / entry["file"]).exists():
            if entry.get("name") != name:
                entry["name"] = name
                entry.setdefault("aliases", []).append(name) if name not in entry.get("aliases", []) else None
                self._register_aliases(entry)
                self.save()
            return "updated"

        data = self._http_bytes(url, cookie=cookie, timeout=timeout)
        digest = hashlib.sha1(data).hexdigest()
        suffix = _guess_suffix(url, data)
        target = self.images_dir / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(data)
        entry = {
            "id": digest,
            "file": target.name,
            "name": name,
            "unique": unique or f"remote_{name}",
            "source": url,
            "aliases": [],
        }
        with self._lock:
            self._register(entry)
        return "added"

    def add_remote(self, url: str, name: str, unique: str = "", *,
                   cookie: str = "", timeout: float = 20.0) -> tuple[dict, str]:
        """Download one emote image and register it (used by harvest mode)."""
        return_entry = None
        existing_key = self._by_unique.get(normalize_key(unique)) if unique else None
        if existing_key:
            entry = self._entries.get(existing_key)
            if entry and (self.images_dir / entry["file"]).exists():
                return entry, "updated"
        data = self._http_bytes(url, cookie=cookie, timeout=timeout)
        digest = hashlib.sha1(data).hexdigest()
        suffix = _guess_suffix(url, data)
        target = self.images_dir / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(data)
        entry = {
            "id": digest,
            "file": target.name,
            "name": name,
            "unique": unique or f"remote_{name}",
            "source": url,
            "aliases": [],
        }
        with self._lock:
            self._register(entry)
        return entry, "added"

    # -- HTTP helpers -----------------------------------------------------
    def _headers(self, cookie: str, referer: str = "https://live.bilibili.com/") -> dict:
        headers = {"User-Agent": DEFAULT_UA, "Referer": referer,
                   "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _http_json(self, url: str, cookie: str = "", timeout: float = 20.0) -> dict:
        req = urllib.request.Request(url, headers=self._headers(cookie))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_bytes(self, url: str, cookie: str = "", timeout: float = 20.0) -> bytes:
        if url.startswith("//"):
            url = "https:" + url
        req = urllib.request.Request(url, headers=self._headers(cookie))
        last: Exception | None = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except Exception as exc:  # transient CDN resets are common
                last = exc
        raise LibraryError(f"下载失败：{url}（{last}）")


def _guess_suffix(url: str, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp") else ".png"


def _vector_to_json(obj) -> dict:
    return {
        "width": obj.width,
        "height": obj.height,
        "shapes": [
            {"color": list(s.color), "alpha": s.alpha, "loops": s.loops} for s in obj.shapes
        ],
    }


def _vector_from_json(data: dict):
    from .vectorize import Shape, VectorEmote

    shapes = [
        Shape(color=tuple(s["color"]), alpha=s["alpha"],
              loops=[[(float(x), float(y)) for x, y in loop] for loop in s["loops"]],
              area=0)
        for s in data["shapes"]
    ]
    return VectorEmote(width=data["width"], height=data["height"], shapes=shapes)
