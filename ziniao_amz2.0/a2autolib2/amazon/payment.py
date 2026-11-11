#!/usr/bin/env python3
"""
亚马逊付款页面处理器 (V2 增强版)
================================
基于 DOM 状态 + ID 选择器判断，消除 V1 文本匹配的假阳性。

核心改进:
- V1: :has-text("已成功启动金额转账") → DOM 中 hidden 的成功 alert 直接命中
- V2: #request-transfer-button → 点击 → is_visible("#disburse-now-submit-success-alert")

检测场景（按优先级）:
1. 无付款数据
2. "请求付款"按钮可点击 → 点击 → disburse 页
3. "请求付款"按钮 disabled
4. "偿还余额"按钮可见
5. 兜底 → 无可提款金额

用法:
    from a2autolib2.amazon.payment import PaymentHandler, PaymentResult
    from a2autolib2.amazon.login import quick_login

    handler = PaymentHandler(page, lambda p: quick_login(p))
    result = handler.handle("EXAMPLE-STORE-UK", "英国")
    if result.success:
        print("提款成功:", result.description)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from playwright.sync_api import Page

from .constants import (
    BTN_REQUEST_PAYMENT_ENABLED,
    BTN_REQUEST_PAYMENT_DISABLED,
    BTN_REPAY_BALANCE,
    NO_PAYMENT_DATA_SELECTORS,
    TEXT_24H_LIMIT,
    TEXT_BALANCE_MINIMUM,
)
from .utils import extract_domain_from_url

log = logging.getLogger(__name__)

SHORT_TIMEOUT = 3000
MED_TIMEOUT = 10000
LONG_TIMEOUT = 60000

@dataclass
class PaymentResult:
    """付款操作结果。"""
    store: str
    site: str
    timestamp: str
    description: str
    success: bool = False


class PaymentHandler:
    """亚马逊卖家后台付款页面处理器 (V2)。

    核心改进: 用 ID 选择器 + is_visible() 替代 :has-text()，
    消除 DOM 中 hidden 状态元素导致的假阳性。

    Args:
        page: Playwright Page（应在付款控制面板 payments/dashboard）
        login_fn: 登录回调，每次导航/点击后调用
    """

    def __init__(self, page: Page, login_fn: Callable[[Page], bool]):
        self._page = page
        self._login_fn = login_fn
        self._fallback_balance = ""

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def handle(self, store_name: str, site_name: str) -> PaymentResult:
        """执行付款页面检测和处理。"""
        now = str(datetime.now()).split(".")[0]

        log.info("[PAYMENT] === 开始付款处理 %s/%s, URL=%s ===",
                 store_name, site_name, self._page.url[:150])

        # 关闭弹窗
        self._dismiss_vibes_popup()

        # ★ 抓取仪表盘可用余额（disburse 页无金额时的兜底）
        self._fallback_balance = self._get_available_balance()
        if self._fallback_balance and self._fallback_balance != "未知":
            log.info("[PAYMENT] 已捕获仪表盘余额: %s", self._fallback_balance)

        # 场景1: 无付款数据
        if self._has_no_payment_data():
            desc = "无付款数据：您在此商城中没有任何可用的付款数据。"
            log.info("[PAYMENT] %s/%s: %s", store_name, site_name, desc)
            return PaymentResult(store_name, site_name, now, desc)

        # 场景2: "请求付款"按钮可点击
        if self._request_payment_enabled():
            log.info("[PAYMENT] %s/%s: 请求付款按钮可点击",
                     store_name, site_name)
            return self._handle_request_payment(store_name, site_name, now)

        # 场景3: "请求付款"按钮 disabled
        if self._request_payment_disabled():
            balance = self._get_available_balance()
            desc = f"无法提款，余额为{balance}"
            log.info("[PAYMENT] %s/%s: %s", store_name, site_name, desc)
            return PaymentResult(store_name, site_name, now, desc)

        # 场景4: "偿还余额"按钮
        if self._repay_balance_visible():
            balance = self._get_available_balance()
            desc = f"无法提款，显示为偿还余额：{balance}"
            log.info("[PAYMENT] %s/%s: %s", store_name, site_name, desc)
            return PaymentResult(store_name, site_name, now, desc)

        # 场景5: 兜底 — 检查是否误入非付款页
        if self._is_on_login_or_selection_page():
            log.warning("[PAYMENT] %s/%s: 非付款页，URL=%s",
                       store_name, site_name, self._page.url)
            desc = "无法提款-页面异常（非付款页）"
            return PaymentResult(store_name, site_name, now, desc)

        desc = "无可提款金额"
        log.warning("[PAYMENT] %s/%s: 无可提款金额", store_name, site_name)
        return PaymentResult(store_name, site_name, now, desc)

    # ------------------------------------------------------------------
    # 场景检测器
    # ------------------------------------------------------------------

    def _has_no_payment_data(self) -> bool:
        for sel in NO_PAYMENT_DATA_SELECTORS:
            if self._exists(sel):
                return True
        return False

    def _request_payment_enabled(self) -> bool:
        return self._exists(BTN_REQUEST_PAYMENT_ENABLED)

    def _request_payment_disabled(self) -> bool:
        return self._exists(BTN_REQUEST_PAYMENT_DISABLED)

    def _repay_balance_visible(self) -> bool:
        return self._exists(BTN_REPAY_BALANCE)

    # ------------------------------------------------------------------
    # 场景2: 请求付款 → disburse 页
    # ------------------------------------------------------------------

    def _handle_request_payment(self, store: str, site: str, now: str
                                ) -> PaymentResult:
        """点击"请求付款"按钮，跳转到 disburse 提交页。

        2轮循环（亚马逊可能要求重新认证）:
        1. 点击"请求付款" → 等待 disburse URL
        2. 未到达 → 登录后重试
        """
        url_before_click = self._page.url

        for attempt in range(2):
            if attempt == 0:
                try:
                    btn = self._page.locator(
                        BTN_REQUEST_PAYMENT_ENABLED
                    ).first
                    log.info("[PAYMENT] 点击'请求付款'...")
                    btn.click(timeout=SHORT_TIMEOUT)
                    log.info("[PAYMENT] 已点击 '请求付款'")
                except Exception as e:
                    log.error("[PAYMENT] 点击请求付款失败: %s", e)
                    return PaymentResult(
                        store, site, now, "无法提款-异常2（点击失败）")
            else:
                current_url = self._page.url
                if "payments/dashboard" not in current_url:
                    log.info("[PAYMENT] 第2轮: 重新导航到付款页")
                    domain = extract_domain_from_url(url_before_click)
                    payment_url = (
                        f"https://sellercentral{domain}"
                        f"/payments/dashboard/index.html"
                    )
                    try:
                        self._page.goto(
                            payment_url, timeout=30000,
                            wait_until="domcontentloaded",
                        )
                    except Exception:
                        pass
                    time.sleep(2)
                    self._login_fn(self._page)
                    time.sleep(2)

                if not self._request_payment_enabled():
                    log.error("[PAYMENT] 第2轮: 请求付款按钮不可用")
                    return PaymentResult(
                        store, site, now, "提款时间冷却期")

                try:
                    btn = self._page.locator(
                        BTN_REQUEST_PAYMENT_ENABLED
                    ).first
                    btn.click(timeout=SHORT_TIMEOUT)
                    log.info("[PAYMENT] 第2轮: 已点击 '请求付款'")
                except Exception as e:
                    log.error("[PAYMENT] 第2轮点击失败: %s", e)
                    return PaymentResult(
                        store, site, now, "无法提款-异常2")

            # 等待导航到 disburse
            try:
                self._page.wait_for_url(
                    "**/payments/disburse**", timeout=MED_TIMEOUT)
                log.info("[PAYMENT] round %d: 已跳转到 disburse",
                         attempt + 1)
            except Exception:
                log.warning("[PAYMENT] round %d: 等待 disburse 超时",
                           attempt + 1)
                try:
                    self._page.wait_for_load_state(
                        "domcontentloaded", timeout=LONG_TIMEOUT)
                except Exception:
                    pass

            time.sleep(2)

            self._login_fn(self._page)
            time.sleep(1)

            if "disburse" in self._page.url:
                log.info("[PAYMENT] round %d: 到达 disburse 提交页",
                         attempt + 1)
                break

        return self._detect_disburse_sub_scenario(store, site, now)

    def _detect_disburse_sub_scenario(self, store: str, site: str,
                                      now: str) -> PaymentResult:
        """检测 disburse 提交页面的子场景并处理。

        核心逻辑 (V2 — 基于 DOM 状态而非文本匹配):
        1. #request-transfer-button 可点击 → 先点击！这是最可靠的 disburse 标志
        2. 按钮 disabled → 检查约束原因
        3. success alert 可见 → 已转账成功（重入）
        4. error alert 可见 → 转账失败
        """
        log.info("[PAYMENT] 子场景检测: %s/%s, URL=%s",
                 store, site, self._page.url[:150])

        # ★ 场景1: 按钮可点击 → 还没点，现在点
        if self._exists("#request-transfer-button:not([disabled])"):
            log.info("[PAYMENT] #request-transfer-button 可点击，执行转账")
            return self._handle_transfer(store, site, now)

        # 场景2: 按钮 disabled → 检查约束
        if self._exists("#request-transfer-button[disabled]"):
            log.info("[PAYMENT] #request-transfer-button disabled")

            if self._exists_text(TEXT_24H_LIMIT):
                return PaymentResult(
                    store, site, now,
                    "二十四小时内只能进行一次付款")

            if self._exists_text(TEXT_BALANCE_MINIMUM):
                balance = self._get_settlement_balance()
                desc = f"余额低于最低限值（余额为{balance}）"
                return PaymentResult(store, site, now, desc)

            balance = self._get_settlement_balance()
            if balance and balance != "未知":
                desc = f"无法提款，余额为{balance}"
            else:
                desc = "无法提款-异常2（按钮disabled）"
            return PaymentResult(store, site, now, desc)

        # 场景3: success alert 已可见（hidden 已移除）
        if self._is_visible("#disburse-now-submit-success-alert"):
            balance = self._get_settlement_balance()
            desc = f"提款成功，提款金额：{balance}"
            log.info("[PAYMENT] ✓ %s/%s: %s", store, site, desc)
            return PaymentResult(store, site, now, desc, success=True)

        # 场景4: error alert 可见
        if self._is_visible("#disburse-now-submit-error-alert"):
            return PaymentResult(store, site, now, "收款请求失败")

        # 兜底文本匹配
        if self._exists_text(TEXT_24H_LIMIT):
            return PaymentResult(
                store, site, now, "二十四小时内只能进行一次付款")

        if self._exists_text(TEXT_BALANCE_MINIMUM):
            balance = self._get_settlement_balance()
            desc = f"余额低于最低限值（余额为{balance}）"
            return PaymentResult(store, site, now, desc)

        log.warning("[PAYMENT] %s/%s: 未匹配任何场景, URL=%s",
                   store, site, self._page.url)
        return PaymentResult(store, site, now, "提款时间冷却期")

    # ------------------------------------------------------------------
    # 转账确认
    # ------------------------------------------------------------------

    def _handle_transfer(self, store: str, site: str, now: str
                         ) -> PaymentResult:
        """点击 #request-transfer-button 并基于 alert 可见性判断结果。

        V2: 不再使用 :has-text() 匹配多语言文字，改为:
        点击按钮 → is_visible("#disburse-now-submit-success-alert") → 成功
        """
        try:
            btn = self._page.locator(
                "#request-transfer-button:not([disabled])"
            ).first
            btn.click(timeout=SHORT_TIMEOUT)
            log.info("[PAYMENT] 已点击 #request-transfer-button")
        except Exception as e:
            log.error("[PAYMENT] 点击 #request-transfer-button 失败: %s", e)
            return PaymentResult(
                store, site, now, "无法提款-异常2（点击transfer失败）")

        time.sleep(3)

        # ★ 核心: 检测 success alert 是否变为可见
        if self._is_visible("#disburse-now-submit-success-alert"):
            price = self._get_settlement_balance()
            desc = f"提款成功，提款金额：{price}"
            log.info("[PAYMENT] ✓ %s/%s: %s", store, site, desc)
            return PaymentResult(store, site, now, desc, success=True)

        # 登录后再检测一次
        self._login_fn(self._page)
        time.sleep(1)

        if self._is_visible("#disburse-now-submit-success-alert"):
            price = self._get_settlement_balance()
            desc = f"提款成功，提款金额：{price}"
            log.info("[PAYMENT] ✓ %s/%s (登录后): %s", store, site, desc)
            return PaymentResult(store, site, now, desc, success=True)

        # 失败路径
        if self._is_visible("#disburse-now-submit-error-alert"):
            return PaymentResult(store, site, now, "收款请求失败")

        if self._exists_text(TEXT_24H_LIMIT):
            return PaymentResult(
                store, site, now, "二十四小时内只能进行一次付款")

        if self._exists_text(TEXT_BALANCE_MINIMUM):
            balance = self._get_settlement_balance()
            desc = f"余额低于最低限值（余额为{balance}）"
            return PaymentResult(store, site, now, desc)

        return PaymentResult(store, site, now, "无法提款-异常2")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _exists(self, selector: str) -> bool:
        """检测元素是否存在（快速）。"""
        try:
            el = self._page.locator(selector).first
            return el.count() > 0
        except Exception:
            return False

    def _is_visible(self, selector: str, timeout: int = SHORT_TIMEOUT
                    ) -> bool:
        """检测元素是否实际可见（hidden/display:none → False）。"""
        try:
            el = self._page.locator(selector).first
            if el.count() == 0:
                return False
            return el.is_visible(timeout=timeout)
        except Exception:
            return False

    def _exists_text(self, selector: str) -> bool:
        """检测包含指定文本的元素是否存在（逗号分隔 = OR）。"""
        for sel in selector.split(","):
            sel = sel.strip()
            if not sel:
                continue
            try:
                if self._page.locator(sel).first.count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _get_text(self, selector: str, timeout: int = MED_TIMEOUT) -> str:
        """获取元素文本。"""
        try:
            el = self._page.locator(selector).first
            if el.count() > 0:
                return el.inner_text(timeout=timeout).strip()
        except Exception:
            pass
        return ""

    def _get_available_balance(self) -> str:
        """获取仪表盘可用余额 (payments/dashboard)。"""
        try:
            amounts = self._page.locator(
                ".available-currency-amount > span")
            for i in range(amounts.count()):
                text = amounts.nth(i).inner_text(
                    timeout=MED_TIMEOUT).strip()
                if text:
                    return text
        except Exception:
            pass
        return "未知"

    def _get_settlement_balance(self) -> str:
        """获取结算余额（disburse 页）；无则回退到仪表盘余额。"""
        balance = self._get_text(".settlement-amount-balance > div")
        if not balance:
            balance = self._fallback_balance
            if balance:
                log.info("[PAYMENT] 回退到仪表盘余额: %s", balance)
        return balance

    def _dismiss_vibes_popup(self) -> None:
        """关闭 vibes 弹窗。"""
        try:
            btn = self._page.locator("#vibes-close-button").first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click(timeout=SHORT_TIMEOUT)
        except Exception:
            pass

    def _is_on_login_or_selection_page(self) -> bool:
        """检测是否在登录页/账户选择页而非付款页。"""
        url = self._page.url
        if any(kw in url for kw in [
            "/ap/signin", "/ap/mfa", "/ap/cvf", "signin", "login",
        ]):
            return True
        try:
            labels = self._page.locator(
                "span.full-page-account-switcher-account-label")
            if labels.count() > 1:
                return True
        except Exception:
            pass
        return False
