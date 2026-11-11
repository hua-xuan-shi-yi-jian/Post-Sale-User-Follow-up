#!/usr/bin/env python3
"""
日志双写模块
============
同时输出到控制台（原样）和日志文件（自动加时间戳前缀）。

用法:
    from a2autolib.common.logging import setup_file_logging
    log_path = setup_file_logging("logs")
    print("这条日志同时出现在控制台和文件中")
"""

import sys
from datetime import datetime
from pathlib import Path


class ConsoleAndFile:
    """
    写控制台原样输出，写日志文件自动加 [HH:MM:SS] 时间戳。

    替换 sys.stdout / sys.stderr 使用，对 print() 透明。
    """

    def __init__(self, console, file):
        self._console = console      # sys.__stdout__（无时间戳）
        self._file = file            # 日志文件（自动加时间戳）
        self._at_newline = True

    def write(self, obj):
        if not obj:
            return
        # 控制台：原样输出
        self._console.write(obj)
        self._console.flush()
        # 日志文件：每行开头加时间戳
        if self._at_newline and obj.strip():
            ts = datetime.now().strftime("[%H:%M:%S] ")
            self._file.write(ts)
        self._file.write(obj)
        self._file.flush()
        self._at_newline = obj.endswith("\n")

    def flush(self):
        self._console.flush()
        self._file.flush()


def setup_file_logging(log_dir: str = "logs") -> str:
    """
    配置日志文件输出（保留控制台输出）。

    控制台：原样输出
    日志文件：每条 print 自动加 [HH:MM:SS] 前缀

    文件位于 {log_dir}/run_YYYYMMDD_HHMMSS.log

    参数:
        log_dir: 日志目录路径

    返回:
        日志文件绝对路径
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(log_dir) / f"run_{timestamp}.log"
    log_file = open(str(log_path), "w", encoding="utf-8", buffering=1)
    sys.stdout = ConsoleAndFile(sys.__stdout__, log_file)
    sys.stderr = ConsoleAndFile(sys.__stderr__, log_file)
    log_file.write("=" * 60 + "\n")
    log_file.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    log_file.flush()
    return str(log_path.resolve())
