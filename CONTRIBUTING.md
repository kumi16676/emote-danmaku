# 贡献指南

感谢你愿意改进这个项目。

## 提交前自检

```bash
python -m unittest discover -s tests        # 必须全绿
python -m py_compile run_from_config.py auto_pipeline.py emote_danmaku/*.py
```

如果你改了表情定位相关的逻辑（`emote_danmaku/ass.py`），请务必同时验证：

1. 单元测试通过；
2. 用 `examples/make_demo.py` 生成预览图，确认表情与文字没有重叠、没有明显空隙；
3. 在 Issue / PR 里说明实测结果（间隙像素值、涉及的弹幕样本）。

## 代码风格

- 遵循 PEP 8，行长尽量不超过 100 字符。
- 注释用英文，用户可见的文案用中文。
- 涉及 libass 行为的改动，请把**实测依据**写进注释（这个项目的很多结论只能靠逐像素验证得出）。

## 不要提交的内容

| 内容 | 原因 |
| --- | --- |
| `.flv` / `.mp4` / `.xml` 录制素材 | 体积巨大，且可能含他人隐私 |
| `emotes/` 下的表情图片 | B 站版权素材，不随仓库分发 |
| `tools/DanmakuFactory/*.exe` | 第三方 GPL 二进制，请使用者自行下载 |
| Cookie、`SESSDATA`、`bili_jct` 等凭据 | 安全问题 |
| 真实主播名、房间号 | 隐私；请用「示例主播」「12345」代替 |

以上均已写入 `.gitignore`，但仍请在 `git add` 前用 `git status` 确认。

## 报告问题

请附上：

- 操作系统、Python 版本、ffmpeg 版本；
- 复现所需的**最小脱敏样本**（把主播名换成「示例主播」、房间号换成 `12345`）；
- 期望行为与实际行为；
- 若涉及定位问题，附一张截图。
