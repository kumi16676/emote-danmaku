"""一条命令跑完整条流水线：录播姬 XML -> DanmakuFactory ASS -> 表情 ASS -> mp4。

这是给「标准模式录制、不开保留原始弹幕数据」的用户准备的主入口：
没有 ``raw`` 数据也能用，表情完全来自本地表情库。

用法
----
    # 1. 先看看这场直播用了哪些表情、本地库能覆盖多少
    python auto_pipeline.py report "D:\\录播\\xxx.xml"

    # 2. 建表情库（三种来源，可混用）
    python auto_pipeline.py fetch  --room 12345 --cookie "SESSDATA=..."   # 整个直播间表情包
    python auto_pipeline.py import "D:\\我的表情"                              # 本地图片，文件名=表情名
    python auto_pipeline.py harvest "D:\\录播\\xxx.xml"                       # 仅当开了 raw 数据

    # 3. 一键出片（找不到视频就只生成 ASS 和预览图）
    python auto_pipeline.py run "D:\\录播\\xxx.xml" -o "D:\\out\\xxx.mp4"

常用可选项
----------
    --video PATH     指定视频；不给时在同目录找同名 .flv/.mp4
    --ass PATH       已有 DanmakuFactory 生成的 ASS，跳过转换
    --keep-ass       保留中间 ASS（默认也保留，便于复用）
    --preview 秒数   额外导出一张预览图，默认 60
    --crf / --preset 编码参数
    --danmaku-factory PATH   DanmakuFactory.exe 路径
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from emote_danmaku.ass import RenderOptions, TextMetrics, read_ass, transform  # noqa: E402
from emote_danmaku.library import EmoteLibrary  # noqa: E402
from emote_danmaku.xmlfile import harvest_emotes, parse_xml  # noqa: E402

DEFAULT_DF = ROOT / "tools" / "DanmakuFactory" / "DanmakuFactory.exe"
VIDEO_SUFFIXES = (".flv", ".mp4", ".mkv", ".ts", ".m4v")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def which_ffmpeg(explicit: str | None = None) -> str:
    exe = explicit or shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("错误：找不到 ffmpeg，请用 --ffmpeg 指定，或把它加进 PATH")
    return exe


def ass_filter(path: Path) -> str:
    """ffmpeg 滤镜里的路径：盘符冒号要转义，反斜杠换正斜杠。"""
    p = str(path.resolve()).replace("\\", "/").replace(":", "\\:")
    return f"ass='{p}'"


def run_cmd(cmd: list[str], label: str = "") -> None:
    if label:
        log(f"  $ {label}")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1500:]
        raise SystemExit(f"命令失败（{proc.returncode}）：\n{' '.join(cmd)}\n{tail}")


def probe_video(ffmpeg: str, video: Path) -> tuple[int, int, float]:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe"))
    if not Path(ffprobe).exists():
        ffprobe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"ffprobe 读不出视频信息：{video}")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    return int(stream.get("width") or 0), int(stream.get("height") or 0), duration


def find_video(xml_path: Path) -> Path | None:
    """录播姬的弹幕和视频同名、同目录，只是扩展名不同。"""
    for suffix in VIDEO_SUFFIXES:
        candidate = xml_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def cmd_report(args) -> int:
    xml_path = Path(args.xml)
    if not xml_path.is_file():
        raise SystemExit(f"找不到弹幕文件：{xml_path}")
    items = parse_xml(xml_path)
    tokens = collections.Counter(t for i in items for t in i.tokens())
    lib = EmoteLibrary(args.library or ROOT / "emotes")
    covered = {t: lib.find(t) is not None for t in tokens}
    hit = sum(c for t, c in tokens.items() if covered[t])
    total = sum(tokens.values())

    log(f"弹幕文件   {xml_path.name}")
    log(f"弹幕条数   {len(items)}")
    if items:
        span = items[-1].time - items[0].time
        log(f"时间跨度   {span / 60:.1f} 分钟（{items[0].time:.1f}s ~ {items[-1].time:.1f}s）")
    log(f"表情 token {len(tokens)} 种，共出现 {total} 次")
    if total:
        log(f"本地可覆盖 {hit}/{total} 处（{hit / total:.0%}），"
            f"{sum(1 for v in covered.values() if v)}/{len(tokens)} 种")
    else:
        log("这场没有表情弹幕")

    if tokens:
        log("\n出现最多的表情：")
        for token, count in tokens.most_common(15):
            mark = "OK " if covered[token] else "-- "
            log(f"  {mark}{token:<28} x{count}")
    missing = [t for t, c in covered.items() if not c]
    if missing:
        log(f"\n缺失 {len(missing)} 种。补齐方式：")
        log("  python auto_pipeline.py fetch --room <房间号> --cookie \"SESSDATA=...\"")
        log("  python auto_pipeline.py import <图片文件夹>   # 文件名即表情名，如 dog.png")
        log("  （开了“录制弹幕原始数据”时可用 harvest 自动下载）")

    h = harvest_emotes(xml_path)
    if h.raw_comments == 0:
        log("\n注：本文件没有 raw 原始数据，harvest 不可用（标准模式录制的正常现象）")
    return 0


# --------------------------------------------------------------------------
# library subcommands
# --------------------------------------------------------------------------

def cmd_fetch(args) -> int:
    lib = EmoteLibrary(args.library or ROOT / "emotes")
    cookie = args.cookie or os.environ.get("BILIBILI_COOKIE", "")
    if not cookie:
        log("提示：未提供 cookie，B 站会返回“账号未登录”，拉不到房间表情包。")
    try:
        res = lib.fetch_room_pack(args.room, cookie=cookie, platform=args.platform)
    except Exception as exc:
        raise SystemExit(f"拉取失败：{exc}")
    log(f"新增 {res['added']}，已存在 {res['updated']}，失败 {res['failed']}；"
        f"表情库共 {len(lib)} 个")
    return 0


def cmd_import(args) -> int:
    lib = EmoteLibrary(args.library or ROOT / "emotes")
    added = 0
    for target in args.paths:
        p = Path(target)
        files = ([p] if p.is_file()
                 else sorted(f for f in p.rglob("*")
                             if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")))
        for f in files:
            try:
                lib.add_file(f, overwrite=args.overwrite)
                added += 1
            except Exception as exc:
                log(f"  跳过 {f.name}: {exc}")
    log(f"导入 {added} 个；表情库共 {len(lib)} 个")
    return 0


def cmd_harvest(args) -> int:
    xml_path = Path(args.xml)
    h = harvest_emotes(xml_path)
    if h.raw_comments == 0:
        raise SystemExit("这个 XML 没有 raw 原始数据（标准模式录制的正常现象）。\n"
                         "请改用 fetch --room <房间号> 或 import <图片文件夹>。")
    lib = EmoteLibrary(args.library or ROOT / "emotes")
    wanted = {t: e for t, e in h.emotes.items() if args.all or t in h.tokens}
    added = failed = 0
    for token, info in sorted(wanted.items()):
        try:
            _, action = lib.add_remote(info.url, token.strip("[]"), info.unique)
            added += 1 if action == "added" else 0
        except Exception as exc:
            failed += 1
            log(f"  失败 {token}: {exc}")
    log(f"新增 {added}，失败 {failed}；表情库共 {len(lib)} 个")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def run_danmaku_factory(df: Path, xml: Path, out_ass: Path, width: int, height: int,
                        scroll_time: float, fixed_time: float) -> None:
    """Run DanmakuFactory, working around its non-ASCII path bug.

    DanmakuFactory 1.70 decodes argv via the ANSI code page, so Chinese paths
    arrive mangled and it reports ``ERROR [code wa100] Failed to create file``.
    ASCII temp names are the reliable fix; the result is moved afterwards.
    """
    base = [str(df), "-o", "ass", "", "-i", "xml", "",
            "-r", f"{width}x{height}", "-s", str(scroll_time),
            "-f", str(fixed_time), "--ignore-warnings", "--force"]
    if is_ascii_path(xml) and is_ascii_path(out_ass):
        base[3] = str(out_ass)
        base[6] = str(xml)
        run_cmd(base)
        return

    # plain mkdir: tempfile.mkdtemp creates 0700 dirs that some sandboxes refuse
    scratch = ROOT / f"_df_tmp_{os.getpid()}_{int(time.time() * 1000) % 100000}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        tmp_xml = scratch / "input.xml"
        tmp_ass = scratch / "output.ass"
        shutil.copyfile(xml, tmp_xml)
        base[3] = str(tmp_ass)
        base[6] = str(tmp_xml)
        run_cmd(base)
        if not tmp_ass.exists():
            raise SystemExit("ASS 生成失败（DanmakuFactory 未输出文件）。")
        out_ass.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_ass), str(out_ass))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def cmd_run(args) -> int:
    started = time.time()
    xml_path = Path(args.xml)
    if not xml_path.is_file():
        raise SystemExit(f"找不到弹幕文件：{xml_path}")

    lib = EmoteLibrary(args.library or ROOT / "emotes")
    if not len(lib):
        raise SystemExit(
            "表情库是空的，先建库再跑：\n"
            "  python auto_pipeline.py import <图片文件夹>\n"
            "  python auto_pipeline.py fetch --room <房间号> --cookie \"SESSDATA=...\"\n"
            "  python auto_pipeline.py harvest <弹幕.xml>   # 仅当开了 raw 数据")

    video = Path(args.video) if args.video else find_video(xml_path)
    ffmpeg = which_ffmpeg(args.ffmpeg)
    work = Path(args.workdir) if args.workdir else xml_path.parent
    work.mkdir(parents=True, exist_ok=True)
    stem = xml_path.stem

    # 1) XML -> ASS
    if args.ass:
        ass_plain = Path(args.ass)
        if not ass_plain.is_file():
            raise SystemExit(f"找不到 ASS：{ass_plain}")
        log(f"[1/4] 复用已有 ASS：{ass_plain.name}")
        width = height = 0
        duration = 0.0
    else:
        df = Path(args.danmaku_factory) if args.danmaku_factory else DEFAULT_DF
        if not df.exists():
            raise SystemExit(
                f"找不到 DanmakuFactory：{df}\n"
                "下载 DanmakuFactory-dev-windows-x64-CLI.zip 解压到 tools/DanmakuFactory/，"
                "或用 --danmaku-factory 指定路径")
        if video:
            width, height, duration = probe_video(ffmpeg, video)
            if not (width and height):
                width, height = 1920, 1080
        else:
            width, height, duration = 1920, 1080, 0.0
        ass_plain = work / f"{stem}.danmaku.ass"
        log(f"[1/4] DanmakuFactory：XML -> ASS（{width}x{height}）")
        run_danmaku_factory(df, xml_path, ass_plain, width, height,
                            args.scroll_time, args.fixed_time)

    # 2) ASS -> 表情 ASS
    doc = read_ass(ass_plain)
    doc, stats = transform(doc, lib, TextMetrics(args.font_dir),
                           RenderOptions(height_ratio=args.emote_scale,
                                         baseline_drop=args.baseline_drop,
                                         slot_pad=args.slot_pad,
                                         max_colors=args.max_colors,
                                         tolerance=args.tolerance))
    ass_emote = work / f"{stem}.emote.ass"
    ass_emote.write_text(doc.to_text(), encoding="utf-8-sig")
    log(f"[2/4] 表情 ASS：{ass_emote.name}")
    log(f"      处理弹幕 {stats.events} 条，绘制表情 {stats.emotes} 个"
        f"（{stats.layers} 个色层）")
    if stats.missing:
        ranked = sorted(stats.missing.items(), key=lambda kv: (-kv[1], kv[0]))
        width = max(len(t) for t, _ in ranked)
        log(f"      未收录 {len(ranked)} 种，共 {stats.missing_total} 处（全部列出）：")
        for token, count in ranked:
            log(f"        {token:<{width}}  x{count}")

    # 3) 预览图
    previews: list[Path] = []
    pw, ph = doc.play_res()
    for at in [args.preview]:
        png = work / f"{stem}.preview.{int(at)}s.png"
        log(f"[3/4] 预览图：第 {at}s -> {png.name}")
        run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"color=c=0x101820:s={pw}x{ph}:d={int(at) + 1}:r=1",
                 "-vf", ass_filter(ass_emote), "-ss", str(at),
                 "-frames:v", "1", "-update", "1", str(png)])
        previews.append(png)

    # 4) 压制
    if not video:
        log("[4/4] 没找到同名视频，跳过压制。")
        log(f"      可直接用：ffmpeg -i <视频> -vf {ass_filter(ass_emote)} out.mp4")
        log(f"\n完成，用时 {time.time() - started:.1f}s")
        return 0
    if not args.output:
        out = work / f"{stem}.emote.mp4"
    else:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
    log(f"[4/4] 压制：{video.name} -> {out.name}")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
           "-vf", ass_filter(ass_emote),
           "-c:v", args.vcodec, "-preset", args.preset, "-crf", str(args.crf),
           "-c:a", "copy"]
    if args.extra:
        cmd += args.extra.split()
    cmd.append(str(out))
    t0 = time.time()
    run_cmd(cmd)
    log(f"      完成，视频用时 {time.time() - t0:.1f}s")

    log(f"\n产物：")
    log(f"  ASS      {ass_emote}")
    for png in previews:
        log(f"  预览     {png}")
    log(f"  视频     {out}")
    log(f"总用时 {time.time() - started:.1f}s")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="auto_pipeline",
        description="录播姬 XML -> DanmakuFactory -> 表情 ASS -> 成片，一条命令跑完。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--library", help="表情库目录（默认 ./emotes）")
    sub = p.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("report", help="统计这场直播用了哪些表情、本地覆盖率")
    rep.add_argument("xml")
    rep.set_defaults(func=cmd_report)

    fet = sub.add_parser("fetch", help="按房间号拉直播间表情包（需 cookie）")
    fet.add_argument("--room", type=int, required=True)
    fet.add_argument("--cookie")
    fet.add_argument("--platform", default="pc", choices=("pc", "android", "ios"))
    fet.set_defaults(func=cmd_fetch)

    imp = sub.add_parser("import", help="导入本地图片/文件夹（文件名即表情名）")
    imp.add_argument("paths", nargs="+")
    imp.add_argument("--overwrite", action="store_true")
    imp.set_defaults(func=cmd_import)

    har = sub.add_parser("harvest", help="从 raw 数据下载表情（需开启原始数据录制）")
    har.add_argument("xml")
    har.add_argument("--all", action="store_true")
    har.set_defaults(func=cmd_harvest)

    run = sub.add_parser("run", help="一键出片")
    run.add_argument("xml")
    run.add_argument("-o", "--output", help="输出 mp4")
    run.add_argument("--video", help="视频文件（默认找同名）")
    run.add_argument("--ass", help="复用已有 ASS，跳过 DanmakuFactory")
    run.add_argument("--danmaku-factory", help="DanmakuFactory.exe 路径")
    run.add_argument("--ffmpeg")
    run.add_argument("--workdir", help="中间文件目录（默认与 XML 同目录）")
    run.add_argument("--preview", type=float, default=60.0, help="预览图时间点（秒）")
    run.add_argument("--scroll-time", type=float, default=12.0)
    run.add_argument("--fixed-time", type=float, default=5.0)
    run.add_argument("--emote-scale", type=float, default=1.30)
    run.add_argument("--baseline-drop", type=float, default=0.10)
    run.add_argument("--slot-pad", type=float, default=1.04)
    run.add_argument("--max-colors", type=int, default=10)
    run.add_argument("--tolerance", type=float, default=0.5)
    run.add_argument("--font-dir")
    run.add_argument("--vcodec", default="libx264")
    run.add_argument("--preset", default="medium")
    run.add_argument("--crf", type=int, default=20)
    run.add_argument("--extra", help="附加 ffmpeg 参数")
    run.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
