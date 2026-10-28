# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-10-28

首个公开版本。

### 功能

- 把录播姬 XML 中的 B 站表情弹幕（`[妙啊]`）在 ASS 中渲染为真实图片。
- 位图转 ASS 矢量绘图：调色板量化、像素边界追踪、RDP 轮廓简化。
- 本地表情包库：`library add`（手动导入）、`library fetch`（拉直播间表情包）、
  `library harvest`（从弹幕 `raw` 数据抓取）、`library list` / `prune`。
- 一键脚本 `auto_pipeline.py`：`report` / `fetch` / `import` / `harvest` / `run`。
- 批处理菜单 `一键出片.bat` + `config.json` 配置，支持 GPU 编码（NVENC / QSV / AMF）。
- 压制实时进度显示。

### 排版

- 用**透明占位绘图**保留 DanmakuFactory 算好的行宽与滚动轨迹（实测推进误差 0）。
- 表情按色层拆分为独立事件，避免 libass 的笔位推进导致图形右移。
- 自动删除表情前的 `\h` 硬空格，消除多余间隙。
- 按前置文字宽度补偿 libass 与 Pillow 的墨迹差异，使表情贴合文字。

### 兼容

- 支持标准模式录制的 XML（无 `raw` 数据）：表情缺失时保留文字并提示手动导入。
- 规避 DanmakuFactory 1.70 CLI 的 ANSI 参数解析问题（中文路径）。
