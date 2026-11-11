#!/usr/bin/env python3
"""
账户选择页站点切换 (V2 增强版)
==============================
相比 V1 的 switch_marketplace 域名直跳（易触发额外登录），
V2 直接导航到 account-switcher 全页 + 条目匹配 + OTP/MFA 重试循环。

用法:
    from a2autolib2.amazon.marketplace import switch_to_marketplace

    ok = switch_to_marketplace(page, "西班牙", login_fn=lambda p: quick_login(p))
"""

import logging
import time
from typing import Any, Callable

from playwright.sync_api import Page

from .login import quick_login
from .utils import matches_site_label, normalize_site_name

log = logging.getLogger(__name__)

ACCOUNT_SWITCHER_URL = (
    "https://sellercentral.amazon.com"
    "/account-switcher/default/merchantMarketplace"
)

SHORT_TIMEOUT = 3000
MED_TIMEOUT = 10000
LONG_TIMEOUT = 60000


def switch_to_marketplace(
    page: Page,
    site_name: str,
    login_fn: Callable[[Page], bool] | None = None,
) -> bool:
    """通过账户选择全页切换到目标站点。

    Args:
        page: Playwright Page
        site_name: 目标站点中文名（如 "西班牙"）
        login_fn: 登录回调（None 则使用内置 quick_login）

    Returns:
        True 切换成功
    """
    if login_fn is None:
        login_fn = lambda p: quick_login(p)

    # 繁简归一化
    site_name = normalize_site_name(site_name)

    log.info("[MARKET] 导航到账户选择页: %s", site_name)
    try:
        page.goto(
            ACCOUNT_SWITCHER_URL,
            timeout=LONG_TIMEOUT,
            wait_until="domcontentloaded",
        )
        log.info("[MARKET] 已到达: %s", page.url[:120])
    except Exception as e:
        log.warning("[MARKET] 导航失败: %s", e)
        return False

    time.sleep(3)
    login_fn(page)
    time.sleep(2)

    # 扫描账户条目
    account_selectors = [
        "span.full-page-account-switcher-account-label",
        ".account-switcher-account-label",
        "[data-testid='account-switcher-label']",
    ]

    labels, count = _scan_account_labels(page, account_selectors)
    log.info("[MARKET] %d 个可见条目", count)

    if count <= 1:
        log.warning("[MARKET] 未找到可选账户")
        return False

    return _select_and_confirm(page, site_name, labels, count, login_fn)


def _scan_account_labels(page: Page, selectors: list[str]) -> tuple:
    """扫描账户选择器，返回 (locator, count)。"""
    for sel in selectors:
        try:
            labels = page.locator(sel)
            cnt = sum(
                1 for j in range(labels.count())
                if labels.nth(j).is_visible(timeout=1000)
            )
            if cnt > 1:
                return labels, cnt
        except Exception:
            continue
    return None, 0


def _select_and_confirm(
    page: Page,
    site_name: str,
    account_labels: Any,
    label_count: int,
    login_fn: Callable[[Page], bool],
) -> bool:
    """匹配账户条目 → 点击 → 确认 → OTP/MFA 重试循环。

    Returns:
        True 切换成功
    """
    # ---- 匹配目标站点 ----
    matched = False
    for i in range(label_count):
        try:
            el = account_labels.nth(i)
            text = el.inner_text(timeout=SHORT_TIMEOUT).strip()
            log.info("[MARKET] 选项 %d: '%s'", i, text)
            if matches_site_label(text, site_name):
                log.info("[MARKET] 匹配 '%s' → '%s'", site_name, text)
                el.click()
                matched = True
                break
        except Exception:
            continue

    if not matched:
        log.warning("[MARKET] 未匹配到 '%s' (%d个条目)", site_name, label_count)
        return False

    time.sleep(2)

    # ---- 点击确认按钮 ----
    for cs in [
        "kat-button[data-test='confirm-selection']",
        "button:has-text('Confirm')",
        "button:has-text('确认')",
        "#ngstrim-modal-confirm-button",
    ]:
        try:
            btn = page.locator(cs).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                log.info("[MARKET] 点击确认: %s", cs)
                btn.click(timeout=5000)
                time.sleep(3)
                break
        except Exception:
            continue

    # ---- OTP/MFA 重试循环 ----
    for retry in range(2):
        login_fn(page)
        time.sleep(2)
        log.info("[MARKET] 第%d次登录后URL: %s", retry + 1, page.url[:120])

        url_after = page.url
        still_on_selection = (
            "account-switcher" in url_after
            or "choose-account" in url_after
        )
        if not still_on_selection:
            log.info("[MARKET] 已跳转到: %s", url_after[:120])
            return True

        if retry >= 1:
            log.warning("[MARKET] 已达最大重试次数，当前URL: %s", url_after[:120])
            return False

        # 重新扫描并点击
        log.info("[MARKET] 仍在选择页，重选 (%d/2)", retry + 1)
        for sel in [
            "span.full-page-account-switcher-account-label",
            ".account-switcher-account-label",
        ]:
            try:
                labels = page.locator(sel)
                re_count = sum(
                    1 for j in range(labels.count())
                    if labels.nth(j).is_visible(timeout=1000)
                )
                if re_count > 1:
                    for i in range(re_count):
                        el = labels.nth(i)
                        t = el.inner_text(timeout=SHORT_TIMEOUT).strip()
                        if matches_site_label(t, site_name):
                            el.click()
                            time.sleep(2)
                            for cs in [
                                "kat-button[data-test='confirm-selection']",
                                "button:has-text('Confirm')",
                                "button:has-text('确认')",
                            ]:
                                try:
                                    btn = page.locator(cs).first
                                    if (btn.count() > 0
                                            and btn.is_visible(timeout=2000)):
                                        btn.click(timeout=5000)
                                        time.sleep(3)
                                        break
                                except Exception:
                                    continue
                            break
                    break
            except Exception:
                continue

    return False
