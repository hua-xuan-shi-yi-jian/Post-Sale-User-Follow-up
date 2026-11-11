#!/usr/bin/env python3
"""
紫鸟 WebDriver HTTP API 客户端
================================
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional, Callable

from .constants import ZIGNIAO_API_TIMEOUT


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

        参数:
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

        匹配策略: 精确名称 → 模糊包含 → browserOauth 精确匹配。
        """
        browsers = self.get_browser_list()

        for b in browsers:
            if b.get("browserName", "") == store_name:
                return b

        for b in browsers:
            if store_name.lower() in b.get("browserName", "").lower():
                print(f"[ziniao] 模糊匹配: '{store_name}' -> '{b.get('browserName')}'")
                return b

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
