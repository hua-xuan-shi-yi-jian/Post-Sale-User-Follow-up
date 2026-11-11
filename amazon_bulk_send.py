#!/usr/bin/env python3
"""
亚马逊批量发送产品说明书邮件
================================
功能：通过紫鸟浏览器自动登录亚马逊卖家后台，批量向已购用户发送产品说明书PDF和回访消息。

使用方式：
  1. 确保紫鸟浏览器已安装并登录账号
  2. 在紫鸟中配置目标店铺的 CDP 调试端口（默认 9222）
  3. 运行本脚本：python amazon_bulk_send.py
  4. 在弹窗中填入参数，开始执行

技术栈：Python 3 + Playwright (connect_over_cdp) + tkinter
"""

import csv
import json
import os
import re
import sys
import time
import random
import threading
import subprocess
import webbrowser
from datetime import datetime
from typing import Literal, Optional
from pathlib import Path

# ---- tkinter GUI ----
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---- Playwright ----
from playwright.sync_api import (
    sync_playwright, Browser, BrowserContext, Page, ElementHandle, Locator,
)
from amazon_selectors import format_selectors, get_selectors
from feishu_sync import (
    build_report, save_run_report, sync_saved_report,
)
from task_models import (
    FailedRecord, OrderInfo, RunState, StatusHolder, TaskConfig, TaskStatus,
)


# ============================================================================
# 常量定义
# ============================================================================

# ---- 导入紫鸟浏览器控制模块 ----
from ziniao_browser import (
    ZiniaoBrowser, ZigniaoAPI, launch_superbrowser, connect_to_store_cdp,
    find_superbrowser_path,
    DEFAULT_SOCKET_PORT, ZIGNIAO_API_TIMEOUT,
)

# ---- 导入 a2autolib2 (ziniao_amz2.0) 亚马逊登录 + 站点切换模块 (V2 增强版) ----
# V2 相比 V1 的核心改进：
#   - 登录：URL白名单+表单检测预检，避免无效登录触发；自动处理 Switch accounts 中间页
#   - 站点切换：account-switcher 全页导航 + 条目匹配 + OTP 重试循环，比域名直跳更稳定
_amz2_path = str(Path(__file__).parent / "ziniao_amz2.0")
if _amz2_path not in sys.path:
    sys.path.insert(0, _amz2_path)

from a2autolib2.amazon.login import LoginHandler, quick_login
from a2autolib2.amazon.marketplace import switch_to_marketplace
from a2autolib2.amazon.constants import DOMAIN_TO_COUNTRY

# ---- 站点 URL 生成（按所选 marketplace 动态拼域名）----
COUNTRY_TO_DOMAIN = {v: k for k, v in DOMAIN_TO_COUNTRY.items()}  # "美国" → "amazon.com"
MARKETPLACE_OPTIONS = list(DOMAIN_TO_COUNTRY.values())            # GUI 下拉框选项
SALES_CHANNEL_OPTIONS = {f"Amazon.{domain.split('amazon.', 1)[1]}": country
                         for domain, country in DOMAIN_TO_COUNTRY.items()}

_FALLBACK_DOMAIN = "amazon.co.uk"  # 未选站点时的默认域名（英国站）


def _get_seller_central_domain(marketplace: str = "") -> str:
    """根据目标站点获取 Seller Central 域名。"""
    return COUNTRY_TO_DOMAIN.get(marketplace, _FALLBACK_DOMAIN)


def get_seller_central_url(marketplace: str = "") -> str:
    """获取指定站点的 Seller Central 入口 URL。"""
    return f"https://sellercentral.{_get_seller_central_domain(marketplace)}"


def get_orders_page_url(marketplace: str = "") -> str:
    """获取指定站点的订单管理页 URL。"""
    domain = _get_seller_central_domain(marketplace)
    return f"https://sellercentral.{domain}/orders-v3/ref=xx_myo_favb_xx"


def get_messages_page_url(marketplace: str = "") -> str:
    """获取指定站点的消息页 URL。"""
    domain = _get_seller_central_domain(marketplace)
    return f"https://sellercentral.{domain}/messaging/inbox?fi=all"


# 向后兼容：未选站点时的默认 URL（英国站）
AMAZON_SELLER_CENTRAL = get_seller_central_url("")
ORDERS_PAGE_URL = get_orders_page_url("")
MESSAGES_PAGE_URL = get_messages_page_url("")

# 线程局部存储：任务线程持有当前站点 URL，避免测试和后台任务互相覆盖
_url_store = threading.local()

def _get_sc_url() -> str:
    """获取当前线程的 Seller Central 基础 URL。"""
    return getattr(_url_store, 'seller_central', AMAZON_SELLER_CENTRAL)

def _get_orders_url() -> str:
    """获取当前线程的订单管理页 URL。"""
    return getattr(_url_store, 'orders_page', ORDERS_PAGE_URL)

def _get_messages_url() -> str:
    """获取当前线程的消息页 URL。"""
    return getattr(_url_store, 'messages_page', MESSAGES_PAGE_URL)


def _matches_sales_channel(order: OrderInfo, selected_channels: list[str]) -> bool:
    """未指定渠道时不限制；指定后只接受订单的精确销售渠道。"""
    return not selected_channels or order.sales_channel.strip().casefold() in {
        channel.casefold() for channel in selected_channels
    }

# 紫鸟 HTTP API 默认端口（从 ziniao_browser 继承，此处保留以兼容旧引用）
# DEFAULT_SOCKET_PORT 和 ZIGNIAO_API_TIMEOUT 已由上面导入

# 操作间隔（秒），加入随机抖动避免行为检测
MIN_DELAY = 0.2
MAX_DELAY = 0.8

# Amazon Business 欧盟账户不是需要发送说明书的终端买家，提取到该精确名称
# 时直接跳过订单，避免进入订单页回跳、买家联系页和邮件发送流程。
SKIP_BUYER_NAME = "Amazon Business EU SARL"

# 应用根目录 —— PyInstaller 单文件模式下 __file__ 指向临时解压目录，
# 需要改用 exe 所在目录，确保 output/、config.json、processed_orders.json
# 在多次运行间持久保留。
if getattr(sys, 'frozen', False):
    _APP_DIR = Path(sys.executable).parent
else:
    _APP_DIR = Path(__file__).parent

# 输出目录
OUTPUT_DIR = _APP_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 应用图标路径（PyInstaller --onefile 时图标在临时解压目录，否则在 exe/脚本同级目录）
_ICON_NAME = "amazon_mail_tool.ico"
if getattr(sys, 'frozen', False):
    # --onefile: sys._MEIPASS 是临时解压目录（--add-data 打包的文件在此）
    _MEIPASS = getattr(sys, '_MEIPASS', str(_APP_DIR))
    _ICON_PATH = Path(_MEIPASS) / _ICON_NAME
else:
    _ICON_PATH = _APP_DIR / _ICON_NAME

def get_app_icon_path() -> Optional[Path]:
    """获取应用图标路径，文件存在则返回，否则返回 None。"""
    # 兼容文件系统里可能带 ~tplv 后缀的实际文件名
    if _ICON_PATH.exists():
        return _ICON_PATH
    # 模糊匹配：找目录下以 "amazon_mail_tool" 开头的 .ico 文件
    candidates = sorted(_APP_DIR.glob("amazon_mail_tool*.ico"))
    if candidates:
        return candidates[0]
    return None

# ---- 已处理订单记录（防重复发送）----
# 默认共享文件（向后兼容）
PROCESSED_ORDERS_FILE = OUTPUT_DIR / "processed_orders.json"
DISABLED_ORDERS_FILE = OUTPUT_DIR / "processed_disabled_orders.json"
UNCONFIRMED_ORDERS_FILE = OUTPUT_DIR / "processed_unconfirmed_orders.json"
FOLLOWUP_NOT_SENT_ORDERS_FILE = OUTPUT_DIR / "processed_followup_not_sent_orders.json"

def _get_processed_orders_file(store_name: str = "") -> Path:
    """获取指定店铺的已处理订单文件路径。
    空字符串返回默认共享文件（向后兼容）；非空则按店铺名隔离。"""
    if not store_name:
        return PROCESSED_ORDERS_FILE
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', store_name)
    return OUTPUT_DIR / f"processed_orders_{safe_name}.json"


def _get_disabled_orders_file(store_name: str = "") -> Path:
    if not store_name:
        return DISABLED_ORDERS_FILE
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', store_name)
    return OUTPUT_DIR / f"processed_disabled_orders_{safe_name}.json"


def _get_unconfirmed_orders_file(store_name: str = "") -> Path:
    if not store_name:
        return UNCONFIRMED_ORDERS_FILE
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', store_name)
    return OUTPUT_DIR / f"processed_unconfirmed_orders_{safe_name}.json"


def _get_followup_not_sent_orders_file(store_name: str = "") -> Path:
    if not store_name:
        return FOLLOWUP_NOT_SENT_ORDERS_FILE
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', store_name)
    return OUTPUT_DIR / f"processed_followup_not_sent_orders_{safe_name}.json"

# 文件级锁：保护 load → modify → save 操作为原子操作
_processed_orders_locks: dict[str, threading.Lock] = {}
_processed_orders_locks_lock = threading.Lock()  # 保护锁字典本身

def _get_processed_orders_lock(filepath: Path) -> threading.Lock:
    """获取或创建指定文件的锁（线程安全）。"""
    key = str(filepath)
    with _processed_orders_locks_lock:
        if key not in _processed_orders_locks:
            _processed_orders_locks[key] = threading.Lock()
        return _processed_orders_locks[key]

def _load_order_ids(filepath: Path, store_name: str = "", strict: bool = False) -> set[str]:
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            ids = data.get("order_ids", [])
            label = f" ({store_name})" if store_name else ""
            print(f"[INFO] 已加载 {len(ids)} 条历史订单记录{label}")
            return set(ids)
        except Exception as e:
            print(f"[WARN] 读取历史订单记录失败: {e}")
            if strict:
                raise RuntimeError(f"无法读取已发送订单记录: {filepath.name}") from e
    return set()


def load_processed_orders(store_name: str = "") -> set[str]:
    """加载按店铺隔离的已发送订单号。"""
    return _load_order_ids(_get_processed_orders_file(store_name), store_name, strict=True)


def load_disabled_orders(store_name: str = "") -> set[str]:
    """加载按店铺隔离的联系主题禁用订单号。"""
    return _load_order_ids(_get_disabled_orders_file(store_name), store_name, strict=True)


def load_unconfirmed_orders(store_name: str = "") -> set[str]:
    """加载已点击发送但结果未确认的订单号。"""
    return _load_order_ids(_get_unconfirmed_orders_file(store_name), store_name, strict=True)


def load_followup_not_sent_orders(store_name: str = "") -> set[str]:
    """加载已发邮件但回访未执行、需人工处理的订单号。"""
    return _load_order_ids(_get_followup_not_sent_orders_file(store_name),
                           store_name, strict=True)


def _save_order_id(filepath: Path, order_id: str, store_name: str = "",
                   strict: bool = True) -> bool:
    """Append an order ID using the same JSON contract and per-file lock."""
    lock = _get_processed_orders_lock(filepath)
    with lock:
        existing = _load_order_ids(filepath, store_name, strict=strict)
        existing.add(order_id)
        data = {
            "order_ids": sorted(existing),
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(existing),
        }
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[WARN] 保存订单历史失败: {e}")
            return False


def save_processed_order(order_id: str, store_name: str = "") -> bool:
    """追加已发送订单，按店铺隔离。"""
    return _save_order_id(_get_processed_orders_file(store_name), order_id, store_name)


def save_disabled_order(order_id: str, store_name: str = "") -> bool:
    """追加联系主题禁用订单，按店铺隔离。"""
    return _save_order_id(_get_disabled_orders_file(store_name), order_id,
                          store_name, strict=True)


def save_unconfirmed_order(order_id: str, store_name: str = "") -> bool:
    """记录发送结果未确认的订单，禁止自动重发。"""
    return _save_order_id(_get_unconfirmed_orders_file(store_name), order_id, store_name)


def save_followup_not_sent_order(order_id: str, store_name: str = "") -> bool:
    """记录回访尚未执行的订单，供重跑跳过和人工补发。"""
    return _save_order_id(_get_followup_not_sent_orders_file(store_name),
                          order_id, store_name)

def clear_processed_orders(store_name: str = "") -> int:
    """清除所有历史记录，返回清除数量。store_name 非空时按店铺隔离。
    使用文件级锁确保读取计数→删除文件为原子操作。"""
    filepath = _get_processed_orders_file(store_name)
    lock = _get_processed_orders_lock(filepath)
    with lock:
        count = len(load_processed_orders(store_name))
        try:
            if filepath.exists():
                filepath.unlink()
            label = f" ({store_name})" if store_name else ""
            print(f"[INFO] 已清除 {count} 条历史订单记录{label}")
        except Exception as e:
            print(f"[WARN] 清除历史记录失败: {e}")
    return count

# ---- 文件日志 ----
_LOG_FILE = OUTPUT_DIR / f"执行日志_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

class _GuiLogger:
    """同时将输出写入日志文件和 GUI 文本组件（线程安全）。

    - 始终写入日志文件（行缓冲，实时刷新）
    - 如果绑定了 tk.Text 组件，通过 after() 在主线程安全写入
    - 不再写入 sys.stdout（console=False 时无控制台窗口）
    """

    def __init__(self, filepath: Path):
        self.terminal = sys.stdout  # 保留引用，开发模式下仍可用
        self.log = open(filepath, "w", encoding="utf-8", buffering=1)  # 行缓冲
        self._widget: Optional[tk.Text] = None
        self._closed = False

    def attach_widget(self, widget: tk.Text):
        """绑定 GUI 日志显示组件（线程安全——可从任意线程调用）。"""
        self._widget = widget

    def detach_widget(self):
        """解绑 GUI 日志组件。"""
        self._widget = None

    def write(self, message):
        # 写入日志文件（关闭后静默忽略——窗口关闭后仍可能有 print 调用）
        if not self._closed:
            try:
                self.log.write(message)
                self.log.flush()
            except (ValueError, OSError):
                pass  # 文件已被关闭
        # 如果绑定了 GUI 组件，通过 after() 序列化到主线程
        if self._widget is not None:
            try:
                self._widget.after(0, self._append_to_widget, message)
            except tk.TclError:
                pass  # 组件已销毁

    def _append_to_widget(self, message: str):
        """在主线程中追加文本到日志组件（由 after() 回调）。"""
        try:
            self._widget.configure(state='normal')
            self._widget.insert(tk.END, message)
            self._widget.see(tk.END)
            self._widget.configure(state='disabled')
        except tk.TclError:
            pass  # 组件已销毁

    def flush(self):
        if not self._closed:
            try:
                self.log.flush()
            except (ValueError, OSError):
                pass

    def close(self):
        self._closed = True
        self._widget = None
        try:
            self.log.close()
        except Exception:
            pass


_logger = _GuiLogger(_LOG_FILE)
sys.stdout = _logger

print(f"[LOG] 日志文件: {_LOG_FILE}")

# 日期格式校验正则
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 配置文件路径（保存紫鸟路径等持久化设置）
CONFIG_FILE = _APP_DIR / "config.json"

# 紫鸟默认搜索路径（按优先级排列）
SUPERBROWSER_DEFAULT_SEARCH_PATHS = [
    r"D:\紫鸟\SuperBrowser\starter.exe",
    r"D:\软件\SuperBrowser\starter.exe",
    r"C:\Program Files\SuperBrowser\starter.exe",
    r"C:\Program Files (x86)\SuperBrowser\starter.exe",
]

def load_config() -> dict:
    """从配置文件加载持久化设置。"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 读取配置文件失败: {e}")
    return {}


def save_config(config: dict):
    """保存持久化设置到配置文件。"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 配置已保存: {CONFIG_FILE}")
    except Exception as e:
        print(f"[WARN] 保存配置文件失败: {e}")


def get_superbrowser_path() -> Optional[str]:
    """
    获取紫鸟浏览器路径。

    优先级：
    1. config.json 中保存的路径（如果文件存在）
    2. 默认搜索路径中第一个存在的
    3. 返回 None（需用户手动输入）
    """
    config = load_config()

    # 1. 从配置读取
    saved_path = config.get("superbrowser_path", "")
    if saved_path and os.path.isfile(saved_path):
        return saved_path

    if saved_path and not os.path.isfile(saved_path):
        print(f"[WARN] 配置中的紫鸟路径已失效: {saved_path}")

    # 2. 搜索默认路径
    for p in SUPERBROWSER_DEFAULT_SEARCH_PATHS:
        if os.path.isfile(p):
            # 自动发现 → 写入配置
            config["superbrowser_path"] = p
            save_config(config)
            print(f"[INFO] 自动发现紫鸟路径: {p}")
            return p

    # 3. 找不到 → 需要用户输入
    return None


# ============================================================================
# 线程安全的 tkinter 辅助工具
# ============================================================================

# 模块级主线程 Tk 根窗口引用
_tk_root: Optional[tk.Tk] = None


def set_tk_root(root: tk.Tk):
    """设置主线程的 Tk 根窗口（在创建子线程前调用）。"""
    global _tk_root
    _tk_root = root


def _restore_tk_parent(parent) -> None:
    """
    强制恢复父窗口到可见/正常状态。

    Windows 下 Tk 的 transient 弹窗与根窗口最小化交互有已知怪癖：根窗口
    最小化期间模态弹窗关闭后，根窗口可能卡在最小化且无法从任务栏恢复
    （2026-08-18 用户报告「窗口最小化后无法唤起」）。另外后台线程触发弹窗
    时若窗口处于最小化，transient 弹窗会随父窗口一起最小化，用户看不到
    弹窗也无法点击，程序在 wait_window 上无限等待。

    因此模态弹窗创建前先恢复父窗口（保证弹窗可见可点），弹窗关闭后再
    显式 deiconify + lift + focus（保证主窗口可恢复）。全部容错。
    """
    if parent is None:
        return
    try:
        if not parent.winfo_exists():
            return
        if parent.state() == "iconic":
            parent.deiconify()
        parent.lift()
        parent.focus_force()
    except Exception:
        pass


def _run_on_tk_thread(dialog_factory):
    """
    从任意线程安全地创建 Toplevel 对话框。

    dialog_factory(parent) → result
    接收一个 Toplevel 父窗口，创建并运行对话框，返回结果。
    实际执行总是序列化到主线程。

    交互式对话框以等待用户操作为主：只要 tk 主线程存活，就一直阻塞等待
    用户操作；仅当 tk 主线程确实已退出时才回退返回 None。
    弹窗为 modal（grab_set），等待期间仅检查 tk 主线程存活作为回退；不响应
    全局停止/暂停（避免干跑模式 stop 置位后把汇总弹窗连带杀掉）。
    """
    if _tk_root is None:
        return dialog_factory(None)

    # 检查 root 是否仍然有效
    try:
        _tk_root.winfo_exists()
    except Exception:
        return dialog_factory(None)

    import queue
    result_queue: queue.Queue = queue.Queue()

    def _wrapper():
        try:
            # 弹窗前恢复父窗口：父窗口最小化时弹窗随之下沉，用户看不到
            # 也无法点击，程序会卡死在 wait_window 上
            _restore_tk_parent(_tk_root)
            result = dialog_factory(_tk_root)
            # 弹窗关闭后再次恢复父窗口：修复根窗口最小化期间弹窗关闭后
            # 卡在最小化、无法从任务栏唤起的 Tk 怪癖
            _restore_tk_parent(_tk_root)
            result_queue.put(("ok", result))
        except Exception as e:
            result_queue.put(("err", e))

    # 始终通过 after() 排队到主线程，避免 "main thread is not in main loop"
    try:
        _tk_root.after(0, _wrapper)
    except Exception:
        # tk root 已被销毁，回退到无 parent 模式
        return dialog_factory(None)

    # 交互式对话框以等待用户操作为主：只要 tk 主线程存活就一直轮询等待，
    # 仅当 tk 主线程确实已退出时才回退返回 None。
    while True:
        try:
            status, value = result_queue.get(timeout=0.5)
            if status == "err":
                raise value
            return value
        except queue.Empty:
            pass
        # 每轮轮询后检查主线程/root 是否仍存活
        try:
            root_alive = _tk_root.winfo_exists()
        except Exception:
            root_alive = False
        if not root_alive:
            # tk 主线程已退出，对话框永远无法返回结果，回退
            print("  [WARN] tk 主线程已退出，对话框无响应")
            return None


# ============================================================================
# 数据模型
# ============================================================================

def _should_skip_buyer_order(order: OrderInfo) -> bool:
    """仅当提取到的买家名称精确匹配时跳过订单。"""
    return order.buyer_name == SKIP_BUYER_NAME


# ============================================================================
# 工具函数
# ============================================================================

def random_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY):
    """随机延迟，降低被亚马逊行为检测的风险。响应暂停/终止事件。"""
    duration = random.uniform(min_s, max_s)
    elapsed = 0.0
    step = 0.15  # 每 150ms 检查一次事件
    while elapsed < duration:
        _check_stop_global()
        _wait_if_paused_global()
        time.sleep(min(step, duration - elapsed))
        elapsed += step


def timestamp_str() -> str:
    """返回当前时间戳字符串，用于文件命名"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_find(page: Page, selectors: list[str], timeout: int = 5000) -> Optional[ElementHandle]:
    """
    尝试多个选择器，返回第一个匹配的元素。
    使用累积超时：每个选择器有合理的等待时间，总计不超过 timeout。
    """
    per_selector = max(1500, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=per_selector)
            return loc.element_handle()
        except Exception:
            continue
    return None


def _wait_page_ready(page: Page, label: str = "", timeout: int = 5000):
    """
    安全等待页面加载完成。
    - 使用 domcontentloaded 而非 networkidle：Amazon SPA 有永不关闭的长轮询连接，
      networkidle 永远达不到，每次白白等满超时。domcontentloaded 在 SPA 场景下
      立即返回（DOM 已加载），在偶发的全页跳转时有效等待。
    - 超时不抛异常，打印警告后继续。
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        if label:
            print(f"  [INFO] '{label}' 等待超时，继续执行...")


def _wait_orders_table_ready(page: Page, label: str = "",
                             timeout: float = 30.0, stable_rounds: int = 5,
                             lazy_load: bool = True) -> bool:
    """
    等待订单表格渲染完成（新版卖家平台 orders-v3 为 SPA + 懒加载）。

    实测渲染生命周期（2026-08-13, Chrome 138）：
      t≈2s   spinner(#orders-table-loading-spinner) 出现，页面显示
             "未找到与指定搜索条件相匹配的订单。"占位文本
      t≈4s   spinner 消失，仅渲染前 15 行（懒加载首波）
      t≈10s  其余行数懒加载补足，期间无 spinner；需向下滚动窗口
             才能触发剩余部分加载（用户实测确认）
    因此等待分两阶段：
      1) 等 spinner 消失（防 0 订单占位符误判）；
      2) "滚到底 → 数行数"循环，行数连续 stable_rounds 轮滚动不变
         才算加载完（覆盖懒加载的假静止期）。

    参数:
        lazy_load: True=完整模式（阶段2滚到底触发懒加载，行数稳定 stable_rounds 轮）；
                   False=轻量模式（不滚动，首波行稳定 2 轮即返回）。
                   搜索 ASIN / 设置时间区间阶段用轻量模式，避免在筛选未定型时
                   反复触发整表懒加载；提取/翻页/回跳阶段用完整模式。

    超时仅告警、不抛异常，由调用方继续原有流程（扫描函数自带诊断）。
    """
    deadline = time.time() + timeout

    def _snapshot() -> Optional[tuple]:
        try:
            return tuple(page.evaluate(
                r"""() => {
                    const el = document.querySelector('#orders-table-loading-spinner');
                    const spinner = !!(el && (el.offsetWidth || el.offsetHeight));
                    const rows = [...document.querySelectorAll('tr')]
                        .filter(r => /\d{3}-\d{7}-\d{7}/.test(r.textContent || '')).length;
                    const txt = (document.body.textContent || '').replace(/\s+/g, ' ');
                    const empty = txt.includes('未找到与指定搜索条件相匹配的订单')
                               || txt.includes('No orders found');
                    return [spinner, rows, empty];
                }"""))
        except Exception:
            return None

    def _scroll_to_bottom() -> None:
        """滚动页面到底部，触发懒加载（异常不影响等待流程）。"""
        try:
            page.evaluate(
                r"""() => {
                    const sc = document.scrollingElement || document.body;
                    sc.scrollTop = sc.scrollHeight;
                    window.scrollTo(0, document.body.scrollHeight);
                }""")
        except Exception:
            pass

    # 阶段1：等 spinner 消失（若出现过）
    while time.time() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        snap = _snapshot()
        if snap is None:
            time.sleep(0.5)
            continue
        if snap[0]:
            time.sleep(0.3)
            continue
        break

    # 阶段2：滚动加载循环，行数连续稳定才算就绪
    # （轻量模式不滚动：首波行稳定 2 轮即返回，不触发剩余行的懒加载）
    stable = 0
    last_rows, last_empty = -1, False
    while time.time() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        if lazy_load:
            _scroll_to_bottom()
        time.sleep(1.0)
        snap = _snapshot()
        if snap is None:
            time.sleep(0.5)
            continue
        spinner, rows, empty = snap
        if spinner:
            # 又开始加载，重置稳定计数
            stable = 0
            last_rows, last_empty = -1, False
            continue
        if rows == last_rows and empty == last_empty:
            stable += 1
        else:
            stable = 1
            last_rows, last_empty = rows, empty
        if last_rows > 0 or last_empty:
            # 有订单行：完整模式需 stable_rounds 轮滚动不变
            # （≈7s，覆盖懒加载平台期 ~6s）；轻量模式 2 轮即可（只等首波行）；
            # 空状态：2 轮即可（真·空结果约 3s 即稳定，不必多等）
            if lazy_load and last_rows > 0:
                need = stable_rounds
            else:
                need = 2
            if stable >= need:
                mode = "懒加载完成" if (lazy_load and last_rows > 0) else "轻量就绪"
                state = f"{last_rows} 行订单" if last_rows else "空状态已确认"
                print(f"  [等待就绪] {label}: 表格稳定（{state}，{mode}）")
                return True
        else:
            # 无订单行且无空状态提示：可能仍在初始化，继续等
            stable = 0

    print(f"  [WARN] 等待订单表格就绪超时 ({timeout}s)，继续执行")
    return False


def safe_click(page: Page, selectors: list[str], label: str = "") -> bool:
    """
    尝试点击匹配的第一个元素。成功返回 True。
    """
    el = safe_find(page, selectors)
    if el:
        try:
            el.click()
            if label:
                print(f"  [OK] 点击: {label}")
            random_delay(0.3, 1.0)
            return True
        except Exception:
            pass
    # 回退：尝试按文本内容点击
    for text in [label] if label else []:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=3000)
            print(f"  [OK] 文本点击: {text}")
            random_delay(0.3, 1.0)
            return True
        except Exception:
            continue
    print(f"  [FAIL] 无法点击: {label or selectors}")
    return False


# ============================================================================
# 运行状态追踪 + 浮窗
# ============================================================================

# ============================================================================
# 回访信息站点解析
# ============================================================================

def parse_follow_up_messages(raw_text: str) -> dict[str, str]:
    """
    解析用户输入的回访信息，支持三种格式：

    格式1（JSON二维数组，用户常用）：
        [["Amazon.de", "Guten Tag!..."],["Amazon.fr", "Bonjour!..."]]

    格式2（JSON对象数组）：
        [{"站点":"de","回访信息":"Guten Tag!..."},{"站点":"fr","回访信息":"Bonjour!..."}]

    格式3（内联列表）：
        [[de，Guten Tag!...],[fr，Bonjour!...]]

    返回：站点代码 → 回访消息 的映射字典。
    无法解析为站点映射格式时返回空字典（视为纯文本回访消息）。
    """
    if not raw_text or not raw_text.strip():
        return {}

    text = raw_text.strip()

    # 如果以 [ 开头，尝试解析为 JSON 或 Python 字面量
    if text.startswith("["):
        items = None
        # 先尝试 JSON
        try:
            items = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 再尝试 Python 字面量（支持 """...""" 三引号等多行字符串）
            try:
                import ast
                items = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                pass

        if isinstance(items, list) and len(items) > 0:
            result = {}
            skipped = 0
            for item in items:
                if isinstance(item, list) and len(item) >= 2:
                    # 格式1: ["Amazon.de", "message text"]
                    site = str(item[0]).strip().lower()
                    msg = str(item[1]).strip()
                    if site and msg:
                        result[site] = msg
                    else:
                        skipped += 1
                elif isinstance(item, dict):
                    site = (item.get("站点") or item.get("site") or "").strip().lower()
                    msg = (item.get("回访信息") or item.get("message") or "").strip()
                    if site and msg:
                        result[site] = msg
                    else:
                        skipped += 1
                else:
                    skipped += 1
                    print(f"  [WARN] 跳过无法识别的条目类型: {type(item).__name__}: {str(item)[:80]}")
            if skipped > 0:
                print(f"  [WARN] {skipped} 个条目因格式不匹配被跳过")
            if result:
                print(f"[INFO] 解析站点回访信息 (JSON数组): {list(result.keys())}")
                return result
            else:
                print("[WARN] JSON/列表解析成功但无有效站点条目，请检查数组元素格式")

    # 尝试格式3：[[站点，回访信息],[站点,回访信息]]（内联文本，非JSON）
    if text.startswith("[[") and text.endswith("]]"):
        try:
            pairs = re.findall(r'\[([^\[\]]+?)[，,]([^\[\]]+?)\]', text)
            if pairs:
                result = {}
                for site, msg in pairs:
                    site = site.strip().lower()
                    msg = msg.strip()
                    if site and msg:
                        result[site] = msg
                if result:
                    print(f"[INFO] 解析站点回访信息 (内联列表): {list(result.keys())}")
                    return result
        except Exception:
            pass

    # 无法解析为站点映射
    # 如果输入以 [ 开头（疑似多站点格式），打印警告帮助排查
    if text.startswith("["):
        print("[WARN] 输入疑似多站点回访格式（以 [ 开头），但 JSON 和 Python 字面量解析均失败")
        print("[WARN] 请检查格式是否正确：使用标准 JSON 双引号，或内联 [[站点,消息]] 格式")
    return {}


def get_follow_up_message_for_order(
    follow_up_messages: dict[str, str],
    sales_channel: str,
    fallback_message: str = "",
) -> str:
    """
    根据订单的销售渠道获取对应的回访消息。

    从 sales_channel（如 "Amazon.de"）中提取站点后缀（如 "de"），
    在 follow_up_messages 中查找匹配的回访消息。

    参数：
        follow_up_messages: 站点代码 → 回访消息 映射
        sales_channel: 销售渠道，如 "Amazon.de", "Amazon.fr"
        fallback_message: 如果没有匹配的站点消息，使用的回退消息

    返回：匹配的回访消息，或回退消息
    """
    # 单站点模式（纯文本回访消息）：直接使用回退消息
    if not follow_up_messages:
        # 安全检查：如果回退消息看起来像未成功解析的多站点格式数据
        # （以 [ 开头，如 [["Amazon.de","..."],...]），则不要使用它，
        # 避免将整个站点→消息映射的原始 data 全部发送给用户。
        stripped = (fallback_message or "").strip()
        if stripped.startswith("["):
            print("[WARN] 回访消息格式疑似多站点数据但解析失败，拒绝将原始数据作为消息发送")
            print("[WARN] 请检查回访信息是否使用了正确的 JSON 格式，或切换为纯文本模式")
            return ""
        return fallback_message

    # 多站点模式但无法识别订单站点：不发送，避免将原始 JSON 数据作为消息发出
    if not sales_channel:
        print("[WARN] 多站点模式但无法识别订单站点，跳过回访")
        return ""

    # 北美三个销售渠道共用 amazon.com 回访模板。
    channel_key = sales_channel.lower().strip()
    if channel_key in {"amazon.com", "amazon.ca", "amazon.com.mx"}:
        channel_key = "amazon.com"

    if channel_key in follow_up_messages:
        print(f"[INFO] 站点匹配: {sales_channel} → 使用站点专用回访消息")
        return follow_up_messages[channel_key]

    # 多站点模式但无匹配 → 不发送
    print(f"[WARN] 站点 '{sales_channel}' 无对应回访消息模板，跳过回访")
    return ""


# ============================================================================
# GUI 输入弹窗
# ============================================================================

class InputDialog:
    """
    tkinter 弹窗：收集单店铺批量发送任务的输入参数。
    """

    # ---- 字体常量 ----
    _FONT = ("Microsoft YaHei", 9)
    _FONT_BOLD = ("Microsoft YaHei", 9, "bold")
    _FONT_TITLE = ("Microsoft YaHei", 10, "bold")
    _FONT_SMALL = ("Microsoft YaHei", 8)
    _LABEL_W = 12  # 标签统一宽度（字符数）

    def __init__(self):
        self.results: list[TaskConfig] = []

        # === 执行模式状态 ===
        self._exec_mode = False
        self._stop_event: Optional[threading.Event] = None
        self._pause_event: Optional[threading.Event] = None
        self._return_event: threading.Event = threading.Event()
        self._status_holder: Optional[StatusHolder] = None
        self._threads: list[threading.Thread] = []
        self._polling_id: Optional[str] = None
        self._closing = False

        # === 执行模式组件引用（按需创建）===
        self._exec_frame: Optional[tk.Frame] = None
        self._log_text: Optional[tk.Text] = None
        self._status_labels: dict = {}

        # === 底部按钮引用 ===
        self._bottom_frame: Optional[tk.Frame] = None
        self.btn_start: Optional[tk.Button] = None
        self.btn_cancel: Optional[tk.Button] = None
        self.btn_pause: Optional[tk.Button] = None
        self.btn_stop_btn: Optional[tk.Button] = None
        self.btn_return: Optional[tk.Button] = None

        self.root = tk.Tk()
        self.root.title("亚马逊批量发送邮件")
        self.root.configure(bg='#F7F8FA')
        self.root.resizable(True, True)
        self.root.minsize(680, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # 设置窗口图标
        icon_path = get_app_icon_path()
        if icon_path:
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        # 按屏幕尺寸计算初始窗口，后续布局由 fill/expand 跟随窗口变化。
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(960, max(680, int(sw * 0.78)))
        h = min(920, max(620, int(sh * 0.82)))
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self._build_ui()

    # ------------------------------------------------------------------
    # 卡片布局辅助
    # ------------------------------------------------------------------
    def _make_card(self, parent, title):
        """创建一个白色卡片 Frame，返回 (card, content)。"""
        card = tk.Frame(parent, bg='white', highlightthickness=1,
                        highlightbackground='#E5E7EB', highlightcolor='#E5E7EB')
        # 标题行
        header = tk.Frame(card, bg='white')
        header.pack(fill='x', padx=18, pady=(14, 6))
        tk.Label(header, text=title, font=self._FONT_TITLE,
                 bg='white', fg='#1F2937').pack(side='left')
        ttk.Separator(card, orient='horizontal').pack(fill='x', padx=18)
        # 内容区
        content = tk.Frame(card, bg='white')
        content.pack(fill='both', expand=True, padx=18, pady=(10, 14))
        return card, content

    @staticmethod
    def _label(parent, text, fg='#6B7280', **kw):
        """统一标签样式。"""
        return tk.Label(parent, text=text, font=InputDialog._FONT,
                        bg='white', fg=fg, **kw)

    @staticmethod
    def _entry(parent, width=None, **kw):
        """统一扁平输入框（无阴影，1px 边框）。"""
        opts = dict(font=InputDialog._FONT, relief='flat', borderwidth=1,
                    highlightthickness=1, highlightbackground='#D1D5DB',
                    highlightcolor='#2563EB')
        if width:
            opts['width'] = width
        opts.update(kw)
        return tk.Entry(parent, **opts)

    @staticmethod
    def _btn_secondary(parent, text, command):
        """灰色次要按钮。"""
        return tk.Button(parent, text=text, font=InputDialog._FONT,
                         bg='#F3F4F6', fg='#374151', relief='flat',
                         padx=14, pady=3, command=command,
                         activebackground='#D1D5DB', activeforeground='#374151',
                         cursor='hand2')

    @staticmethod
    def _btn_primary(parent, text, command):
        """蓝色主按钮。"""
        return tk.Button(parent, text=text, font=InputDialog._FONT_BOLD,
                         bg='#2563EB', fg='white', relief='flat',
                         padx=24, pady=5, command=command,
                         activebackground='#1D4ED8', activeforeground='white',
                         cursor='hand2')

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        """构建 Windows 11 Fluent Design 风格 UI — 4 卡片模块布局。"""
        saved_config = load_config()

        # ---- 全局去阴影：ttk 扁平样式 ----
        style = ttk.Style()
        style.configure('TCombobox', relief='flat', borderwidth=1)

        # ---- 滚动区域（Canvas + Scrollbar）----
        canvas_frame = tk.Frame(self.root, bg='#F7F8FA')
        canvas_frame.pack(fill='both', expand=True)
        self._input_container = canvas_frame  # 保存引用，执行模式切换用

        self._canvas = tk.Canvas(canvas_frame, bg='#F7F8FA', highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient='vertical', command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side='right', fill='y')
        self._canvas.pack(side='left', fill='both', expand=True)

        # ---- 卡片主容器（Canvas 内部）----
        main = tk.Frame(self._canvas, bg='#F7F8FA')
        self._canvas_window = self._canvas.create_window((0, 0), window=main, anchor='nw')

        # Canvas 宽度变化 → 同步 inner frame 宽度 + 更新滚动区域
        def _on_canvas_configure(event):
            self._canvas.itemconfig(self._canvas_window, width=event.width)
            content_height = self._scroll_inner.winfo_reqheight() + 22
            self._canvas.itemconfig(
                self._canvas_window, height=max(event.height, content_height))
            self._canvas.configure(
                scrollregion=(0, 0, event.width, max(event.height, content_height)))
        self._canvas.bind('<Configure>', _on_canvas_configure)

        # 鼠标滚轮滚动
        def _on_mousewheel(event):
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        self._canvas.bind('<Enter>', lambda e: self._canvas.bind_all('<MouseWheel>', _on_mousewheel))
        self._canvas.bind('<Leave>', lambda e: self._canvas.unbind_all('<MouseWheel>'))

        # 卡片内容 padding
        self._scroll_inner = tk.Frame(main, bg='#F7F8FA')
        self._scroll_inner.pack(fill='both', expand=True, padx=16, pady=(14, 8))
        main = self._scroll_inner  # 后续代码用 main 作为卡片容器

        # Inner frame 大小变化 → 更新滚动区域
        def _on_inner_configure(event):
            ch = self._canvas.winfo_height()
            fh = self._scroll_inner.winfo_reqheight() + 22
            self._canvas.configure(
                scrollregion=(0, 0, self._canvas.winfo_width(), max(fh, ch)))
        self._scroll_inner.bind('<Configure>', _on_inner_configure)

        # ================================================================
        # Card 1: 基础环境配置
        # ================================================================
        card1, c1 = self._make_card(main, "基础环境配置")

        # 紫鸟浏览器路径
        r1 = tk.Frame(c1, bg='white')
        r1.pack(fill='x', pady=(0, 10))
        self._label(r1, "浏览器路径", width=self._LABEL_W, anchor='w').pack(side='left')
        self.entry_superbrowser = self._entry(r1)
        self.entry_superbrowser.pack(side='left', fill='x', expand=True, padx=(10, 0))
        self._btn_secondary(r1, "浏览", self._select_superbrowser).pack(side='left', padx=(8, 0))

        saved_path = get_superbrowser_path()
        if saved_path:
            self.entry_superbrowser.insert(0, saved_path)

        # 紫鸟登录信息
        r2 = tk.Frame(c1, bg='white')
        r2.pack(fill='x')
        self._label(r2, "登录信息", width=self._LABEL_W, anchor='w').pack(side='left')
        login_frame = tk.Frame(r2, bg='white')
        login_frame.pack(side='left', fill='x', expand=True, padx=(10, 0))

        for col, (lbl, var_name, w) in enumerate([("公司", "entry_company", 16),
                                                  ("用户名", "entry_username", 10),
                                                  ("密码", "entry_password", 10),
                                                  ("端口", "entry_port", 5)]):
            label_col = col * 2
            entry_col = label_col + 1
            tk.Label(login_frame, text=lbl, font=self._FONT,
                     bg='white', fg='#9CA3AF').grid(
                         row=0, column=label_col, sticky='w', padx=(0, 2))
            ent = self._entry(login_frame, width=w,
                              show='*' if lbl == '密码' else '')
            ent.grid(row=0, column=entry_col, sticky='ew',
                     padx=(0, 10) if lbl != '端口' else (0, 0))
            login_frame.grid_columnconfigure(entry_col, weight=1)
            setattr(self, var_name, ent)

        self.entry_port.insert(0, str(saved_config.get('socket_port', DEFAULT_SOCKET_PORT)))

        # 预填登录信息
        for key, attr in [('company', 'entry_company'), ('username', 'entry_username'),
                          ('password', 'entry_password')]:
            if saved_config.get(key):
                getattr(self, attr).insert(0, saved_config[key])

        # 飞书凭据和表 ID 与紫鸟登录信息一样保存在本机配置。
        for label, attr, key, masked, default in (
            ("飞书 App ID", "entry_feishu_app_id", "feishu_app_id", False, ""),
            ("飞书 App Secret", "entry_feishu_app_secret", "feishu_app_secret", True, ""),
            ("美规回传表 ID", "entry_feishu_report_table_id", "feishu_report_table_id", False,
             os.environ.get("FEISHU_REPORT_TABLE_ID", "")),
            ("产品映射表 ID", "entry_feishu_product_table_id", "feishu_product_table_id", False,
             os.environ.get("FEISHU_PRODUCT_TABLE_ID", "")),
        ):
            row = tk.Frame(c1, bg='white')
            row.pack(fill='x', pady=(10, 0))
            self._label(row, label, width=self._LABEL_W, anchor='w').pack(side='left')
            entry = self._entry(row, show='*' if masked else '')
            entry.pack(side='left', fill='x', expand=True, padx=(10, 0))
            if saved_config.get(key, default):
                entry.insert(0, saved_config.get(key, default))
            setattr(self, attr, entry)

        card1.pack(fill='x', pady=(0, 12))

        # ================================================================
        # Card 2: 店铺任务设置
        # ================================================================
        card2, c2 = self._make_card(main, "店铺任务设置")

        # ---- 店铺名称 ----
        rn1 = tk.Frame(c2, bg='white')
        rn1.pack(fill='x', pady=(0, 12))
        self._label(rn1, "店铺名称", width=self._LABEL_W, anchor='w').pack(side='left')
        self.entry_store1 = self._entry(rn1)
        self.entry_store1.pack(side='left', fill='x', expand=True, padx=(10, 0))
        if saved_config.get('store_name'):
            self.entry_store1.insert(0, saved_config['store_name'])

        # ---- 店铺设置 ----
        self._store_section_header(c2, "店铺")
        self._store_fields(c2, '1',
                           saved_config.get('asins', ''),
                           saved_config.get('pdf_path', ''),
                           saved_config.get('marketplace', ''),
                           saved_config.get('followup_text', ''),
                           saved_config.get('sales_channels', []))

        # 店铺1 日期范围
        self._make_date_row(c2, '1',
                            saved_config.get('start_date', datetime.now().strftime('%Y-%m-01')),
                            saved_config.get('end_date', datetime.now().strftime('%Y-%m-%d')))

        card2.pack(fill='x', pady=(0, 12))

        # ================================================================
        # Card 3: 回访消息模板
        # ================================================================
        card3, c3 = self._make_card(main, "回访消息模板")

        # 回访信息
        fw1 = tk.Frame(c3, bg='white')
        fw1.pack(fill='both', expand=True, pady=(0, 8))
        tk.Label(fw1, text="消息", font=self._FONT_BOLD,
                 bg='white', fg='#2563EB', width=6, anchor='w').pack(side='left')
        self.text_followup1 = tk.Text(fw1, font=self._FONT, relief='flat', borderwidth=1,
                                       highlightthickness=1, highlightbackground='#E5E7EB',
                                       height=3, wrap='word', undo=True)
        self.text_followup1.pack(side='left', fill='both', expand=True, padx=(6, 0))
        if saved_config.get('followup_text'):
            self.text_followup1.insert('1.0', saved_config['followup_text'])

        card3.pack(fill='both', expand=True, pady=(0, 12))

        # ================================================================
        # 底部操作栏（固定在窗口底部，不随卡片滚动）
        # ================================================================
        self._bottom_frame = tk.Frame(self.root, bg='#F7F8FA')
        self._bottom_frame.pack(side='bottom', fill='x', padx=16, pady=(4, 10))

        # ---- 输入模式按钮 ----
        self.btn_cancel = tk.Button(self._bottom_frame, text="取消", font=self._FONT,
                  bg='#E5E7EB', fg='#374151', relief='flat',
                  padx=22, pady=4, command=self.root.destroy,
                  activebackground='#D1D5DB', activeforeground='#374151',
                  cursor='hand2')
        self.btn_cancel.pack(side='right', padx=(12, 0))

        self.btn_start = tk.Button(self._bottom_frame, text="开始执行", font=self._FONT_BOLD,
                  bg='#2563EB', fg='white', relief='flat',
                  padx=28, pady=6, command=self._on_start,
                  activebackground='#1D4ED8', activeforeground='white',
                  cursor='hand2')
        self.btn_start.pack(side='right')

        # ---- 执行模式按钮（初始隐藏）----
        self.btn_pause = tk.Button(self._bottom_frame, text="暂停", font=self._FONT_BOLD,
                  bg='#2563EB', fg='white', relief='flat',
                  padx=24, pady=5, command=self._on_pause,
                  activebackground='#1D4ED8', activeforeground='white',
                  cursor='hand2')

        self.btn_stop_btn = tk.Button(self._bottom_frame, text="终止", font=self._FONT,
                  bg='#E5E7EB', fg='#374151', relief='flat',
                  padx=22, pady=4, command=self._on_stop,
                  activebackground='#D1D5DB', activeforeground='#374151',
                  cursor='hand2')

        self.btn_return = tk.Button(self._bottom_frame, text="返回启动界面", font=self._FONT,
                  bg='#F3F4F6', fg='#374151', relief='flat',
                  padx=22, pady=4, command=self._on_return,
                  activebackground='#D1D5DB', activeforeground='#374151',
                  cursor='hand2')

    # ------------------------------------------------------------------
    # 店铺行构建辅助
    # ------------------------------------------------------------------
    def _store_section_header(self, parent, text):
        """店铺小节标题条。"""
        bar = tk.Frame(parent, bg='#F9FAFB')
        bar.pack(fill='x', pady=(2, 6))
        tk.Label(bar, text=text, font=self._FONT_BOLD,
                 bg='#F9FAFB', fg='#2563EB').pack(side='left', padx=2, pady=2)

    def _make_date_row(self, parent, idx, default_start, default_end):
        """构建日期范围输入行，挂到 self.entry_start_date{idx} / self.entry_end_date{idx}。"""
        row = tk.Frame(parent, bg='white')
        row.pack(fill='x', pady=(10, 0))
        self._label(row, "订单日期范围", width=self._LABEL_W, anchor='w').pack(side='left')
        inner = tk.Frame(row, bg='white')
        inner.pack(side='left', fill='x', expand=True, padx=(10, 0))
        tk.Label(inner, text="从", font=self._FONT, bg='white', fg='#9CA3AF').pack(side='left')
        entry_s = self._entry(inner, width=12)
        entry_s.pack(side='left', fill='x', expand=True, padx=(4, 12))
        entry_s.insert(0, default_start)
        tk.Label(inner, text="到", font=self._FONT, bg='white', fg='#9CA3AF').pack(side='left')
        entry_e = self._entry(inner, width=12)
        entry_e.pack(side='left', fill='x', expand=True, padx=(4, 0))
        entry_e.insert(0, default_end)
        setattr(self, f'entry_start_date{idx}', entry_s)
        setattr(self, f'entry_end_date{idx}', entry_e)

    def _store_fields(self, parent, idx, asin_default, pdf_default, mkt_default, fw_default,
                      channels_default=None):
        """构建单店铺输入行：目标站点 / ASIN / PDF。"""
        # 第1行：目标站点
        r1 = tk.Frame(parent, bg='white')
        r1.pack(fill='x', pady=(0, 6))
        self._label(r1, "目标站点", width=self._LABEL_W, anchor='w').pack(side='left')
        mkt_var = tk.StringVar()
        combo = ttk.Combobox(r1, textvariable=mkt_var, values=MARKETPLACE_OPTIONS,
                             state='readonly', width=14, font=self._FONT)
        combo.pack(side='left', fill='x', expand=True, padx=(10, 0))
        setattr(self, f'marketplace_var{idx}', mkt_var)
        setattr(self, f'combo_marketplace{idx}', combo)
        if mkt_default and mkt_default in MARKETPLACE_OPTIONS:
            combo.set(mkt_default)

        self._label(r1, "实发站点", anchor='w').pack(side='left', padx=(16, 6))
        selected = set(channels_default) if isinstance(channels_default, list) else set()
        channel_vars = {
            channel: tk.BooleanVar(value=channel in selected)
            for channel in SALES_CHANNEL_OPTIONS
        }
        summary = tk.StringVar()

        def refresh_summary():
            chosen = [SALES_CHANNEL_OPTIONS[channel] for channel, var in channel_vars.items()
                      if var.get()]
            summary.set("全部" if not chosen else
                        chosen[0] if len(chosen) == 1 else f"已选 {len(chosen)} 个站点")

        menu_button = tk.Menubutton(r1, textvariable=summary, relief='solid',
                                    borderwidth=1, width=16, anchor='w',
                                    bg='white', fg='#374151', font=self._FONT,
                                    indicatoron=True)
        menu_button.pack(side='left')
        menu = tk.Menu(menu_button, tearoff=False)
        for channel, country in SALES_CHANNEL_OPTIONS.items():
            menu.add_checkbutton(label=country,
                                 variable=channel_vars[channel], command=refresh_summary)
        menu_button.configure(menu=menu)
        refresh_summary()
        setattr(self, f'sales_channel_vars{idx}', channel_vars)

        # 第2行：ASIN
        r2 = tk.Frame(parent, bg='white')
        r2.pack(fill='x', pady=(0, 6))
        self._label(r2, "ASIN", width=self._LABEL_W, anchor='w').pack(side='left')
        entry_asin = self._entry(r2)
        entry_asin.pack(side='left', fill='x', expand=True, padx=(10, 0))
        setattr(self, f'entry_asins{idx}', entry_asin)
        if asin_default:
            entry_asin.insert(0, asin_default)

        # 第3行：说明书PDF
        r3 = tk.Frame(parent, bg='white')
        r3.pack(fill='x', pady=(0, 4))
        self._label(r3, "说明书PDF", width=self._LABEL_W, anchor='w').pack(side='left')
        entry_pdf = self._entry(r3)
        entry_pdf.pack(side='left', fill='x', expand=True, padx=(10, 0))
        setattr(self, f'entry_pdf{idx}', entry_pdf)
        if pdf_default and os.path.isfile(pdf_default):
            entry_pdf.insert(0, pdf_default)
        self._btn_secondary(r3, "选择文件", self._select_pdf1).pack(side='left', padx=(8, 0))

    def _select_superbrowser(self):
        """打开文件选择器选紫鸟 starter.exe"""
        path = filedialog.askopenfilename(
            title="选择紫鸟浏览器程序 (starter.exe)",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")]
        )
        if path:
            self.entry_superbrowser.delete(0, tk.END)
            self.entry_superbrowser.insert(0, path)

    def _select_pdf1(self):
        """选择说明书 PDF。"""
        path = filedialog.askopenfilename(
            title="选择说明书 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")]
        )
        if path:
            self.entry_pdf1.delete(0, tk.END)
            self.entry_pdf1.insert(0, path)

    def _on_start(self):
        """用户点击'开始执行'：校验输入 → 封装配置 → 切换到执行模式"""
        # 读取公共字段
        company = self.entry_company.get().strip()
        username = self.entry_username.get().strip()
        password = self.entry_password.get().strip()
        feishu_app_id = self.entry_feishu_app_id.get().strip()
        feishu_app_secret = self.entry_feishu_app_secret.get().strip()
        feishu_report_table_id = self.entry_feishu_report_table_id.get().strip()
        feishu_product_table_id = self.entry_feishu_product_table_id.get().strip()
        store_name = self.entry_store1.get().strip()
        superbrowser_raw = self.entry_superbrowser.get().strip()

        asins_raw = self.entry_asins1.get().strip()
        pdf_path = self.entry_pdf1.get().strip()
        followup = self.text_followup1.get("1.0", tk.END).strip()
        marketplace = self.marketplace_var1.get()
        selected_channels = [channel for channel, var in
                             getattr(self, 'sales_channel_vars1', {}).items() if var.get()]
        start_date = self.entry_start_date1.get().strip()
        end_date = self.entry_end_date1.get().strip()

        # ---- 校验 ----
        errors = []

        if superbrowser_raw and not os.path.isfile(superbrowser_raw):
            errors.append(f"紫鸟浏览器路径无效: {superbrowser_raw}")
        if not company:
            errors.append("紫鸟公司名不能为空")
        if not username:
            errors.append("紫鸟用户名不能为空")
        if not password:
            errors.append("紫鸟密码不能为空")
        if bool(feishu_app_id) != bool(feishu_app_secret):
            errors.append("飞书 App ID 和 App Secret 必须同时填写")
        if not (feishu_app_id or os.environ.get("FEISHU_APP_ID")) or not (
                feishu_app_secret or os.environ.get("FEISHU_APP_SECRET")):
            errors.append("请填写飞书 App ID 和 App Secret")
        for label, table_id in (("美规回传表", feishu_report_table_id),
                                ("产品名称映射表", feishu_product_table_id)):
            if not re.fullmatch(r"tbl[A-Za-z0-9]+", table_id):
                errors.append(f"{label} ID 格式无效")
        if not store_name:
            errors.append("店铺名称不能为空")

        if not asins_raw:
            errors.append("ASIN不能为空")
        if not pdf_path:
            errors.append("请选择说明书PDF文件")
        elif not os.path.isfile(pdf_path):
            errors.append(f"PDF文件不存在: {pdf_path}")

        for label, val in [("起始日期", start_date), ("结束日期", end_date)]:
            if not val:
                errors.append(f"{label}不能为空")
            elif not _DATE_PATTERN.match(val):
                errors.append(f"{label}格式错误，请使用 YYYY-MM-DD 格式")

        if _DATE_PATTERN.match(start_date) and _DATE_PATTERN.match(end_date) and end_date < start_date:
            errors.append(f"结束日期（{end_date}）不能早于起始日期（{start_date}）")

        if errors:
            messagebox.showerror("输入校验失败", "\n".join(errors))
            return

        # ---- API 端口 ----
        try:
            socket_port = int(self.entry_port.get().strip())
        except ValueError:
            socket_port = DEFAULT_SOCKET_PORT

        # ---- 保存配置 ----
        config = load_config()
        if superbrowser_raw and os.path.isfile(superbrowser_raw):
            old_path = config.get("superbrowser_path", "")
            config["superbrowser_path"] = superbrowser_raw
            if old_path and old_path != superbrowser_raw:
                print(f"[INFO] 紫鸟路径已更新: {old_path} → {superbrowser_raw}")
        config["company"] = company
        config["username"] = username
        config["password"] = password
        config["feishu_app_id"] = feishu_app_id
        config["feishu_app_secret"] = feishu_app_secret
        config["feishu_report_table_id"] = feishu_report_table_id
        config["feishu_product_table_id"] = feishu_product_table_id
        config["store_name"] = store_name
        config["asins"] = asins_raw
        config["pdf_path"] = pdf_path if os.path.isfile(pdf_path) else ""
        config["start_date"] = start_date
        config["end_date"] = end_date
        config["followup_text"] = followup
        dry_run = bool(config.get("dry_run", True))
        config["dry_run"] = dry_run
        config["socket_port"] = socket_port
        config["marketplace"] = marketplace
        config["sales_channels"] = selected_channels
        # 清理旧版本遗留的双店铺配置，避免无效字段继续被保存。
        for legacy_key in (
            "store_name2", "asins2", "pdf_path2", "start_date2",
            "end_date2", "followup_text2", "marketplace2",
        ):
            config.pop(legacy_key, None)
        save_config(config)

        # ---- 构建单店铺任务配置 ----
        task_config = TaskConfig(
            company=company, username=username, password=password,
            store_name=store_name,
            asins=[a.strip() for a in asins_raw.split(",") if a.strip()],
            pdf_path=pdf_path, start_date=start_date, end_date=end_date,
            follow_up_message=followup,
            follow_up_messages=parse_follow_up_messages(followup),
            product_table_url=(config.get("feishu_product_table_url") or
                               config.get("feishu_product_sheet_url", "")),
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
            feishu_report_table_id=feishu_report_table_id,
            feishu_product_table_id=feishu_product_table_id,
            socket_port=socket_port, dry_run=dry_run,
            marketplace=marketplace,
            sales_channels=selected_channels,
        )

        if marketplace:
            print(f"[INFO] 店铺 ({store_name}) 目标站点: {marketplace}")
        print(f"[INFO] 实发站点: {', '.join(selected_channels) if selected_channels else '全部'}")

        # ---- 切换到执行模式 ----
        mode_str = "干跑模式（邮件正常发送，回访前停止）" if task_config.dry_run else "正式运行（全部发送）"
        print(f"[INFO] 运行模式: {mode_str}")

        # 创建共享事件
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._return_event = threading.Event()

        self._status_holder = StatusHolder(store_name=task_config.store_name)

        # 切换到执行模式 UI
        self._transition_to_exec_mode()

        # 设置 tk root 供线程安全弹窗使用
        set_tk_root(self.root)

        # 启动工作线程
        test_mode = False

        def _run_store():
            try:
                run_task(task_config, test_mode=test_mode,
                         stop_event=self._stop_event, pause_event=self._pause_event,
                         status_holder=self._status_holder)
            except Exception as e:
                print(f"[ERROR] 店铺 {task_config.store_name} 执行异常: {e}")
                try:
                    self._status_holder.status.state = RunState.ERROR
                    self._status_holder.status.message = str(e)[:80]
                except Exception:
                    pass

        t1 = threading.Thread(target=_run_store, daemon=True)
        t1.start()
        self._threads = [t1]

        # 开始轮询状态
        self._poll_status()

    # ------------------------------------------------------------------
    # 执行模式 UI 切换
    # ------------------------------------------------------------------
    def _transition_to_exec_mode(self):
        """隐藏输入卡片，显示执行 UI（状态栏 + 日志区 + 执行按钮）。"""
        self._exec_mode = True

        # 1. 隐藏输入区域
        self._input_container.pack_forget()

        # 2. 创建执行框架
        self._exec_frame = tk.Frame(self.root, bg='#F7F8FA')
        self._exec_frame.pack(fill='both', expand=True, padx=10, pady=(8, 0))

        # 3. 状态显示区
        status_container = tk.Frame(self._exec_frame, bg='#F7F8FA')
        status_container.pack(fill='x', pady=(0, 4))

        sh = self._status_holder
        store_label = sh.store_name if sh else "店铺"
        row_frame = tk.Frame(status_container, bg='white', highlightthickness=1,
                             highlightbackground='#E5E7EB')
        row_frame.pack(fill='x')

        # 状态圆点 + 文本
        state_lbl = tk.Label(row_frame, text="● 等待开始",
                             font=("Microsoft YaHei", 10, "bold"),
                             bg='white', fg='black')
        state_lbl.pack(side='left', padx=(10, 5))

        # 店铺名
        tk.Label(row_frame, text=store_label, font=("Microsoft YaHei", 9, "bold"),
                 bg='white', fg='#1F2937').pack(side='left', padx=(0, 15))

        # 进度
        progress_lbl = tk.Label(row_frame, text="", font=("Microsoft YaHei", 9),
                                bg='white', fg='#374151')
        progress_lbl.pack(side='left', padx=(0, 15))

        # 统计
        stats_lbl = tk.Label(row_frame, text="成功: 0 | 失败: 0",
                             font=("Microsoft YaHei", 9), bg='white', fg='#374151')
        stats_lbl.pack(side='right', padx=(0, 10))

        self._status_labels = {
            'state': state_lbl, 'progress': progress_lbl, 'stats': stats_lbl,
        }

        # 4. 日志显示区（深色终端风格，占据大部分空间）
        log_frame = tk.Frame(self._exec_frame, bg='white', highlightthickness=1,
                             highlightbackground='#E5E7EB')
        log_frame.pack(fill='both', expand=True, pady=(4, 0))

        self._log_text = tk.Text(log_frame, font=("Consolas", 9), wrap='word',
                                 bg='#1E1E1E', fg='#D4D4D4', insertbackground='white',
                                 relief='flat', borderwidth=0, state='disabled')
        log_scrollbar = ttk.Scrollbar(log_frame, orient='vertical',
                                      command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side='right', fill='y')
        self._log_text.pack(side='left', fill='both', expand=True)

        # 绑定日志组件到 _GuiLogger
        _logger.attach_widget(self._log_text)

        # 5. 切换按钮
        self._switch_buttons_to_exec_mode()

    def _switch_buttons_to_exec_mode(self):
        """按钮切换：隐藏 [开始执行] [取消] [干跑]，显示 [暂停] [终止] [返回(disabled)]。"""
        self.btn_start.pack_forget()
        self.btn_cancel.pack_forget()
        self.btn_pause.pack(side='left', padx=(0, 10))
        self.btn_stop_btn.pack(side='left')
        self.btn_return.pack(side='left', padx=(10, 0))
        self.btn_pause.configure(state='normal')
        self.btn_stop_btn.configure(state='normal')
        self.btn_return.configure(state='disabled')

    def _switch_buttons_to_done_mode(self):
        """按钮切换：暂停→关闭，终止 disabled，返回 enabled。"""
        self.btn_pause.configure(text="关闭", command=self._safe_close, state='normal')
        self.btn_stop_btn.configure(state='disabled')
        self.btn_return.configure(state='normal')

    def _switch_buttons_to_input_mode(self):
        """按钮切换：恢复输入模式按钮布局。"""
        self.btn_pause.pack_forget()
        self.btn_stop_btn.pack_forget()
        self.btn_return.pack_forget()
        self.btn_pause.configure(text="暂停", command=self._on_pause)  # 重置
        self.btn_cancel.pack(side='right', padx=(12, 0))
        self.btn_start.pack(side='right')

    # ------------------------------------------------------------------
    # 状态轮询 + 操作按钮
    # ------------------------------------------------------------------
    def _poll_status(self):
        """每 300ms 轮询 StatusHolder，刷新执行 UI。"""
        if not self._exec_mode:
            return

        done_states = (RunState.FINISHED, RunState.STOPPED, RunState.ERROR)
        all_done = True

        sh = self._status_holder
        if sh is not None:
            s = sh.status
            labels = self._status_labels

            if s.state not in done_states:
                all_done = False

            # 状态颜色
            state_colors = {
                RunState.ERROR: "red", RunState.STOPPED: "orange",
                RunState.PAUSED: "orange", RunState.FINISHED: "green",
            }
            color = state_colors.get(s.state, "#2563EB")  # 蓝色=运行中
            labels['state'].config(text=f"● {s.state.value}", foreground=color)

            # 进度
            progress_parts = []
            if s.asin_total > 0:
                progress_parts.append(f"ASIN: {s.asin_index}/{s.asin_total}")
            if s.order_total > 0:
                progress_parts.append(f"订单: {s.order_index}/{s.order_total}")
            labels['progress'].config(
                text=" | ".join(progress_parts) if progress_parts else s.message or "")

            # 统计
            labels['stats'].config(text=f"成功: {s.success_count} | 失败: {s.fail_count}")

        # 按钮状态管理
        if all_done:
            self._switch_buttons_to_done_mode()
        elif self._pause_event and self._pause_event.is_set():
            self.btn_pause.configure(text="继续", state='normal')
        else:
            self.btn_pause.configure(text="暂停", state='normal')

        self._polling_id = self.root.after(300, self._poll_status)

    def _on_pause(self):
        """暂停 / 继续切换。"""
        if not self._pause_event:
            return
        if self._pause_event.is_set():
            self._pause_event.clear()
            print("[UI] 用户点击'继续'")
        else:
            self._pause_event.set()
            if self._status_holder:
                self._status_holder.status.state = RunState.PAUSED
            print("[UI] 用户点击'暂停'")

    def _on_stop(self):
        """终止当前任务。"""
        if messagebox.askyesno("确认终止", "确定要终止当前任务吗？\n已处理的订单不会被撤销。"):
            if self._stop_event:
                self._stop_event.set()
            if self._pause_event:
                self._pause_event.clear()
            if self._status_holder:
                self._status_holder.status.state = RunState.STOPPED
                self._status_holder.status.message = "任务已终止"
            print("[UI] 用户点击'终止'")

    def _on_return(self):
        """返回参数输入界面。"""
        done_states = (RunState.FINISHED, RunState.STOPPED, RunState.ERROR)
        states_done = self._status_holder is None or self._status_holder.status.state in done_states
        if not states_done:
            # 仍在运行中——先终止
            if self._stop_event:
                self._stop_event.set()
            if self._pause_event:
                self._pause_event.clear()
            if self._status_holder:
                self._status_holder.status.state = RunState.STOPPED
                self._status_holder.status.message = "正在返回启动界面..."
        self._return_event.set()
        self._reset_to_input_mode()

    def _reset_to_input_mode(self):
        """从执行模式恢复到输入模式。

        关键：必须先确保工作线程完全终止——否则上一轮的"幽灵线程"
        仍会继续发送并记录订单，导致下一轮误跳过未执行的订单。
        """
        # 1. 停止轮询
        if self._polling_id:
            self.root.after_cancel(self._polling_id)
            self._polling_id = None
        self._exec_mode = False

        # 2. 解绑日志组件
        _logger.detach_widget()

        # 3. 等待工作线程结束
        for t in self._threads:
            t.join(timeout=10)

        alive = [t for t in self._threads if t.is_alive()]
        if alive:
            # 线程仍存活（卡在长超时操作中）→ 显示等待提示，禁止开始新一轮
            print(f"[WARN] 仍有 {len(alive)} 个后台线程未停止，"
                  f"等待其完全退出后再返回启动界面...")
            try:
                self._status_labels['state'].config(
                    text="● 正在停止后台任务...", foreground="orange")
            except tk.TclError:
                pass
            # 禁用所有按钮，防止用户提前开始新一轮
            for b in (self.btn_pause, self.btn_stop_btn, self.btn_return):
                if b:
                    try:
                        b.configure(state='disabled')
                    except tk.TclError:
                        pass
            self.root.after(500, self._finish_reset_after_threads_dead)
            return

        self._complete_reset_to_input()

    def _finish_reset_after_threads_dead(self):
        """轮询等待工作线程完全退出后完成 UI 复位。"""
        if any(t.is_alive() for t in self._threads):
            self.root.after(500, self._finish_reset_after_threads_dead)
            return
        self._complete_reset_to_input()

    def _complete_reset_to_input(self):
        """执行实际的 UI 复位（输入区域恢复、按钮切换、状态清理）。"""
        self._threads = []
        self._status_holder = None
        self._status_labels = {}

        # 清空日志区（为下一轮准备）
        if self._log_text:
            try:
                self._log_text.configure(state='normal')
                self._log_text.delete('1.0', tk.END)
                self._log_text.configure(state='disabled')
            except tk.TclError:
                pass

        # 销毁执行框架
        if self._exec_frame:
            self._exec_frame.destroy()
            self._exec_frame = None
            self._log_text = None

        # 重置全局事件
        global _global_stop, _global_pause
        _global_stop = None
        _global_pause = None
        self._stop_event = None
        self._pause_event = None
        self._return_event = threading.Event()

        # 显示输入区域
        self._input_container.pack(fill='both', expand=True)

        # 恢复按钮
        self._switch_buttons_to_input_mode()

        # 重置结果
        self.results = []

        print(f"{'=' * 60}")
        print("  返回启动界面，准备下一轮查询...\n")

    def _on_window_close(self):
        """WM_DELETE_WINDOW：点 X 关闭窗口。"""
        if self._closing:
            return
        if self._exec_mode and any(t.is_alive() for t in self._threads):
            done_states = (RunState.FINISHED, RunState.STOPPED, RunState.ERROR)
            states_done = self._status_holder is None or self._status_holder.status.state in done_states
            if not states_done:
                if not messagebox.askyesno("确认退出", "任务正在执行中，确定要终止并退出吗？"):
                    return
                if self._stop_event:
                    self._stop_event.set()
                if self._pause_event:
                    self._pause_event.clear()
            self._closing = True
            self.root.after(300, self._close_when_workers_done)
            return
        self._safe_close()

    def _close_when_workers_done(self):
        if any(t.is_alive() for t in self._threads):
            self.root.after(300, self._close_when_workers_done)
        else:
            self._safe_close()

    def _safe_close(self):
        """安全关闭应用窗口。"""
        if self._polling_id:
            self.root.after_cancel(self._polling_id)
        _logger.close()
        self.root.destroy()

    def show(self):
        """显示窗口并进入主循环。窗口关闭后返回。"""
        self.root.mainloop()



# ============================================================================
# 紫鸟 HTTP API 客户端
# （已迁移到 ziniao_browser.py — 此处通过 import 引用 ZigniaoAPI）
# ============================================================================


# ============================================================================
# 紫鸟启动 + 店铺打开 + CDP 连接
# （核心逻辑已迁移到 ziniao_browser.py — 此处为项目适配层）
# ============================================================================

# launch_superbrowser、connect_to_store_cdp 已从 ziniao_browser 导入，直接可用


def show_manual_step(message: str):
    """弹窗引导用户手动完成操作（线程安全）。"""
    result = {"confirmed": False}

    def _dialog(parent):
        dlg = tk.Toplevel(parent) if parent is not None else tk.Tk()
        dlg.title("操作提示")
        # 计算居中位置
        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"420x200+{(sw-420)//2}+{(sh-200)//2}")
        dlg.attributes("-topmost", True)
        dlg.transient(parent)

        def _on_confirm():
            result["confirmed"] = True
            dlg.destroy()

        ttk.Label(dlg, text=message, justify="center", padding=20).pack()
        ttk.Button(dlg, text="已完成，继续执行", command=_on_confirm).pack(pady=10)

        dlg.focus_force()
        dlg.grab_set()
        # 阻塞等待对话框关闭
        dlg.wait_window()
        return result["confirmed"]

    return _run_on_tk_thread(_dialog)


def open_store_via_api(config, stop_event=None) -> int:
    """
    通过紫鸟 HTTP API 启动店铺并返回 CDP 调试端口。

    委托给 ziniao_browser.ZiniaoBrowser.open_store()，
    并桥接本项目的 tkinter 弹窗（手动启动）与暂停/终止事件。

    """
    if stop_event and stop_event.is_set():
        raise RuntimeError("用户终止任务")

    def _manual_launch():
        show_manual_step(
            f"紫鸟未自动启动。\\n\\n请手动完成：\\n"
            f"1. 打开紫鸟客户端\\n"
            f"2. 确认已登录（公司: {config.company}）\\n\\n完成后点击下方按钮继续"
        )

    def _check_stop():
        if stop_event and stop_event.is_set():
            raise RuntimeError("用户终止任务")
        _check_stop_global()

    def _wait_pause():
        _wait_if_paused_global()

    zb = ZiniaoBrowser(
        company=config.company,
        username=config.username,
        password=config.password,
        socket_port=config.socket_port,
        starter_path=get_superbrowser_path(),
    )

    _check_stop()
    launched = zb.launch()
    if not launched:
        _manual_launch()

    _check_stop()

    # 使用底层 API 完成店铺打开（保持与原有日志风格一致）
    api = zb.api
    api.update_core(check_stop=_check_stop, wait_if_paused=_wait_pause)

    _check_stop()
    api.apply_auth()

    _check_stop()
    store = api.find_store(config.store_name)
    if store is None:
        browsers = api.get_browser_list()
        names = [b.get("browserName", "?") for b in browsers[:20]]
        raise RuntimeError(f"未找到店铺 '{config.store_name}'。可用: {', '.join(names)}")
    if store.get("browserName") != config.store_name:
        raise RuntimeError(
            f"紫鸟店铺未精确匹配：期望 '{config.store_name}'，"
            f"实际 '{store.get('browserName', '')}'")

    browser_oauth = store.get("browserOauth", "")
    store_name = store.get("browserName", config.store_name)
    print(f"[OK] 匹配店铺: {store_name}")

    _check_stop()
    result = api.start_browser(browser_oauth, store_name)
    debugging_port = result.get("debuggingPort")
    if not debugging_port:
        raise RuntimeError("startBrowser 未返回 debuggingPort")

    print(f"[OK] CDP 调试端口: {debugging_port}")
    return int(debugging_port)


# connect_to_store_cdp 已从 ziniao_browser 导入，不再需要本地定义


# ============================================================================
# 亚马逊登录处理
# ============================================================================

def _ensure_logged_in(page: Page) -> bool:
    """
    防御性登录检查 —— 在页面切换/点击事件后调用，处理亚马逊可能触发的登录验证。

    V2 quick_login 内置预检（URL 白名单 + 表单检测），当页面无需登录时几乎零开销。
    适合在以下场景调用：
      - 导航到新页面后（page.goto 可能被重定向到登录页）
      - 点击买家名称后（新标签页可能要求重新验证）
      - 从消息页返回订单页后（跨页面跳转可能触发站点切换验证）

    返回 True 表示页面正常（在卖家后台），False 表示登录恢复失败。
    """
    try:
        url = page.url
        # 快捷路径：已在卖家后台且不在登录流程中
        if "sellercentral" in url and "signin" not in url.lower():
            return True

        # 需要登录处理
        print(f"  [防御] 检测到页面可能需登录验证: {url[:100]}")
        quick_login(page)

        # 检查恢复结果
        url_after = page.url
        if "sellercentral" in url_after and "signin" not in url_after.lower():
            print(f"  [防御] 登录验证已通过")
            return True
        return False
    except Exception as e:
        print(f"  [防御] 登录检查异常: {e}")
        return "sellercentral" in page.url


def handle_login(page: Page, timeout: int = 120) -> bool:
    """
    处理亚马逊卖家后台登录流程（使用 V2 增强版 quick_login）。

    V2 预检逻辑（比 V1 更智能）：
    1. 已在 sellercentral.* → 跳过（无需重复登录）
    2. 已在 account-switcher / choose-account → 跳过
    3. 不在登录页且无登录表单 → 跳过
    4. 确实需要登录 → 执行标准 LoginHandler 流程
    5. 新增：自动处理 Switch accounts（切换账号）中间页
    6. 新增：OTP/MFA 验证码自动填入 + 确认

    返回：是否成功登录（已进入卖家后台）。
    """
    print("[STEP] 检查登录状态（V2 增强预检）...")

    # 先检查是否已在卖家后台（快捷路径）
    if "sellercentral" in page.url and "signin" not in page.url.lower():
        print("[OK] 已在卖家后台，无需登录")
        return True

    # 使用 V2 quick_login：内置 URL 白名单 + 表单检测 + Switch accounts + OTP
    try:
        logged_in = quick_login(page)
        if logged_in:
            print("[OK] V2 quick_login 返回成功")
            # 二次确认：URL 确实在卖家后台
            if "sellercentral" in page.url and "signin" not in page.url.lower():
                print("[OK] 登录成功，已进入卖家后台")
                return True
    except Exception as e:
        print(f"[WARN] quick_login 异常: {e}，尝试降级方案...")

    # 降级方案：使用标准 LoginHandler（兼容旧版逻辑）
    try:
        print("[INFO] 使用标准 LoginHandler 处理...")
        handler = LoginHandler(page)
        if handler.handle(timeout=timeout):
            print("[OK] LoginHandler 登录成功")
            return True
    except Exception as e:
        print(f"[WARN] LoginHandler 异常: {e}")

    # 最终确认（容错：可能 V2 已完成登录但未正确返回 True）
    url = page.url
    if "sellercentral" in url and "signin" not in url.lower():
        print("[OK] 登录已确认（URL 容错检查）")
        return True

    print("[WARN] 自动登录未完成，请检查页面状态")
    return False


def _poll_login(page: Page) -> bool:
    """最多检查四次登录状态，相邻检查间隔五秒。"""
    for attempt in range(4):
        if attempt:
            random_delay(5, 5)
        _check_stop_global()
        _wait_if_paused_global()
        print(f"[INFO] 检查亚马逊登录状态 [{attempt + 1}/4]")
        if handle_login(page):
            return True
    return False


def _prompt_manual_intervention(step_name: str) -> bool:
    """
    弹窗提示用户手动完成某个步骤（如安全验证），
    用户完成后点击确认继续（线程安全）。
    自动根据内容行数调整窗口高度，防止按钮被挤出屏幕。
    """
    result = {"confirmed": False}

    def _dialog(parent):
        dlg = tk.Toplevel(parent) if parent is not None else tk.Tk()
        dlg.title("需要人工操作")
        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()

        message = f"请在紫鸟浏览器中手动完成【{step_name}】操作。\n\n完成后点击下方按钮继续。"

        # 根据内容行数动态计算窗口高度
        line_count = message.count("\n") + 1
        # 每行约 20px，文本区最小 80px，最大不超过屏幕 70%
        text_height = min(max(line_count * 20 + 20, 80), int(sh * 0.65))
        win_height = text_height + 70  # 按钮区域 70px
        win_width = min(550, sw - 100)

        dlg.geometry(f"{win_width}x{win_height}+{(sw - win_width) // 2}+{(sh - win_height) // 2}")
        dlg.transient(parent)
        dlg.resizable(True, True)
        dlg.minsize(350, 150)

        def _on_confirm():
            result["confirmed"] = True
            dlg.destroy()

        # 使用可滚动 Text 展示内容
        text_frame = ttk.Frame(dlg)
        text_frame.pack(fill="both", expand=True, padx=10, pady=(10, 5))

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")

        text_widget = tk.Text(
            text_frame,
            wrap="word",
            yscrollcommand=scrollbar.set,
            font=("TkDefaultFont", 10),
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=10,
        )
        text_widget.insert("1.0", message)
        text_widget.configure(state="disabled")  # 只读
        text_widget.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=text_widget.yview)

        # 按钮始终固定在底部
        ttk.Button(dlg, text="已完成，继续执行", command=_on_confirm).pack(pady=(0, 10))

        dlg.focus_force()
        dlg.grab_set()
        dlg.wait_window()
        return result["confirmed"]

    return _run_on_tk_thread(_dialog)


# ============================================================================
# 订单管理操作
# ============================================================================

def navigate_to_orders(page: Page) -> bool:
    """导航到订单管理页面。"""
    print("[STEP] 导航到订单管理页面...")
    try:
        page.goto(_get_orders_url(), timeout=30000, wait_until="domcontentloaded")
        random_delay(0.5, 1.0)
        _wait_page_ready(page, "订单管理页")
        print("[OK] 已进入订单管理页面")
        return True
    except Exception as e:
        print(f"[FAIL] 导航到订单页面失败: {e}")
        return False


def _build_asin_search_url(asin: str, date_range: str) -> str:
    """构造 ASIN 搜索结果页 URL（search_asin 与 _goto_order_page 共用，
    保证首次搜索与回跳分页的 URL 参数完全一致）。
    URL 格式: /orders-v3/search?q=ASIN&qt=asin[&date-range=...]"""
    return f"{_get_sc_url()}/orders-v3/search?q={asin}&qt=asin{date_range}"


def search_asin(page: Page, asin: str, date_range: str = "") -> bool:
    """
    在订单管理页面按 ASIN 搜索。
    通过 URL 参数直接跳转，避免 UI 选择器不稳定的问题。
    URL 格式: /orders-v3/search?q=ASIN&qt=asin
    保留当前已有的 date-range 参数。

    参数:
        date_range: 可选的日期范围参数字符串，如 "&date-range=1782864000000-1784159999000"。
                    传入后直接使用，不再从 page.url 提取。
                    用于从消息页等非订单页返回搜索结果页时保留日期筛选。
    """
    print(f"[STEP] 搜索 ASIN: {asin}")
    random_delay(0.5, 1)

    # 优先使用传入的 date_range；否则从当前 URL 提取已有的 date-range 参数
    if not date_range:
        if "date-range=" in page.url:
            try:
                dr_part = page.url.split("date-range=")[1].split("&")[0]
                if dr_part:
                    date_range = f"&date-range={dr_part}"
            except Exception:
                pass

    search_url = _build_asin_search_url(asin, date_range)

    try:
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        random_delay(0.5, 1.0)
        # 轻量等待：搜索阶段只确认首波行渲染，不滚到底触发懒加载
        # （完整懒加载推迟到时间区间设置完成后的提取阶段）
        _wait_orders_table_ready(page, f"ASIN搜索:{asin}", lazy_load=False)
        print(f"[OK] ASIN '{asin}' 搜索完成: {page.url}")
        return True
    except Exception as e:
        print(f"[FAIL] ASIN 搜索失败: {e}")
        return False


def _goto_order_page(page: Page, asin: str, date_range: str,
                     page_num: int = 1) -> bool:
    """
    回跳 ASIN 搜索结果列表的指定分页，并等待懒加载完成（完整模式）。

    执行阶段每条订单处理前调用：提取结束后浏览器停在最后一页，
    且回访验证会把主 tab 导航到消息页——点击买家名称前必须回到
    该订单所在分页，才能在当前页 DOM 中定位订单行。

    URL 与 search_asin 一致并追加 &page=N；第 1 页不加 page 参数
    （与提取阶段翻页逻辑保持一致）。页面重新加载后行数会重新懒加载，
    因此这里使用完整模式等待（滚到底直到行数稳定）。
    """
    print(f"  [导航] 回跳 ASIN 搜索结果 分页 {page_num} ...")
    target = _build_asin_search_url(asin, date_range)
    if page_num > 1:
        target = f"{target}&page={page_num}"
    try:
        page.goto(target, timeout=30000, wait_until="domcontentloaded")
        random_delay(0.5, 1.0)
        _wait_orders_table_ready(page, f"回跳分页{page_num}")
        print(f"  [导航] 已回到分页 {page_num}")
        return True
    except Exception as e:
        print(f"  [WARN] 回跳分页 {page_num} 导航失败: {e}（将尝试直开联系页兜底）")
        return False


def _build_date_range_param(start_date: str, end_date: str) -> str:
    """
    固化日期范围 URL 参数（epoch 毫秒，本机时区）。

    阶段4 不再单独跳转日期设置页：日期范围直接作为 &date-range=...
    参数并入每个 ASIN 的搜索 URL（search_asin 一次 goto 同时带全
    q/qt/date-range）。后续从消息页返回订单列表时需要该参数
    （page.url 切换后丢失），故本地固化一份备用。
    """
    try:
        _start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        _end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        # time.mktime 使用本机时区，避免 UTC 时差导致日期偏移
        _start_ms = int(time.mktime(_start_dt.timetuple())) * 1000
        _end_ms = (int(time.mktime(_end_dt.timetuple())) + 86399) * 1000
        param = f"&date-range={_start_ms}-{_end_ms}"
        print(f"[INFO] 日期范围参数已固化: {param}")
        return param
    except Exception:
        print("[WARN] 日期范围参数固化失败，回退到从 URL 提取")
        return ""


def _ensure_100_per_page(page: Page) -> bool:
    """
    确保订单列表每页显示 100 条结果。

    Amazon 默认每页可能显示 15、25 或 50 条，不足 100 时需要切换到 100，
    减少翻页次数、加快扫描速度。

    实际 DOM（新版）：
      <select name="myo-table-results-per-page" id="myo-table-results-per-page"
              class="a-native-dropdown a-declarative" tabindex="0">
        <option value="15">15</option> ... <option value="100">100</option>
      </select>
      （旁边的 a-button-dropdown 只是视觉外壳，aria-hidden="true"）

    因此直接对原生 <select> 做 select_option("100") 即可触发亚马逊 JS。
    返回 True 表示当前已是 100（或切换成功），False 表示切换失败。
    """
    def _read_current() -> int:
        """读取原生 select 的当前值（最可靠）。"""
        try:
            v = page.evaluate(
                "document.querySelector('#myo-table-results-per-page')?.value || ''")
            return int(v) if str(v).isdigit() else 0
        except Exception:
            return 0

    # 1. 检测当前每页条数
    current = _read_current()
    if not current:
        # 回退：读 a-dropdown-prompt 文本
        try:
            txt = page.locator(get_selectors("orders_page_size_prompt")[0]).first.text_content(timeout=2000) or ""
            import re as _re
            m = _re.search(r'(\d+)', txt)
            current = int(m.group(1)) if m else 0
        except Exception:
            current = 0
    print(f"  [分页设置] 当前每页: {current or '?'} 条")

    if current == 100:
        print("  [分页设置] 已是每页 100 条，无需切换")
        return True

    # 2. 策略 A：原生 <select> 直接 select_option("100")
    try:
        sel = page.locator(get_selectors("orders_page_size_select")[0]).first
        if sel.count() > 0:
            sel.select_option('100', timeout=5000)
            print("  [分页设置] 已提交原生 select 切换为 100")
            random_delay(1, 2)
            _wait_orders_table_ready(page, "切换每页100条")
            # 验证切换结果（页面刷新后 select 元素会重新渲染）
            if _read_current() == 100:
                print("  [分页设置] 已切换到每页 100 条")
                return True
            # 验证失败但可能因页面刷新导致元素未就绪——再读一次 prompt
            try:
                txt = page.locator(get_selectors("orders_page_size_prompt")[0]).first.text_content(timeout=3000) or ""
                if '100' in txt:
                    print("  [分页设置] 已切换到每页 100 条")
                    return True
            except Exception:
                pass
            print("  [分页设置] 原生 select 切换后无法确认，继续使用视觉外壳兜底")
    except Exception as e:
        print(f"  [分页设置] 原生 select 切换失败: {e}，继续使用视觉外壳兜底")

    # 3. 策略 B：点击视觉下拉外壳（a-button-dropdown）→ 点击模拟列表项
    try:
        trigger = page.locator(get_selectors("orders_page_size_dropdown")[0]).first
        if trigger.count() > 0:
            trigger.click(timeout=3000)
            random_delay(0.3, 0.5)

        # 亚马逊模拟下拉项 ID 约定：{select_id}_{index}（100 是第 4 项 → _3）
        option_100 = page.locator(get_selectors("orders_page_size_100_option")[0]).first
        if option_100.count() > 0 and option_100.is_visible(timeout=3000):
            option_100.click(timeout=3000)
            random_delay(1, 2)
            _wait_orders_table_ready(page, "切换每页100条")
            if _read_current() == 100:
                print("  [分页设置] 已切换到每页 100 条")
                return True

        # 模拟列表项文本匹配（ID 序号不确定时）
        for selector in get_selectors("orders_page_size_options"):
            opts = page.locator(selector)
            n = opts.count()
            for i in range(min(n, 10)):
                opt = opts.nth(i)
                try:
                    if opt.is_visible(timeout=1000) and (opt.text_content() or "").strip() == "100":
                        opt.click(timeout=3000)
                        random_delay(1, 2)
                        _wait_orders_table_ready(page, "切换每页100条")
                        if _read_current() == 100:
                            print("  [分页设置] 已切换到每页 100 条")
                            return True
                except Exception:
                    continue
    except Exception as e:
        print(f"  [分页设置] 视觉外壳兜底失败: {e}")

    print("  [分页设置] 未能切换到每页 100 条（将按实际页容量继续翻页）")
    return False


def _get_total_orders(page: Page) -> int:
    """
    从页面 total-orders-heading 元素提取订单总数（用于计算翻页页数）。

    实测 DOM（2026-08-13, 新版卖家平台）：
      <div class="total-orders-heading">
        <span class="">275 个订单</span>
        <span class="total-orders-sub-heading">...</span>
      </div>
    提取失败返回 0（调用方退回连续空页兜底逻辑）。
    """
    try:
        txt = page.locator(get_selectors("orders_total_heading")[0]).first.text_content(timeout=3000) or ""
        m = re.search(r'(\d[\d,]*)\s*(?:个订单|orders)', txt, re.IGNORECASE)
        if m:
            total = int(m.group(1).replace(",", ""))
            print(f"  [诊断] total-orders-heading: {total} 个订单")
            return total
    except Exception as e:
        print(f"  [WARN] 提取订单总数失败: {e}")
    return 0


def _iter_paid_order_pages(page: Page, asin: str):
    """
    逐页提取付款/已发货订单；消费完当前页才导航到下一页。

    自动确保每页 100 条；优先按 total-orders-heading 的订单总数计算
    翻页页数（总数驱动分页），总数提取失败时退回连续空页兜底逻辑。
    """
    print("[STEP] 提取订单（URL翻页，每页100条）...")

    # ASIN 统一大写：页面渲染的 ASIN 均为大写（正则 B0[A-Z0-9]{8} 也只匹配大写），
    # 用户输入可能为小写，未归一化会导致整组 ASIN 并集匹配永远落空
    asin = (asin or "").strip().upper()

    # 尽量切到每页 100 条；失败时读取实际页容量计算页数。
    page_size_ok = _ensure_100_per_page(page)
    page_size = 100 if page_size_ok else 0
    if not page_size_ok:
        try:
            page_size = int(page.locator(get_selectors("orders_page_size_select")[0])
                            .first.input_value(timeout=2000))
        except Exception:
            page_size = 0
        if page_size <= 0:
            print("  [WARN] 无法确认每页条数，将使用连续空页检测末页")

    seen_order_ids: set[str] = set()
    seen_page_order_ids: set[str] = set()
    max_pages = 50
    empty_streak = 0

    # 构建基础 URL（去除已有的 page 参数）
    base_url = page.url.split("&page=")[0] if "&page=" in page.url else page.url

    # 总数驱动分页：优先从 total-orders-heading 提取总数计算页数
    total = _get_total_orders(page)
    if total > 0 and page_size > 0:
        total_pages = min(max_pages, (total + page_size - 1) // page_size)
        print(f"  [INFO] 总数驱动分页: 共 {total} 个订单, {total_pages} 页")
        page_range = range(1, total_pages + 1)
    else:
        total_pages = 0
        page_range = range(1, max_pages + 1)

    for page_num in page_range:
        if page_num > 1:
            # URL 翻页：直接跳转 page=N
            target = f"{base_url}&page={page_num}"
            try:
                page.goto(target, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  [翻页] page={page_num} 导航失败: {e}")
                break

        random_delay(0.3, 0.6)
        _wait_orders_table_ready(page, f"分页{page_num}")

        print(f"  [分页 {page_num}] 扫描订单...")
        page_orders = _extract_orders_from_current_page(page, asin, page_num)

        new_orders = []
        for order in page_orders:
            if order.order_id and order.order_id not in seen_order_ids:
                seen_order_ids.add(order.order_id)
                new_orders.append(order)

        new_count = len(new_orders)
        progress = f"（累计 {len(seen_order_ids)}/{total}）" if total_pages else f"（累计 {len(seen_order_ids)}）"
        print(f"  本页新增 {new_count} 个订单{progress}")
        stop_after_page = False
        if not total_pages:
            # 发送会离开订单页，必须在交给发送循环前采集原始订单号。
            try:
                row_text = "\n".join(page.locator(get_selectors("order_rows")[0]).all_text_contents())
                visible_ids = set(re.findall(r"\d{3}-\d{7}-\d{7}", row_text))
            except Exception:
                visible_ids = {o.order_id for o in page_orders}
            visible_ids.update(o.order_id for o in page_orders if o.order_id)
            new_page_ids = visible_ids - seen_page_order_ids
            seen_page_order_ids.update(visible_ids)
            empty_streak = 0 if new_page_ids else empty_streak + 1
            stop_after_page = empty_streak >= 2

        yield new_orders
        if stop_after_page:
            print("  连续2页无新订单号，停止翻页")
            break

    print(f"[INFO] 共找到 {len(seen_order_ids)} 个符合条件的订单")


def extract_paid_orders(page: Page, asin: str) -> list[OrderInfo]:
    """兼容旧调用：汇总逐页提取的订单。"""
    return [order for orders in _iter_paid_order_pages(page, asin) for order in orders]


def _extract_orders_from_current_page(page: Page, asin: str,
                                      page_num: int = 1) -> list[OrderInfo]:
    """
    从当前页面提取订单（JS 优先，DOM 回退）。
    以订单为单位提取：一单多品时主行（含订单号/买家/状态列）+ 子行
    （仅商品单元格）合并为一个订单，ASIN 取整组并集，状态只从主行
    .order-status-column 读取。只提取"付款完成"等付款/发货状态的订单，
    已申请退款（refund-is-applied）的订单排除。
    买家名称：主行内空 href 的 <a> 标签文本（href="" 或 javascript:void(0)）。

    参数:
        page_num: 当前分页号，记录到 OrderInfo.page_num，
                  执行阶段据此回跳定位买家行。
    """
    orders: list[OrderInfo] = []

    js_extract = r"""(params) => {
  const asin = params.asin;
  const pageNum = params.pageNum;
  const selectors = params.selectors;
  const orders = [];
  const statusCounts = {};
  const debugRows = [];  // 逐单诊断日志
  const skippedAsinCounts = {};  // 订单级ASIN过滤统计
  let refundSkipCount = 0;  // 退款订单排除统计
  let productRows = 0;  // 并入所属订单的子行（一单多品）数量
  const MAX_DEBUG = 20;  // 最多记录单数，避免日志过多
  // 匹配各种付款/发货状态（与 DOM 回退路径保持同一语义）
  const paidPatterns = [
    '付款完成', 'Payment complete', '已付款', 'Paid',
    '已配送', 'Shipped', '已送达', 'Delivered',
    '配送中', 'In transit', '待发货', 'Pending',
    'Unshipped', 'Awaiting', 'Dispatched',
  ];
  // 主行判定：实测仅主行带 .order-status-column 状态列；
  // 兜底：行文本匹配订单号格式 123-1234567-1234567 也视为主行。
  const isMainRow = (row) => {
    if (row.querySelector(selectors.status)) return true;
    return /\d{3}-\d{7}-\d{7}/.test(row.textContent || '');
  };
  // 一单多品：列表 = 主行（rowspan，含订单号/买家/状态列）+ 子行（仅商品单元格）。
  // 以订单为单位提取：主行开启一个订单组，后续非主行并入该组，直到下一个主行。
  const processGroup = (mainRow, groupRows) => {
    const mainText = mainRow.textContent || '';
    const groupText = groupRows.map(r => r.textContent || '').join('\n');
    // 退款订单排除：买家已申请退款的订单虽显示"付款完成"但不处理。
    // 退款标记只出现在主行，类名 refund-is-applied 是语言无关的可靠标记，
    // 文本"已申请退款"作兜底。
    const refundApplied = mainRow.querySelector(selectors.refund[0])
        || mainRow.querySelector(selectors.refund[1]);
    if (refundApplied || mainText.includes('已申请退款')) {
      refundSkipCount++;
      return;
    }
    // 状态：只从主行的 .order-status-column 读取，避免子行"已配送数量"等
    // 商品文本被误判为"已配送"状态；无状态列时（防御）退回主行整行文本匹配。
    const statusCol = mainRow.querySelector(selectors.status);
    let statusText = '';
    if (statusCol) {
      statusText = (statusCol.textContent || '').replace(/\s+/g, ' ').trim();
    }
    const statusSource = statusText || mainText;
    let isPaidOrShipped = false;
    let matchedStatus = '';
    for (const pat of paidPatterns) {
      if (statusSource.includes(pat)) {
        isPaidOrShipped = true;
        matchedStatus = pat;
        break;
      }
    }
    // 诊断：只统计匹配到的状态；未匹配时记录状态列文本（而非任意 td 文本，
    // 旧逻辑会把"更多信息 «/标准/平邮"等非状态文本混入状态分布）
    if (matchedStatus) {
      statusCounts[matchedStatus] = (statusCounts[matchedStatus] || 0) + 1;
    } else if (statusText) {
      statusCounts[statusText] = (statusCounts[statusText] || 0) + 1;
    }
    if (!isPaidOrShipped) {
      return;
    }
    // 订单号：只从主行文本提取（子行没有订单号）
    const orderMatch = mainText.match(/(\d{3}-\d{7}-\d{7})/);
    const orderId = orderMatch ? orderMatch[1] : '';
    // 订单级 ASIN 校验：取整组（主行+子行）的 ASIN 并集。
    // 目标 ASIN 在任一子行时订单也能被提取；
    // 搜索连带返回的"相关商品"订单因整组不含目标 ASIN 仍被排除
    // 页面搜索可能混入仅含另一 ASIN 的关联商品订单。
    const groupAsins = [...new Set([...groupText.matchAll(/B0[A-Z0-9]{8}/g)].map(m => m[0]))];
    const hasTargetAsin = groupAsins.includes(asin);
    if (!hasTargetAsin) {
      groupAsins.forEach(a => { skippedAsinCounts[a] = (skippedAsinCounts[a] || 0) + 1; });
    }
    let buyerName = '';
    let buyerDebug = '';  // 记录定位过程
    const buyerLink = mainRow.querySelector(selectors.buyer);
    if (buyerLink) {
      const t = buyerLink.textContent.trim();
      if (t) {
        buyerName = t;
        buyerDebug = 'data-test-id → "' + t + '"';
      } else {
        buyerDebug = 'data-test-id 存在但文本为空';
      }
    } else {
      // 收集所有 <a> 的 href 信息用于诊断
      const linkInfos = [];
      const links = mainRow.querySelectorAll('a');
      links.forEach(a => {
        const href = a.getAttribute('href') || '';
        // 匹配无实际导航的链接：无href、空href、javascript:void(0)、#
        if (!href || href === '#' || href.startsWith('javascript:')) {
          const t = a.textContent.trim();
          linkInfos.push({ text: t.substring(0, 40), href: href.substring(0, 60) || '(空)' });
          if (!buyerName && t && t.length > 0 && t.length < 80
              && !t.includes('退款') && !t.includes('发票')) {
            buyerName = t;
          }
        }
      });
      if (buyerName) {
        buyerDebug = '兜底匹配 → "' + buyerName + '" (候选: ' + JSON.stringify(linkInfos) + ')';
      } else if (linkInfos.length > 0) {
        buyerDebug = '兜底链接均被过滤 (候选: ' + JSON.stringify(linkInfos) + ')';
      } else {
        // 列出所有链接的 href（前 5 个），帮助定位为什么没匹配到
        const allLinks = mainRow.querySelectorAll('a');
        const allLinkInfos = [];
        allLinks.forEach(a => {
          const h = a.getAttribute('href') || '';
          const t = a.textContent.trim().substring(0, 30);
          allLinkInfos.push({ text: t, href: h.substring(0, 80) || '(空)' });
        });
        buyerDebug = '无匹配链接 (全部' + allLinks.length + '个链接: ' + JSON.stringify(allLinkInfos.slice(0, 5)) + ')';
      }
    }
    // 提取销售渠道（如 Amazon.de, Amazon.fr, Amazon.co.uk 等），只看主行
    let salesChannel = '';
    const channelMatch = mainText.match(/Amazon\.([a-z]{2,3}(?:\.[a-z]{2})?)/i);
    if (channelMatch) {
      salesChannel = channelMatch[0];
    }
    const channelDebug = salesChannel ? '匹配 → ' + salesChannel : '未匹配 (主行文本前100字: "' + mainText.substring(0, 100).replace(/\s+/g, ' ') + '")';
    if (orderId && hasTargetAsin) {
      orders.push({ order_id: orderId, buyer_name: buyerName, asin: asin,
                    sales_channel: salesChannel, page_num: pageNum });
    }
    // 记录诊断（限制数量）
    if (debugRows.length < MAX_DEBUG) {
      debugRows.push({
        order_id: orderId || '(未提取到)',
        status: matchedStatus,
        buyer_name: buyerName || '(未提取到)',
        buyer_debug: buyerDebug,
        sales_channel: salesChannel || '(未提取到)',
        channel_debug: channelDebug,
        row_asins: groupAsins.join(','),
        asin_skipped: !hasTargetAsin,
        group_rows: groupRows.length,
      });
    }
  };
  // 遍历所有行：主行开启新订单组，后续非主行并入当前组（首个主行前的表头行忽略）
  let currentMain = null;
  let currentGroup = [];
  document.querySelectorAll('tr').forEach(row => {
    if (isMainRow(row)) {
      if (currentMain) {
        processGroup(currentMain, currentGroup);
      }
      currentMain = row;
      currentGroup = [row];
    } else if (currentMain) {
      currentGroup.push(row);
      productRows++;
    }
  });
  if (currentMain) {
    processGroup(currentMain, currentGroup);
  }
  // 返回订单 + 诊断信息
  return { orders: orders, statusCounts: statusCounts, debugRows: debugRows,
           skippedAsinCounts: skippedAsinCounts, refundSkipCount: refundSkipCount,
           productRows: productRows };
}"""

    try:
        result = page.evaluate(js_extract, {
            "asin": asin,
            "pageNum": page_num,
            "selectors": {
                "status": get_selectors("order_status")[0],
                "refund": get_selectors("order_refund_status"),
                "buyer": get_selectors("order_buyer_link")[0],
            },
        })
        if result:
            status_counts = result.get("statusCounts", {}) if isinstance(result, dict) else {}
            items = result.get("orders", []) if isinstance(result, dict) else result
            for item in items:
                orders.append(OrderInfo(
                    order_id=item.get("order_id", ""),
                    buyer_name=item.get("buyer_name", ""),
                    asin=asin,
                    sales_channel=item.get("sales_channel", ""),
                    page_num=item.get("page_num", page_num),
                ))
            # 打印诊断：退款订单排除统计
            refund_skips = result.get("refundSkipCount", 0) if isinstance(result, dict) else 0
            if refund_skips:
                print(f"  [诊断] 退款订单过滤: 排除 {refund_skips} 单 (refund-is-applied/已申请退款)")
            # 打印诊断：一单多品子行并入统计
            product_rows = result.get("productRows", 0) if isinstance(result, dict) else 0
            if product_rows:
                print(f"  [诊断] 一单多品行: {product_rows} 行已并入所属订单")
            # 打印诊断：页面上有哪些状态（仅匹配到的状态 / 未匹配时的状态列文本）
            if status_counts:
                matched_list = [f"{k}({v})" for k, v in sorted(status_counts.items(), key=lambda x: -x[1])[:10]]
                print(f"  [诊断] 页面状态分布: {', '.join(matched_list)}")
            # 打印诊断：订单级 ASIN 过滤跳过统计
            skipped_asins = result.get("skippedAsinCounts", {}) if isinstance(result, dict) else {}
            if skipped_asins:
                skip_list = [f"{k}({v})" for k, v in sorted(skipped_asins.items(), key=lambda x: -x[1])]
                print(f"  [诊断] ASIN过滤: 跳过 {sum(skipped_asins.values())} 单 (非目标ASIN: {', '.join(skip_list)})")
            # 打印逐单元素定位诊断
            debug_rows = result.get("debugRows", []) if isinstance(result, dict) else []
            if debug_rows:
                print(f"  [诊断] ── 逐单元素定位详情（前 {len(debug_rows)} 单）──")
                for i, dr in enumerate(debug_rows, 1):
                    print(f"  [诊断] [{i}] 订单={dr.get('order_id','?')} | 状态={dr.get('status','?')}")
                    print(f"  [诊断]     买家: {dr.get('buyer_name','?')}")
                    print(f"  [诊断]     买家定位: {dr.get('buyer_debug','?')}")
                    print(f"  [诊断]     站点: {dr.get('sales_channel','?')}")
                    print(f"  [诊断]     站点定位: {dr.get('channel_debug','?')}")
                    if dr.get('group_rows', 1) > 1:
                        print(f"  [诊断]     一单多品: 共 {dr.get('group_rows')} 行并入本单")
                    if dr.get('asin_skipped'):
                        print(f"  [诊断]     ASIN校验: 已跳过 (组内ASIN: {dr.get('row_asins','?')})")
                print(f"  [诊断] ── 逐单诊断结束 ──")
            elif not orders:
                # 没匹配到订单也没有状态 → 直接打印页面内容摘要
                try:
                    body_text = page.locator("body").inner_text(timeout=3000)[:500]
                    print(f"  [诊断] 页面文本摘要(前500字): {body_text}")
                except Exception:
                    pass
    except Exception as e:
        print(f"  [WARN] JS 提取失败: {e}")

    if not orders:
        orders = _extract_orders_via_dom(page, asin, page_num)

    return orders


def _extract_orders_via_dom(page: Page, asin: str,
                            page_num: int = 1) -> list[OrderInfo]:
    """DOM 遍历方式提取订单（备选方案）。包含详细元素定位诊断日志。
    与 JS 提取路径保持一致：以订单为单位（一单多品时主行+子行合并为一个订单），
    状态只从主行 .order-status-column 读取，排除已申请退款（refund-is-applied）
    订单，并记录分页号。"""
    orders: list[OrderInfo] = []
    debug_rows = []  # 逐单诊断
    skipped_asins: dict = {}  # 订单级ASIN过滤统计
    refund_skips = 0  # 退款订单排除统计
    product_rows = 0  # 并入所属订单的子行（一单多品）数量
    status_counts: dict = {}  # 页面状态分布诊断
    MAX_DEBUG = 20

    # 匹配各种付款/发货状态（与 JS 提取路径保持同一语义）
    paid_patterns = ["付款完成", "Payment complete", "已付款", "Paid",
                     "已配送", "Shipped", "已送达", "Delivered",
                     "配送中", "In transit", "待发货", "Pending", "Unshipped",
                     "Dispatched", "Awaiting"]

    try:
        rows = page.locator(get_selectors("order_rows")[0]).all()
    except Exception as e:
        print(f"  [诊断-DOM] 获取行元素失败: {e}")
        rows = []

    def _is_main_row(row) -> bool:
        """主行判定：实测仅主行带 .order-status-column 状态列；
        兜底：行文本匹配订单号格式 123-1234567-1234567 也视为主行。"""
        try:
            if row.locator(get_selectors("order_status")[0]).count() > 0:
                return True
        except Exception:
            pass
        try:
            return bool(re.search(r'\d{3}-\d{7}-\d{7}', row.inner_text(timeout=2000)))
        except Exception:
            return False

    # 一单多品：列表 = 主行（rowspan，含订单号/买家/状态列）+ 子行（仅商品单元格）。
    # 以订单为单位提取：主行开启一个订单组，后续非主行并入该组，直到下一个主行。
    current_main = None
    current_group: list = []

    def _flush_group():
        """处理已收集的订单组（主行 + 子行）。"""
        nonlocal refund_skips
        if current_main is None:
            return
        main_row, group = current_main, current_group
        try:
            main_text = main_row.inner_text(timeout=3000)
        except Exception:
            main_text = ""
        group_text = main_text
        try:
            for r in group[1:]:
                group_text += "\n" + r.inner_text(timeout=3000)
        except Exception:
            pass
        # 退款订单排除：退款标记只出现在主行（与 JS 提取路径保持一致），
        # 文本"已申请退款"作兜底。
        refund_applied = False
        try:
            refund_applied = any(
                main_row.locator(selector).count() > 0
                for selector in get_selectors("order_refund_status")
            )
        except Exception:
            pass
        if refund_applied or '已申请退款' in main_text:
            refund_skips += 1
            return
        # 状态：只从主行的 .order-status-column 读取，避免子行"已配送数量"等
        # 商品文本被误判；无状态列时（防御）退回主行整行文本匹配。
        status_text = ""
        try:
            status_text = (main_row.locator(get_selectors("order_status")[0]).first
                           .inner_text(timeout=2000) or "").strip()
        except Exception:
            status_text = ""
        status_source = status_text or main_text
        matched_status = ""
        for pat in paid_patterns:
            if pat in status_source:
                matched_status = pat
                break
        # 诊断：只统计匹配到的状态；未匹配时记录状态列文本（而非任意 td 文本，
        # 旧逻辑会把"更多信息 «/标准/平邮"等非状态文本混入状态分布）
        if matched_status:
            status_counts[matched_status] = status_counts.get(matched_status, 0) + 1
        elif status_text:
            status_counts[status_text] = status_counts.get(status_text, 0) + 1
        if not matched_status:
            return
        # 订单号：只从主行文本提取（子行没有订单号）
        order_match = re.search(r'(\d{3}-\d{7}-\d{7})', main_text)
        if not order_match:
            return
        order_id = order_match.group(1)
        if any(o.order_id == order_id for o in orders):
            return
        # 订单级 ASIN 校验：取整组（主行+子行）的 ASIN 并集。
        # 目标 ASIN 在任一子行时订单也能被提取；
        # 搜索连带返回的"相关商品"订单因整组不含目标 ASIN 仍被排除。
        group_asins = re.findall(r'B0[A-Z0-9]{8}', group_text)
        has_target = asin in group_asins
        if not has_target:
            for a in group_asins:
                skipped_asins[a] = skipped_asins.get(a, 0) + 1
            return
        # 买家名称：只从主行定位（data-test-id 优先，空 href 链接兜底，
        # 排除"退款/发票"链接文本，与 JS 提取路径保持一致）
        buyer_name = ""
        buyer_debug = ""
        try:
            link_infos = []
            buyer_link = main_row.locator(get_selectors("order_buyer_link")[0])
            if buyer_link.count() > 0:
                txt = buyer_link.first.inner_text(timeout=1000).strip()
                if txt:
                    buyer_name = txt
                    buyer_debug = f'data-test-id → "{txt}"'
                else:
                    buyer_debug = "data-test-id 存在但文本为空"
            if not buyer_name:
                for link in main_row.locator("a").all():
                    href = link.get_attribute("href") or ""
                    txt = link.inner_text(timeout=1000).strip()
                    if href == "" or href.startswith("javascript:") or href == "#":
                        link_infos.append(f"text=\"{txt[:30]}\" href=\"{href[:40] or '(空)'}\"")
                        if not buyer_name and txt and len(txt) < 80 and "退款" not in txt and "发票" not in txt:
                            buyer_name = txt
                if buyer_name:
                    buyer_debug = buyer_debug or f"兜底匹配 → \"{buyer_name}\" (候选: [{', '.join(link_infos)}])"
                elif link_infos:
                    buyer_debug = f"兜底链接均被过滤 (候选: [{', '.join(link_infos)}])"
                else:
                    buyer_debug = "无匹配链接 (主行内未找到符合条件的 <a> 标签)"
        except Exception as e:
            buyer_debug = f"链接提取异常: {e}"
        # 提取销售渠道（如 Amazon.de, Amazon.fr, Amazon.co.uk 等），只看主行
        sales_channel = ""
        ch_match = re.search(r'Amazon\.[a-z]{2,3}(?:\.[a-z]{2})?', main_text, re.IGNORECASE)
        if ch_match:
            sales_channel = ch_match.group(0)
        channel_debug = sales_channel if sales_channel else f"未匹配 (主行文本前100字: \"{main_text[:100]}\")"
        orders.append(OrderInfo(
            order_id=order_id,
            buyer_name=buyer_name,
            asin=asin,
            sales_channel=sales_channel,
            page_num=page_num,
        ))
        if len(debug_rows) < MAX_DEBUG:
            debug_rows.append({
                "order_id": order_id,
                "status": matched_status,
                "buyer_name": buyer_name or "(未提取到)",
                "buyer_debug": buyer_debug,
                "sales_channel": sales_channel or "(未提取到)",
                "channel_debug": channel_debug,
                "group_rows": len(group),
            })

    # 遍历所有行：主行开启新订单组，后续非主行并入当前组（首个主行前的表头行忽略）
    for row in rows:
        if _is_main_row(row):
            _flush_group()
            current_main = row
            current_group = [row]
        elif current_main is not None:
            current_group.append(row)
            product_rows += 1
    _flush_group()

    # 打印 DOM 回退模式的诊断
    if refund_skips:
        print(f"  [诊断-DOM] 退款订单过滤: 排除 {refund_skips} 单 (refund-is-applied/已申请退款)")
    if product_rows:
        print(f"  [诊断-DOM] 一单多品行: {product_rows} 行已并入所属订单")
    if status_counts:
        matched_list = [f"{k}({v})" for k, v in sorted(status_counts.items(), key=lambda x: -x[1])[:10]]
        print(f"  [诊断-DOM] 页面状态分布: {', '.join(matched_list)}")
    if skipped_asins:
        skip_list = [f"{k}({v})" for k, v in sorted(skipped_asins.items(), key=lambda x: -x[1])]
        print(f"  [诊断-DOM] ASIN过滤: 跳过 {sum(skipped_asins.values())} 单 (非目标ASIN: {', '.join(skip_list)})")
    if debug_rows:
        print(f"  [诊断-DOM] ── 逐单元素定位详情（前 {len(debug_rows)} 单）──")
        for i, dr in enumerate(debug_rows, 1):
            print(f"  [诊断-DOM] [{i}] 订单={dr['order_id']} | 状态={dr['status']}")
            print(f"  [诊断-DOM]     买家: {dr['buyer_name']}")
            print(f"  [诊断-DOM]     买家定位: {dr['buyer_debug']}")
            print(f"  [诊断-DOM]     站点: {dr['sales_channel']}")
            print(f"  [诊断-DOM]     站点定位: {dr['channel_debug']}")
            if dr.get('group_rows', 1) > 1:
                print(f"  [诊断-DOM]     一单多品: 共 {dr.get('group_rows')} 行并入本单")
        print(f"  [诊断-DOM] ── 逐单诊断结束 ──")

    return orders


# ============================================================================
# 邮件发送操作
# ============================================================================

def _safe_page_url(page: Page) -> str:
    """读取 Page URL 用于导航诊断；页面刚关闭时返回空字符串。"""
    try:
        return page.url or ""
    except Exception:
        return ""


def _is_orders_page_url(url: str) -> bool:
    """当前 Page 是否仍是可继续重试买家点击的订单管理页。"""
    return "/orders-v3" in (url or "").lower()


def _order_row_on_current_page(page: Page, order: OrderInfo) -> bool:
    """订单行仍在当前列表时，无需重新导航和等待整页懒加载。"""
    if not _is_orders_page_url(_safe_page_url(page)):
        return False
    try:
        return page.locator(get_selectors("order_rows")[0]).filter(
            has_text=order.order_id).count() > 0
    except Exception:
        return False


def _locate_buyer_link(page: Page, order: OrderInfo) -> tuple[Optional[Locator], dict]:
    """
    在目标订单行内构造一个新鲜的买家链接 Locator。

    返回的 Locator 只允许由本轮点击立即消费，调用方不得跨刷新保存。新版
    data-test-id 链接优先；旧版无 href 链接必须与提取到的买家名称精确对应，
    避免误点同一订单行内的退款、发票等其他动作。
    """
    try:
        rows = page.locator(get_selectors("order_rows")[0]).filter(has_text=order.order_id)
        if rows.count() <= 0:
            return None, {"found": False, "reason": "row_missing"}

        row = rows.first
        primary = row.locator(get_selectors("order_buyer_link")[0])
        if primary.count() > 0:
            target = primary.first
            try:
                text = (target.inner_text(timeout=2000) or "").strip()
            except RuntimeError:
                raise
            except Exception:
                text = ""
            return target, {
                "found": True,
                "kind": "buyer-name-with-link",
                "text": text,
            }

        # 旧版订单页把买家动作渲染成多种“无实际导航 href”的链接：
        # 缺失/空 href、# 或 javascript:。提取订单时四类都会被识别，
        # 跳转定位必须保持相同兼容范围；最终仍以买家名称精确匹配防止误点。
        fallback = row.locator(get_selectors("order_buyer_fallback_link")[0])
        observed_texts: list[str] = []
        for index in range(fallback.count()):
            candidate = fallback.nth(index)
            try:
                text = (candidate.inner_text(timeout=2000) or "").strip()
            except RuntimeError:
                raise
            except Exception:
                continue
            if text:
                observed_texts.append(text)
            if order.buyer_name and text == order.buyer_name:
                return candidate, {
                    "found": True,
                    "kind": "no-href-link",
                    "text": text,
                }

        reason = "buyer_name_mismatch" if observed_texts else "link_missing"
        return None, {
            "found": False,
            "reason": reason,
            "observed_texts": observed_texts,
        }
    except RuntimeError:
        raise
    except Exception as exc:
        return None, {
            "found": False,
            "reason": "locator_error",
            "error": str(exc),
        }


def _is_contact_page_url(url: str) -> bool:
    """联系页完成跳转后的 URL；临时 contact.html 仍需继续等待重定向。"""
    normalized = (url or "").lower()
    return "messaging/contact" in normalized


def _is_contact_navigation_url(url: str) -> bool:
    """点击后可接受的中间/最终联系页导航信号。"""
    normalized = (url or "").lower()
    return "contact.html" in normalized or _is_contact_page_url(normalized)


def _wait_and_validate_contact_page(
    contact_page: Page,
    source_page: Page,
    order: OrderInfo,
) -> Optional[Page]:
    """等待联系页重定向完成，并保留既有 orderID 校验和失败页清理。"""
    if contact_page is not source_page:
        try:
            contact_page.bring_to_front()
        except Exception:
            pass

    for _ in range(120):
        _check_stop_global()
        _wait_if_paused_global()
        if _is_contact_page_url(_safe_page_url(contact_page)):
            break
        contact_page.wait_for_timeout(500)
    else:
        print(f"  [FAIL] 页面始终不是联系页面: {_safe_page_url(contact_page)[:100]}")
        if contact_page is not source_page:
            try:
                contact_page.close()
            except Exception:
                pass
        return None

    try:
        contact_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    contact_url = _safe_page_url(contact_page)
    url_oid = re.search(r'orderID=([\d-]+)', contact_url)
    if url_oid and url_oid.group(1) != order.order_id:
        print(f"  [FAIL] orderID 不匹配！期望: {order.order_id}, 实际: {url_oid.group(1)}")
        if contact_page is not source_page:
            try:
                contact_page.close()
            except Exception:
                pass
        return None

    print(f"  [OK] 已切换到联系页面: {contact_url[:120]}")
    return contact_page


def _wait_for_buyer_navigation(
    source_page: Page,
    pages_before: list,
    source_url_before: str,
    timeout: float = 8.0,
    poll_interval: float = 0.2,
) -> Optional[tuple[Page, str]]:
    """
    有界检测真实点击产生的新 Page 或同 Page 联系页导航。

    Page 来源字符串固定为 new_page / same_page，供日志和回归测试区分。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    pages_after = list(pages_before)

    while True:
        _check_stop_global()
        _wait_if_paused_global()

        try:
            pages_after = list(source_page.context.pages)
        except RuntimeError:
            raise
        except Exception:
            pages_after = list(pages_before)

        new_pages = [
            candidate for candidate in pages_after
            if not any(candidate is existing for existing in pages_before)
        ]
        source_url_after = _safe_page_url(source_page)

        # 多个 Page 同时出现时，优先选择已经进入联系页路径的候选。
        for candidate in reversed(new_pages):
            candidate_url = _safe_page_url(candidate)
            if _is_contact_navigation_url(candidate_url):
                print(
                    "  [导航检测] 候选页来源=新页 "
                    f"| 源URL={source_url_before[:90]} → {source_url_after[:90]} "
                    f"| 页签={len(pages_before)} → {len(pages_after)} "
                    f"| 候选URL={candidate_url[:100]}")
                return candidate, "new_page"

        if _is_contact_navigation_url(source_url_after):
            print(
                "  [导航检测] 候选页来源=同页 "
                f"| 源URL={source_url_before[:90]} → {source_url_after[:90]} "
                f"| 页签={len(pages_before)} → {len(pages_after)}")
            return source_page, "same_page"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # 新页可能先以 about:blank 或其他中间 URL 出现。不要在首轮就选中
            # 一个未解析的新页，否则可能错过稍后出现的真正联系页。达到本轮
            # 有界超时后仍保留最新新页，由统一联系页校验继续处理慢重定向。
            if new_pages:
                candidate = new_pages[-1]
                print(
                    "  [导航检测] 候选页来源=新页(超时仍在加载) "
                    f"| 源URL={source_url_before[:90]} → {source_url_after[:90]} "
                    f"| 页签={len(pages_before)} → {len(pages_after)} "
                    f"| 候选URL={_safe_page_url(candidate)[:100] or 'about:blank'}")
                return candidate, "new_page"
            print(
                "  [点击无导航] 有界等待结束 "
                f"| 源URL={source_url_before[:90]} → {source_url_after[:90]} "
                f"| 页签={len(pages_before)} → {len(pages_after)}")
            return None
        source_page.wait_for_timeout(int(min(max(0.01, poll_interval), remaining) * 1000))


def _click_buyer_link_once(
    page: Page,
    order: OrderInfo,
    navigation_timeout: float = 20.0,
) -> Optional[Page]:
    """
    用新鲜 Locator 执行一次真实 Playwright 点击并捕获导航结果。

    Locator.click() 返回只表示输入 API 已完成；必须观察到新 Page 或源 Page
    进入 contact.html / messaging/contact，才会进入联系页校验。
    """
    target, info = _locate_buyer_link(page, order)
    if target is None:
        reason = info.get("reason", "unknown")
        if reason == "row_missing":
            print(f"  [未就绪] 目标订单行尚未进入 DOM [{order.order_id}]")
        elif reason == "link_missing":
            print(f"  [未就绪] 目标订单行已出现，但未找到买家链接 [{order.order_id}]")
        elif reason == "buyer_name_mismatch":
            observed = ", ".join(info.get("observed_texts", [])) or "(空)"
            print(
                "  [未就绪] 目标订单行只有不匹配的无 href 链接 "
                f"| 期望买家={order.buyer_name!r} | 候选={observed!r} "
                f"[{order.order_id}]")
        else:
            detail = f": {info.get('error')}" if info.get("error") else ""
            print(f"  [WARN] 买家 Locator 定位失败 ({reason}){detail} [{order.order_id}]")
        return None

    _check_stop_global()
    _wait_if_paused_global()
    source_url_before = _safe_page_url(page)
    try:
        pages_before = list(page.context.pages)
    except RuntimeError:
        raise
    except Exception:
        pages_before = [page]
    if not any(page is candidate for candidate in pages_before):
        pages_before.append(page)

    print(
        "  [点击准备] 方式=Playwright Locator.click "
        f"| 定位={info.get('kind', '?')} | 文本={info.get('text') or '?'} "
        f"| 源URL={source_url_before[:100]} | 页签={len(pages_before)} "
        f"[{order.order_id}]")

    click_error: Optional[Exception] = None
    try:
        target.click(timeout=10000)
        print(f"  [点击派发] Locator.click 已完成，等待实际导航 [{order.order_id}]")
    except RuntimeError:
        raise
    except Exception as exc:
        click_error = exc
        print(f"  [点击异常] Locator.click 未正常返回: {exc} [{order.order_id}]")

    navigation = _wait_for_buyer_navigation(
        page,
        pages_before,
        source_url_before,
        timeout=navigation_timeout,
    )
    if navigation is None:
        if click_error is not None:
            print(f"  [点击失败] 真实点击异常且未产生导航 [{order.order_id}]")
        return None

    candidate_page, source = navigation
    if click_error is not None:
        print(
            f"  [INFO] Locator.click 虽抛出异常，但检测到{source}导航，继续校验 "
            f"[{order.order_id}]")
    return _wait_and_validate_contact_page(candidate_page, page, order)


def _wait_for_target_buyer_link(
    page: Page,
    order: OrderInfo,
    timeout: float = 30.0,
    poll_interval: float = 1.0,
) -> bool:
    """刷新后持续滚动，直到目标订单行及买家链接进入 DOM（有界等待）。"""
    deadline = time.monotonic() + timeout
    last_info = {"reason": "row_missing"}
    while time.monotonic() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        target, info = _locate_buyer_link(page, order)
        if target is not None:
            print(
                "  [等待就绪] 刷新后目标订单行及买家链接已就绪 "
                f"| 定位={info.get('kind', '?')} | 文本={info.get('text') or '?'} "
                f"[{order.order_id}]")
            return True
        if info.get("reason") in {"link_missing", "buyer_name_mismatch"}:
            # 恢复等待的完成信号是目标订单行进入 DOM；链接是否可用必须由
            # 随后的第三次新鲜定位决定。这样新版无链接列表不会额外空等。
            print(
                "  [等待就绪] 刷新后目标订单行已进入 DOM，买家链接待重定位 "
                f"| 当前状态={info.get('reason')} [{order.order_id}]")
            return True
        last_info = info
        try:
            page.evaluate(r"""() => {
                const sc = document.scrollingElement || document.body;
                sc.scrollTop = sc.scrollHeight;
                window.scrollTo(0, document.body.scrollHeight);
            }""")
        except RuntimeError:
            raise
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(max(0.01, poll_interval), remaining))

    last_reason = last_info.get("reason", "unknown")
    if last_reason == "buyer_name_mismatch":
        observed = ", ".join(last_info.get("observed_texts", [])) or "(空)"
        print(
            "  [WARN] 刷新后目标订单行已出现，但无 href 链接与买家不匹配 "
            f"| 期望={order.buyer_name!r} | 候选={observed!r} [{order.order_id}]")
    elif last_reason == "link_missing":
        print(f"  [WARN] 刷新后目标订单行已出现，但买家链接始终未就绪 [{order.order_id}]")
    else:
        print(f"  [WARN] 刷新后目标订单行始终未进入 DOM [{order.order_id}]")
    return False


def _click_buyer_name(page: Page, order: OrderInfo) -> Optional[Page]:
    """Click the buyer link once and accept a late contact popup without clicking again."""
    if not _is_orders_page_url(_safe_page_url(page)):
        print(f"  [FAIL] 当前页不是订单列表，无法点击买家名称 [{order.order_id}]")
        return None

    pages_before = list(page.context.pages)
    source_url_before = _safe_page_url(page)
    contact_page = _click_buyer_link_once(page, order)
    if contact_page is not None:
        return contact_page

    # Amazon can create the popup after click() has returned; never dispatch a second click.
    late_navigation = _wait_for_buyer_navigation(
        page, pages_before, source_url_before, timeout=2.0)
    if late_navigation is not None:
        candidate, _ = late_navigation
        return _wait_and_validate_contact_page(candidate, page, order)

    print(f"  [FAIL] 买家点击未打开联系页，跳过当前订单 [{order.order_id}]")
    return None


def _confirm_step(step_desc: str) -> bool:
    """弹窗询问用户：当前步骤是否正确完成？返回 True=继续, False=放弃"""
    result = _prompt_manual_intervention(
        f"请确认以下操作已正确完成：\n\n{step_desc}\n\n"
        f"如果操作正确请点击「已完成」继续；\n"
        f"如果出现问题请关闭窗口，程序将跳过此订单。"
    )
    if not result:
        print(f"  [SKIP] 用户确认操作失败，跳过此订单")
    return result


def _is_page_error(exc: Exception) -> bool:
    """判断异常是否为页面导航/刷新/关闭所致。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in [
        'execution context was destroyed',
        'target closed',
        'page has been closed',
        'has been closed',
        'target page, context or browser',
        'navigating',
        'detached frame',
    ])


def _is_page_unstable(page: Page) -> bool:
    """检查页面是否已脱离联系页面（刷新/跳转到非预期 URL）。"""
    try:
        url = page.url or ""
        # 联系页面 URL 应包含 messaging 或 contact
        if "messaging" not in url and "contact" not in url:
            return True
        # 尝试执行简单 JS 确认页面可交互
        page.evaluate("() => 1")
        return False
    except Exception:
        return True


def _is_page_navigated_away(page: Page) -> bool:
    """
    检查页面 URL 是否已完全脱离联系页面。
    与 _is_page_unstable 的区别：本函数仅检查 URL 变化（全页导航），
    不检查 JS 可交互性。用于判断是否需要全局重试（重新点击买家名称）。
    AJAX 内部刷新（URL 不变）不会触发此条件。
    """
    try:
        url = page.url or ""
        return "messaging" not in url and "contact" not in url
    except Exception:
        return True


def _has_loading_overlay(page: Page) -> bool:
    """
    检测联系买家页是否出现「无法找到订单。请重试。」遮罩层（多语言）。

    网络波动时，Amazon 的 AJAX 请求可能临时失败，页面会在同一 URL 下
    弹出 ayb-disabled-overlay 遮罩（含刷新图标 + 错误文案），
    但几十秒后自动恢复。此函数用于检测该状态是否存在。

    返回 True 表示遮罩层存在（页面暂不可用）。
    """
    try:
        return page.evaluate("""() => {
            const overlay = document.querySelector('.ayb-disabled-overlay');
            if (!overlay) return false;
            // 检查 overlay 是否可见（display/visibility 未被隐藏）
            const style = window.getComputedStyle(overlay);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            // 多语言文案匹配：中文 / 英文
            const text = (overlay.textContent || '').toLowerCase();
            const zh = text.includes('无法找到订单') || text.includes('请重试');
            const en = text.includes('unable to find') || text.includes('cannot find')
                    || text.includes('please retry') || text.includes('try again')
                    || text.includes('please try');
            // 结构特征匹配：ayb-disabled-overlay 内含 kat-icon[name="refresh"]
            const hasRetryIcon = overlay.querySelector('kat-icon[name="refresh"]') !== null;
            return zh || en || hasRetryIcon;
        }""")
    except Exception:
        return False


def _wait_contact_page_recover(page: Page, timeout: int = 90) -> bool:
    """
    等待联系买家页面的临时遮罩层消失（自动恢复）。

    参数：
        timeout: 最长等待秒数（默认 90s，覆盖用户描述的"几十秒"）

    返回：
        True  — 页面已恢复（遮罩消失）
        False — 超时未恢复
    """
    if not _has_loading_overlay(page):
        return True  # 没有遮罩，无需等待

    print("  [INFO] 检测到「无法找到订单。请重试。」遮罩层，等待自动恢复...")
    start = time.time()
    while time.time() - start < timeout:
        _check_stop_global()
        _wait_if_paused_global()
        if not _has_loading_overlay(page):
            elapsed = int(time.time() - start)
            print(f"  [OK] 遮罩已消失，页面恢复正常（等待 {elapsed}s）")
            return True
        time.sleep(2)

    print(f"  [WARN] 等待 {timeout}s 后遮罩仍未消失")
    return False


def _safe_get_url(page: Page) -> str:
    """安全获取页面 URL，页面异常时返回错误提示。"""
    try:
        return page.url or "about:blank"
    except Exception:
        return "<页面不可用>"


def _safe_wait_page(page: Page, label: str, timeout: int = 15000):
    """安全等待页面加载，超时/异常不抛错。"""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        if label:
            print(f"  [INFO] '{label}' networkidle 超时（属正常现象）")


def _safe_select_radio(page: Page, order: OrderInfo):
    """等待目标主题可选后选择 radio。

    按站点类型自动适配：
      - 欧洲区：选择「必要的法律披露信息」(aria-label 含"必要的法律" / "legaldisclosure")
      - 美国区：选择「发送发票消息模板」(aria-label 含"发送发票"/"sendInvoice")
      - 都不存在：返回 None，由调用方跳过该订单
      - 目标存在但一直禁用：返回 "disabled"，跳过且不刷新
    """
    targets = (
        ('input[aria-label*="必要的法律"]', 'ok:legal_disclosure_eu'),
        ('input[aria-label*="legaldisclosure"]', 'ok:legaldisclosure'),
        ('input[aria-label*="发送发票"]', 'ok:send_invoice_us'),
        ('input[value="sendInvoice"]', 'ok:sendInvoice'),
    )
    saw_disabled = False
    for attempt in range(15):
        _check_stop_global()
        _wait_if_paused_global()
        for selector, result in targets:
            try:
                radio = page.locator(selector).first
                if not radio.count():
                    continue
                if not radio.is_enabled(timeout=1000):
                    saw_disabled = True
                    continue
            except Exception:
                continue
            try:
                icon = radio.locator('xpath=..').locator('.kat-radiobutton-icon').first
                (icon if icon.count() else radio).click(timeout=3000)
                for check in range(16):
                    if radio.is_checked():
                        return result
                    if check < 15:
                        time.sleep(0.2)
                print("  [WARN] 联系主题已点击，但未确认选中")
            except Exception as exc:
                print(f"  [WARN] 联系主题点击失败: {type(exc).__name__}")
            return None
        if attempt < 14:
            time.sleep(1)
    return "disabled" if saw_disabled else None


def _is_invalid_contact_page(page: Page) -> bool:
    """Amazon 将不可联系的订单重定向到显示“无效链接。”的页面。"""
    try:
        return page.get_by_text("无效链接。", exact=True).count() > 0
    except Exception:
        return False


def _upload_pdf_safe(page: Page, pdf_path: str) -> bool:
    """安全上传 PDF，页面刷新时返回 False 触发重试。"""
    try:
        return _upload_pdf(page, pdf_path)
    except Exception as e:
        print(f"  [WARN] PDF 上传异常: {e}")
        return False


def send_email_with_pdf(
    page: Page,
    order: OrderInfo,
    pdf_path: str,
    test_mode: bool = True,
    dry_run: bool = True,
    max_retries: int = 2,
) -> Optional[bool] | Literal["disabled", "unconfirmed"]:
    """
    向买家发送附带 PDF 说明书的邮件。

    步骤（非干跑模式下每步有校验）：
    1. 点击买家名称 → 跳转联系页面
    2. 选择主题「必要的法律披露信息」
    3. 上传 PDF 附件
    4. 点击发送按钮
    5. 轮询校验发送成功（检测成功文案/按钮状态/URL变化）

    返回 None 表示买家联系页显示无效链接，调用方应记录并跳过该订单。
    内置页面刷新恢复：网络波动导致联系页面异常刷新时，
    自动重新打开联系页面并重试全部步骤（最多 max_retries 次）。
    """
    label = f"订单 {order.order_id}"
    print(f"[STEP] 发送邮件 -> {label}")

    contact_page = None
    last_error = ""

    for attempt in range(max_retries + 1):
        send_clicked = False
        if attempt > 0:
            print(f"  [重试] 第 {attempt}/{max_retries} 次重试 -> {label}（原因: {last_error}）")
            # 清理残留的联系页标签
            if contact_page is not None and contact_page is not page:
                try:
                    contact_page.close()
                except Exception:
                    pass
            contact_page = None
            random_delay(0.5, 1.0)

        try:
            # ---- 步骤 1：点击买家名称跳转到联系页面 ----
            if contact_page is None:
                contact_page = _click_buyer_name(page, order)
                if contact_page is None:
                    return False

            random_delay(0.5, 1.0)
            _safe_wait_page(contact_page, "联系页面")
            current_url = _safe_get_url(contact_page)
            print(f"  联系页面: {current_url}")

            if _is_invalid_contact_page(contact_page):
                print(f"  [SKIP] 买家不可联系：联系页显示无效链接 -> {label}")
                if contact_page is not page:
                    try:
                        contact_page.close()
                    except Exception:
                        pass
                return None

            # 联系页加载后先检查是否有遮罩层（网络波动导致 AJAX 临时失败）
            if not _wait_contact_page_recover(contact_page):
                print(f"  [FAIL] 联系页面遮罩层未恢复")
                return False

            # ---- 步骤 2：选择 radio ----
            radio_ok = _safe_select_radio(contact_page, order)
            if radio_ok == "disabled":
                print(f"  [SKIP] 联系主题已被 Amazon 禁用，跳过 -> {label}")
                if contact_page is not page:
                    try:
                        contact_page.close()
                    except Exception:
                        pass
                return "disabled"
            if not radio_ok:
                if not _is_contact_page_url(_safe_page_url(contact_page)):
                    print(f"  [FAIL] 选择联系主题后已离开联系页 -> {label}")
                    return False
                print(f"  [INFO] 未点中联系主题，刷新当前联系页重试一次 -> {label}")
                try:
                    contact_page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    print(f"  [FAIL] 刷新当前联系页失败: {exc}")
                    return False
                if _is_invalid_contact_page(contact_page):
                    print(f"  [SKIP] 买家不可联系：联系页显示无效链接 -> {label}")
                    if contact_page is not page:
                        try:
                            contact_page.close()
                        except Exception:
                            pass
                    return None
                if not _is_contact_page_url(_safe_page_url(contact_page)):
                    print(f"  [FAIL] 刷新后页面已离开联系页 -> {label}")
                    return False
                if not _wait_contact_page_recover(contact_page):
                    print(f"  [FAIL] 刷新后联系页遮罩层未恢复 -> {label}")
                    return False
                radio_ok = _safe_select_radio(contact_page, order)
                if radio_ok == "disabled":
                    print(f"  [SKIP] 刷新后联系主题仍被 Amazon 禁用，跳过 -> {label}")
                    if contact_page is not page:
                        try:
                            contact_page.close()
                        except Exception:
                            pass
                    return "disabled"
                if not radio_ok:
                    print(f"  [FAIL] 未找到可用的 radio（欧洲区「必要的法律披露信息」或美国区「发送发票消息模板」）")
                    if contact_page is not page:
                        try:
                            contact_page.close()
                        except Exception:
                            pass
                    return False
            print(f"  [OK] radio 已选中")

            # ---- 步骤 3：上传 PDF ----
            random_delay(0.5, 1)
            upload_ok = _upload_pdf_safe(contact_page, pdf_path)
            # 上传后主动检测遮罩：即使 set_input_files 返回成功，遮罩层下的文件可能未真正附加上
            if _has_loading_overlay(contact_page):
                print("  [INFO] 上传 PDF 后检测到遮罩，等待恢复后重新上传...")
                if _wait_contact_page_recover(contact_page):
                    upload_ok = _upload_pdf_safe(contact_page, pdf_path)  # 遮罩消失 → 重新上传
            if not upload_ok:
                if _is_page_navigated_away(contact_page):
                    last_error = "上传 PDF 时页面刷新"
                    continue  # 全页跳转 → 全局重试
                print(f"  [FAIL] PDF 上传失败")
                return False
            print(f"  [OK] PDF 已上传")

            # ---- 步骤 4：测试确认 + 点击发送 ----
            if test_mode and not _test_confirm_dialog(order, pdf_path):
                print(f"  [SKIP] 用户取消发送 -> {label}")
                return False

            # 点击发送前主动检测遮罩
            if _has_loading_overlay(contact_page):
                print("  [INFO] 点击发送前检测到遮罩，等待恢复...")
                _wait_contact_page_recover(contact_page)

            send_clicked = _click_send_button(contact_page)
            if not send_clicked:
                if _is_page_navigated_away(contact_page):
                    last_error = "点击发送时页面导航离开"
                    continue  # 全页跳转 → 全局重试
                if _has_loading_overlay(contact_page):
                    print("  [INFO] 点击发送时遇到遮罩，等待恢复后原地重试...")
                    if _wait_contact_page_recover(contact_page):
                        send_clicked = _click_send_button(contact_page)  # 遮罩消失 → 原地重试
                if not send_clicked:
                    print(f"  [FAIL] 未找到可点击的发送按钮 -> {label}")
                    return False
            print(f"  [OK] 已点击发送按钮 -> {label}")

            # ---- 步骤 5：校验发送成功 ----
            send_confirmed = _verify_send_success(contact_page, order, timeout=25)
            if not send_confirmed:
                print(f"  [WARN] 邮件已点击发送但结果未确认，禁止自动重试 -> {label}")
                return "unconfirmed"

            print(f"  [OK] 邮件发送成功 -> {label}")
            # 发送成功后关闭联系页标签
            if contact_page is not None and contact_page is not page:
                try:
                    contact_page.close()
                except Exception:
                    pass
            return True

        except Exception as e:
            last_error = str(e)[:120]
            if send_clicked:
                print(f"  [WARN] 邮件已点击发送但校验异常，禁止自动重试: {last_error}")
                return "unconfirmed"
            if _is_page_error(e) and attempt < max_retries:
                print(f"  [WARN] 页面异常，将重试: {last_error}")
                continue
            print(f"  [FAIL] 发送邮件异常: {last_error}")
            # 异常退出前关闭联系页
            if contact_page is not None and contact_page is not page:
                try:
                    contact_page.close()
                except Exception:
                    pass
            return False

    # 重试耗尽：关闭残留联系页
    if contact_page is not None and contact_page is not page:
        try:
            contact_page.close()
        except Exception:
            pass
    print(f"  [FAIL] 已达最大重试次数 ({max_retries}) -> {label}")
    return False


def _upload_pdf(page: Page, pdf_path: str) -> bool:
    """
    上传 PDF 文件到邮件附件区域。

    亚马逊用 <input type="file"> 上传附件。Playwright 可以直接 set_input_files。
    """
    print(f"[INFO] 上传 PDF: {pdf_path}")

    if not os.path.isfile(pdf_path):
        print(f"[FAIL] PDF 文件不存在: {pdf_path}")
        return False

    # 寻找文件上传 input
    file_input_selectors = get_selectors("file_input")

    for sel in file_input_selectors:
        try:
            file_input = page.locator(sel).first
            if file_input.count() > 0:
                file_input.set_input_files(pdf_path, timeout=10000)
                print(f"[OK] PDF 已上传")
                random_delay(1, 3)  # 等上传完成
                return True
        except Exception:
            continue

    # 备选：可能需要先点击"添加附件"按钮
    print("[INFO] 未找到文件 input，尝试点击添加附件按钮...")
    attach_clicked = safe_click(page, get_selectors("attachment_button"), label="添加附件")

    if attach_clicked:
        random_delay(0.5, 1.5)
        # 再次尝试
        for sel in file_input_selectors:
            try:
                file_input = page.locator(sel).first
                if file_input.count() > 0:
                    file_input.set_input_files(pdf_path, timeout=10000)
                    print(f"[OK] PDF 已上传")
                    random_delay(1, 3)
                    return True
            except Exception:
                continue

    # 最后尝试：使用文件选择器对话框
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            safe_click(page, get_selectors("attachment_button") + (get_selectors("file_input")[0],),
                       label="触发文件选择器")
        file_chooser = fc_info.value
        file_chooser.set_files(pdf_path)
        print(f"[OK] PDF 通过文件选择器上传")
        random_delay(1, 3)
        return True
    except Exception as e:
        print(f"[FAIL] 文件选择器方式也失败: {e}")

    return False


def _page_still_alive(page: Page) -> bool:
    """检查页面是否仍然存在且可交互（未崩溃/关闭/完全导航走）。"""
    try:
        page.evaluate("() => 1")
        return True
    except Exception:
        return False


def _safe_wait_with_check(page: Page, seconds: float):
    """分步等待并响应暂停/终止，同时检查页面是否存活。"""
    step = 0.5
    elapsed = 0.0
    while elapsed < seconds:
        _check_stop_global()
        _wait_if_paused_global()
        time.sleep(min(step, seconds - elapsed))
        elapsed += step


def _dump_button_diagnostics(page: Page):
    """输出页面按钮状态诊断信息。"""
    try:
        diag = page.evaluate('''() => {
            const info = [];
            const kbs = document.querySelectorAll('kat-button');
            info.push('kat-button 数量: ' + kbs.length);
            kbs.forEach((k, i) => {
                info.push(`  [${i}] label="${k.getAttribute('label')}", disabled=${k.hasAttribute('disabled')}`
                    + `, shadowRoot=${!!k.shadowRoot}`);
                if (k.shadowRoot) {
                    const btns = k.shadowRoot.querySelectorAll('button');
                    info.push(`    shadow buttons: ${btns.length}`);
                    btns.forEach((b, j) => {
                        info.push(`    [${j}] text="${b.textContent}", disabled=${b.disabled}`);
                    });
                }
            });
            const plainBtns = document.querySelectorAll('button');
            info.push('普通 button 数量: ' + plainBtns.length);
            plainBtns.forEach((b, j) => {
                info.push(`  [${j}] text="${b.textContent?.substring(0,50)}", disabled=${b.disabled}`);
            });
            // 也输出页面标题和 URL 片段
            info.push(`页面标题: ${document.title}`);
            info.push(`URL: ${window.location.href?.substring(0, 150)}`);
            return info.join('\\n');
        }''')
        print(f"  [诊断]\n{diag}")
    except Exception as e:
        print(f"  [诊断] 无法获取按钮信息: {e}")


def _click_send_button(page: Page) -> bool:
    """
    点击联系买家页面的「发送」按钮。

    Amazon 使用 kat-button web component，其 shadow DOM 内的 <button>
    需要真实的鼠标事件序列才能触发提交。

    策略（按优先级）：
    1. Playwright locator.click() — 穿透 shadow DOM，真实鼠标事件
    2. Playwright locator.dispatchEvent('click')
    3. JS MouseEvent 序列 — mousedown→mouseup→click
    4. JS 点击宿主 kat-button 元素（兜底）

    内置刷新恢复：上传 PDF 后页面可能因网络波动触发内部列表刷新，
    导致按钮暂时从 DOM 消失。此时等待页面稳定后重试，不慌张返回失败。
    """
    send_locators = get_selectors("send_button")

    for attempt in range(4):  # 最多 4 轮尝试（含等待）
        if attempt > 0:
            wait_sec = min(3 + attempt * 2, 10)
            print(f"  [点击] 发送按钮未出现，等待页面刷新完成（{wait_sec}s）后重试...")
            _safe_wait_with_check(page, wait_sec)

        # ---- 策略 1: Playwright 原生 click ----
        for sel in send_locators:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_enabled(timeout=2000):
                    loc.click(timeout=5000, trial=False)
                    print(f"  [点击] Playwright click 成功: {sel}")
                    return True
            except Exception:
                pass

        # ---- 策略 2: dispatchEvent ----
        try:
            loc = page.locator(get_selectors("send_button")[0]).first
            if loc.count() > 0:
                loc.dispatch_event('click')
                print(f"  [点击] dispatchEvent click 已触发")
                return True
        except Exception:
            pass

        # ---- 策略 3: JS MouseEvent 序列 ----
        try:
            result = page.evaluate('''() => {
                const kbs = document.querySelectorAll('kat-button');
                for (const k of kbs) {
                    const label = k.getAttribute('label') || '';
                    if ((label === '发送' || label === 'Send') && !k.hasAttribute('disabled')) {
                        const btn = k.shadowRoot
                            ? k.shadowRoot.querySelector('button:not([disabled])')
                            : k;
                        if (btn) {
                            const opts = { bubbles: true, cancelable: true, composed: true };
                            btn.dispatchEvent(new MouseEvent('mousedown', opts));
                            btn.dispatchEvent(new MouseEvent('mouseup', opts));
                            btn.dispatchEvent(new MouseEvent('click', opts));
                            return true;
                        }
                    }
                }
                return false;
            }''')
            if result:
                print(f"  [点击] JS MouseEvent 序列已触发")
                return True
        except Exception:
            pass

        # ---- 策略 4: 兜底 — 点击宿主元素 / 普通 button ----
        try:
            result = page.evaluate('''() => {
                const kbs = document.querySelectorAll('kat-button');
                for (const k of kbs) {
                    const label = k.getAttribute('label') || '';
                    if ((label === '发送' || label === 'Send') && !k.hasAttribute('disabled')) {
                        k.click();
                        return true;
                    }
                }
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.textContent || '').trim();
                    if ((t === '发送' || t === 'Send') && !b.disabled) {
                        b.click();
                        return true;
                    }
                }
                return false;
            }''')
            if result:
                print(f"  [点击] 兜底 click 成功")
                return True
        except Exception:
            pass

        # 本轮未找到按钮 → 检查页面是否仍可交互（排除页面崩溃）
        if not _page_still_alive(page):
            print(f"  [点击] 页面已不可交互，放弃")
            break

    # ---- 全部轮次失败：输出诊断信息 ----
    print(f"  [诊断] 等待后仍未找到发送按钮，输出页面状态：")
    _dump_button_diagnostics(page)
    return False


def _test_confirm_dialog(order: OrderInfo, pdf_path: str) -> bool:
    """
    测试模式：弹窗让开发者确认填写内容后再发送（线程安全）。
    确认 → True，取消 → False。
    """
    result = {"confirmed": False}

    def _dialog(parent):
        dlg = tk.Toplevel(parent) if parent is not None else tk.Tk()
        dlg.title("测试确认 - 发送邮件")
        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"500x300+{(sw-500)//2}+{(sh-300)//2}")
        dlg.transient(parent)

        def _on_close(confirmed):
            result["confirmed"] = confirmed
            dlg.destroy()

        ttk.Label(dlg, text="确认发送邮件？", font=("", 12, "bold")).pack(pady=(15, 5))

        info_frame = ttk.Frame(dlg)
        info_frame.pack(fill="both", padx=20, pady=5)
        ttk.Label(info_frame, text=f"订单编号: {order.order_id}").pack(anchor="w")
        ttk.Label(info_frame, text=f"ASIN: {order.asin}").pack(anchor="w")
        ttk.Label(info_frame, text=f"买家名称: {order.buyer_name}").pack(anchor="w")
        ttk.Label(info_frame, text=f"PDF 文件: {os.path.basename(pdf_path)}").pack(anchor="w")
        ttk.Label(info_frame, text=f"邮件主题: 必要的法律披露信息").pack(anchor="w")

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="取消（跳过此订单）",
                   command=lambda: _on_close(False)).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="确认发送",
                   command=lambda: _on_close(True)).pack(side="left", padx=10)

        dlg.focus_force()
        dlg.grab_set()
        dlg.wait_window()
        return result["confirmed"]

    return _run_on_tk_thread(_dialog)


def _verify_send_success(page: Page, order: OrderInfo, timeout: int = 25) -> bool:
    """
    校验邮件是否真正发送成功。

    发送按钮点击后，亚马逊需要时间处理。此函数多策略轮询检测：
    1. 页面出现成功提示文字（如"消息已发送"、"Message sent"）
    2. 页面出现成功横幅 / banner 组件
    3. URL 变化（如从 compose 跳转到 sent 或 conversation 页面）

    注意：按钮变为 disabled 仅表示发送已启动，不代表亚马逊已完成处理。
    过早关闭页面会中断发送，故此函数只接受确定性成功信号。

    返回：True=发送成功，False=超时未检测到成功标识。
    """
    # 先等待最小处理时间（亚马逊至少需要几秒处理发送请求）
    min_wait = 3.0
    print(f"  [校验] 等待亚马逊处理（{min_wait}s 最小等待）...")
    waited = 0.0
    while waited < min_wait:
        _check_stop_global()
        _wait_if_paused_global()
        time.sleep(0.3)
        waited += 0.3

    print(f"  [校验] 开始轮询检测发送结果（最长 {timeout}s）...")
    start_time = time.time()
    check_interval = 1.0
    last_btn_state = "?"
    _overlay_handled = False  # 防止重复触发遮罩恢复流程

    while time.time() - start_time < timeout:
        # 支持暂停/终止
        _check_stop_global()
        _wait_if_paused_global()

        # 遮罩不能证明第一次发送未生效，恢复后只继续校验，不再次点击。
        if _has_loading_overlay(page) and not _overlay_handled:
            print("  [校验] 检测到遮罩层，等待恢复后继续确认发送结果...")
            _overlay_handled = True
            if _wait_contact_page_recover(page):
                print("  [校验] 遮罩已消失，继续确认发送结果...")
            else:
                print("  [校验] 遮罩等待超时")
                return False

        try:
            result = page.evaluate('''() => {
                const body = (document.body || document.documentElement).innerText || '';

                // 1. 成功文字（中 / 英 / 德 / 法 / 西 / 意 / 日）
                const successPatterns = [
                    '消息已发送', 'Message sent', '已发送', 'Sent',
                    'Nachricht gesendet', 'Nachricht wurde gesendet',
                    'Message envoyé', 'Message envoyée',
                    'Mensaje enviado', 'Messaggio inviato',
                    'メッセージが送信されました', '送信完了',
                    'Your message has been sent',
                    'has been sent successfully',
                ];
                for (const p of successPatterns) {
                    if (body.includes(p)) return {success: true, reason: 'text:' + p};
                }

                // 2. 发送按钮状态诊断（仅日志，不作为成功依据）
                //    按钮 disabled 只说明发送已启动，不代表亚马逊已完成处理。
                //    过早关闭页面会中断发送，因此必须等待下文确定性信号。
                const kbs = document.querySelectorAll('kat-button');
                let sendBtnState = 'not_found';
                for (const k of kbs) {
                    const label = k.getAttribute('label') || '';
                    if (label === '发送') {
                        if (k.hasAttribute('disabled')) {
                            sendBtnState = 'disabled';
                        } else {
                            let innerDisabled = false;
                            if (k.shadowRoot) {
                                const btn = k.shadowRoot.querySelector('button');
                                if (btn && btn.disabled) innerDisabled = true;
                            }
                            sendBtnState = innerDisabled ? 'disabled' : 'enabled';
                        }
                        break;
                    }
                }
                // 也检查普通 button
                if (sendBtnState === 'not_found') {
                    const buttons = document.querySelectorAll('button');
                    for (const b of buttons) {
                        if ((b.textContent || '').includes('发送')) {
                            sendBtnState = b.disabled ? 'disabled' : 'enabled';
                            break;
                        }
                    }
                }

                // 3. 成功横幅 / banner（Amazon 的 kat-banner / alert 组件）
                const banners = document.querySelectorAll(
                    'kat-banner, [class*="success"], [class*="Success"], '
                    + '[class*="alert-success"], [data-testid="success-banner"], '
                    + '[class*="toast"], [class*="notification"], [role="alert"]'
                );
                for (const b of banners) {
                    const t = (b.textContent || '').trim();
                    if (t && t.length > 2 && t.length < 300) {
                        for (const p of successPatterns) {
                            if (t.includes(p)) return {success: true, reason: 'banner:' + p};
                        }
                    }
                }

                // 4. URL 变化：离开 compose 页面，进入 sent / conversation 页面
                const url = window.location.href || '';
                if (url.includes('/conversation') || url.includes('/thread') || url.includes('/sent')) {
                    return {success: true, reason: 'url:' + url.substring(0, 120)};
                }

                return {success: false, sendBtnState: sendBtnState};
            }''')

            if result and result.get("success"):
                reason = result.get("reason", "?")
                print(f"  [校验] ✓ 发送成功确认: {reason}")
                return True

            # 诊断：每 5 次轮询输出按钮状态
            if result and int((time.time() - start_time) / check_interval) % 5 == 0:
                btn_state = result.get("sendBtnState", "?")
                if btn_state != last_btn_state:
                    print(f"  [校验] 发送按钮状态: {btn_state}")
                    last_btn_state = btn_state

        except Exception as e:
            # 页面可能正在刷新/跳转，evaluate 偶发失败属正常
            print(f"  [校验] 轮询检测异常（继续重试）: {e}")

        time.sleep(check_interval)
        # 渐进式延长检测间隔
        check_interval = min(check_interval + 0.3, 2.0)

    print(f"  [校验] ✗ 超时：未检测到发送成功标识")
    return False


# ============================================================================
# 回访消息与验证
# ============================================================================

def _prompt_unmapped_site_manual(order: OrderInfo) -> bool:
    """
    弹窗提示：当前订单的站点未映射回访消息模板，需操作者手动处理。

    在亚马逊消息页面的对话中手动输入并发送回访消息后，
    点击「已完成手动发送」继续执行下一个订单。
    """
    result = {"action": "skip"}

    def _dialog(parent):
        dlg = tk.Toplevel(parent) if parent is not None else tk.Tk()
        dlg.title("未映射站点 — 需手动处理")
        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"520x300+{(sw - 520) // 2}+{(sh - 300) // 2}")
        dlg.transient(parent)
        dlg.resizable(False, False)

        ttk.Label(dlg, text="⚠ 未映射站点，请手动发送回访消息",
                   font=("", 12, "bold"), foreground="orange").pack(pady=(15, 10))

        info_frame = ttk.Frame(dlg)
        info_frame.pack(fill="x", padx=30, pady=5)
        ttk.Label(info_frame, text=f"订单编号: {order.order_id}").pack(anchor="w")
        ttk.Label(info_frame, text=f"ASIN: {order.asin}").pack(anchor="w")
        ttk.Label(info_frame, text=f"买家名称: {order.buyer_name}").pack(anchor="w")
        ttk.Label(info_frame, text=f"销售站点: {order.sales_channel}（未映射回访消息模板）",
                   foreground="red").pack(anchor="w")

        hint_frame = ttk.Frame(dlg)
        hint_frame.pack(fill="x", padx=30, pady=(10, 5))
        ttk.Label(hint_frame, text="请在浏览器中完成以下操作：",
                   font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(hint_frame, text="1. 在消息页面的对话中输入回访消息",
                   foreground="blue").pack(anchor="w")
        ttk.Label(hint_frame, text="2. 点击发送按钮将消息发出",
                   foreground="blue").pack(anchor="w")
        ttk.Label(hint_frame, text="3. 完成后回到此处点击下方按钮",
                   foreground="blue").pack(anchor="w")

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=(15, 10))
        ttk.Button(btn_frame, text="跳过（不发送回访）",
                   command=lambda: (_set_action("skip"), dlg.destroy()),
                   width=18).pack(side="left", padx=10)
        ttk.Button(btn_frame, text="已完成手动发送",
                   command=lambda: (_set_action("done"), dlg.destroy()),
                   width=18).pack(side="left", padx=10)

        def _set_action(a):
            result["action"] = a

        dlg.focus_force()
        dlg.grab_set()
        dlg.wait_window()
        return result["action"]

    action = _run_on_tk_thread(_dialog)
    if action is None:
        # tk 主线程已退出等极端情况，对话框永远无法返回结果
        print("  [WARN] 对话框无响应（tk 主线程可能已退出），按跳过处理")
        return True  # PDF 已发送成功，仅跳过回访，不阻断流程
    if action == "done":
        print(f"  [OK] 用户已手动完成回访消息发送 -> {order.order_id}")
        return True
    else:
        print(f"  [SKIP] 用户选择跳过回访消息 -> {order.order_id}")
        return True  # PDF 已发送成功，仅跳过回访，不阻断流程


def _wait_messaging_loading_done(page: Page, timeout: float = 8.0) -> bool:
    """
    等待消息页 SPA 加载完成（全屏遮罩消失）。

    消息页为 SPA：回车搜索后 domcontentloaded 立即返回，但搜索结果仍异步
    渲染，期间页面被 <div class="ayb-loading-overlay"> 全屏遮罩
    （2026-08-17 实测 Playwright 报错 "ayb-loading-overlay intercepts
    pointer events"）。遮罩存在期间 DOM 尚未渲染完成，此时检查对话记录
    会误判为未找到，点击输入框会被遮罩拦截，必须等遮罩消失后再继续。

    轮询 JS 快照：遮罩元素不存在或 offsetWidth/offsetHeight 为 0 视为
    加载完成（间隔 0.3s）。轮询内响应全局停止/暂停。超时仅告警、不抛
    异常，由调用方继续原有流程。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        try:
            loading = bool(page.evaluate(
                r"""() => {
                    const el = document.querySelector('.ayb-loading-overlay');
                    return !!(el && (el.offsetWidth || el.offsetHeight));
                }"""))
        except Exception:
            loading = True  # 读取失败按仍在加载处理
        if not loading:
            return True
        time.sleep(0.3)

    print(f"  [WARN] 等待消息页加载完成超时 ({timeout}s)，继续执行")
    return False


def _diagnose_messaging_search(page: Page, order_id: str) -> None:
    """
    消息页搜索验证失败时的诊断输出。

    输出：当前 URL、遮罩状态、.case-list-item 数量与前 3 条的 subject 文本
    和 data-ph-capture-attribute-order-id、搜索框当前 value。
    全部容错：任何一步 evaluate/读取失败只打印占位，不抛出。
    """
    print("  [DIAG] ---------- 消息页搜索诊断 ----------")
    print(f"  [DIAG] 搜索目标订单号: {order_id}")
    try:
        print(f"  [DIAG] 当前 URL: {page.url}")
    except Exception:
        print("  [DIAG] 当前 URL: <读取失败>")

    try:
        overlay = page.evaluate(
            r"""() => {
                const el = document.querySelector('.ayb-loading-overlay');
                if (!el) return '遮罩不存在';
                return (el.offsetWidth || el.offsetHeight)
                    ? '遮罩可见（加载中）' : '遮罩隐藏';
            }""")
        print(f"  [DIAG] 遮罩状态: {overlay}")
    except Exception:
        print("  [DIAG] 遮罩状态: <读取失败>")

    try:
        info = page.evaluate(
            r"""() => {
                const nodes = [...document.querySelectorAll('.case-list-item')];
                const sample = nodes.slice(0, 3).map(n => {
                    const subj = n.querySelector('.subject');
                    return {
                        subject: ((subj ? subj.textContent : n.textContent) || '')
                            .replace(/\s+/g, ' ').trim().slice(0, 120),
                        order_id: n.getAttribute(
                            'data-ph-capture-attribute-order-id') || '',
                    };
                });
                return { count: nodes.length, sample: sample };
            }""")
        print(f"  [DIAG] .case-list-item 数量: {info['count']}")
        for i, item in enumerate(info['sample']):
            print(f"  [DIAG]   [{i + 1}] order-id={item['order_id']!r} "
                  f"subject={item['subject']!r}")
    except Exception:
        print("  [DIAG] .case-list-item 摘要: <读取失败>")

    try:
        value = page.evaluate(
            r"""() => {
                // 按 2026-08-18 回归修复后的搜索框契约查找：
                // 新 DOM 是 kat-input 自定义组件（placeholder 在组件元素上），
                // 旧 DOM 是普通 input；unique-id 为标识改名后的最后兜底
                const selectors = [
                    "kat-input[placeholder*='订单编号']",
                    "kat-input[placeholder*='电子邮件地址']",
                    "input[placeholder*='订单编号']",
                    "input[placeholder*='电子邮件地址']",
                    "[unique-id='katal-id-0']",
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    // 自定义组件：优先读 shadow DOM 内真实 input 的 value，
                    // 回退组件自身的 value 属性/特性
                    const inner = el.shadowRoot
                        ? el.shadowRoot.querySelector('input') : null;
                    if (inner) return inner.value || '';
                    return el.value || el.getAttribute('value') || '';
                }
                return '<未找到搜索框>';
            }""")
        print(f"  [DIAG] 搜索框 value: {value!r}")
    except Exception:
        print("  [DIAG] 搜索框 value: <读取失败>")

    try:
        panel = page.evaluate(
            r"""() => ({
                panel_open: !!document.querySelector('.case-message-view'),
                textarea: !!document.querySelector('textarea'),
            })""")
        print(f"  [DIAG] 对话面板打开: {panel['panel_open']}, "
              f"输入框存在: {panel['textarea']}")
    except Exception:
        print("  [DIAG] 对话面板状态: <读取失败>")
    print("  [DIAG] ------------------------------------")


def _click_messaging_search_button(page: Page) -> bool:
    """
    点击消息页搜索放大镜按钮（kat-input-group 内的 kat-link）。

    kat-input 是自定义组件，回车不保证触发搜索，点击放大镜按钮是页面
    规范操作（用户要求「再点一次搜索」即再点此按钮）。多策略：
    Playwright locator click → JS 点击 kat-link → JS 点击内部 kat-icon。
    全部容错，返回是否执行了任一点击。
    """
    for sel in get_selectors("message_search_button"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                return True
        except Exception:
            pass
    try:
        clicked = page.evaluate(r"""() => {
            const links = [...document.querySelectorAll('kat-input-group kat-link')];
            for (const l of links) {
                if (l.querySelector('kat-icon[name="search"]')) {
                    l.click();
                    return true;
                }
            }
            const icons = [...document.querySelectorAll(
                'div.case-top-bar kat-icon[name="search"]')];
            if (icons.length) {
                icons[0].click();
                return true;
            }
            return false;
        }""")
        return bool(clicked)
    except Exception:
        return False


def _trigger_messaging_search(page: Page, search_input, order_id: str) -> None:
    """
    重填订单号并触发消息页搜索：回车 + 点击放大镜按钮双保险。

    填充自愈：fill() 只支持 <input>/<textarea>/[contenteditable]，当搜索框
    命中 kat-input 自定义组件元素（shadow 不可穿透的兜底路径）时 fill() 抛
    "Element is not an <input>..."。此时点击已把焦点放进组件内部 input，
    改用 Ctrl+A 全选清空 + 键盘逐字输入（与 shadow 形态无关）。
    """
    search_input.click()
    try:
        search_input.fill("")
        search_input.fill(order_id)
    except RuntimeError:
        raise  # 用户终止：不吞掉停止信号，禁止键盘路径继续输入
    except Exception:
        # 自定义组件元素不支持 fill()：焦点已在内部 input 上，键盘路径兜底
        page.keyboard.press("Control+A")
        page.keyboard.type(order_id, delay=30)
    random_delay(0.3, 0.6)
    page.keyboard.press("Enter")
    random_delay(0.3, 0.6)
    _click_messaging_search_button(page)


def _wait_conversation_open(page: Page, timeout: float = 15.0) -> bool:
    """
    点击搜索结果项后等待对话内容真正加载完成。

    消息页为 SPA：结果列表渲染后，对话面板仍异步加载，期间可能再次出现
    ayb-loading-overlay 遮罩。联系买家中转提示优先于隐藏的 textarea；
    没有中转提示时，以消息输入框挂载作为对话内容加载完成的信号。

    每 0.5s 检查遮罩和输入框；遮罩消失且输入框挂载后返回 True。
    轮询内响应全局停止/暂停；超时返回 False。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        # 面板加载期间可能再次出现全屏遮罩，遮罩未消失视为未加载完成
        overlay = True
        try:
            overlay = bool(page.evaluate(
                r"""() => {
                    const el = document.querySelector('.ayb-loading-overlay');
                    return !!(el && (el.offsetWidth || el.offsetHeight));
                }"""))
        except Exception:
            overlay = True  # 读取失败按仍在加载处理
        if not overlay:
            try:
                if page.locator(get_selectors("contact_buyer_prompt")[0]).first.is_visible():
                    return False
            except Exception:
                pass
            try:
                page.locator(", ".join(get_selectors("message_input"))).first.wait_for(
                    state="attached", timeout=500)
                return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _return_from_contact_buyer(page: Page, order_id: str) -> bool:
    """打开“联系买家”中转页并返回原订单会话，不在联系页发送。"""
    try:
        if not page.locator(get_selectors("contact_buyer_prompt")[0]).first.is_visible():
            return False
        button = page.locator(get_selectors("contact_buyer_navigation")[0]).first
        if button.count() != 1:
            print("  [WARN] 联系买家提示可见，但没有找到唯一的跳转按钮")
            return False
        conversation_url = page.url
        pages_before = list(page.context.pages)
        print(f"  [INFO] 检测到联系买家提示，点击按钮 -> 订单 {order_id}")
        try:
            button.click(timeout=5000)
        except RuntimeError:
            raise
        except Exception:
            button.dispatch_event("click")
        navigation = _wait_for_buyer_navigation(page, pages_before, conversation_url)
        if navigation is None:
            print("  [WARN] 点击“联系买家”后未检测到联系页跳转")
            return False
        contact_page, _ = navigation
        contact_url = _safe_page_url(contact_page)
        order_match = re.search(r'[?&]orderID=([\d-]+)', contact_url, re.IGNORECASE)
        wrong_order = order_match and order_match.group(1) != order_id
        _wait_page_ready(contact_page, "联系买家中转页", timeout=10000)
        try:
            contact_page.locator("kat-radiobutton, input[type='radio']").first.wait_for(
                state="attached", timeout=10000)
        except RuntimeError:
            raise
        except Exception:
            print("  [WARN] 联系买家页面模板选项未就绪，仍尝试返回消息会话")
        if contact_page is page:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=20000)
            except RuntimeError:
                raise
            except Exception as e:
                print(f"  [WARN] 浏览器回退失败，直达原消息会话: {e}")
            source_case = re.search(r'[?&]cc=([^&#]+)', conversation_url)
            returned_case = re.search(r'[?&]cc=([^&#]+)', page.url)
            if not source_case or not returned_case or source_case.group(1) != returned_case.group(1):
                print("  [INFO] 回退未返回原订单会话，直达原会话 URL")
                page.goto(conversation_url, wait_until="domcontentloaded", timeout=20000)
        else:
            if not _is_contact_navigation_url(contact_url):
                print("  [WARN] 新页不是联系买家页面，保持原会话")
                return False
            contact_page.close()
            page.bring_to_front()
            page.reload(wait_until="domcontentloaded", timeout=20000)
        if wrong_order:
            print(f"  [WARN] 联系页订单号与目标订单 {order_id} 不一致，放弃本次回访")
            return False
        print("  [INFO] 已从联系买家页面返回消息会话，重新检查输入框")
        return True
    except RuntimeError:
        raise
    except Exception as e:
        print(f"  [WARN] 联系买家页面往返失败: {e}")
        return False


def verify_and_follow_up(
    page: Page,
    order: OrderInfo,
    follow_up_message: str,
    dry_run: bool = True,
    follow_up_messages: dict = None,
    stop_event: threading.Event = None,
) -> Optional[bool] | Literal["disabled", "input_unavailable"]:
    """
    在消息页面按订单编号搜索，验证邮件是否发送成功，并发送回访消息。

    如果 follow_up_messages 提供了站点→消息映射，则根据订单的 sales_channel
    自动选择对应站点的回访消息；否则使用 follow_up_message 作为默认消息。

    返回 True 表示对话找到且回访已确认（如需回访）；False 表示未找到对话；
    input_unavailable 表示找到订单对话但输入框未就绪；None 表示回访发送未确认。
    """
    print(f"[STEP] 验证消息 -> 订单: {order.order_id}")

    # 根据站点选择回访消息
    if follow_up_messages is None:
        follow_up_messages = {}
    active_message = get_follow_up_message_for_order(
        follow_up_messages,
        order.sales_channel,
        fallback_message=follow_up_message,
    )
    if order.sales_channel:
        print(f"  [INFO] 销售渠道: {order.sales_channel}")

    # 导航到消息页面
    try:
        page.goto(_get_messages_url(), timeout=20000, wait_until="domcontentloaded")
        random_delay(0.5, 1.0)
        _wait_page_ready(page, "消息页面")
    except Exception as e:
        print(f"[FAIL] 导航到消息页面失败: {e}")
        return False

    # 消息页专用搜索框（注意：不是顶部全局搜索 #sc-search-field）
    # 2026-08-18 回归修复：亚马逊消息页改版后搜索框由普通 input 变为
    # kat-input 自定义组件，标识属性从 id 改名 unique-id（katal-id-0），
    # placeholder 挂在组件元素上、真实 input 在其 shadow DOM 内且不反射
    # placeholder。旧兜底 input[placeholder*='搜索'] 会命中每个页面都存在的
    # 顶部全局搜索框 #sc-search-field（导航先渲染、消息搜索框后渲染，
    # 「第一个匹配即返回」让永远存在的错误框抢走订单号）。
    # 因此：组件元素精确定位 + >> 穿透 shadow 到真实 input 成对编写，
    # 保留旧版普通 input 兼容项，不保留任何能匹配导航框的通用兜底；
    # 全部 miss 由下方 safe_find 告警并放弃，绝不落入错误输入框。
    # 说明：>> 穿透 shadow DOM 返回真实 input；最后一项不带 >> 返回组件
    # 元素本身，用于 shadow 不可穿透（closed shadow）时的键盘填充路径。
    search_selectors = get_selectors("message_search_input")

    search_input = safe_find(page, search_selectors, timeout=8000)
    if search_input:
        try:
            # 重填订单号 + 回车 + 点击放大镜按钮双保险触发搜索
            _trigger_messaging_search(page, search_input, order.order_id)
            random_delay(0.5, 1.0)
            _wait_page_ready(page, "消息搜索")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[WARN] 搜索订单号失败: {e}")
    else:
        print("[WARN] 找不到消息搜索框")
        return False

    # 验证搜索结果：是否出现了该订单的对话
    # 如果发送过说明书 PDF，消息中必定有该订单的对话记录
    # 搜索结果精确匹配且已点击，即确认该订单有对话；输入框加载由发送步骤校验。
    # 消息页 SPA 可能晚于对话面板才渲染输入框，不能因此误报无对话。
    # 用户要求：结果不存在时隔 5s 再点一次搜索重判，最多重试 3 次
    # （含首次共最多 4 轮），全部失败才判定验证失败，避免误报。
    found = False
    input_unavailable = False
    max_retries = 3  # 首次搜索之外的重试次数上限
    item_selectors = format_selectors("message_item", order_id=order.order_id)
    for retry in range(max_retries + 1):  # retry=0 为首次搜索，1..3 为重试
        _check_stop_global()
        _wait_if_paused_global()
        # 等本轮搜索触发的 SPA 渲染完成（遮罩消失），再检查结果
        _wait_messaging_loading_done(page)

        # 1) 搜索结果项出现：订单号属性精确匹配（渲染后 DOM 契约），
        #    :has-text 兜底（限制在列表项内，避免收件箱全局文本误命中）
        item_loc = None
        for sel in item_selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="attached", timeout=4000)
                item_loc = loc
                break
            except Exception:
                continue

        if item_loc is not None:
            # 2) 点击结果项打开对话（先等遮罩消失；JS click 兜底防遮罩拦截）
            _wait_messaging_loading_done(page)
            clicked = False
            try:
                item_loc.click(timeout=5000)
                clicked = True
            except Exception as e:
                print(f"  [WARN] 点击对话结果项失败: {e}，改用 JS 兜底")
            if not clicked:
                try:
                    clicked = bool(page.evaluate(r"""(orderId) => {
                        const items = [...document.querySelectorAll('a.case-list-item')];
                        const target = items.find(i =>
                            (i.getAttribute('data-ph-capture-attribute-order-id') || '')
                                === orderId
                            || i.textContent.includes(orderId));
                        if (target) { target.click(); return true; }
                        return false;
                    }""", order.order_id))
                except Exception:
                    clicked = False
            # 3) 等待输入框加载；未就绪时不能向可能仍显示的旧会话填写回访。
            if clicked:
                for refresh in range(3):
                    if refresh:
                        print(f"  [输入框重试 {refresh}/2] 5s 后刷新消息页并重新检查...")
                        random_delay(5, 5)
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=20000)
                            _wait_page_ready(page, "消息输入框刷新")
                            if "cc=" not in page.url:
                                page.locator(item_selectors[0]).first.click(timeout=5000)
                        except RuntimeError:
                            raise
                        except Exception as e:
                            print(f"  [WARN] 第 {refresh}/2 次刷新消息页失败: {e}")
                            continue
                    if _wait_conversation_open(page):
                        found = True
                        break
                    if _return_from_contact_buyer(page, order.order_id):
                        if _wait_conversation_open(page):
                            found = True
                            break
                        try:
                            if page.locator(get_selectors("contact_buyer_prompt")[0]).first.is_visible():
                                print("  [INFO] 返回后仍显示联系买家提示，重新打开目标订单对话")
                                page.locator(item_selectors[0]).first.click(timeout=5000)
                                if _wait_conversation_open(page):
                                    found = True
                                    break
                        except RuntimeError:
                            raise
                        except Exception as e:
                            print(f"  [WARN] 重新打开目标订单对话失败: {e}")
                input_unavailable = not found
                break

        if retry >= max_retries:
            break
        # 未找到对话记录：隔 5s 再点一次搜索重判（random_delay 内部响应停止/暂停）
        print(f"  [重试 {retry + 1}/{max_retries}] 未找到对话记录，5s 后重新搜索...")
        random_delay(5, 5)
        try:
            # search_input 是 Playwright ElementHandle：SPA 重渲染可能替换搜索框
            # DOM 节点，使旧 handle 失效（Element is not attached to the DOM），
            # 失效时由 except 捕获后重新查找搜索框，找不到则放弃重试
            _trigger_messaging_search(page, search_input, order.order_id)
            random_delay(0.5, 1.0)
            _wait_page_ready(page, "消息搜索重试")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[WARN] 重试搜索失败: {e}")
            # SPA 重渲染可能已替换搜索框 DOM 节点，旧 handle 失效：
            # 重新查找并赋回 search_input；仍找不到说明搜索框已不可用，
            # 继续重试无意义，放弃
            search_input = safe_find(page, search_selectors, timeout=8000)
            if search_input is None:
                print("[WARN] 重新查找消息搜索框失败，放弃重试")
                break
            try:
                _trigger_messaging_search(page, search_input, order.order_id)
            except RuntimeError:
                raise
            except Exception:
                continue

    if input_unavailable:
        print(f"[WARN] 订单 {order.order_id} 对话已找到，但回访输入框未就绪")
        return "input_unavailable"
    if found:
        print(f"[OK] 消息验证成功：订单 {order.order_id} 存在对话记录")

        # 检查是否为未映射站点（多站点模式但当前站点无对应回访消息模板）
        if follow_up_messages and order.sales_channel and not active_message:
            # 未映射站点 → 弹窗提示操作者手动处理
            print(f"  [WARN] 站点 '{order.sales_channel}' 无映射回访消息，需手动处理")
            _prompt_unmapped_site_manual(order)
        elif active_message:
            if not _send_follow_up_message(page, active_message, dry_run=dry_run,
                                           stop_event=stop_event):
                return None
        # else: 无回访消息且非多站点模式 → 静默跳过

        return True
    else:
        print(f"[FAIL] 消息验证失败：订单 {order.order_id} 未找到对话记录")
        _diagnose_messaging_search(page, order.order_id)
        return False


def _sent_follow_up_count(page: Page, message: str) -> int:
    """Count sent bubbles whose body matches this follow-up text."""
    expected = " ".join(message.split())
    return page.locator(get_selectors("sent_message_body")[0]).evaluate_all(
        "(nodes, expected) => nodes.filter(node => "
        "(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim() === expected).length",
        expected,
    )


def _wait_for_follow_up_confirmation(page: Page, message: str,
                                     previous_count: int, timeout: float = 30.0) -> bool:
    """Wait for a new sent bubble, then reload this conversation to verify persistence."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        try:
            if _sent_follow_up_count(page, message) > previous_count:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    try:
        page.reload(wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        print(f"  [WARN] 回访发送后刷新复核失败: {type(exc).__name__}")
        return False

    deadline = time.monotonic() + min(15.0, timeout)
    while time.monotonic() < deadline:
        _check_stop_global()
        _wait_if_paused_global()
        try:
            if _sent_follow_up_count(page, message) > previous_count:
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _send_follow_up_message(page: Page, message: str, dry_run: bool = False,
                           stop_event: threading.Event = None) -> bool:
    """
    在已打开的对话中发送回访消息。
    干跑模式：填入消息后通过 stop_event 触发优雅终止（不点击发送按钮）。
    """
    print("[INFO] 发送回访消息...")
    random_delay(0.5, 1)

    # 消息页为 SPA，可能仍在异步渲染（ayb-loading-overlay 全屏遮罩）。
    _wait_messaging_loading_done(page, timeout=10.0)

    # 找到消息输入框（仅匹配消息编辑区元素，排除页面顶部全局搜索框 #sc-search-field）
    # SPA 重渲染会使先前解析的 ElementHandle 失效（Element is not attached to
    # the DOM），因此不用 safe_find 一次到位，而是重试循环：每轮用 locator
    # 重新解析并点击/填写，捕获异常后等待 1.5s 重试，最多 3 次。
    input_selectors = get_selectors("message_input")
    # 注意：page.locator() 只接受单个选择器字符串，传列表会被字符串化后
    # 当作非法 CSS 解析（2026-08-18 实测 SyntaxError）。多选一用 CSS 并集。
    input_selector = ", ".join(input_selectors)
    filled = False
    last_error = ""
    for attempt in range(3):
        # 每轮点击前再等一次遮罩消失，避免 click 被遮罩拦截
        _wait_messaging_loading_done(page, timeout=10.0)
        try:
            msg_input = page.locator(input_selector).first
            # Amazon 先挂载 textarea，再通过 expanded/动画切换可见状态。
            msg_input.wait_for(state="attached", timeout=3000)
            if not msg_input.is_visible(timeout=500):
                page.locator("#case-message-composer .editor").first.click(timeout=3000)
            msg_input.wait_for(state="visible", timeout=5000)
            msg_input.click(timeout=5000)
            msg_input.fill(message)
            filled = True
            break
        except Exception as e:
            last_error = str(e)
            print(f"  [WARN] 输入框操作失败（第 {attempt + 1}/3 次）: {e}，1.5s 后重试...")
            _check_stop_global()
            _wait_if_paused_global()
            time.sleep(1.5)

    if filled:
        try:
            random_delay(2, 2)

            # 干跑模式：消息已填入输入框，通过 stop_event 触发优雅终止
            if dry_run:
                print("[DRY-RUN] 回访消息已填入输入框，程序停止（不点击发送）")
                print("[DRY-RUN] 请在浏览器中检查消息内容，确认无误后手动发送或关闭页面")
                print("[DRY-RUN] ========== 程序已停止 ==========")
                if stop_event is not None:
                    stop_event.set()
                raise RuntimeError("干跑模式：回访消息已填入，程序停止")

            # 点击发送（复用统一的 kat-button 点击函数）
            previous_count = _sent_follow_up_count(page, message)
            send_clicked = _click_send_button(page)
            if not send_clicked:
                print("[WARN] 回访消息发送按钮点击可能失败")
                return False
            print("  [校验] 等待回访消息进入已发送记录...")
            if not _wait_for_follow_up_confirmation(page, message, previous_count):
                print("[WARN] 回访发送未确认，将记录该订单并继续后续订单")
                return False
            print("[OK] 回访消息已发送并在刷新后确认")
            return True
        except RuntimeError as e:
            if "用户终止任务" in str(e):
                raise
            print(f"[FAIL] 发送回访消息失败: {e}")
        except Exception as e:
            print(f"[FAIL] 发送回访消息失败: {e}")
    else:
        if last_error:
            print(f"[WARN] 找不到消息输入框（最后错误: {last_error}）")
        else:
            print("[WARN] 找不到消息输入框")

    return False


# ============================================================================
# 失败记录导出
# ============================================================================

def export_failed_records(records: list[FailedRecord]) -> str:
    """
    导出失败记录为 CSV 文件，返回文件路径。
    """
    if not records:
        return ""

    filepath = OUTPUT_DIR / f"失败记录_{timestamp_str()}.csv"
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["ASIN", "订单编号", "买家名称", "失败原因"])
        for r in records:
            writer.writerow([r.asin, r.order_id, r.buyer_name, r.reason])

    print(f"[INFO] 失败记录已导出: {filepath}")
    return str(filepath)


def show_failure_summary(
    records: list[FailedRecord],
    csv_path: str = "",
    total_processed: int = 0,
    followup_not_sent_ids: list[str] = None,
):
    """
    弹窗展示失败汇总信息（线程安全）。
    """
    def _dialog(parent):
        dlg = tk.Toplevel(parent) if parent is not None else tk.Tk()
        dlg.title("执行结果汇总")
        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        height = 500 if followup_not_sent_ids else 400
        dlg.geometry(f"550x{height}+{(sw-550)//2}+{(sh-height)//2}")
        dlg.transient(parent)

        ttk.Label(dlg, text="批量发送执行完毕", font=("", 12, "bold")).pack(pady=(15, 5))

        success = total_processed - len(records)
        summary = f"总处理: {total_processed} | 成功: {success} | 失败: {len(records)}"
        ttk.Label(dlg, text=summary, foreground="blue").pack()

        if followup_not_sent_ids:
            ttk.Label(dlg, text=f"未执行回访：{len(followup_not_sent_ids)} 单，"
                                "请人工核对并手动发送", foreground="red").pack(pady=(8, 0))
            order_list = tk.Text(dlg, height=4, wrap="word")
            order_list.pack(fill="x", padx=20, pady=(4, 0))
            order_list.insert("1.0", "\n".join(followup_not_sent_ids))
            order_list.configure(state="disabled")

        if records:
            ttk.Label(dlg, text="\n失败详情:", font=("", 10, "bold")).pack(anchor="w", padx=20)
            tree_frame = ttk.Frame(dlg)
            tree_frame.pack(fill="both", expand=True, padx=20, pady=5)

            tree = ttk.Treeview(tree_frame, columns=("ASIN", "订单号", "买家", "原因"),
                                show="headings", height=8)
            tree.heading("ASIN", text="ASIN")
            tree.heading("订单号", text="订单编号")
            tree.heading("买家", text="买家名称")
            tree.heading("原因", text="失败原因")
            tree.column("ASIN", width=100)
            tree.column("订单号", width=140)
            tree.column("买家", width=100)
            tree.column("原因", width=150)
            for r in records:
                tree.insert("", "end", values=(r.asin, r.order_id, r.buyer_name, r.reason))
            tree.pack(fill="both", expand=True)

        if csv_path:
            ttk.Label(dlg, text=f"失败记录已保存至: {csv_path}").pack(pady=(5, 0))

        ttk.Button(dlg, text="关闭", command=dlg.destroy).pack(pady=(10, 15))
        dlg.focus_force()
        dlg.grab_set()
        dlg.wait_window()

    _run_on_tk_thread(_dialog)


# ============================================================================
# 主流程编排
# ============================================================================

# 模块级事件，供 random_delay 等函数在不改签名的情况下响应暂停/终止
_global_stop = None
_global_pause = None


def _set_global_events(stop: threading.Event, pause: threading.Event):
    global _global_stop, _global_pause
    _global_stop = stop
    _global_pause = pause


def _check_stop_global():
    """检查全局终止标志（供 random_delay 等使用）。"""
    if _global_stop and _global_stop.is_set():
        raise RuntimeError("用户终止任务")


def _wait_if_paused_global():
    """检查全局暂停标志（供 random_delay 等使用）。"""
    while _global_pause and _global_pause.is_set():
        if _global_stop and _global_stop.is_set():
            raise RuntimeError("用户终止任务")
        time.sleep(0.2)


def _check_stop(stop_event: threading.Event):
    """检查是否需要终止。如果需要终止，raise RuntimeError。"""
    if stop_event.is_set():
        raise RuntimeError("用户终止任务")


def _wait_if_paused(pause_event: threading.Event, stop_event: threading.Event):
    """如果暂停中，阻塞等待继续或终止。"""
    while pause_event.is_set():
        time.sleep(0.2)
        if stop_event.is_set():
            raise RuntimeError("用户终止任务")


def _update_status(target, **kwargs):
    """安全更新状态（判空 + 异常保护）。

    接受 StatusWindow（旧）或 StatusHolder（新），两者都有 .status 属性。
    """
    if target is None:
        return
    try:
        for k, v in kwargs.items():
            setattr(target.status, k, v)
    except Exception:
        pass


def run_task(
    config: TaskConfig,
    test_mode: bool = True,
    stop_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    status_holder=None,
):
    """
    执行完整的批量发送任务（线程安全版本）。
    通过 stop_event/pause_event 支持暂停和终止；
    通过 status_holder (StatusHolder 或 StatusWindow) 实时更新运行状态。
    """
    if stop_event is None:
        stop_event = threading.Event()
    if pause_event is None:
        pause_event = threading.Event()

    # 设置全局事件，让 random_delay 等底层函数也能响应暂停/终止
    _set_global_events(stop_event, pause_event)

    sh = status_holder
    _update_status(sh, state=RunState.IDLE)

    print("=" * 60)
    print("  亚马逊批量发送产品说明书邮件")
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"\n配置: 店铺={config.store_name}, ASIN={config.asins}")
    print(f"日期: {config.start_date} ~ {config.end_date}")
    print(f"PDF: {config.pdf_path}")
    print(f"模式: {'干跑(邮件正常发送，回访前停止)' if config.dry_run else '正式运行'}")
    print()

    browser = None
    playwright = None
    failed_records: list[FailedRecord] = []
    total_processed = 0
    skipped_buyer_count = 0
    skipped_disabled_count = 0
    skipped_unconfirmed_count = 0
    skipped_followup_not_sent_count = 0
    skipped_channel_count = 0
    followup_not_sent_order_ids: set[str] = set()
    attempted_orders = {asin: set() for asin in config.asins}
    successful_orders = {asin: set() for asin in config.asins}
    end_reason = ""
    show_summary = False
    summary_csv_path = ""

    try:
        def _fail_check():
            _check_stop(stop_event)
            _wait_if_paused(pause_event, stop_event)

        # ---- [阶段 1/5] 启动紫鸟 + 通过 API 打开店铺 ----
        _fail_check()
        _update_status(sh, state=RunState.LAUNCHING, message="启动紫鸟 (WebDriver模式)...")
        print()
        print("=" * 40)
        print("[阶段 1/5] 启动紫鸟 -> API 打开店铺")
        print("=" * 40)

        debugging_port = open_store_via_api(config, stop_event=stop_event)
        print(f"[OK] 店铺浏览器 CDP 端口: {debugging_port}")

        # ---- [阶段 2/5] CDP 连接店铺浏览器 ----
        _fail_check()
        _update_status(sh, state=RunState.CONNECTING, message="连接店铺浏览器 CDP...")
        print()
        print("=" * 40)
        print("[阶段 2/5] CDP 连接店铺浏览器")
        print("=" * 40)

        playwright, browser, context, page = connect_to_store_cdp(debugging_port)
        print(f"[INFO] CDP 连接成功，当前页面: {page.url}")

        # 根据所选站点设置当前任务线程的 URL
        _sc_url = get_seller_central_url(config.marketplace)
        _url_store.seller_central = _sc_url
        _url_store.orders_page = get_orders_page_url(config.marketplace)
        _url_store.messages_page = get_messages_page_url(config.marketplace)
        if config.marketplace:
            print(f"[INFO] 目标站点: {config.marketplace} → {_sc_url}")

        # 紫鸟启动后通常是空白页，先导航到亚马逊
        if "sellercentral" not in page.url:
            print("[STEP] 导航到亚马逊卖家后台...")
            try:
                page.goto(_sc_url, timeout=30000, wait_until="domcontentloaded")
                print(f"[OK] 已导航到: {page.url}")
            except Exception as e:
                print(f"[WARN] 导航超时: {e}，继续检查登录状态...")

        # ---- [阶段 3/5] 登录亚马逊 ----
        _fail_check()
        _update_status(sh, state=RunState.LOGGING_IN, message="检查亚马逊登录...")
        print("\n" + "=" * 40)
        print("[阶段 3/5] 检查亚马逊登录状态")
        print("=" * 40)

        if not _poll_login(page):
            raise RuntimeError("登录失败，无法进入卖家后台")

        # ---- [阶段 3.5/5] 站点检测与切换 (V2 account-switcher) ----
        if config.marketplace:
            _fail_check()
            _update_status(sh, state=RunState.LOGGING_IN,
                           message=f"检测/切换站点 → {config.marketplace}...")
            print("\n" + "=" * 40)
            print(f"[阶段 3.5/5] 站点切换 (V2 account-switcher) → 目标: {config.marketplace}")
            print("=" * 40)

            # V2 switch_to_marketplace：导航到 account-switcher 全页 → 扫描条目 →
            #   匹配目标站点 → 点击确认 → OTP/MFA 重试循环（最多2轮）
            # 比 V1 域名直跳更稳定，不易触发额外登录验证
            switched = switch_to_marketplace(
                page, config.marketplace,
                login_fn=lambda p: quick_login(p),
            )
            if not switched:
                print(f"[WARN] V2 站点切换失败，尝试在当前位置继续...")
                _prompt_manual_intervention(
                    f"站点切换失败\n\n"
                    f"目标站点: {config.marketplace}\n\n"
                    f"请在浏览器中手动切换到 {config.marketplace} 站点后点击下方按钮继续。"
                )
            else:
                print(f"[OK] V2 站点已切换到: {config.marketplace}")
                _fail_check()
            print("=" * 40)

        # 加载历史已处理订单（防重复发送）
        processed_order_ids: set[str] = load_processed_orders(config.store_name)
        disabled_order_ids: set[str] = load_disabled_orders(config.store_name)
        unconfirmed_order_ids: set[str] = load_unconfirmed_orders(config.store_name)
        followup_not_sent_order_ids = load_followup_not_sent_orders(config.store_name)
        skipped_history_count = 0

        # ---- [阶段 4/5] 导航到订单页 → 固化日期参数（并入每个 ASIN 的搜索 URL，不单独跳日期页）----
        _fail_check()
        _update_status(sh, state=RunState.SEARCHING,
                       asin_total=len(config.asins), asin_index=0,
                       message="导航到订单管理页...")
        print("\n" + "=" * 40)
        print("[阶段 4/5] 处理订单")
        print("=" * 40)

        if not navigate_to_orders(page):
            raise RuntimeError("导航到订单页面失败")

        # 防御检查：导航可能触发登录重定向
        _ensure_logged_in(page)

        # 固化日期范围参数：不再单独跳转日期设置页，日期范围直接并入
        # 后续每个 ASIN 的搜索 URL（search_asin 一次 goto 同时带全 q/qt/date-range）
        _date_range_param = _build_date_range_param(config.start_date, config.end_date)

        # ---- [阶段 4.5/5] 逐个 ASIN 处理 ----
        for idx, asin in enumerate(config.asins, 1):
            _fail_check()
            _update_status(sh, state=RunState.SEARCHING,
                           current_asin=asin, asin_index=idx,
                           message=f"搜索 ASIN: {asin}")
            print(f"\n--- ASIN [{idx}/{len(config.asins)}]: {asin} ---")

            # 搜索 ASIN（通过 URL 参数，不会重复刷日期页）
            if not search_asin(page, asin, date_range=_date_range_param):
                failed_records.append(FailedRecord(asin=asin, reason="ASIN 搜索失败"))
                continue

            # 防御检查：搜索页面跳转可能触发登录验证或站点切换
            _ensure_logged_in(page)

            def _orders_for_current_asin():
                nonlocal skipped_history_count, skipped_disabled_count
                nonlocal skipped_unconfirmed_count, skipped_followup_not_sent_count
                for orders in _iter_paid_order_pages(page, asin):
                    if orders and all(o.order_id in processed_order_ids or
                                      o.order_id in disabled_order_ids or
                                      o.order_id in unconfirmed_order_ids or
                                      o.order_id in followup_not_sent_order_ids for o in orders):
                        skipped_followup_not_sent_count += sum(
                            o.order_id in followup_not_sent_order_ids for o in orders)
                        skipped_history_count += sum(
                            o.order_id not in followup_not_sent_order_ids and
                            o.order_id in processed_order_ids for o in orders)
                        skipped_disabled_count += sum(
                            o.order_id not in followup_not_sent_order_ids and
                            o.order_id not in processed_order_ids and
                            o.order_id in disabled_order_ids for o in orders)
                        skipped_unconfirmed_count += sum(
                            o.order_id not in followup_not_sent_order_ids and
                            o.order_id not in processed_order_ids and
                            o.order_id not in disabled_order_ids and
                            o.order_id in unconfirmed_order_ids for o in orders)
                        print(f"[INFO] 分页 {orders[0].page_num} 的订单均已有记录，直接进入下一页")
                        continue
                    for oi, order in enumerate(orders, 1):
                        yield oi, order, len(orders)

            # 当前页处理完后，迭代器才导航到下一页。
            for oi, order, page_order_total in _orders_for_current_asin():
                _fail_check()

                if not _matches_sales_channel(order, config.sales_channels):
                    print(f"\n  >> 跳过非实发站点订单: {order.order_id} "
                          f"(销售渠道: {order.sales_channel or '未识别'})")
                    skipped_channel_count += 1
                    continue

                if order.order_id in followup_not_sent_order_ids:
                    print(f"\n  >> 跳过待人工发送回访订单: {order.order_id}")
                    skipped_followup_not_sent_count += 1
                    continue
                if order.order_id in processed_order_ids:
                    print(f"\n  >> 跳过已处理订单: {order.order_id}")
                    skipped_history_count += 1
                    continue
                if order.order_id in disabled_order_ids:
                    print(f"\n  >> 跳过联系主题已禁用订单: {order.order_id}")
                    skipped_disabled_count += 1
                    continue
                if order.order_id in unconfirmed_order_ids:
                    print(f"\n  >> 跳过发送结果未确认订单: {order.order_id}")
                    skipped_unconfirmed_count += 1
                    continue

                # 特定 Amazon Business 账户不是终端买家：名称必须精确匹配，
                # 且在任何订单页回跳/买家点击前跳过，保持其他买家原有行为。
                if _should_skip_buyer_order(order):
                    print(
                        f"\n  >> 跳过指定买家订单: {order.order_id} "
                        f"(买家: {order.buyer_name})")
                    skipped_buyer_count += 1
                    continue

                total_processed += 1
                attempted_orders.setdefault(asin, set()).add(order.order_id)
                _update_status(
                    sh, state=RunState.SENDING,
                    current_asin=asin, current_order_id=order.order_id,
                    asin_index=idx, order_index=oi, order_total=page_order_total,
                    success_count=total_processed - len(failed_records),
                    fail_count=len(failed_records),
                )
                print(f"\n  >> 处理订单 [{oi}/{page_order_total}]: {order.order_id}")

                # 回访验证可能把主 tab 带到消息页；订单行仍在当前页时直接处理。
                if not _order_row_on_current_page(page, order):
                    _goto_order_page(page, asin, _date_range_param, order.page_num)
                    random_delay(0.5, 1)

                # 防御检查：点击买家名称前确保页面正常（防止订单页跳转触发登录）
                if not _ensure_logged_in(page):
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="登录验证失败，页面不在卖家后台"))
                    continue

                # 发送邮件（内部会点击买家名称→联系页面→操作→返回）
                _update_status(sh, state=RunState.SENDING,
                               current_order_id=order.order_id,
                               message=f"发送邮件 -> {order.order_id}")
                email_sent = send_email_with_pdf(
                    page, order, config.pdf_path,
                    test_mode=test_mode, dry_run=config.dry_run
                )

                if email_sent == "disabled":
                    if not save_disabled_order(order.order_id, config.store_name):
                        failed_records.append(FailedRecord(
                            asin=asin, order_id=order.order_id,
                            buyer_name=order.buyer_name,
                            reason="联系主题已被 Amazon 禁用，记录保存失败"))
                        raise RuntimeError("联系主题禁用订单记录保存失败，停止后续订单")
                    disabled_order_ids.add(order.order_id)
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="联系主题已被 Amazon 禁用"))
                    _update_status(sh, fail_count=len(failed_records))
                    continue

                if email_sent is None:
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="买家不可联系：无效链接或联系主题不可用"))
                    _update_status(sh, fail_count=len(failed_records))
                    continue

                if not email_sent:
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="PDF 邮件发送失败或被跳过"))
                    _update_status(sh, fail_count=len(failed_records))
                    continue

                if email_sent == "unconfirmed":
                    if not save_unconfirmed_order(order.order_id, config.store_name):
                        failed_records.append(FailedRecord(
                            asin=asin, order_id=order.order_id,
                            buyer_name=order.buyer_name,
                            reason="PDF 邮件发送未确认，异常记录保存失败，需人工核对"))
                        raise RuntimeError(f"订单 {order.order_id} 未确认记录保存失败，停止后续发送")
                    unconfirmed_order_ids.add(order.order_id)
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="PDF 邮件发送未确认，需人工核对"))
                    _update_status(sh, fail_count=len(failed_records))
                    continue

                # 确认成功后持久化，防止后续重复发送。
                if not save_processed_order(order.order_id, config.store_name):
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="邮件发送后历史记录保存失败，需人工核对"))
                    raise RuntimeError(f"订单 {order.order_id} 历史记录保存失败，停止后续发送")
                processed_order_ids.add(order.order_id)

                # 验证消息
                _update_status(sh, state=RunState.VERIFYING,
                               message=f"验证消息 -> {order.order_id}")
                verified = verify_and_follow_up(
                    page, order, config.follow_up_message,
                    dry_run=config.dry_run,
                    follow_up_messages=config.follow_up_messages,
                    stop_event=stop_event,
                )
                if (verified is False or verified == "input_unavailable") and (
                        config.follow_up_message.strip() or config.follow_up_messages):
                    try:
                        saved = save_followup_not_sent_order(order.order_id, config.store_name)
                    except Exception as e:
                        print(f"[WARN] 待回访订单记录写入异常: {e}")
                        saved = False
                    if not saved:
                        failed_records.append(FailedRecord(
                            asin=asin, order_id=order.order_id,
                            buyer_name=order.buyer_name,
                            reason="回访未执行且异常记录保存失败，需人工核对"))
                        raise RuntimeError(f"订单 {order.order_id} 待回访记录保存失败，停止后续发送")
                    followup_not_sent_order_ids.add(order.order_id)
                if verified == "input_unavailable":
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="订单对话已找到，但回访输入框未就绪，需人工核对"))
                    _update_status(sh, fail_count=len(failed_records))
                    continue
                if verified is None:
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="回访发送未确认，需人工核对"))
                    _update_status(sh, fail_count=len(failed_records))
                    print(f"[WARN] 订单 {order.order_id} 回访未确认，继续处理下一订单")
                    continue
                if not verified:
                    failed_records.append(FailedRecord(
                        asin=asin, order_id=order.order_id,
                        buyer_name=order.buyer_name,
                        reason="消息验证失败：未找到对话记录"))
                    _update_status(sh, fail_count=len(failed_records))
                else:
                    successful_orders.setdefault(asin, set()).add(order.order_id)
                    _update_status(sh, success_count=total_processed - len(failed_records))

                # 下一条订单的处理入口已改为回跳其所在分页
                # （见循环顶部 _goto_order_page），此处不再回第 1 页

        # ---- [阶段 5/5] 结果汇总 ----
        _fail_check()
        _update_status(sh, state=RunState.FINISHED,
                       success_count=total_processed - len(failed_records),
                       fail_count=len(failed_records),
                       message="任务完成")
        print("\n" + "=" * 40)
        print("[阶段 5/5] 结果汇总")
        print("=" * 40)

        success_count = total_processed - len(failed_records)
        print(f"\n总处理订单: {total_processed}")
        print(f"成功: {success_count}")
        print(f"失败: {len(failed_records)}")
        if skipped_history_count > 0:
            print(f"跳过(历史已处理): {skipped_history_count}")
        if skipped_buyer_count > 0:
            print(f"跳过(指定买家): {skipped_buyer_count}")
        if skipped_disabled_count > 0:
            print(f"跳过(联系主题已禁用): {skipped_disabled_count}")
        if skipped_unconfirmed_count > 0:
            print(f"跳过(发送结果未确认): {skipped_unconfirmed_count}")
        if skipped_followup_not_sent_count > 0:
            print(f"跳过(待人工发送回访): {skipped_followup_not_sent_count}")
        if skipped_channel_count > 0:
            print(f"跳过(非实发站点): {skipped_channel_count}")

        csv_path = export_failed_records(failed_records)
        summary_csv_path = csv_path
        show_summary = True

    except RuntimeError as e:
        end_reason = str(e)[:200]
        if "终止" in str(e):
            _update_status(sh, state=RunState.STOPPED, message="任务已终止")
            print(f"\n[STOPPED] {e}")
        else:
            _update_status(sh, state=RunState.ERROR, message=str(e))
            print(f"\n[ERROR] {e}")
            if failed_records:
                csv_path = export_failed_records(failed_records)
                summary_csv_path = csv_path
                show_summary = True
    except Exception as e:
        end_reason = str(e)[:200]
        import traceback
        traceback.print_exc()
        _update_status(sh, state=RunState.ERROR, message=str(e)[:80])
        print(f"\n[ERROR] 执行异常: {e}")
        if failed_records:
            csv_path = export_failed_records(failed_records)
            summary_csv_path = csv_path
            show_summary = True

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass
        try:
            report = build_report(
                config, attempted_orders, successful_orders, failed_records, end_reason)
            report_path = save_run_report(report, OUTPUT_DIR)
            print(f"[INFO] 本次飞书同步报告已保存: {report_path}")
            _update_status(sh, message="正在同步飞书多维表格...")
            created, updated = sync_saved_report(
                report_path, app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret)
            print(f"[OK] 飞书同步完成：新增 {created} 行，更新 {updated} 行")
        except Exception as exc:
            print(f"[WARN] 飞书同步未完成，已保留本地报告供核对: {exc}")
            _update_status(sh, state=RunState.ERROR, message=f"飞书同步失败: {str(exc)[:60]}")

    if followup_not_sent_order_ids:
        print("[WARN] 待人工核对并手动发送回访的订单: " +
              ", ".join(sorted(followup_not_sent_order_ids)))
    if show_summary:
        show_failure_summary(
            failed_records, summary_csv_path, total_processed,
            followup_not_sent_ids=sorted(followup_not_sent_order_ids))

    print(f"\n{'=' * 60}")
    print(f"  执行完成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")


# ============================================================================
# 程序入口
# ============================================================================

def main():
    """主入口：显示统一窗口，内部管理输入→执行→返回→再执行的完整生命周期。"""
    window = InputDialog()
    window.show()
    print("程序退出")


if __name__ == "__main__":
    main()
