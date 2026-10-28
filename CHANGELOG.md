# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-10-28

### 新增

- **接入 `bem` 自动补表情**：发现弹幕里有本地库没有的表情时，自动从 B 站
  全站表情库（七千多个表情包）按名字查找并下载。
  - 新增 `emote_danmaku/bem.py` 桥接层，复用 `bem.py` 的 `--json` 接口；
  - 新增命令 `library bem <弹幕.xml 或 表情名...>`；
  - 批处理菜单新增 `[7] 补齐缺失表情`；
  - 压制流程中 `bem_auto_fetch` 默认开启，补完自动重新生成 ASS。
- **查不到的表情自动跳过并逐个列出**，不会中断压制。
- 表情库索引缓存 7 天（`%LOCALAPPDATA%\emote-danmaku\bem_catalog.json`），
  避免每次压制都重新联网扫描。

### 配置

新增 `bem_auto_fetch`、`bem_path`、`bem_query`、`bem_proxy` 四项。

### 修复

- `convert` 命令引用了已删除的 `slot_pad` / `baseline_drop` 字段，运行即崩溃。
- `run_from_config.py` 与 `_render_options` 的 `side_bearing` 默认值不一致。

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
