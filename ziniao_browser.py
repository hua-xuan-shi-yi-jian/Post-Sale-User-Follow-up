#!/usr/bin/env python3
"""
紫鸟浏览器 (Zigniao/SuperBrowser) 自动化控制模块
=================================================
通过 WebDriver HTTP API 启动紫鸟、管理店铺、获取 CDP 调试端口、
连接 Playwright 进行浏览器自动化。

使用方式
--------
>>> from ziniao_browser import ZiniaoBrowser
>>> zb = ZiniaoBrowser("公司名", "用户名", "密码")
>>> port = zb.open_store("EXAMPLE-STORE-UK")  # 启动紫鸟 → 打开店铺 → 返回 CDP 端口
>>> pw, browser, ctx, page = zb.connect_cdp(port)  # Playwright 连接
>>> # ... 自动化操作 ...
>>> zb.stop()                                  # 关闭店铺 + 清理

依赖: pip install playwright
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Callable

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# ============================================================================
# 常量
# ============================================================================

DEFAULT_SOCKET_PORT = 18888          # 紫鸟 HTTP API 默认端口
ZIGNIAO_API_TIMEOUT = 130            # API 请求超时（秒），文档要求 ≥120s

# 紫鸟默认搜索路径（按优先级排列）
SUPERBROWSER_DEFAULT_SEARCH_PATHS = [
    r"D:\紫鸟\SuperBrowser\starter.exe",
    r"D:\软件\SuperBrowser\starter.exe",
    r"C:\Program Files\SuperBrowser\starter.exe",
    r"C:\Program Files (x86)\SuperBrowser\starter.exe",
]

# 通用品牌名称（不是店铺名，用于 validate_store_name 过滤）
GENERIC_STORE_NAMES = {
    "amazon", "亚马逊", "amazon seller central", "seller central",
    "amazon.co.uk", "amazon.com", "sellercentral", "后台",
}


# ============================================================================
# 工具函数
# ============================================================================

def find_superbrowser_path(
    saved_path: str = "",
    search_paths: list[str] | None = None,
    on_found: Callable[[str], None] | None = None,
) -> Optional[str]:
    """
    查找紫鸟浏览器可执行文件路径。

    优先级：
    1. saved_path（如配置文件中的路径，如果文件存在）
    2. search_paths 中第一个存在的路径
    3. 返回 None（需用户手动指定）

    参数：
        saved_path: 之前保存的路径（优先级最高）
        search_paths: 额外的搜索路径列表
        on_found: 自动发现路径时的回调（如保存到配置文件）
    """
    # 1. 已保存路径
    if saved_path and os.path.isfile(saved_path):
        return saved_path

    # 2. 合并搜索路径
    all_paths = list(search_paths or []) + SUPERBROWSER_DEFAULT_SEARCH_PATHS
    for p in all_paths:
        if p and os.path.isfile(p):
            if on_found:
                on_found(p)
            return p

    return None


def launch_superbrowser(
    path: str | None = None,
    socket_port: int = DEFAULT_SOCKET_PORT,
    wait: float = 3.0,
) -> bool:
    """
    以 WebDriver 模式启动紫鸟浏览器。

    参数：
        path: 紫鸟 starter.exe 路径（None 则自动搜索）
        socket_port: HTTP API 端口
        wait: 启动后等待秒数

    返回：是否成功启动
    """
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


# ============================================================================
# 紫鸟 HTTP API 客户端
# ============================================================================

class ZigniaoAPI:
    """
    紫鸟 WebDriver HTTP API 客户端。

    启动紫鸟后，通过 HTTP POST 与本地服务通信，
    完成内核更新、设备授权、店铺搜索与启停。

    用法::

        api = ZigniaoAPI(18888, "公司", "用户名", "密码")
        api.update_core()
        api.apply_auth()
        store = api.find_store("EXAMPLE-STORE-UK")
        result = api.start_browser(store["browserOauth"])
        port = result["debuggingPort"]
    """

    def __init__(self, port: int, company: str, username: str, password: str):
        self.base_url = f"http://127.0.0.1:{port}"
        self.company = company
        self.username = username
        self.password = password
        self._request_id_counter = 0

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _post(self, data: dict, timeout: int = ZIGNIAO_API_TIMEOUT) -> dict:
        """发送 POST 请求到紫鸟 API。"""
        import urllib.request
        import urllib.error

        self._request_id_counter += 1
        data["requestId"] = f"req_{self._request_id_counter}_{int(time.time())}"
        req_data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=req_data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"紫鸟 API 请求失败 ({data.get('action')}): {e}")
        except json.JSONDecodeError:
            raise RuntimeError(f"紫鸟 API 返回无效 JSON ({data.get('action')})")

    def _check(self, result: dict, action: str) -> None:
        """校验 API 返回的 statusCode。"""
        code = result.get("statusCode")
        if code != 0 and code != "0":
            err = result.get("err", result.get("statusMsg", "未知错误"))
            raise RuntimeError(f"紫鸟 API [{action}] 失败 (code={code}): {err}")

    # ------------------------------------------------------------------
    # 公开 API 方法
    # ------------------------------------------------------------------

    def update_core(
        self,
        max_retries: int = 150,
        interval: float = 2.0,
        check_stop: Callable[[], None] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
    ) -> bool:
        """
        检测并更新浏览器内核（循环轮询直到完成）。

        参数：
            max_retries: 最大轮询次数（每次间隔 interval 秒）
            interval: 轮询间隔（秒）
            check_stop: 可选，每次循环检查是否应终止（raise 异常）
            wait_if_paused: 可选，每次循环检查是否需要暂停等待
        """
        print("[ziniao] 检测浏览器内核...")
        for i in range(max_retries):
            try:
                result = self._post({
                    "company": self.company,
                    "username": self.username,
                    "password": self.password,
                    "action": "updateCore",
                })
                if result.get("statusCode") == 0:
                    print("[ziniao] 内核就绪")
                    return True
                msg = result.get("msg", "下载中...")
                if i % 5 == 0:
                    print(f"  [ziniao] 内核更新: {msg}")
            except Exception as e:
                if i % 10 == 0:
                    print(f"  [ziniao] 等待客户端就绪... ({e})")

            if check_stop:
                check_stop()
            if wait_if_paused:
                wait_if_paused()
            time.sleep(interval)

        raise RuntimeError("紫鸟内核更新超时（300s）")

    def apply_auth(self) -> bool:
        """申请设备授权。"""
        print("[ziniao] 申请设备授权...")
        result = self._post({
            "company": self.company,
            "username": self.username,
            "password": self.password,
            "action": "applyAuth",
        })
        self._check(result, "applyAuth")
        print("[ziniao] 授权成功")
        return True

    def get_browser_list(self) -> list[dict]:
        """获取店铺列表。"""
        print("[ziniao] 获取店铺列表...")
        result = self._post({
            "company": self.company,
            "username": self.username,
            "password": self.password,
            "action": "getBrowserList",
        })
        self._check(result, "getBrowserList")
        browsers = result.get("browserList", [])
        print(f"[ziniao] 共 {len(browsers)} 个店铺")
        for b in browsers:
            print(f"  - {b.get('browserName', '?')} (ID: {b.get('browserOauth', '?')})")
        return browsers

    def find_store(self, store_name: str) -> dict | None:
        """
        按店铺名称搜索匹配的店铺。

        匹配策略：精确名称 → 模糊包含 → browserOauth 精确匹配。
        """
        browsers = self.get_browser_list()

        # 精确匹配
        for b in browsers:
            if b.get("browserName", "") == store_name:
                return b

        # 模糊包含
        for b in browsers:
            name = b.get("browserName", "")
            if store_name.lower() in name.lower():
                print(f"[ziniao] 模糊匹配: '{store_name}' -> '{name}'")
                return b

        # browserOauth 匹配
        for b in browsers:
            if b.get("browserOauth", "") == store_name:
                return b

        return None

    def start_browser(
        self,
        browser_oauth: str,
        store_name: str = "",
        is_headless: bool = False,
        is_wait_plugin_update: bool = False,
        privacy_mode: bool = False,
        not_prompt_for_download: int = 1,
    ) -> dict:
        """
        启动店铺浏览器，返回包含 debuggingPort 的响应。

        参数：
            browser_oauth: 店铺加密 ID
            store_name: 店铺名称（仅用于日志）
            is_headless: 是否无头模式
            is_wait_plugin_update: 是否等待插件更新（仅 V5）
            privacy_mode: 隐私模式（不加载 cookies）
            not_prompt_for_download: 1=不弹下载提示, 0=弹提示
        """
        print(f"[ziniao] 启动店铺: {store_name or browser_oauth}")
        result = self._post({
            "company": self.company,
            "username": self.username,
            "password": self.password,
            "action": "startBrowser",
            "browserOauth": browser_oauth,
            "isHeadless": is_headless,
            "isWaitPluginUpdate": is_wait_plugin_update,
            "privacyMode": privacy_mode,
            "notPromptForDownload": not_prompt_for_download,
        })
        self._check(result, "startBrowser")
        dp = result.get("debuggingPort")
        cv = result.get("coreVersion", result.get("core_version", "?"))
        print(f"[ziniao] 店铺已启动: debuggingPort={dp}, core={cv}")
        return result

    def stop_browser(self, browser_oauth: str) -> bool:
        """关闭店铺浏览器。"""
        print(f"[ziniao] 关闭店铺: {browser_oauth}")
        try:
            result = self._post({
                "company": self.company,
                "username": self.username,
                "password": self.password,
                "action": "stopBrowser",
                "browserOauth": browser_oauth,
            })
            self._check(result, "stopBrowser")
            return True
        except Exception as e:
            print(f"[ziniao] 关闭店铺失败: {e}")
            return False


# ============================================================================
# CDP 连接
# ============================================================================

def connect_to_store_cdp(debugging_port: int) -> tuple:
    """
    通过 CDP (Chrome DevTools Protocol) 连接到紫鸟店铺浏览器。

    参数：
        debugging_port: startBrowser 返回的调试端口

    返回：
        (playwright, browser, context, page) 元组
    """
    cdp_url = f"http://127.0.0.1:{debugging_port}"
    print(f"[ziniao] CDP 连接: {cdp_url}")

    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(cdp_url)
    contexts = br.contexts
    if not contexts:
        raise RuntimeError("未找到浏览器上下文")
    ctx = contexts[0]
    pages = ctx.pages
    pg = pages[-1] if pages else ctx.new_page()
    print(f"[ziniao] CDP 连接成功: {pg.url}")
    return pw, br, ctx, pg


# ============================================================================
# 高层封装：一键启动 → 开店 → 连接
# ============================================================================

class ZiniaoBrowser:
    """
    紫鸟浏览器一站式管理。

    封装启动紫鸟 → API 调用 → CDP 连接的完整流程，
    方便在自动化脚本中一键接入。

    用法::

        zb = ZiniaoBrowser("公司", "用户", "密码", starter_path="C:\\Path\\To\\SuperBrowser\\starter.exe")
        pw, browser, ctx, page = zb.open_and_connect("EXAMPLE-STORE-UK")
        # ... Playwright 自动化 ...
        zb.close()

    参数：
        company: 紫鸟公司名
        username: 紫鸟用户名
        password: 紫鸟密码
        socket_port: HTTP API 端口（默认 18888）
        starter_path: 紫鸟 starter.exe 路径（可选，None 则自动搜索）
    """

    def __init__(
        self,
        company: str,
        username: str,
        password: str,
        socket_port: int = DEFAULT_SOCKET_PORT,
        starter_path: str | None = None,
    ):
        self.company = company
        self.username = username
        self.password = password
        self.socket_port = socket_port
        self.starter_path = starter_path

        self._api: Optional[ZigniaoAPI] = None
        self._playwright: Optional[any] = None
        self._browser: Optional[Browser] = None
        self._browser_oauth: Optional[str] = None

    # ------------------------------------------------------------------
    # 启动 + API
    # ------------------------------------------------------------------

    def launch(self, on_manual_launch: Callable[[], None] | None = None) -> bool:
        """
        启动紫鸟浏览器（WebDriver 模式）。

        参数：
            on_manual_launch: 自动启动失败时的回调（如弹窗引导用户手动打开）
        """
        path = self.starter_path or find_superbrowser_path()
        ok = launch_superbrowser(path=path, socket_port=self.socket_port)
        if not ok and on_manual_launch:
            on_manual_launch()
        return ok

    @property
    def api(self) -> ZigniaoAPI:
        """懒初始化 API 客户端。"""
        if self._api is None:
            self._api = ZigniaoAPI(
                self.socket_port, self.company, self.username, self.password
            )
        return self._api

    def open_store(
        self,
        store_name: str,
        check_stop: Callable[[], None] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        on_manual_launch: Callable[[], None] | None = None,
    ) -> int:
        """
        完整流程：启动紫鸟 → 内核更新 → 授权 → 搜索店铺 → 启动店铺。

        参数：
            store_name: 店铺名称（如 "EXAMPLE-STORE-UK"）
            check_stop: 终止检查回调
            wait_if_paused: 暂停等待回调
            on_manual_launch: 自动启动失败时的手动操作回调

        返回：
            CDP 调试端口号 (int)

        异常：
            RuntimeError: 未找到店铺或启动失败
        """
        if check_stop:
            check_stop()

        # 1. 启动紫鸟
        self.launch(on_manual_launch=on_manual_launch)

        if check_stop:
            check_stop()

        # 2. 内核更新 + 授权
        self.api.update_core(check_stop=check_stop, wait_if_paused=wait_if_paused)

        if check_stop:
            check_stop()

        self.api.apply_auth()

        if check_stop:
            check_stop()

        # 3. 搜索店铺
        store = self.api.find_store(store_name)
        if store is None:
            browsers = self.api.get_browser_list()
            names = [b.get("browserName", "?") for b in browsers[:20]]
            raise RuntimeError(
                f"未找到店铺 '{store_name}'。可用: {', '.join(names)}"
            )

        self._browser_oauth = store.get("browserOauth", "")
        store_display = store.get("browserName", store_name)
        print(f"[ziniao] 匹配店铺: {store_display}")

        if check_stop:
            check_stop()

        # 4. 启动店铺
        result = self.api.start_browser(self._browser_oauth, store_display)
        debugging_port = result.get("debuggingPort")
        if not debugging_port:
            raise RuntimeError("startBrowser 未返回 debuggingPort")

        print(f"[ziniao] CDP 调试端口: {debugging_port}")
        return int(debugging_port)

    def connect_cdp(self, debugging_port: int) -> tuple:
        """
        通过 CDP 连接到已启动的店铺浏览器。

        返回：(playwright, browser, context, page)
        """
        self._playwright, self._browser, ctx, pg = connect_to_store_cdp(debugging_port)
        return self._playwright, self._browser, ctx, pg

    def open_and_connect(
        self,
        store_name: str,
        check_stop: Callable[[], None] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        on_manual_launch: Callable[[], None] | None = None,
    ) -> tuple:
        """
        一键：启动紫鸟 → 打开店铺 → CDP 连接。

        相当于::

            port = zb.open_store("店铺名")
            pw, br, ctx, pg = zb.connect_cdp(port)

        返回：(playwright, browser, context, page)
        """
        port = self.open_store(
            store_name,
            check_stop=check_stop,
            wait_if_paused=wait_if_paused,
            on_manual_launch=on_manual_launch,
        )
        return self.connect_cdp(port)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def stop_browser(self) -> bool:
        """关闭当前店铺浏览器（通过 API）。"""
        if self._browser_oauth:
            return self.api.stop_browser(self._browser_oauth)
        return False

    def close(self):
        """
        清理所有资源：关闭浏览器连接、停止 Playwright。
        注意：不会通过 API 关闭店铺（店铺保持打开以保留 cookie）。
        """
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================================
# 辅助：店铺名称验证（从页面提取 + 对比）
# ============================================================================

def validate_store_name(page: Page, expected_name: str) -> bool:
    """
    从亚马逊卖家后台页面提取当前店铺名称，
    与 expected_name 对比验证，防止发错账号。

    参数：
        page: Playwright Page（需在卖家后台）
        expected_name: 用户输入的店铺名称

    返回：验证通过 → True，不匹配 → False
    """
    print(f"[ziniao] 验证店铺名称: 期望 '{expected_name}'...")

    store_name_selectors = [
        "kat-navigation a[href*='store']",
        "[data-testid='store-name']",
        ".store-name",
        "header a[href*='store']",
        "a:has-text('Store')",
        "[class*='store-switcher'] span",
        "header [class*='account']",
    ]

    found_name = ""
    for sel in store_name_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                found_name = el.inner_text(timeout=3000).strip()
                if found_name and found_name.lower() not in GENERIC_STORE_NAMES:
                    break
        except Exception:
            continue

    if not found_name or found_name.lower() in GENERIC_STORE_NAMES:
        print("[ziniao] 页面没有明确的店铺名称，不使用页面标题判断店铺")
        return True

    print(f"[ziniao] 检测到店铺名称: '{found_name}'")

    norm_expected = expected_name.lower().replace("-", " ").replace("_", " ").strip()
    norm_found = found_name.lower().replace("-", " ").replace("_", " ").strip()

    if norm_expected in norm_found or norm_found in norm_expected:
        print("[ziniao] ✓ 店铺名称验证通过")
        return True

    print(f"[ziniao] ✗ 店铺名称不匹配! 期望: '{expected_name}', 实际: '{found_name}'")
    return False


# ============================================================================
# CLI：CDP 端口诊断
# ============================================================================

def scan_cdp_ports(scan_range: range = range(9200, 9600)) -> list[tuple[int, dict]]:
    """
    扫描本地端口，检测哪些是 Chrome DevTools Protocol 端点。

    参数：
        scan_range: 要扫描的端口范围

    返回：
        [(port, version_info), ...] 列表
    """
    import socket
    import urllib.request
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _check(port: int) -> tuple[int, dict] | None:
        try:
            url = f"http://127.0.0.1:{port}/json/version"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    browser = data.get("Browser", "")
                    if "Chrome" in browser or "Chromium" in browser:
                        return (port, data)
        except Exception:
            pass
        return None

    # 1. TCP 快速扫描
    open_ports = []
    with ThreadPoolExecutor(max_workers=30) as pool:

        def _tcp(p):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                ok = s.connect_ex(("127.0.0.1", p)) == 0
                s.close()
                return p if ok else None
            except Exception:
                return None

        for f in as_completed({pool.submit(_tcp, p): p for p in scan_range}):
            r = f.result()
            if r:
                open_ports.append(r)

    open_ports.sort()
    print(f"[ziniao] 发现 {len(open_ports)} 个开放端口: {open_ports}")

    # 2. CDP 端点验证
    cdp_ports = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for f in as_completed({pool.submit(_check, p): p for p in open_ports}):
            r = f.result()
            if r:
                cdp_ports.append(r)

    for port, info in cdp_ports:
        print(f"  CDP 端口 {port}: {info.get('Browser', '?')}")

    return cdp_ports
