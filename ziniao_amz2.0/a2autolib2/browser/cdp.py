#!/usr/bin/env python3
"""
CDP 连接与高层封装
====================
"""

import time
from typing import Optional, Callable

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from .api import ZigniaoAPI
from .launcher import find_superbrowser_path, kill_existing_superbrowser, launch_superbrowser
from .constants import DEFAULT_SOCKET_PORT, GENERIC_STORE_NAMES


def connect_to_store_cdp(debugging_port: int) -> tuple:
    """
    通过 CDP (Chrome DevTools Protocol) 连接到紫鸟店铺浏览器。

    参数:
        debugging_port: startBrowser 返回的调试端口

    返回:
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


class ZiniaoBrowser:
    """
    紫鸟浏览器一站式管理。

    封装启动紫鸟 → API 调用 → CDP 连接的完整流程。

    用法::

        zb = ZiniaoBrowser("公司", "用户", "密码", starter_path="C:\\Path\\To\\SuperBrowser\\starter.exe")
        pw, browser, ctx, page = zb.open_and_connect("EXAMPLE-STORE-UK")
        # ... Playwright 自动化 ...
        zb.close()

    支持上下文管理器::

        with ZiniaoBrowser("公司", "用户", "密码") as zb:
            pw, br, ctx, page = zb.open_and_connect("EXAMPLE-STORE-UK")
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

    def launch(self, on_manual_launch: Callable[[], None] | None = None) -> bool:
        """启动紫鸟浏览器（WebDriver 模式）。"""
        kill_existing_superbrowser()
        time.sleep(2)
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

        返回: CDP 调试端口号 (int)
        """
        if check_stop:
            check_stop()

        self.launch(on_manual_launch=on_manual_launch)
        if check_stop:
            check_stop()

        self.api.update_core(check_stop=check_stop, wait_if_paused=wait_if_paused)
        if check_stop:
            check_stop()

        self.api.apply_auth()
        if check_stop:
            check_stop()

        store = self.api.find_store(store_name)
        if store is None:
            browsers = self.api.get_browser_list()
            names = [b.get("browserName", "?") for b in browsers[:20]]
            raise RuntimeError(f"未找到店铺 '{store_name}'。可用: {', '.join(names)}")

        self._browser_oauth = store.get("browserOauth", "")
        print(f"[ziniao] 匹配店铺: {store.get('browserName', store_name)}")
        if check_stop:
            check_stop()

        result = self.api.start_browser(self._browser_oauth, store.get("browserName", store_name))
        debugging_port = result.get("debuggingPort")
        if not debugging_port:
            raise RuntimeError("startBrowser 未返回 debuggingPort")
        print(f"[ziniao] CDP 调试端口: {debugging_port}")
        return int(debugging_port)

    def connect_cdp(self, debugging_port: int) -> tuple:
        """通过 CDP 连接到已启动的店铺浏览器。返回 (pw, browser, ctx, page)。"""
        self._playwright, self._browser, ctx, pg = connect_to_store_cdp(debugging_port)
        return self._playwright, self._browser, ctx, pg

    def open_and_connect(self, store_name: str, **kwargs) -> tuple:
        """一键：启动紫鸟 → 打开店铺 → CDP 连接。返回 (pw, browser, ctx, page)。"""
        port = self.open_store(store_name, **kwargs)
        return self.connect_cdp(port)

    def stop_browser(self) -> bool:
        """关闭当前店铺浏览器（通过 API）。"""
        if self._browser_oauth:
            return self.api.stop_browser(self._browser_oauth)
        return False

    def close(self):
        """清理资源：关闭浏览器连接、停止 Playwright。不通过 API 关闭店铺（保留 cookie）。"""
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
