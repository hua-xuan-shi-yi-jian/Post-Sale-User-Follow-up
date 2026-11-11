"""紫鸟浏览器管理模块 — API、CDP 连接、进程管理。"""

from .cdp import ZiniaoBrowser, connect_to_store_cdp
from .launcher import launch_superbrowser, kill_existing_superbrowser

__all__ = [
    "ZiniaoBrowser",
    "connect_to_store_cdp",
    "launch_superbrowser",
    "kill_existing_superbrowser",
]
