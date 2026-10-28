"""Batch-friendly entry point: reads ``config.json`` and runs the whole pipeline.

The Windows batch files (``一键出片.bat`` etc.) call this so that users can edit
one JSON file instead of remembering command line flags.

    python run_from_config.py <video-or-xml> [--config config.json] [--mode burn|ass|fetch]

Modes
-----
burn   video -> ASS -> emote ASS -> mp4   (default; needs a video)
ass    XML   -> ASS -> emote ASS          (no encoding, for handing to an editor)
fetch  refresh the emote library from the room in the config
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import PIL  # noqa: F401
except ImportError:
    raise SystemExit(
        "缺少 Pillow（表情转矢量图需要它）。\n"
        f"当前 Python：{sys.executable}\n"
        "请执行：  \"%s\" -m pip install pillow\n"
        "或把带 Pillow 的便携版 Python 放到本目录的 python\\ 下。"
        % sys.executable)

from emote_danmaku.ass import RenderOptions, TextMetrics, read_ass, transform  # noqa: E402
from emote_danmaku.library import EmoteLibrary  # noqa: E402
from emote_danmaku.progress import (ask_yes_no, pause,  # noqa: E402
                                    run_with_progress)
from emote_danmaku.xmlfile import harvest_emotes  # noqa: E402

DF_NAME = "DanmakuFactory.exe"
VIDEO_SUFFIXES = (".flv", ".mp4", ".mkv", ".ts", ".m4v")
XML_SUFFIXES = (".xml",)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到配置文件：{path}")
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"配置文件格式有误（第 {exc.lineno} 行）：{exc.msg}\n{path}")
    return data


def cfg_get(cfg: dict, key: str, default=None):
    value = cfg.get(key, default)
    if isinstance(value, str) and not value.strip():
        return default
    return value


def resolve_path(cfg: dict, value: str | None, default: str) -> Path:
    """Relative paths are resolved against the config file's folder."""
    text = (value or default).strip()
    p = Path(text)
    return p if p.is_absolute() else (ROOT / p)


def find_python(cfg: dict) -> str:
    explicit = (cfg.get("python") or "").strip()
    if explicit:
        return explicit
    local = ROOT / "python" / "python.exe"
    if local.exists():
        return str(local)
    return sys.executable or "python"


def find_tool(name: str) -> Path | None:
    for candidate in (ROOT / name, ROOT / "tools" / name, ROOT / "tools" / "DanmakuFactory" / name):
        if candidate.exists():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def find_ffmpeg() -> str:
    exe = find_tool("ffmpeg.exe") or find_tool("ffmpeg")
    if not exe:
        raise SystemExit("找不到 ffmpeg.exe。请把它放在脚本目录，或加进 PATH。")
    return str(exe)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def log(msg: str = "") -> None:
    print(msg, flush=True)


def ass_filter(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/").replace(":", "\\:")
    return f"ass='{p}'"


def run_cmd(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"命令失败（{proc.returncode}）：\n{' '.join(cmd)}\n"
                         f"{(proc.stderr or '')[-1200:]}")


def probe_size(ffmpeg: str, video: Path) -> tuple[int, int]:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe"))
    if not Path(ffprobe).exists():
        ffprobe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True, errors="replace")
    if proc.returncode == 0:
        try:
            s = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
            if s.get("width") and s.get("height"):
                return int(s["width"]), int(s["height"])
        except Exception:
            pass
    return 1920, 1080


def df_args(cfg: dict, resolution: str) -> list[str]:
    """Translate config.json keys into DanmakuFactory CLI flags.

    ``msgboxSize`` / ``msgboxPos`` accept either ``[380, 650]`` (DanmakuFactory's
    own config format) or ``"380x650"``.
    """
    args: list[str] = ["-r", resolution]
    mapping = {
        "fontname": "--fontname",
        "fontsize": "-S",
        "scrolltime": "-s",
        "fixtime": "-f",
        "density": "-d",
        "opacity": "-O",
        "outline": "-L",
        "shadow": "-D",
        "displayarea": "--displayarea",
        "scrollarea": "--scrollarea",
        "msgboxFontSize": "--msgboxfontsize",
        "giftMinPrice": "--giftminprice",
        "msgboxDuration": "--msgboxduration",
    }
    for key, flag in mapping.items():
        value = cfg.get(key)
        if value is not None and value != "":
            args += [flag, str(value)]
    for key, flag in (("bold", "-B"), ("showUsernames", "--showusernames"),
                      ("showMsgbox", "--showmsgbox")):
        value = cfg.get(key)
        if value is not None:
            args += [flag, "true" if value else "false"]
    size = _pair(cfg.get("msgboxSize"))
    if size:
        args += ["--msgboxsize", size]
    pos = _pair(cfg.get("msgboxPos"))
    if pos:
        args += ["--msgboxpos", pos]
    blocks = cfg.get("blockmode") or []
    if blocks:
        args += ["-b", "-".join(str(b) for b in blocks)]
    # DanmakuFactory normally rescales each comment by its XML font size (25),
    # so the ASS style says 40 while libass draws ~29px glyphs.  That mismatch
    # makes every width we measure (for emote placement) too large.  Keeping the
    # configured size strict makes the rendered text match the style exactly.
    if cfg_get(cfg, "font_size_strict", True):
        args.append("--font-size-strict")
    return args


def _pair(value) -> str:
    """Normalise ``[380, 650]`` / ``"380x650"`` / ``"380,650"`` to ``380x650``."""
    if not value:
        return ""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return f"{value[0]}x{value[1]}"
    text = str(value).strip().replace(",", "x").replace(" ", "")
    return text


def config_resolution(cfg: dict) -> str:
    """``[1920, 1080]`` or ``"1920x1080"`` or ``"auto"``."""
    value = cfg_get(cfg, "resolution", "auto")
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return f"{value[0]}x{value[1]}"
    return str(value or "auto")


def find_sidecar(target: Path, suffixes) -> Path | None:
    for suffix in suffixes:
        candidate = target.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def do_fetch(cfg: dict) -> int:
    lib = EmoteLibrary(resolve_path(cfg, cfg.get("library"), "emotes"))
    room = cfg_get(cfg, "room_id")
    if not room:
        raise SystemExit("配置里的 room_id 是空的，没法更新表情包。")
    cookie = (cfg.get("cookie") or os.environ.get("BILIBILI_COOKIE", "")).strip()
    if not cookie:
        raise SystemExit("配置里的 cookie 是空的。\n"
                         "要拉直播间表情包，必须填 B 站 Cookie（至少含 SESSDATA）。")
    log(f"正在从直播间 {room} 拉取表情包 ...")
    res = lib.fetch_room_pack(int(room), cookie=cookie)
    log(f"完成：新增 {res['added']}，已存在 {res['updated']}，失败 {res['failed']}")
    log(f"表情库现有 {len(lib)} 个：{lib.root}")
    return 0


def is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def run_danmaku_factory(df: Path, xml: Path, out_ass: Path, cfg: dict,
                        resolution: str) -> None:
    """Run DanmakuFactory, working around its non-ASCII path bug.

    DanmakuFactory 1.70 decodes its argv through the ANSI code page, so any
    Chinese path arrives mangled and it fails with ``ERROR [code wa100] Failed
    to create file``.  Feeding it ASCII-only temp names is the reliable fix;
    the result is then moved to the requested location.
    """
    if is_ascii_path(xml) and is_ascii_path(out_ass):
        run_cmd([str(df), "-o", "ass", str(out_ass), "-i", "xml", str(xml),
                 *df_args(cfg, resolution), "--ignore-warnings", "--force"])
        return

    # plain mkdir: tempfile.mkdtemp creates 0700 dirs that some sandboxes refuse
    scratch = ROOT / f"_df_tmp_{os.getpid()}_{int(time.time() * 1000) % 100000}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        tmp_xml = scratch / "input.xml"
        tmp_ass = scratch / "output.ass"
        shutil.copyfile(xml, tmp_xml)
        run_cmd([str(df), "-o", "ass", str(tmp_ass), "-i", "xml", str(tmp_xml),
                 *df_args(cfg, resolution), "--ignore-warnings", "--force"])
        if not tmp_ass.exists():
            raise SystemExit("ASS 生成失败（DanmakuFactory 未输出文件）。")
        out_ass.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_ass), str(out_ass))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build_ass(cfg: dict, xml: Path, video: Path | None) -> tuple[Path, Path]:
    """XML -> plain ASS -> emote ASS. Returns (plain, emote)."""
    ffmpeg = find_ffmpeg()
    df = find_tool(DF_NAME)
    if not df:
        raise SystemExit(f"找不到 {DF_NAME}。请把它放在脚本目录，或 tools/ 下。")

    resolution = config_resolution(cfg)
    if resolution.lower() in ("auto", ""):
        resolution = f"{probe_size(ffmpeg, video)[0]}x{probe_size(ffmpeg, video)[1]}" \
            if video else "1920x1080"

    plain = xml.with_suffix(".danmaku.ass")
    log(f"[1/3] 生成 ASS 字幕（{resolution}）...")
    if not is_ascii_path(xml):
        log("      路径含中文，已自动改用 ASCII 临时名调用 DanmakuFactory")
    run_danmaku_factory(df, xml, plain, cfg, resolution)
    if not plain.exists():
        raise SystemExit("ASS 生成失败，请检查 XML 是否损坏。")
    log("      字幕生成成功")

    lib = EmoteLibrary(resolve_path(cfg, cfg.get("library"), "emotes"))
    log(f"[2/3] 注入表情（表情库 {len(lib)} 个）...")
    doc = read_ass(plain)
    doc, stats = transform(doc, lib, TextMetrics(), _render_options(cfg))
    emote_ass = xml.with_suffix(".emote.ass")
    emote_ass.write_text(doc.to_text(), encoding="utf-8-sig")
    log(f"      绘制表情 {stats.emotes} 个（{stats.layers} 色层），"
        f"处理弹幕 {stats.events} 条")

    if stats.missing:
        log("")
        log("!" * 62)
        log(f"  有 {len(stats.missing)} 种表情没收录，共 {stats.missing_total} 处（全部列出）")
        log("!" * 62)
        ranked = sorted(stats.missing.items(), key=lambda kv: (-kv[1], kv[0]))
        width = max(len(t) for t, _ in ranked)
        for token, count in ranked:
            log(f"    {token:<{width}}  x{count}")
        log("")

        # 先尝试用 bem 自动补齐：bem 能列出 B 站全部表情包的逐条表情，
        # 正好覆盖房间接口拿不到的（别家粉丝牌、活动表情等）。
        if cfg_get(cfg, "bem_auto_fetch", True):
            missing_tokens = [t for t, _ in ranked]
            fetched = _try_bem_fetch(cfg, lib, missing_tokens)
            if fetched and fetched.added:
                log(f"  已自动补下载 {len(fetched.added)} 个，正在重新生成 ASS…")
                # 表情库变了，需要重跑一遍注入
                doc = read_ass(plain)
                doc, stats = transform(doc, lib, TextMetrics(),
                                       _render_options(cfg))
                emote_ass.write_text(doc.to_text(), encoding="utf-8-sig")
                log(f"      绘制表情 {stats.emotes} 个（{stats.layers} 色层），"
                    f"处理弹幕 {stats.events} 条")
            if fetched and fetched.missing:
                log("")
                log(f"  B 站表情库里也没有这 {len(fetched.missing)} 个（已跳过，"
                    f"将显示为文字）：")
                mw = max(len(t) for t in fetched.missing)
                for token in fetched.missing:
                    log(f"    {token:<{mw}}")
                log("")
            if fetched and fetched.failed:
                log(f"  下载失败 {len(fetched.failed)} 个（将显示为文字）："
                    + "、".join(fetched.failed[:8]))
                log("")

        if stats.missing:
            lib_path = resolve_path(cfg, cfg.get("library"), "emotes")
            log(f"  仍有 {len(stats.missing)} 种没收录。把图片放进：{lib_path}")
            log("  （子文件夹也会被扫描，文件名就是表情名，例如  示例主播_偷笑.png）")
            log("")
            if not ask_yes_no("  仍然继续压制吗？（未收录的会显示成 [名字] 文字）",
                              default=True):
                raise SystemExit("已取消。补齐表情后重新运行即可。")
            log("")
    return plain, emote_ass


def _render_options(cfg: dict):
    return RenderOptions(
        height_ratio=float(cfg_get(cfg, "emote_scale", 1.3)),
        side_bearing=float(cfg_get(cfg, "emote_gap_tweak", 0.0)),
        max_colors=int(cfg_get(cfg, "max_colors", 10)),
        tolerance=float(cfg_get(cfg, "tolerance", 0.5)),
        unknown=str(cfg_get(cfg, "unknown_emote", "text")),
    )


def _try_bem_fetch(cfg: dict, lib, tokens):
    """用 bem 补下载缺失表情；不可用时返回 None 并给出提示。"""
    try:
        from emote_danmaku.bem import BemError, fetch_missing
    except ImportError:
        return None
    try:
        log("  尝试从 B 站表情库自动补下载…")
        return fetch_missing(
            tokens, lib,
            bem=cfg_get(cfg, "bem_path", "") or None,
            cookie=(cfg.get("cookie") or os.environ.get("BILIBILI_COOKIE", "")).strip(),
            proxy=cfg_get(cfg, "bem_proxy", "") or "",
            query=cfg_get(cfg, "bem_query", "") or "",
            log=log,
        )
    except BemError as exc:
        log(f"  跳过自动下载：{exc}")
        return None


def do_ass(cfg: dict, target: Path) -> int:
    xml = target if target.suffix.lower() in XML_SUFFIXES else find_sidecar(target, XML_SUFFIXES)
    if not xml or not xml.exists():
        raise SystemExit(f"找不到同名 XML 弹幕文件：{target.with_suffix('.xml')}")
    video = target if target.suffix.lower() in VIDEO_SUFFIXES else None
    plain, emote = build_ass(cfg, xml, video)
    log("")
    log("完成。字幕文件：")
    log(f"  原始 ASS   {plain}")
    log(f"  表情 ASS   {emote}")
    log("可直接拖进剪映/PR，或用 ffmpeg 压制。")
    return 0


# --------------------------------------------------------------------------
# encoders
# --------------------------------------------------------------------------

#: encoder -> (quality args, speed args) tuned per family
ENCODER_ARGS = {
    "libx264": ("crf", ("preset",)),
    "libx265": ("crf", ("preset",)),
    "h264_nvenc": ("cq", ("preset",)),
    "hevc_nvenc": ("cq", ("preset",)),
    "av1_nvenc": ("cq", ("preset",)),
    "h264_qsv": ("global_quality", ("preset",)),
    "hevc_qsv": ("global_quality", ("preset",)),
    "h264_amf": ("qp", ()),
    "hevc_amf": ("qp", ()),
}

_NVENC_PRESETS = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7",
}
_X264_PRESETS = {"p1": "ultrafast", "p2": "superfast", "p3": "faster",
                 "p4": "fast", "p5": "medium", "p6": "slow", "p7": "slower"}


def encoder_available(ffmpeg: str, encoder: str) -> bool:
    """Actually try the encoder — merely being compiled in is not enough."""
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=10",
         "-c:v", encoder, "-f", "null", "-"],
        capture_output=True, text=True, errors="replace")
    return probe.returncode == 0


def pick_encoder(ffmpeg: str, wanted: str) -> str:
    """Resolve ``auto`` / an unavailable encoder into something that works.

    Order matters: NVENC first (what most people have), then QSV/AMF, and
    libx264 as the always-available fallback.
    """
    wanted = (wanted or "auto").strip().lower()
    if wanted not in ("", "auto"):
        if encoder_available(ffmpeg, wanted):
            return wanted
        log(f"      [警告] 编码器 {wanted} 不可用，改用自动选择")
    for candidate in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf", "libx264"):
        if encoder_available(ffmpeg, candidate):
            return candidate
    return "libx264"


def quality_args(encoder: str, cfg: dict) -> list[str]:
    """Map the generic ``crf``/``preset`` settings onto the chosen encoder."""
    args: list[str] = []
    if encoder in ENCODER_ARGS:
        quality_key, speed_keys = ENCODER_ARGS[encoder]
    else:
        quality_key, speed_keys = "crf", ("preset",)

    preset = str(cfg_get(cfg, "preset", "medium")).strip()
    if encoder.endswith("_nvenc"):
        preset = _NVENC_PRESETS.get(preset, preset)
    elif not encoder.endswith(("_qsv", "_amf")):
        preset = _X264_PRESETS.get(preset, preset)

    value = cfg_get(cfg, "crf", 20)
    bitrate = str(cfg_get(cfg, "bitrate", "") or "").strip()
    if encoder.endswith(("_nvenc", "_qsv", "_amf")):
        # hardware encoders: constant-quality mode, bitrate is only a cap.
        # ``-b:v 0`` lets CQ alone decide, which keeps file sizes sane.
        if quality_key == "cq":
            args += ["-rc", "vbr", "-cq", str(value), "-b:v", bitrate or "0"]
        else:
            args += [f"-{quality_key}", str(value)]
            if bitrate:
                args += ["-b:v", bitrate]
    else:
        args += [f"-{quality_key}", str(value)]

    for key in speed_keys:
        if preset:
            args += [f"-{key}", preset]
    return args


def do_burn(cfg: dict, target: Path) -> int:
    video = target if target.suffix.lower() in VIDEO_SUFFIXES else find_sidecar(target, VIDEO_SUFFIXES)
    if not video or not video.exists():
        raise SystemExit(f"找不到同名视频文件：{target.with_suffix('.flv')} 或 .mp4")
    xml = video.with_suffix(".xml")
    if not xml.exists():
        raise SystemExit(f"在视频同目录没找到同名弹幕 XML：{xml}")

    plain, emote = build_ass(cfg, xml, video)

    log("[3/3] 压制视频...")
    ffmpeg = find_ffmpeg()
    suffix = str(cfg_get(cfg, "output_suffix", "_带表情"))
    out = video.with_name(video.stem + suffix + ".mp4")
    encoder = pick_encoder(ffmpeg, str(cfg_get(cfg, "encoder", "auto")))
    hardware = encoder.endswith(("_nvenc", "_qsv", "_amf"))
    log(f"      编码器 {encoder}" + ("（硬件加速）" if hardware else "（CPU）"))

    cmd = [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-vf", ass_filter(emote),
           "-c:v", encoder, *quality_args(encoder, cfg)]
    if cfg_get(cfg, "decode_hwaccel", False):
        # decode on the GPU too; harmless if the codec is not supported
        cmd[2:2] = ["-hwaccel", "auto"]
    extra = (cfg.get("extra") or "").strip()
    if extra:
        cmd += extra.split()
    cmd += ["-c:a", "copy", str(out)]
    if run_with_progress(cmd, label="压制", log=log) != 0:
        raise SystemExit("ffmpeg 压制失败，请检查上面的输出。")

    log("")
    log("=" * 55)
    log("压制完成！")
    log(f"  成品  {out}")
    log(f"  字幕  {emote}")
    log("=" * 55)
    return 0


def do_encoder(cfg: dict, config_path: Path) -> int:
    """Interactively pick an encoder and persist it into config.json."""
    ffmpeg = find_ffmpeg()
    current = str(cfg_get(cfg, "encoder", "auto"))
    log(f"当前编码器设置：{current}")
    log(f"实际会使用：{pick_encoder(ffmpeg, current)}")
    log("")
    options = [
        ("auto", "自动选择（优先显卡，没有就 CPU）"),
        ("h264_nvenc", "NVIDIA 显卡 H.264（推荐，最快）"),
        ("hevc_nvenc", "NVIDIA 显卡 HEVC（同画质更省体积）"),
        ("h264_qsv", "Intel 核显 QuickSync H.264"),
        ("h264_amf", "AMD 显卡 AMF H.264"),
        ("libx264", "CPU libx264（最稳，慢）"),
    ]
    log("可选：")
    for i, (key, desc) in enumerate(options, 1):
        mark = " *" if key == current else "  "
        ok = "" if key == "auto" else ("  [可用]" if encoder_available(ffmpeg, key)
                                       else "  [不可用]")
        log(f"  {mark}{i}) {key:<12} {desc}{ok}")
    log("")
    choice = input("输入编号（直接回车保持不变）: ").strip()
    if not choice:
        log("未修改。")
        return 0
    try:
        key = options[int(choice) - 1][0]
    except (ValueError, IndexError):
        raise SystemExit("编号无效。")

    raw = config_path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = cfg
    data["encoder"] = key
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log("")
    log(f"已保存：encoder = {key}")
    log(f"下次压制将使用：{pick_encoder(ffmpeg, key)}")
    return 0


def do_scan(cfg: dict) -> int:
    """Re-scan the library folder so images dropped in by hand get indexed."""
    lib = EmoteLibrary(resolve_path(cfg, cfg.get("library"), "emotes"))
    log(f"表情库 {lib.root}")
    log(f"共 {len(lib)} 个表情（含自动登记的手动图片）")
    loose = [e for e in lib.entries() if e.get("source") == "loose"]
    if loose:
        log(f"其中 {len(loose)} 个来自直接放进目录的图片：")
        for entry in loose[:20]:
            log(f"  {entry.get('name')}")
        if len(loose) > 20:
            log(f"  ... 还有 {len(loose) - 20} 个")
    log("")
    log("提示：文件名就是表情名。想让 [示例主播_偷笑] 生效，")
    log("      把图片命名为  示例主播_偷笑.png  即可（带不带方括号都行）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="按 config.json 跑完整流水线")
    p.add_argument("target", nargs="?", help="视频或 XML（拖拽进来的路径）")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--mode", choices=("burn", "ass", "fetch", "report", "scan", "encoder"),
                   default="burn")
    args = p.parse_args(argv)

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    if args.mode == "fetch":
        return do_fetch(cfg)
    if args.mode == "scan":
        return do_scan(cfg)
    if args.mode == "encoder":
        return do_encoder(cfg, cfg_path)
    if not args.target:
        raise SystemExit("请把视频或 XML 文件拖到批处理上。")
    target = Path(args.target)
    if not target.exists():
        raise SystemExit(f"文件不存在：{target}")
    if args.mode == "ass":
        return do_ass(cfg, target)
    if args.mode == "report":
        h = harvest_emotes(target if target.suffix.lower() in XML_SUFFIXES
                           else target.with_suffix(".xml"))
        log(f"弹幕 {h.total_comments} 条，raw 数据 {h.raw_comments} 条，"
            f"表情表 {len(h.emotes)} 个")
        return 0
    return do_burn(cfg, target)


if __name__ == "__main__":
    raise SystemExit(main())
