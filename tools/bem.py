#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bem - B站表情包 / 收藏集 / 直播间专属表情 的命令行下载器。

BiliEmojiDD 的 CLI 版：接口层直接复用其依赖的 biliemoji SDK（PyPI 上的 2.0.0），
只把 GUI 换成命令行，因此 - 目录命名与 GUI 完全一致，已下载的文件会被自动跳过。

适合被其它程序调用：--json 时 stdout 只有 JSON，日志一律走 stderr，退出码可判。

依赖: pip install biliemoji==2.0.0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from biliemoji import (
        AuthRequired,
        BiliError,
        Downloader,
        DownloadStatus,
        DownloadTask,
        Dress,
        DressNotFound,
        Emoji,
        EmojiNotFound,
        NetworkError,
        NotFoundError,
        ValidationError,
    )
    from biliemoji.models import EmotePackage
    from biliemoji.sanitize import sanitize_filename
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖：请先执行  pip install biliemoji==2.0.0\n")
    raise SystemExit(4)

import requests

VERSION = "1.0.0"

BILI_REFERER = "https://www.bilibili.com/"
BILI_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.108 Safari/537.36"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
LIVE_EMOTICON_URL = "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
ANCHOR_INFO_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_PARTIAL = 3
EXIT_DEP = 4

JSON_MODE = False
QUIET = False


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def _safe_write(stream, text):
    try:
        stream.write(text)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(enc, "replace").decode(enc, "replace"))
    try:
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


def log(msg=""):
    if JSON_MODE:
        _safe_write(sys.stderr, msg + "\n")
    elif not QUIET:
        _safe_write(sys.stdout, msg + "\n")


def warn(msg):
    _safe_write(sys.stderr, msg + "\n")


def emit(obj):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:  # noqa: BLE001
        _safe_write(sys.stdout, text + "\n")


def fail(code, message, exit_code=EXIT_ERROR, **extra):
    if JSON_MODE:
        payload = {"ok": False, "error": {"code": code, "message": message}}
        payload["error"].update(extra)
        emit(payload)
    else:
        warn(f"错误: {message}")
        if extra.get("hint"):
            warn(f"提示: {extra['hint']}")
    return exit_code


def handle_exc(exc, what=""):
    """把 SDK 的异常翻成统一的退出码 + JSON 错误。"""
    prefix = f"{what}: " if what else ""
    if isinstance(exc, AuthRequired):
        return fail(
            "auth_required",
            prefix + "需要登录（Cookie 缺失或已失效）",
            EXIT_NOT_FOUND,
            hint="通过 --cookie / 环境变量 BILIEMOJI_COOKIE / --cookie-file 提供 SESSDATA 与 bili_jct",
        )
    if isinstance(exc, (EmojiNotFound, DressNotFound, NotFoundError)):
        return fail("not_found", prefix + str(exc), EXIT_NOT_FOUND)
    if isinstance(exc, NetworkError):
        return fail("network_error", prefix + str(exc), EXIT_ERROR)
    if isinstance(exc, ValidationError):
        return fail("invalid", prefix + str(exc), EXIT_NOT_FOUND)
    if isinstance(exc, BiliError):
        return fail("bili_error", prefix + str(exc), EXIT_ERROR)
    return fail("unknown", prefix + f"{type(exc).__name__}: {exc}", EXIT_ERROR)


# --------------------------------------------------------------------------
# 参数 / Cookie
# --------------------------------------------------------------------------


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return Path(base) / "biliemoji-dd"


def cookie_path() -> Path:
    return config_dir() / "cookie.txt"


def resolve_cookie(args) -> str:
    if getattr(args, "cookie", None):
        return args.cookie.strip()
    if getattr(args, "cookie_file", None):
        p = Path(args.cookie_file).expanduser()
        if not p.is_file():
            raise ValidationError(f"cookie 文件不存在: {p}")
        return p.read_text(encoding="utf-8").strip()
    env = os.environ.get("BILIEMOJI_COOKIE", "").strip()
    if env:
        return env
    p = cookie_path()
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


def cookie_source(args) -> str | None:
    if getattr(args, "cookie", None):
        return "arg"
    if getattr(args, "cookie_file", None):
        return "file"
    if os.environ.get("BILIEMOJI_COOKIE", "").strip():
        return "env"
    if cookie_path().is_file() and cookie_path().read_text(encoding="utf-8").strip():
        return "config"
    return None


def proxies_of(args):
    if not getattr(args, "proxy", None):
        return None
    return {"http": args.proxy, "https": args.proxy}


def make_session(args):
    s = requests.Session()
    s.headers.update({"User-Agent": BILI_WEB_UA, "Referer": BILI_REFERER})
    cookie = resolve_cookie(args)
    if cookie:
        s.headers["Cookie"] = cookie
    px = proxies_of(args)
    if px:
        s.proxies.update(px)
    return s


def make_emoji(args):
    return Emoji(cookie=resolve_cookie(args), proxies=proxies_of(args))


def make_dress(args):
    return Dress(cookie=resolve_cookie(args), proxies=proxies_of(args))


# --------------------------------------------------------------------------
# 目录命名：与 BiliEmojiDD 保持一致，保证两边共用同一份下载目录
# --------------------------------------------------------------------------


def _truncate(name: str, limit: int = 60) -> str:
    name = name or ""
    return name if len(name) <= limit else name[:limit]


def package_dir(out: Path, pkg) -> Path:
    return out / f"{_truncate(sanitize_filename(pkg.text or f'package_{pkg.id}'))} [{pkg.id}]"


def collection_dir(out: Path, name: str) -> Path:
    return out / sanitize_filename(name or "收藏集")


def live_dir(out: Path, room_name: str, room_id: str) -> Path:
    return out / f"{_truncate(sanitize_filename(room_name or f'直播间 {room_id}'))} [{room_id}]"


# --------------------------------------------------------------------------
# 直播间专属表情（biliemoji 2.0.0 不覆盖直播接口，沿用 BiliEmojiDD 的解析口径）
# --------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("http://") and ".hdslb.com" in url:
        return "https://" + url[len("http://") :]
    return url


def _iter_emoticons(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "emoticons" and isinstance(value, list):
                for em in value:
                    if isinstance(em, dict):
                        yield em
            else:
                yield from _iter_emoticons(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_emoticons(item)


def _image_ext(url: str) -> str:
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if suffix in (".gif", ".png", ".webp", ".jpg", ".jpeg") else ".png"


def fetch_live(session, room_id: str):
    resp = session.get(
        LIVE_EMOTICON_URL, params={"platform": "pc", "room_id": room_id}, timeout=15
    )
    resp.raise_for_status()
    payload = resp.json()
    code = payload.get("code")
    if code == -101:
        raise AuthRequired("直播间表情需要登录后获取", api_code=-101, url=LIVE_EMOTICON_URL)
    if code != 0:
        raise BiliError(f"直播间接口返回 code={code}: {payload.get('message') or payload.get('msg')}")

    prefix = f"room_{room_id}_"
    seen = set()
    emotes = []
    for raw in _iter_emoticons(payload.get("data")):
        unique = str(raw.get("emoticon_unique") or "")
        if not unique.startswith(prefix) or unique in seen:
            continue
        url = _normalize_url(str(raw.get("url") or ""))
        if not url:
            continue
        seen.add(unique)
        emotes.append(
            {"text": str(raw.get("emoji") or unique), "url": url, "unique": unique}
        )

    room_name, face = "", ""
    try:
        info = session.get(ROOM_INFO_URL, params={"room_id": room_id}, timeout=10).json()
        uid = (info.get("data") or {}).get("uid") if info.get("code") == 0 else None
        if uid:
            detail = session.get(ANCHOR_INFO_URL, params={"uid": uid}, timeout=10).json()
            if detail.get("code") == 0:
                base = (detail.get("data") or {}).get("info") or {}
                room_name = str(base.get("uname") or "")
                face = _normalize_url(str(base.get("face") or ""))
    except Exception:  # noqa: BLE001 展示信息拿不到不影响表情
        pass

    return room_name, face, emotes


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------


class Progress:
    def __init__(self):
        self.t0 = time.time()

    def __call__(self, done, total, result):
        if QUIET:
            return
        if sys.stderr.isatty():
            pct = (done * 100 // total) if total else 100
            _safe_write(sys.stderr, f"\r  下载中 {done}/{total} ({pct}%)")
            if done >= total:
                _safe_write(sys.stderr, "\n")
        elif done == total or done == 1 or done % 25 == 0:
            _safe_write(sys.stderr, f"  {done}/{total}\n")


def sum_up(results):
    ok = sum(1 for r in results if r.status is DownloadStatus.SUCCESS)
    skipped = sum(1 for r in results if r.status is DownloadStatus.SKIPPED)
    failed = [r for r in results if r.status is DownloadStatus.FAILED]
    return ok, skipped, failed


def report(results, out: Path, groups, t0: float, kind: str):
    ok, skipped, failed = sum_up(results)
    totals = {
        "files": len(results),
        "downloaded": ok,
        "skipped": skipped,
        "failed": len(failed),
        "elapsed": round(time.time() - t0, 2),
    }

    per_item = []
    for folder, meta in groups.items():
        mine = [r for r in results if r.target.parent == folder]
        a, b, c = sum_up(mine)
        per_item.append(
            {
                "kind": kind,
                "name": meta.get("name"),
                **({"id": meta["id"]} if "id" in meta else {}),
                "dir": str(folder),
                "files": len(mine),
                "downloaded": a,
                "skipped": b,
                "failed": len(c),
            }
        )

    if JSON_MODE:
        emit(
            {
                "ok": not failed,
                "out": str(out),
                "totals": totals,
                "items": per_item,
                "errors": [
                    {"target": str(r.target), "error": str(r.error)}
                    for r in failed[:50]
                ],
            }
        )
    else:
        for item in per_item:
            log(
                f"  {item['name']}  ->  {item['dir']}\n"
                f"     文件 {item['files']}，下载 {item['downloaded']}，"
                f"跳过 {item['skipped']}，失败 {item['failed']}"
            )
        log(
            f"完成: {totals['files']} 个文件，下载 {ok}，跳过 {skipped}，"
            f"失败 {len(failed)}，{totals['elapsed']}s"
        )
        for r in failed[:10]:
            warn(f"  失败: {r.target}  ({r.error})")
    return EXIT_PARTIAL if failed else EXIT_OK


def run_downloader(args, tasks, out, groups, kind, dry_run=False):
    t0 = time.time()
    if dry_run:
        planned = {}
        for t in tasks:
            planned[t.target.parent] = planned.get(t.target.parent, 0) + 1
        items = [
            {
                "kind": kind,
                "name": groups[f].get("name"),
                **({"id": groups[f]["id"]} if "id" in groups[f] else {}),
                "dir": str(f),
                "files": n,
                "planned": n,
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
            }
            for f, n in planned.items()
        ]
        if JSON_MODE:
            emit(
                {
                    "ok": True,
                    "dry_run": True,
                    "out": str(out),
                    "totals": {
                        "files": len(tasks),
                        "downloaded": 0,
                        "skipped": 0,
                        "failed": 0,
                        "elapsed": 0,
                    },
                    "items": items,
                    "errors": [],
                }
            )
        else:
            for it in items:
                log(f"  [dry-run] {it['dir']}  （{it['files']} 个文件）")
            log(f"预演: {len(groups)} 个包 / {len(tasks)} 个文件，未下载")
        return EXIT_OK

    downloader = Downloader(
        max_workers=max(1, args.jobs),
        timeout=args.timeout,
        proxies=proxies_of(args),
        on_progress=Progress(),
    )
    batch = downloader.download_many(tasks)
    return report(list(batch.results), out, groups, t0, kind)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_whoami(args):
    s = make_session(args)
    try:
        data = s.get(NAV_URL, timeout=15).json()
    except requests.RequestException as exc:
        return fail("network_error", f"请求失败: {exc}", EXIT_ERROR)

    if data.get("code") != 0 or not (data.get("data") or {}).get("isLogin"):
        if JSON_MODE:
            emit({"ok": True, "logged_in": False})
            return EXIT_OK
        return fail("not_logged_in", "当前 Cookie 未登录或已失效", EXIT_NOT_FOUND)

    d = data["data"]
    info = {
        "ok": True,
        "logged_in": True,
        "mid": d.get("mid"),
        "uname": d.get("uname"),
        "vip": (d.get("vipStatus") == 1),
        "cookie_source": cookie_source(args),
        "cookie_file": str(cookie_path()) if cookie_path().is_file() else None,
    }
    if JSON_MODE:
        emit(info)
    else:
        log(f"账号   {info['uname']} (mid {info['mid']})")
        log(f"大会员 {'是' if info['vip'] else '否'}")
    return EXIT_OK


def cmd_list(args):
    emoji = make_emoji(args)
    if not resolve_cookie(args):
        return fail(
            "auth_required",
            "「全部表情包」需要登录 Cookie",
            EXIT_NOT_FOUND,
            hint="用 --cookie 传 SESSDATA=...; bili_jct=...，或写进 "
            + str(cookie_path()),
        )
    try:
        packages = emoji.all_packages()
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, "拉取全部表情包")

    kw = (args.query or "").strip().lower()
    if kw:
        packages = [
            p for p in packages
            if kw in (p.text or "").lower() or kw == str(p.id)
        ]
    if args.limit:
        packages = packages[: args.limit]

    if args.ids:
        for p in packages:
            log(str(p.id))
        return EXIT_OK if packages else EXIT_NOT_FOUND

    if JSON_MODE:
        emit(
            {
                "ok": True,
                "count": len(packages),
                "items": [
                    {"id": p.id, "name": p.text, "cover": p.url} for p in packages
                ],
            }
        )
        return EXIT_OK

    for p in packages:
        log(f"{p.id:>7}  {p.text}")
    log(f"-- 共 {len(packages)} 个")
    return EXIT_OK


def cmd_pkg(args):
    emoji = make_emoji(args)
    try:
        pkg = emoji.certain_emoji_typed(int(args.id))
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, f"表情包 {args.id}")

    emotes = [e for e in pkg.emote if e.url]
    if JSON_MODE:
        emit(
            {
                "ok": True,
                "id": pkg.id,
                "name": pkg.text,
                "cover": pkg.url,
                "is_gif": bool(getattr(pkg, "is_gif", False)),
                "emote_count": len(emotes),
                "emotes": [
                    {"text": e.text, "url": e.url, "gif_url": e.gif_url} for e in emotes
                ],
            }
        )
        return EXIT_OK

    log(f"id       {pkg.id}")
    log(f"名称     {pkg.text}")
    log(f"表情数   {len(emotes)}")
    log(f"含动图   {'是' if getattr(pkg, 'is_gif', False) else '否'}")
    for e in emotes[:20]:
        log(f"  {e.text}  {e.url}")
    if len(emotes) > 20:
        log(f"  ... 其余 {len(emotes) - 20} 个")
    return EXIT_OK


def cmd_live(args):
    s = make_session(args)
    if not resolve_cookie(args):
        return fail(
            "auth_required",
            "直播间专属表情需要登录 Cookie",
            EXIT_NOT_FOUND,
            hint="直播间接口匿名返回 -101，请提供 Cookie",
        )
    try:
        room_name, face, emotes = fetch_live(s, str(args.room_id).strip())
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, f"直播间 {args.room_id}")

    if JSON_MODE:
        emit(
            {
                "ok": True,
                "room_id": str(args.room_id),
                "room_name": room_name,
                "anchor_face": face,
                "count": len(emotes),
                "emotes": emotes,
            }
        )
        return EXIT_OK if emotes else EXIT_NOT_FOUND

    log(f"直播间   {room_name or args.room_id}  (room {args.room_id})")
    log(f"专属表情 {len(emotes)} 个")
    for e in emotes[:20]:
        log(f"  {e['text']}  {e['url']}")
    if len(emotes) > 20:
        log(f"  ... 其余 {len(emotes) - 20} 个")
    return EXIT_OK if emotes else EXIT_NOT_FOUND


def _act_lottery(summary):
    props = summary.raw.get("properties") or {}
    act = props.get("dlc_act_id") or summary.dlc_act_id
    lot = props.get("dlc_lottery_id") or summary.dlc_lottery_id
    try:
        return int(act), int(lot) if lot is not None else None
    except (TypeError, ValueError):
        return None, None


def cmd_search(args):
    dress = make_dress(args)
    try:
        hits = dress.search_dress_typed(args.num, keyword=args.keyword or "")
    except DressNotFound as exc:
        if JSON_MODE:
            emit({"ok": True, "count": 0, "items": []})
            return EXIT_NOT_FOUND
        return handle_exc(exc, "搜索收藏集")
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, "搜索收藏集")

    items = []
    for s in hits:
        act, lot = _act_lottery(s)
        items.append(
            {
                "name": s.name,
                "act_id": act,
                "lottery_id": lot,
                "item_id": s.raw.get("item_id"),
                "state": s.raw.get("state"),
                "cover": s.image_cover,
            }
        )

    if JSON_MODE:
        emit({"ok": True, "count": len(items), "items": items})
        return EXIT_OK if items else EXIT_NOT_FOUND

    for it in items:
        log(f"{it['name']}")
        log(f"    act={it['act_id']} lottery={it['lottery_id']} state={it['state']}")
    log(f"-- 命中 {len(items)} 个")
    return EXIT_OK if items else EXIT_NOT_FOUND


def cmd_collection(args):
    dress = make_dress(args)
    try:
        coll = dress.certain_lottery_typed(int(args.act_id), int(args.lottery_id))
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, "收藏集")

    items = [
        {
            "card_id": it.card_id,
            "name": it.card_name,
            "image": it.card_img_download,
            "videos": list(it.video_list),
        }
        for it in coll.item_list
    ]
    if JSON_MODE:
        emit({"ok": True, "name": coll.name, "count": len(items), "items": items})
        return EXIT_OK

    log(f"收藏集   {coll.name}")
    log(f"条目     {len(items)}")
    img = sum(1 for i in items if i["image"])
    vid = sum(1 for i in items if i["videos"])
    log(f"图片 {img}  视频 {vid}")
    return EXIT_OK


def _read_ids(raw_ids):
    ids = []
    for item in raw_ids:
        if item == "-":
            ids.extend(x.strip() for x in sys.stdin.read().splitlines() if x.strip())
        else:
            ids.append(item.strip())
    out = []
    for x in ids:
        if x.isdigit():
            out.append(int(x))
        else:
            raise ValidationError(f"表情包 id 必须是数字: {x}")
    return out


def cmd_get_pkg(args):
    ids = _read_ids(args.ids)
    if not ids:
        return fail("no_target", "没有指定表情包 id", EXIT_NOT_FOUND)

    emoji = make_emoji(args)
    out = Path(args.out).expanduser().resolve()
    tasks, groups = [], {}

    for pid in ids:
        try:
            pkg = emoji.certain_emoji_typed(pid)
        except Exception as exc:  # noqa: BLE001
            if len(ids) == 1:
                return handle_exc(exc, f"表情包 {pid}")
            warn(f"跳过 {pid}: {exc}")
            continue

        use_gif = pkg.is_gif if args.gif is None else args.gif
        folder = package_dir(out, pkg)
        groups[folder] = {"id": pid, "name": pkg.text}
        for em in pkg.emote:
            if use_gif and em.gif_url:
                url, ext = em.gif_url, ".gif"
            elif em.url:
                url, ext = em.url, ".png"
            else:
                continue
            tasks.append(
                DownloadTask(
                    url=url,
                    target=folder / f"{_truncate(sanitize_filename(em.text))}{ext}",
                    expected_ext=ext,
                    overwrite=args.overwrite,
                )
            )

    if not groups:
        return fail("not_found", "没有可下载的表情包", EXIT_NOT_FOUND)

    log(f"共 {len(groups)} 个表情包 / {len(tasks)} 个文件 -> {out}")
    return run_downloader(args, tasks, out, groups, "package", args.dry_run)


def cmd_get_live(args):
    s = make_session(args)
    room_id = str(args.room_id).strip()
    try:
        room_name, _, emotes = fetch_live(s, room_id)
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, f"直播间 {room_id}")

    if not emotes:
        return fail("empty", f"直播间 {room_id} 没有专属表情", EXIT_NOT_FOUND)

    out = Path(args.out).expanduser().resolve()
    folder = live_dir(out, room_name, room_id)
    groups = {folder: {"id": room_id, "name": room_name or f"直播间 {room_id}"}}
    tasks = [
        DownloadTask(
            url=e["url"],
            target=folder / f"{_truncate(sanitize_filename(e['text']))}{_image_ext(e['url'])}",
            expected_ext=_image_ext(e["url"]),
            overwrite=args.overwrite,
        )
        for e in emotes
    ]
    log(f"{room_name or room_id}: {len(tasks)} 个专属表情 -> {out}")
    return run_downloader(args, tasks, out, groups, "live", args.dry_run)


def cmd_get_collection(args):
    dress = make_dress(args)
    try:
        coll = dress.certain_lottery_typed(int(args.act_id), int(args.lottery_id))
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc, "收藏集")

    out = Path(args.out).expanduser().resolve()
    folder = collection_dir(out, coll.name)
    groups = {folder: {"id": f"{args.act_id}/{args.lottery_id}", "name": coll.name}}

    tasks = []
    for item in coll.item_list:
        name = _truncate(sanitize_filename(item.card_name or "unknown"))
        if args.mode in ("image", "both") and item.card_img_download:
            tasks.append(
                DownloadTask(
                    url=item.card_img_download,
                    target=folder / f"{name}.png",
                    expected_ext=".png",
                    overwrite=args.overwrite,
                )
            )
        if args.mode in ("video", "both") and item.video_list:
            tasks.append(
                DownloadTask(
                    url=item.video_list[0],
                    target=folder / f"{name}.mp4",
                    expected_ext=".mp4",
                    overwrite=args.overwrite,
                )
            )

    if not tasks:
        return fail("empty", f"收藏集「{coll.name}」没有可下载内容", EXIT_NOT_FOUND)

    log(f"{coll.name}: {len(tasks)} 个文件（mode={args.mode}）-> {out}")
    return run_downloader(args, tasks, out, groups, "collection", args.dry_run)


def cmd_version(args):
    if JSON_MODE:
        emit({"ok": True, "version": VERSION})
    else:
        log(f"bem {VERSION}")
    return EXIT_OK


def cmd_cookie(args):
    p = cookie_path()
    if args.save:
        value = args.save.strip()
        if not value:
            return fail("invalid", "cookie 不能为空", EXIT_NOT_FOUND)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding="utf-8")
        log(f"已写入 {p}")
        return EXIT_OK

    current = resolve_cookie(args)
    info = {
        "ok": True,
        "path": str(p),
        "exists": p.is_file(),
        "configured": bool(current),
        "preview": (current[:12] + "...") if current else "",
    }
    if JSON_MODE:
        emit(info)
    else:
        log(f"配置文件  {info['path']}")
        log(f"已存在    {'是' if info['exists'] else '否'}")
        log(f"当前可用  {'是' if info['configured'] else '否（可用 --save 写入）'}")
    return EXIT_OK


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def add_common(p):
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="stdout 只输出 JSON")
    p.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="不打印进度")
    p.add_argument("--cookie", default=argparse.SUPPRESS, help="Cookie 字符串")
    p.add_argument("--cookie-file", default=argparse.SUPPRESS, help="从文件读 Cookie")
    p.add_argument("--proxy", default=argparse.SUPPRESS, help="如 http://127.0.0.1:7890")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="请求超时秒")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    add_common(common)
    p = argparse.ArgumentParser(
        prog="bem",
        parents=[common],
        description="B站表情包 / 收藏集 / 直播间专属表情 命令行下载器（BiliEmojiDD 的 CLI 版）",
        epilog=(
            "退出码: 0 成功 | 1 出错 | 2 未找到/需登录 | 3 部分文件失败 | 4 缺依赖\n"
            "Cookie 优先级: --cookie > --cookie-file > 环境变量 BILIEMOJI_COOKIE > 配置文件\n"
            "示例:\n"
            "  bem whoami\n"
            "  bem list -q 洛天依\n"
            "  bem list --ids | bem get pkg - -o ./downloads\n"
            "  bem pkg 9 --json\n"
            "  bem live 5236391 -o ./downloads\n"
            "  bem search 洛天依\n"
            "  bem get collection 102550 102551 -o ./downloads --mode both\n"
            "  bem --json get pkg 1 9 -o ./downloads"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(
        json=False, quiet=False, cookie=None, cookie_file=None, proxy=None, timeout=20.0
    )

    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    sp = sub.add_parser("whoami", parents=[common], help="检查 Cookie 与登录状态")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("list", parents=[common], help="全部表情包（需 Cookie）")
    sp.add_argument("-q", "--query", help="按包名/ID 过滤")
    sp.add_argument("--ids", action="store_true", help="只输出 id，便于管道")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("pkg", parents=[common], help="表情包详情")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_pkg)

    sp = sub.add_parser("live", parents=[common], help="直播间专属表情（需 Cookie）")
    sp.add_argument("room_id")
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("search", parents=[common], help="搜索收藏集 / 装扮")
    sp.add_argument("keyword")
    sp.add_argument("--num", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("collection", parents=[common], help="收藏集详情")
    sp.add_argument("act_id")
    sp.add_argument("lottery_id")
    sp.set_defaults(func=cmd_collection)

    sp = sub.add_parser("cookie", parents=[common], help="查看/保存 Cookie")
    sp.add_argument("--save", help="把 Cookie 写入配置文件")
    sp.set_defaults(func=cmd_cookie)

    sp = sub.add_parser("version", parents=[common], help="显示版本")
    sp.set_defaults(func=cmd_version)

    get = sub.add_parser("get", parents=[common], help="下载")
    gsub = get.add_subparsers(dest="target", metavar="<target>")

    gp = gsub.add_parser("pkg", parents=[common], help="下载表情包")
    gp.add_argument("ids", nargs="+", help="一个或多个 id，- 表示从 stdin 读")
    gp.add_argument("-o", "--out", default="./downloads")
    gp.add_argument("-j", "--jobs", type=int, default=8)
    gp.add_argument("--gif", dest="gif", action="store_true", default=None, help="优先下动图")
    gp.add_argument("--no-gif", dest="gif", action="store_false", help="只用静态图")
    gp.add_argument("--overwrite", action="store_true")
    gp.add_argument("--dry-run", action="store_true")
    gp.set_defaults(func=cmd_get_pkg)

    gl = gsub.add_parser("live", parents=[common], help="下载直播间专属表情")
    gl.add_argument("room_id")
    gl.add_argument("-o", "--out", default="./downloads")
    gl.add_argument("-j", "--jobs", type=int, default=8)
    gl.add_argument("--overwrite", action="store_true")
    gl.add_argument("--dry-run", action="store_true")
    gl.set_defaults(func=cmd_get_live)

    gc = gsub.add_parser("collection", parents=[common], help="下载收藏集")
    gc.add_argument("act_id")
    gc.add_argument("lottery_id")
    gc.add_argument("-o", "--out", default="./downloads")
    gc.add_argument("-j", "--jobs", type=int, default=8)
    gc.add_argument("--mode", choices=("image", "video", "both"), default="image")
    gc.add_argument("--overwrite", action="store_true")
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=cmd_get_collection)

    return p


GLOBAL_OPTS = {
    "--json": 0,
    "--quiet": 0,
    "--cookie": 1,
    "--cookie-file": 1,
    "--proxy": 1,
    "--timeout": 1,
}


def split_globals(argv):
    """先把全局参数摘出来，再把剩下的交给 argparse。

    argparse 的子解析器会用自身默认值覆盖父级解析结果（子命名空间整体拷回父级），
    所以 `bem --json get pkg 9` 里的 --json 原本会被子命令的默认值冲掉。这里提前
    摘走全局参数，就不受参数写在子命令前面还是后面影响了。
    """
    rest, vals = [], {}
    i = 0
    while i < len(argv):
        token = argv[i]
        key = token.split("=", 1)[0]
        if key in GLOBAL_OPTS:
            if "=" in token:
                vals[key] = token.split("=", 1)[1]
            elif GLOBAL_OPTS[key]:
                if i + 1 >= len(argv):
                    raise ValidationError(f"{key} 缺少值")
                vals[key] = argv[i + 1]
                i += 1
            else:
                vals[key] = True
        else:
            rest.append(token)
        i += 1
    return rest, vals


def main(argv=None):
    global JSON_MODE, QUIET
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        argv, globals_ = split_globals(argv)
    except ValidationError as exc:
        warn(f"错误: {exc}")
        return EXIT_NOT_FOUND

    parser = build_parser()
    args = parser.parse_args(argv)

    if globals_.get("--json"):
        args.json = True
    if globals_.get("--quiet"):
        args.quiet = True
    if globals_.get("--cookie"):
        args.cookie = globals_["--cookie"]
    if globals_.get("--cookie-file"):
        args.cookie_file = globals_["--cookie-file"]
    if globals_.get("--proxy"):
        args.proxy = globals_["--proxy"]
    if globals_.get("--timeout"):
        try:
            args.timeout = float(globals_["--timeout"])
        except ValueError:
            warn("错误: --timeout 必须是数字")
            return EXIT_NOT_FOUND

    JSON_MODE = bool(getattr(args, "json", False))
    QUIET = bool(getattr(args, "quiet", False))

    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_NOT_FOUND
    if args.cmd == "get" and not getattr(args, "target", None):
        warn("用法: bem get {pkg|live|collection} ...   （bem get --help 查看详情）")
        return EXIT_NOT_FOUND

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    try:
        return args.func(args)
    except KeyboardInterrupt:
        warn("已中断")
        return EXIT_ERROR
    except ValidationError as exc:
        return fail("invalid", str(exc), EXIT_NOT_FOUND)
    except Exception as exc:  # noqa: BLE001
        return handle_exc(exc)


if __name__ == "__main__":
    sys.exit(main())
