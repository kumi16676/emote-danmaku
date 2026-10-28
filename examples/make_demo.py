"""Build a self-contained demo: XML -> DanmakuFactory ASS -> emote ASS -> mp4.

Requires: ffmpeg on PATH, the DanmakuFactory CLI, and a populated emote library
(``emotes/``).  Run from the project root::

    python examples/make_demo.py

Outputs into ``examples/out/``: demo.ass, demo.emote.ass, demo.mp4,
preview_plain.png and preview_emote.png.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emote_danmaku.ass import RenderOptions, TextMetrics, read_ass, transform  # noqa: E402
from emote_danmaku.library import EmoteLibrary  # noqa: E402

OUT = ROOT / "examples" / "out"
DF = ROOT / "tools" / "DanmakuFactory" / "DanmakuFactory.exe"

DEMO_XML = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <chatserver>chat.bilibili.com</chatserver>
  <chatid>0</chatid>
  <mission>0</mission>
  <maxlimit>1000</maxlimit>
  <state>0</state>
  <real_name>0</real_name>
  <source>0</source>
  <BililiveRecorder version="2.16.0"/>
  <BililiveRecorderRecordInfo roomid="12345" shortid="0" name="演示直播间" title="演示"
      areanameparent="虚拟主播" areanamechild="虚拟主播"
      start_time="2026-10-25T01:00:00+08:00"/>
  <d p="1.200,1,25,16777215,1690000000000,0,10001,0" user="观众A">[妙啊]</d>
  <d p="2.600,1,25,16777215,1690000001000,0,10002,0" user="观众B">这也太[妙啊]了吧</d>
  <d p="4.000,1,25,16777215,1690000002000,0,10003,0" user="观众C">普通弹幕一条</d>
  <d p="5.400,1,25,16777215,1690000003000,0,10004,0" user="观众D">[awsl]</d>
  <d p="6.800,1,25,16777215,1690000004000,0,10005,0" user="观众E">[妙啊][妙啊][妙啊]</d>
  <d p="8.200,1,25,16777215,1690000005000,0,10006,0" user="观众F">还有普通弹幕</d>
  <d p="9.600,1,25,16777215,1690000006000,0,10007,0" user="观众G">前面[妙啊]后面</d>
  <d p="11.000,5,25,16777215,1690000007000,0,10008,0" user="观众H">顶部固定[妙啊]</d>
  <d p="12.400,4,25,16777215,1690000008000,0,10009,0" user="观众I">底部固定[awsl]</d>
  <d p="14.000,1,25,16777215,1690000009000,0,10010,0" user="观众J">最后一条弹幕</d>
</i>
"""


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def ass_filter(path: Path) -> str:
    """Build an ``ass=`` filter value that survives the filtergraph parser.

    A Windows path contains a colon, which the filtergraph parser treats as an
    option separator, so it must be escaped (and backslashes normalised).
    """
    return "ass='" + str(path.resolve()).replace("\\", "/").replace(":", "\\:") + "'"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    xml = OUT / "demo.xml"
    xml.write_text(DEMO_XML, "utf-8")

    lib = EmoteLibrary(ROOT / "emotes")
    if not len(lib):
        print("表情库是空的，请先运行：\n"
              "  python -m emote_danmaku library add <图片或文件夹>\n"
              "  python -m emote_danmaku library harvest <弹幕.xml>", file=sys.stderr)
        return 2

    if not DF.exists():
        print(f"找不到 DanmakuFactory：{DF}\n"
              "请从 https://github.com/hihkm/DanmakuFactory/releases/tag/dev 下载 "
              "DanmakuFactory-dev-windows-x64-CLI.zip 并解压到 tools/DanmakuFactory/",
              file=sys.stderr)
        return 2

    ass = OUT / "demo.ass"
    run([str(DF), "-o", "ass", str(ass), "-i", "xml", str(xml),
         "-r", "1920x1080", "-s", "10", "-f", "5"])

    doc = read_ass(ass)
    doc, stats = transform(doc, lib, TextMetrics(), RenderOptions())
    emote_ass = OUT / "demo.emote.ass"
    emote_ass.write_text(doc.to_text(), encoding="utf-8-sig")
    print(f"表情事件 {stats.emotes} 个（{stats.layers} 层），"
          f"未收录 {stats.missing_total} 处")

    # dark background so the emotes are easy to see
    bg = OUT / "bg.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "gradients=s=1920x1080:c0=0x0e141c:c1=0x22303e:d=18:r=30",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", str(bg)])

    for label, source in (("plain", ass), ("emote", emote_ass)):
        png = OUT / f"preview_{label}.png"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=0x0e141c:s=1920x1080:d=6:r=2", "-vf", ass_filter(source),
             "-ss", "3", "-frames:v", "1", "-update", "1", str(png)])

    mp4 = OUT / "demo.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(bg),
         "-vf", ass_filter(emote_ass), "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "copy", str(mp4)])
    print(f"\n完成，输出在 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
