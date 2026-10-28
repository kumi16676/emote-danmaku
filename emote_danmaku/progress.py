"""Prompt helpers and live ffmpeg progress reporting."""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path


# --------------------------------------------------------------------------
# prompting
# --------------------------------------------------------------------------

def ask_yes_no(question: str, default: bool = True) -> bool:
    """Ask a yes/no question on the console; safe when stdin is not a TTY."""
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是", "1")


def pause(message: str = "按回车继续...") -> None:
    try:
        input(message)
    except EOFError:
        pass


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def parse_time(text: str) -> float | None:
    m = _TIME_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def parse_duration(text: str) -> float | None:
    m = _DURATION_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def run_with_progress(cmd: list[str], label: str = "压制",
                      log=None) -> int:
    """Run ffmpeg and print a live progress line.

    ffmpeg writes ``time=..`` to stderr; we parse it and redraw a single line so
    the console shows percentage, elapsed/remaining time and speed.
    """
    started = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, errors="replace", bufsize=1)

    duration: float | None = None
    last_draw = 0.0
    tail: list[str] = []

    def draw(line: str) -> None:
        sys.stdout.write("\r" + line.ljust(78))
        sys.stdout.flush()

    try:
        for raw in proc.stderr:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            if duration is None:
                d = parse_duration(line)
                if d:
                    duration = d
            t = parse_time(line)
            if t is None:
                if line.strip() and not line.startswith(("frame=", "size=")):
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
                continue
            now = time.time()
            if now - last_draw < 0.25:
                continue
            last_draw = now
            elapsed = now - started
            speed = (t / elapsed) if elapsed > 0.5 else 0.0
            if duration and duration > 0:
                pct = min(100.0, t / duration * 100)
                remain = (duration - t) / speed if speed > 0.05 else 0.0
                draw(f"  {label} {pct:5.1f}%  {_fmt(t)}/{_fmt(duration)}  "
                     f"速度 {speed:4.2f}x  剩余 {_fmt(remain)}")
            else:
                draw(f"  {label} {_fmt(t)}  速度 {speed:4.2f}x")
    finally:
        code = proc.wait()
        sys.stdout.write("\n")
        sys.stdout.flush()

    if code != 0:
        if log:
            log("ffmpeg 失败，末尾输出：")
            for line in tail[-15:]:
                log("  " + line)
        else:
            for line in tail[-15:]:
                print("  " + line)
    return code
