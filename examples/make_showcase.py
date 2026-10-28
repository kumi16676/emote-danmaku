"""Build a cropped showcase image for the README.

Renders a few representative danmaku at a fixed position so the emote/text
relationship is visible without scrolling artefacts.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emote_danmaku.ass import AssDocument, RenderOptions, TextMetrics, transform
from emote_danmaku.library import EmoteLibrary

HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1200
PlayResY: 420
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: R2L,Microsoft YaHei,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2.0,1.0,7,0,0,0,1
Style: EMOTE,Microsoft YaHei,25,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

BODY = [
    (r"{\pos(40,60)}", r"观众A:\h{\c&HBCACF7}这也太[妙啊]了吧"),
    (r"{\pos(40,140)}", r"观众B:\h{\c&HBCACF7}[妙啊][妙啊][妙啊]"),
    (r"{\pos(40,220)}", r"观众C:\h{\c&HBCACF7}前面[awsl]后面"),
    (r"{\pos(40,300)}", r"观众D:\h{\c&HBCACF7}普通弹幕一条，没有表情"),
]

lib = EmoteLibrary(ROOT / "emotes")
if not len(lib):
    print("表情库为空，先运行 library add / fetch", file=sys.stderr)
    raise SystemExit(2)

lines = []
for i, (pos, text) in enumerate(BODY):
    body = pos + text
    doc = AssDocument.parse(
        HEAD + f"Dialogue: 0,0:00:01.00,0:00:10.00,R2L,,0,0,0,,{body}\n")
    doc, _ = transform(doc, lib, TextMetrics(), RenderOptions())
    for e in doc.events:
        lines.append(e.format())

tmp = ROOT / "docs" / "_show.ass"
tmp.write_text(HEAD + "\n".join(lines) + "\n", encoding="utf-8-sig")

png = ROOT / "docs" / "preview.png"
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "color=c=0x121821:s=1200x420:d=4:r=2", "-vf", "ass=_show.ass",
                "-ss", "2", "-frames:v", "1", "-update", "1", str(png)],
               cwd=str(ROOT / "docs"), check=True)
tmp.unlink(missing_ok=True)
print(f"已生成 {png}")
