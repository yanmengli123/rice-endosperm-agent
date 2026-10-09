"""BrowserSkill 守护进程的无窗口计划任务启动器。

`bsk daemon start` 只能前台运行,没有后台/隐藏参数。计划任务若直接执行
bsk.exe,InteractiveToken 会话里会弹出一个常驻控制台窗口;配合 1 分钟自恢复
触发器,用户手动关掉窗口后还会反复弹出,严重打扰桌面工作。

本启动器由 pythonw(无控制台解释器)调用,以 CREATE_NO_WINDOW 派生 bsk 并
等待其退出,退出码原样透传给任务计划程序(自恢复触发器依赖非零退出码)。
stdout/stderr 重定向到日志文件,保留排障能力(否则无窗口进程的输出会丢失)。

用法:
    pythonw bsk_daemon_launcher.py --bsk <bsk.exe> [--log <file>] [-- <daemon start 额外参数>]
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Run `bsk daemon start` without a console window")
    parser.add_argument("--bsk", required=True, help="Path to bsk.exe")
    parser.add_argument("--log", default="", help="Append child stdout/stderr to this file")
    parser.add_argument("extra", nargs="*", help="Extra arguments appended to `daemon start`")
    args = parser.parse_args()

    command = [args.bsk, "daemon", "start", *args.extra]
    log_handle = None
    try:
        if args.log:
            # 长期追加、不做轮转:守护进程日志量低,轮转交给运维清理。
            log_handle = open(args.log, "ab", buffering=0)
        proc = subprocess.Popen(
            command,
            stdout=log_handle if log_handle else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle else subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as error:
        print(f"bsk_daemon_launcher: failed to start {args.bsk}: {error}", file=sys.stderr)
        return 1
    finally:
        # 句柄所有权已移交给子进程;父进程侧关闭不会截断子进程输出。
        if log_handle:
            log_handle.close()
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 130


if __name__ == "__main__":
    sys.exit(main())
