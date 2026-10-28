"""``emote-danmaku`` command line interface.

Sub-commands
------------
  library add      import local image files (or whole folders) into the library
  library fetch    download a live room's emote pack from Bilibili
  library list     inspect what the library can resolve
  library prune    drop cached vector traces / unused images
  convert          DanmakuFactory ASS -> ASS with inline emote drawings
  preview          render one PNG frame so you can check placement
  burn             one-shot: XML -> ASS -> mp4 with ffmpeg

``--library`` defaults to ``./emotes`` next to the project root.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .ass import (EMOTE_STYLE, AssDocument, RenderOptions, TextMetrics, read_ass,
                  transform, write_ass)
from .library import EmoteLibrary, LibraryError
from .vectorize import TraceError, trace_image


def _default_library() -> Path:
    env = os.environ.get("EMOTE_DANMAKU_LIBRARY")
    return Path(env) if env else Path.cwd() / "emotes"


def _library(args) -> EmoteLibrary:
    return EmoteLibrary(args.library or _default_library())


# --------------------------------------------------------------------------
# library commands
# --------------------------------------------------------------------------

def cmd_library_add(args) -> int:
    lib = _library(args)
    added = 0
    for target in args.paths:
        path = Path(target)
        files = ([path] if path.is_file()
                 else sorted(p for p in path.rglob("*")
                             if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")))
        if not files:
            print(f"跳过（没有图片）：{target}", file=sys.stderr)
            continue
        for f in files:
            name = args.name if (args.name and len(files) == 1) else None
            try:
                entry = lib.add_file(f, name=name, overwrite=args.overwrite)
            except (LibraryError, TraceError) as exc:
                print(f"  失败 {f.name}: {exc}", file=sys.stderr)
                continue
            if args.trace:
                try:
                    vec = lib.vector(entry, height=args.trace_height,
                                     max_colors=args.max_colors, tolerance=args.tolerance)
                    print(f"  {f.name} -> {entry['name']}  "
                          f"({len(vec.shapes)} 色块, {vec.height}px)")
                except TraceError as exc:
                    print(f"  失败 {f.name}: {exc}", file=sys.stderr)
                    continue
            else:
                print(f"  {f.name} -> {entry['name']}")
            added += 1
    print(f"完成：新增 {added} 个表情，表情库共 {len(lib)} 个")
    return 0


def cmd_library_fetch(args) -> int:
    lib = _library(args)
    cookie = args.cookie or os.environ.get("BILIBILI_COOKIE", "")
    if not cookie:
        # 批处理菜单调用时不带 --cookie，回退到项目 config.json
        cookie = _cookie_from_config()
    if not cookie:
        print("提示：未提供 --cookie / BILIBILI_COOKIE，B 站可能只返回通用表情。",
              file=sys.stderr)

    def progress(done, total, name):
        if done == total or done % 10 == 0:
            print(f"  下载 {done}/{total} {name}", file=sys.stderr)

    try:
        result = lib.fetch_room_pack(args.room, cookie=cookie, platform=args.platform,
                                     progress=progress if args.verbose else None)
    except LibraryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"完成：共 {result['total']} 个，新增 {result['added']}，"
          f"已存在 {result['updated']}，失败 {result['failed']}；"
          f"表情库共 {len(lib)} 个")
    return 0


def cmd_library_harvest(args) -> int:
    """Download every emote referenced by a recording's raw danmaku data."""
    from .xmlfile import harvest_emotes

    lib = _library(args)
    xml_path = Path(args.xml)
    if not xml_path.is_file():
        print(f"错误：找不到弹幕 XML {xml_path}", file=sys.stderr)
        return 2
    result = harvest_emotes(xml_path)
    if result.raw_comments == 0:
        print("错误：这个 XML 里没有 raw 原始数据，无法读取表情地址。\n"
              "      请在录播姬里打开“录制弹幕原始数据”后重新录制，\n"
              "      或用 library fetch --room <房间号> 直接抓直播间表情。",
              file=sys.stderr)
        return 2
    # only the emotes that actually appear as [token] in the comments
    wanted = {code: info for code, info in result.emotes.items()
              if args.all or code in result.tokens}
    if not wanted:
        print(f"没有找到表情弹幕（共 {result.total_comments} 条弹幕，"
              f"raw 数据 {result.raw_comments} 条，表情表 {len(result.emotes)} 个）。")
        return 0
    cookie = args.cookie or os.environ.get("BILIBILI_COOKIE", "")
    added = updated = failed = 0
    for i, (code, info) in enumerate(sorted(wanted.items()), 1):
        name = code.strip("[]")
        try:
            _, action = lib.add_remote(info.url, name, info.unique, cookie=cookie)
        except Exception as exc:
            failed += 1
            print(f"  [{i}/{len(wanted)}] {code} 失败：{exc}", file=sys.stderr)
            continue
        if action == "added":
            added += 1
        else:
            updated += 1
        if args.verbose or i == len(wanted):
            print(f"  [{i}/{len(wanted)}] {code} -> {name}")
    print(f"完成：新增 {added}，已存在 {updated}，失败 {failed}；"
          f"表情库共 {len(lib)} 个")
    return 0



def _cookie_from_config() -> str:
    """读项目 config.json 里的 cookie，供批处理菜单这类无参数调用使用。"""
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if not cfg_path.is_file():
            return ""
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return str(data.get("cookie") or "").strip()
    except Exception:  # noqa: BLE001 读不到就当没有
        return ""


def cmd_library_bem(args) -> int:
    """用 bem 把缺失的表情从 B 站表情库补齐。"""
    try:
        from .bem import BemError, fetch_missing
    except ImportError as exc:
        print(f"错误：无法加载 bem 桥接模块（{exc}）", file=sys.stderr)
        return 2

    lib = _library(args)
    cookie = args.cookie or os.environ.get("BILIBILI_COOKIE", "")

    # source 既可以是 XML，也可以直接是表情名
    tokens: list[str] = []
    for src in args.source:
        p = Path(src)
        if p.is_file() and p.suffix.lower() == ".xml":
            from .xmlfile import parse_xml

            found = set()
            for d in parse_xml(p):
                for m in re.finditer(r"\[[^\[\]\s]{1,40}\]", d.text or ""):
                    found.add(m.group(0))
            tokens.extend(sorted(found))
            print(f"{p.name}: 提取到 {len(found)} 种表情")
        else:
            tokens.append(src.strip())

    if not tokens:
        print("错误：没有可处理的输入", file=sys.stderr)
        return 2

    try:
        res = fetch_missing(
            tokens, lib, bem=args.bem or None, cookie=cookie,
            proxy=args.proxy or "", query=args.query or "", limit=args.limit,
            log=print, max_age=0 if args.refresh else 7 * 86400,
        )
    except BemError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print()
    print(f"新增     {len(res.added)}")
    for t in res.added:
        print(f"  + {t}")
    if res.existed:
        print(f"已存在   {len(res.existed)}（跳过）")
    if res.missing:
        print(f"B 站没有 {len(res.missing)}（跳过，压制时显示为文字）")
        for t in res.missing:
            print(f"  - {t}")
    if res.failed:
        print(f"下载失败 {len(res.failed)}")
        for t in res.failed:
            print(f"  ! {t}")
    print(f"\n表情库现在共 {len(lib)} 个")
    return 0 if not res.failed else 1


def cmd_library_list(args) -> int:
    lib = _library(args)
    entries = lib.entries()
    if args.json:
        print(json.dumps({"count": len(entries), "emotes": entries},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"表情库 {lib.root}  共 {len(entries)} 个")
    for entry in entries:
        flag = " " if (lib.images_dir / entry["file"]).exists() else "!"
        print(f" {flag} {entry.get('name') or '-':<16} {entry.get('unique') or '-':<18} "
              f"{entry['file']}")
    if any(not (lib.images_dir / e["file"]).exists() for e in entries):
        print("! 表示图片文件缺失，可用 library prune 清理")
    return 0


def cmd_library_prune(args) -> int:
    lib = _library(args)
    removed = 0
    if args.vectors or args.all:
        for f in lib.vector_dir.glob("*.json"):
            f.unlink()
            removed += 1
    if args.all:
        keep = {e["file"] for e in lib.entries()}
        for f in lib.images_dir.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink()
                removed += 1
    print(f"清理完成，删除 {removed} 个文件")
    return 0


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------

def _render_options(args) -> RenderOptions:
    return RenderOptions(
        height_ratio=args.emote_scale,
        side_bearing=getattr(args, "gap_tweak", 0.0),
        max_colors=args.max_colors,
        tolerance=args.tolerance,
        layer_boost=args.layer_boost,
        unknown=args.unknown,
    )


def cmd_convert(args) -> int:
    lib = _library(args)
    if not len(lib):
        print(f"错误：表情库是空的（{lib.root}）。先运行 library add / library fetch。",
              file=sys.stderr)
        return 2
    src = Path(args.ass)
    if not src.is_file():
        print(f"错误：找不到 ASS 文件 {src}", file=sys.stderr)
        return 2
    doc = read_ass(src)
    metrics = TextMetrics(args.font_dir)
    doc, stats = transform(doc, lib, metrics, _render_options(args))
    out = Path(args.output) if args.output else src.with_name(src.stem + ".emote.ass")
    write_ass(doc, out)

    print(f"已写入 {out}")
    print(f"  处理弹幕 {stats.events} 条，绘制表情 {stats.emotes} 个")
    if stats.replaced:
        top = sorted(stats.replaced.items(), key=lambda kv: -kv[1])[:8]
        print("  命中：" + "，".join(f"{k}×{v}" for k, v in top))
    if stats.missing:
        top = sorted(stats.missing.items(), key=lambda kv: -kv[1])[:15]
        print(f"  未收录 {stats.missing_total} 处：" + "，".join(f"{k}×{v}" for k, v in top))
        print("  提示：用 library fetch --room <房间号> 补充直播间表情，"
              "或 library add <图片> 手动补。")
    return 0


# --------------------------------------------------------------------------
# preview / burn
# --------------------------------------------------------------------------

def _ffmpeg(args) -> str:
    exe = args.ffmpeg or shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("错误：找不到 ffmpeg，请用 --ffmpeg 指定路径")
    return exe


def _ass_filter(path: Path) -> str:
    """Build an ``ass=`` filter value safe for ffmpeg's filtergraph parser.

    Windows paths contain a colon, which the filtergraph parser reads as an
    option separator, so it has to be escaped; backslashes are normalised.
    """
    p = str(path.resolve()).replace("\\", "/").replace(":", "\\:")
    return f"ass='{p}'"


def cmd_preview(args) -> int:
    ffmpeg = _ffmpeg(args)
    ass = Path(args.ass)
    if not ass.is_file():
        print(f"错误：找不到 ASS 文件 {ass}", file=sys.stderr)
        return 2
    doc = read_ass(ass)
    w, h = doc.play_res()
    out = Path(args.output) if args.output else Path("preview.png")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c=0x1e2430:s={w}x{h}:d={args.at + 1}:r=1",
           "-vf", _ass_filter(ass), "-ss", str(args.at),
           "-frames:v", "1", "-update", "1", str(out)]
    subprocess.run(cmd, check=True)
    print(f"已写入预览图 {out}（第 {args.at} 秒，{w}x{h}）")
    return 0


def cmd_burn(args) -> int:
    ffmpeg = _ffmpeg(args)
    ass = Path(args.ass)
    video = Path(args.video)
    for p in (ass, video):
        if not p.is_file():
            print(f"错误：找不到文件 {p}", file=sys.stderr)
            return 2
    out = Path(args.output) if args.output else video.with_name(video.stem + ".danmaku.mp4")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
           "-vf", _ass_filter(ass),
           "-c:v", args.vcodec, "-preset", args.preset, "-crf", str(args.crf),
           "-c:a", "copy"]
    if args.extra:
        cmd += args.extra.split()
    cmd.append(str(out))
    print("运行：" + " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"已写入 {out}")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="emote-danmaku",
        description="把录播姬弹幕里的 [表情] 变成真正显示出来的表情图片（纯 ASS 矢量，无需二次叠加）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"emote-danmaku {__version__}")
    p.add_argument("--library", help="表情库目录（默认 ./emotes）")
    sub = p.add_subparsers(dest="command", required=True)

    lib = sub.add_parser("library", help="管理本地表情库")
    libsub = lib.add_subparsers(dest="lib_command", required=True)

    add = libsub.add_parser("add", help="导入本地图片或文件夹")
    add.add_argument("paths", nargs="+", help="图片文件或包含图片的文件夹")
    add.add_argument("--name", help="重命名（仅单个文件时生效）")
    add.add_argument("--overwrite", action="store_true", help="覆盖同名条目")
    add.add_argument("--trace", action="store_true", help="立即矢量化为 ASS 并检查")
    add.add_argument("--trace-height", type=int, default=48, help="试矢量化高度（像素）")
    add.add_argument("--max-colors", type=int, default=12)
    add.add_argument("--tolerance", type=float, default=0.45)
    add.set_defaults(func=cmd_library_add)

    fetch = libsub.add_parser("fetch", help="从 B 站直播间下载表情包")
    fetch.add_argument("--room", type=int, required=True, help="直播间长号")
    fetch.add_argument("--cookie", help="B 站 Cookie（含 SESSDATA）")
    fetch.add_argument("--platform", default="pc", choices=("pc", "android", "ios"))
    fetch.add_argument("-v", "--verbose", action="store_true")
    fetch.set_defaults(func=cmd_library_fetch)

    lst = libsub.add_parser("list", help="列出表情库内容")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_library_list)

    harv = libsub.add_parser(
        "harvest", help="从录播姬弹幕 XML 的 raw 数据里抓出用到的表情")
    harv.add_argument("xml", help="录播姬导出的弹幕 XML")
    harv.add_argument("--all", action="store_true",
                      help="下载 raw 里出现的所有表情（默认只下弹幕实际用到的）")
    harv.add_argument("--cookie", help="B 站 Cookie（含 SESSDATA）")
    harv.add_argument("-v", "--verbose", action="store_true")
    harv.set_defaults(func=cmd_library_harvest)

    bem = libsub.add_parser(
        "bem", help="用 bem.py 从 B 站表情库补齐缺失的表情（需 biliemoji）")
    bem.add_argument("source", nargs="+",
                     help="弹幕 XML 路径，或直接写表情名（如 \"[doge_金箍]\"）")
    bem.add_argument("--cookie", help="B 站 Cookie（含 SESSDATA、bili_jct）")
    bem.add_argument("--bem", help="bem.py 路径（默认自动查找 tools/bem.py）")
    bem.add_argument("--query", help="只在这些表情包里找（按包名过滤），更快")
    bem.add_argument("--limit", type=int, default=0, help="最多扫多少个表情包")
    bem.add_argument("--proxy", help="如 http://127.0.0.1:7890")
    bem.add_argument("--refresh", action="store_true", help="忽略缓存，重新扫描")
    bem.set_defaults(func=cmd_library_bem)

    prune = libsub.add_parser("prune", help="清理缓存")
    prune.add_argument("--vectors", action="store_true", help="删除矢量缓存")
    prune.add_argument("--all", action="store_true", help="同时删除未引用的图片")
    prune.set_defaults(func=cmd_library_prune)

    conv = sub.add_parser("convert", help="ASS -> 内嵌表情的 ASS")
    conv.add_argument("ass", help="DanmakuFactory 输出的 .ass")
    conv.add_argument("-o", "--output", help="输出 .ass（默认 <name>.emote.ass）")
    conv.add_argument("--font-dir", help="字体目录（默认 C:\\Windows\\Fonts）")
    conv.add_argument("--emote-scale", type=float, default=1.30,
                      help="表情高度 / 字号（默认 1.30）")
    conv.add_argument("--gap-tweak", type=float, default=0.0,
                      help="表情与左侧文字的贴合微调，越大贴得越紧（默认 0.0）")
    conv.add_argument("--max-colors", type=int, default=10, help="矢量化色数上限")
    conv.add_argument("--tolerance", type=float, default=0.5, help="曲线简化容差")
    conv.add_argument("--layer-boost", type=int, default=1, help="表情事件提升的层数")
    conv.add_argument("--unknown", choices=("text", "drop"), default="text",
                      help="表情库里没有的表情如何处理")
    conv.set_defaults(func=cmd_convert)

    prev = sub.add_parser("preview", help="渲染一帧 PNG 检查位置")
    prev.add_argument("ass")
    prev.add_argument("-o", "--output", default="preview.png")
    prev.add_argument("--at", type=float, default=1.0, help="取第几秒")
    prev.add_argument("--ffmpeg")
    prev.set_defaults(func=cmd_preview)

    burn = sub.add_parser("burn", help="把 ASS 压制进视频")
    burn.add_argument("video")
    burn.add_argument("ass")
    burn.add_argument("-o", "--output")
    burn.add_argument("--ffmpeg")
    burn.add_argument("--vcodec", default="libx264")
    burn.add_argument("--preset", default="medium")
    burn.add_argument("--crf", type=int, default=20)
    burn.add_argument("--extra", help="附加给 ffmpeg 的参数（引号包裹）")
    burn.set_defaults(func=cmd_burn)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except LibraryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg 失败（{exc.returncode}）", file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
