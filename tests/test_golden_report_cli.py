"""`scripts/golden_report.py` 的命令行入口必须能用。

## 为什么单独守这一条

argparse 的 `_expand_help()` 会对 `help=` 文本做 **`%` 格式化**（`text % params`），
所以 help 正文里出现的 `%` 必须写成 `%%`。中文文案里百分号极常见，而中文标点紧跟
其后（`66.7%，规划层…`、`96.2%——`）会让 argparse 把 `%，` / `%—` 当成格式符，
于是：

    ValueError: unsupported format character '?' (0xff0c) at index 30

**`--help` 直接抛栈**。这不是"帮助文字不好看"，是入口不可用——而 `scripts/check.sh`
只用固定参数调用本脚本，**不会**触发 `--help`，所以这个 bug 能穿过全部闸门。

实测（2026-09-23，`5fa7ad9` 之后）：`python scripts/golden_report.py --help` →
`ValueError: unsupported format character '?' (0xff0c)`。

## 为什么用子进程而不是 import

`--help` 的行为（打印到 stdout、退出码 0）**只在真实进程边界上成立**。直接调用
`main()` 会 `SystemExit`，而且 argparse 的格式化路径只在 `parse_args()` 里走到，
import 层面测不到它。子进程是这里唯一诚实的测法。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "golden_report.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    # **总是带 `--no-write`**：本文件测的是命令行入口，不该顺手改写
    # `docs/golden-baseline.md`（我第一次写这几条时就把它写坏了——一次
    # `--since not-a-date` 的调用把基线报告覆盖成了"无法度量"）。
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--no-write", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_help_exits_zero():
    """`--help` 必须正常打印并退出 0，不得抛栈。"""
    result = _run("--help")

    assert result.returncode == 0, (
        f"--help 崩了：\n{result.stderr[-600:]}"
    )
    assert "usage" in result.stdout.lower(), "帮助正文里必须有 usage"


def test_cli_help_renders_percent_signs_literally():
    """help 里的百分号要**原样显示**（`%%` 必须渲染成 `%`），不得被吃掉或崩掉。"""
    result = _run("--help")

    assert result.returncode == 0, f"--help 崩了：\n{result.stderr[-600:]}"
    # --fail-under-recall 的帮助文案里带实测基线，百分号是它的一部分信息。
    assert "66.7%" in result.stdout, (
        "实测基线里的百分号丢了：`%%` 转义后应渲染为 `%`"
    )
    assert "96.2%" in result.stdout


def test_cli_rejects_a_bad_window_with_a_message_not_a_traceback():
    """坏时间窗要给 argparse 的正常报错（退出码 2），不是 Python 栈。"""
    result = _run("--since", "not-a-date")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr, (
        "非法参数应由 argparse 报错，栈说明解析路径本身有问题"
    )
