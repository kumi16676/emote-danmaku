"""通过外部 ``bem.py`` 下载缺失的表情包。

``bem``（BiliEmojiDD 的命令行版）能列出 B 站全部表情包的逐条表情，
正好补上本项目「房间接口拿不到的表情」这一缺口。

它被设计成可被程序调用：``--json`` 时 stdout 只有 JSON，日志走 stderr，
退出码 0/1/2/3/4 分别表示成功/出错/未找到/部分失败/缺依赖。

本模块只做三件事：

1. 用 ``bem pkg <id>`` 拉取每个表情包的表情清单（名称 → 图片地址）；
2. 与本地表情库比对，找出缺失的那些；
3. 用 ``library.add_remote`` 下载并登记。

找不到的表情会被跳过并汇总输出，不会中断流程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: bem 的退出码含义（与其 docstring 一致）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_PARTIAL = 3
EXIT_DEP = 4


class BemError(RuntimeError):
    """bem 不可用或调用失败。"""


@dataclass
class BemEmote:
    """bem 返回的单条表情。"""
    text: str
    url: str
    gif_url: str | None = None
    package: str = ""
    package_id: int = 0


@dataclass
class FetchResult:
    """一次批量下载的结果。"""
    added: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # bem 里没有
    failed: list[str] = field(default_factory=list)    # 下载失败

    @property
    def total(self) -> int:
        return (len(self.added) + len(self.existed)
                + len(self.missing) + len(self.failed))


def find_bem(explicit: str | Path | None = None) -> Path:
    """定位 ``bem.py``。

    顺序：显式路径 → 环境变量 ``BEM_SCRIPT`` → 当前目录及若干常见位置。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("BEM_SCRIPT", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent.parent
    candidates += [
        here / "tools" / "bem.py",
        here / "bem.py",
        Path.home() / "bem.py",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise BemError(
        "找不到 bem.py。请把它放到 tools/bem.py，或用 --bem 指定路径，"
        "或设置环境变量 BEM_SCRIPT。\n"
        "bem 依赖：pip install biliemoji==2.0.0"
    )


def _run(bem: Path, args: list[str], *, cookie: str = "", timeout: float = 60.0,
         proxy: str = "") -> dict:
    """调用 bem 并解析它的 JSON 输出。"""
    cmd = [sys.executable, str(bem), "--json"]
    if cookie:
        cmd += ["--cookie", cookie]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += args
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise BemError(f"bem 超时（{timeout}s）") from exc

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode == EXIT_DEP:
        raise BemError("bem 缺少依赖，请先执行：pip install biliemoji==2.0.0")

    # 部分失败(3) 仍会输出 JSON，可以继续用
    if not out:
        raise BemError(f"bem 没有输出（退出码 {proc.returncode}）：{err[:300]}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise BemError(f"bem 输出不是 JSON：{out[:200]}") from exc

    if not payload.get("ok") and proc.returncode in (EXIT_ERROR, EXIT_NOT_FOUND):
        err_obj = payload.get("error") or {}
        msg = err_obj.get("message") or err
        hint = err_obj.get("hint")
        raise BemError(f"{msg}" + (f"\n提示：{hint}" if hint else ""))
    return payload


def list_packages(bem: Path, *, cookie: str = "", proxy: str = "",
                  query: str = "", limit: int = 0,
                  timeout: float = 120.0) -> list[dict]:
    """列出表情包（id + 名称）。需要 Cookie。"""
    args = ["list"]
    if query:
        args += ["-q", query]
    if limit:
        args += ["--limit", str(limit)]
    payload = _run(bem, args, cookie=cookie, proxy=proxy, timeout=timeout)
    return payload.get("items") or []


def package_emotes(bem: Path, package_id: int, *, cookie: str = "",
                   proxy: str = "", timeout: float = 120.0) -> list[BemEmote]:
    """取某个表情包里的全部表情。"""
    payload = _run(bem, ["pkg", str(package_id)], cookie=cookie, proxy=proxy,
                   timeout=timeout)
    name = str(payload.get("name") or "")
    out: list[BemEmote] = []
    for e in payload.get("emotes") or []:
        text = str(e.get("text") or "").strip()
        url = str(e.get("url") or "").strip()
        if not text or not url:
            continue
        out.append(BemEmote(text=text, url=url,
                            gif_url=(e.get("gif_url") or None),
                            package=name, package_id=int(package_id)))
    return out


def build_catalog(bem: Path, *, cookie: str = "", proxy: str = "",
                  query: str = "", limit: int = 0,
                  log=None, timeout: float = 120.0,
                  cache: Path | None = None,
                  max_age: float = 7 * 86400) -> dict[str, BemEmote]:
    """扫描表情包，建「表情名 → 表情」索引。

    B 站有七千多个表情包，逐个拉清单很慢，所以结果会缓存到磁盘
    （默认 ``~/.cache/emote-danmaku/bem_catalog.json``）。缓存过期或
    ``query`` 变化时重新扫描。``query`` 可只扫名字匹配的包，快得多。
    """
    cache_path = cache or default_cache_path()
    key = f"{query}|{limit}"
    cached = _load_cache(cache_path, key, max_age) if cache_path else None
    if cached is not None:
        if log:
            log(f"      使用表情库缓存（{len(cached)} 个），跳过联网扫描")
        return cached

    packages = list_packages(bem, cookie=cookie, proxy=proxy, query=query,
                             limit=limit, timeout=timeout)
    if log:
        log(f"      表情包 {len(packages)} 个，逐个读取表情清单…")

    catalog: dict[str, BemEmote] = {}
    for i, pkg in enumerate(packages, 1):
        pid = pkg.get("id")
        if pid is None:
            continue
        try:
            emotes = package_emotes(bem, int(pid), cookie=cookie, proxy=proxy,
                                    timeout=timeout)
        except BemError as exc:
            if log:
                log(f"      [{i}/{len(packages)}] 跳过包 {pkg.get('name')}：{exc}")
            continue
        for e in emotes:
            catalog.setdefault(e.text, e)
        if log and (i % 25 == 0 or i == len(packages)):
            log(f"      [{i}/{len(packages)}] 已收录 {len(catalog)} 个表情")

    if cache_path and catalog:
        _save_cache(cache_path, key, catalog)
        if log:
            log(f"      已缓存到 {cache_path}")
    return catalog


def default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or str(Path.home() / ".cache")
    return Path(base) / "emote-danmaku" / "bem_catalog.json"


def _load_cache(path: Path, key: str, max_age: float) -> dict[str, BemEmote] | None:
    try:
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > max_age:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("key") != key:
            return None
        items = raw.get("items") or {}
        out = {}
        for text, v in items.items():
            out[text] = BemEmote(text=text, url=v.get("url", ""),
                                 gif_url=v.get("gif_url") or None,
                                 package=v.get("package", ""),
                                 package_id=int(v.get("package_id") or 0))
        return out or None
    except Exception:  # noqa: BLE001 缓存坏了就当没有
        return None


def _save_cache(path: Path, key: str, catalog: dict[str, BemEmote]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "saved": int(time.time()),
            "items": {
                t: {"url": e.url, "gif_url": e.gif_url,
                    "package": e.package, "package_id": e.package_id}
                for t, e in catalog.items()
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 写缓存失败不影响主流程
        pass


def fetch_missing(tokens, library, *, bem: Path | None = None, cookie: str = "",
                  proxy: str = "", query: str = "", limit: int = 0,
                  log=None, timeout: float = 120.0,
                  max_age: float = 7 * 86400) -> FetchResult:
    """把 ``tokens`` 里本地缺失的表情从 B 站下载进表情库。

    ``tokens`` 是弹幕里出现的 ``[表情名]`` 列表。返回的 ``FetchResult``
    分四类：新增、本地已有、bem 里找不到（跳过）、下载失败。
    无论哪一类都不会抛异常打断压制流程。
    """
    result = FetchResult()
    todo: list[str] = []
    seen = set()
    for tok in tokens:
        key = str(tok).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if library.find(key) is not None:
            result.existed.append(key)
        else:
            todo.append(key)

    if not todo:
        return result

    path = find_bem(bem)
    if log:
        log(f"      本地缺失 {len(todo)} 个，正在查询 B 站表情库…")
    catalog = build_catalog(path, cookie=cookie, proxy=proxy, query=query,
                            limit=limit, log=log, timeout=timeout,
                            max_age=max_age)
    if log:
        log(f"      B 站表情库共 {len(catalog)} 个")

    for tok in todo:
        emote = catalog.get(tok)
        if emote is None:
            result.missing.append(tok)
            continue
        url = emote.gif_url or emote.url
        try:
            library.add_remote(url, emote.text,
                               unique=f"bem_{emote.package_id}_{emote.text}",
                               cookie=cookie, timeout=timeout)
            result.added.append(tok)
        except Exception as exc:  # noqa: BLE001 单个失败不该中断整批
            result.failed.append(tok)
            if log:
                log(f"      下载失败 {tok}：{exc}")
    return result
