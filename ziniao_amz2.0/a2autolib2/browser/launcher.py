#!/usr/bin/env python3
"""
紫鸟浏览器进程生命周期管理
============================
"""

import os
import subprocess
import time
from typing import Optional, Callable

from .constants import DEFAULT_SOCKET_PORT, SUPERBROWSER_DEFAULT_SEARCH_PATHS


def find_superbrowser_path(
    saved_path: str = "",
    search_paths: list[str] | None = None,
    on_found: Callable[[str], None] | None = None,
) -> Optional[str]:
    """
    查找紫鸟浏览器可执行文件路径。

    优先级:
        1. saved_path（如配置文件中的路径，如果文件存在）
        2. search_paths 中第一个存在的路径
        3. SUPERBROWSER_DEFAULT_SEARCH_PATHS 中第一个存在的路径
        4. 返回 None（需用户手动指定）

    参数:
        saved_path: 之前保存的路径（优先级最高）
        search_paths: 额外的搜索路径列表
        on_found: 自动发现路径时的回调（如保存到配置文件）
    """
    if saved_path and os.path.isfile(saved_path):
        return saved_path

    all_paths = list(search_paths or []) + SUPERBROWSER_DEFAULT_SEARCH_PATHS
    for p in all_paths:
        if p and os.path.isfile(p):
            if on_found:
                on_found(p)
            return p

    return None


def kill_existing_superbrowser() -> bool:
    """
    终止所有正在运行的紫鸟浏览器进程（SuperBrowser.exe 和 starter.exe）。

    返回:
        是否终止了任何进程
    """
    killed = False
    for proc_name in ["SuperBrowser.exe", "starter.exe"]:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"[ziniao] 已终止进程: {proc_name}")
                killed = True
            elif result.stderr:
                stderr_lower = result.stderr.lower()
                if any(kw in stderr_lower for kw in
                       ["not found", "没有找到", "no tasks", "没有运行的任务"]):
                    pass  # 正常：进程未运行
                else:
                    print(f"[ziniao] 终止 {proc_name} 出现预期外输出: {result.stderr.strip()}")
        except FileNotFoundError:
            print(f"[ziniao] taskkill 不可用，跳过终止 {proc_name}")
        except Exception as e:
            print(f"[ziniao] 终止 {proc_name} 时出错: {e}")
    return killed


def launch_superbrowser(
    path: str | None = None,
    socket_port: int = DEFAULT_SOCKET_PORT,
    wait: float = 3.0,
) -> bool:
    """
    以 WebDriver 模式启动紫鸟浏览器。

    启动前自动终止任何已在运行的紫鸟进程。

    参数:
        path: 紫鸟 starter.exe 路径（None 则自动搜索）
        socket_port: HTTP API 端口
        wait: 启动后等待秒数

    返回:
        是否成功启动
    """
    kill_existing_superbrowser()
    time.sleep(2)

    if path is None:
        path = find_superbrowser_path()
    if not path or not os.path.isfile(path):
        print("[ziniao] 紫鸟浏览器路径未配置或无效")
        return False

    print(f"[ziniao] 启动: {path}  (API端口: {socket_port})")
    try:
        cmd = [path, "--run_type=web_driver", "--ipc_type=http", f"--port={socket_port}"]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[ziniao] 紫鸟已启动（WebDriver 模式），等待加载...")
        time.sleep(wait)
        return True
    except Exception as e:
        print(f"[ziniao] 启动失败: {e}")
        return False
