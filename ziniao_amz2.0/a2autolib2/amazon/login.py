#!/usr/bin/env python3
"""
亚马逊登录处理器 (V2 增强版)
============================
相比 V1：增加预检逻辑，避免在无需登录的页面上触发登录流程。

用法:
    from a2autolib2.amazon.login import LoginHandler, quick_login

    # 方式1: 完整处理器
    handler = LoginHandler(page)
    handler.handle()

    # 方式2: 快速登录（带预检的一键调用）
    quick_login(page)
"""

import logging
import time
from playwright.sync_api import Page

from .constants import SIGNIN_SELECTORS, OTP_CONFIRM_SELECTORS

log = logging.getLogger(__name__)

# 登录页 URL 关键词
LOGIN_URL_KW = ["/ap/signin", "/ap/mfa", "/ap/cvf", "signin", "challenge"]

# "Switch accounts" 页检测特征
SWITCH_ACCOUNTS_KW = ["switchaccount", "switch_account", "Switch accounts"]


def _handle_switch_accounts_page(page: Page) -> bool:
    """处理 Amazon "Switch accounts"（切换账号）页面.

    该页面在登录流程中出现，列出可用账号供用户选择。
    需要点击第一个账号卡片（a-fixed-left-grid 区域）才能进入 OTP 验证。

    页面特征:
      - URL 含 /ap/signin 且有 switchaccount 参数
      - <h1>Switch accounts</h1> 标题
      - data-test-id="switchableAccounts" 容器

    选择器策略（按优先级）:
      1. a.cvf-widget-btn-verify-account-switcher     → 精确匹配
      2. a[data-name="switch_account_request"]        → data 属性匹配
      3. [data-test-id="switchableAccounts"] a:has(.a-fixed-left-grid) → 通用回退

    Returns:
        True 已点击账号并等待页面跳转.
    """
    url = page.url

    # 检测: URL 包含 signin + switchaccount 参数
    url_has_switch = any(kw in url for kw in SWITCH_ACCOUNTS_KW[:2])

    # 或页面标题包含 "Switch accounts"
    has_title = False
    try:
        h1 = page.locator('h1')
        if h1.count() > 0:
            h1_text = h1.first.inner_text(timeout=2000)
            has_title = "Switch accounts" in h1_text
    except Exception:
        pass

    # 或页面包含可切换账号容器
    has_container = False
    try:
        container = page.locator('[data-test-id="switchableAccounts"]')
        has_container = container.count() > 0 and container.first.is_visible(timeout=1000)
    except Exception:
        pass

    if not (url_has_switch or has_title or has_container):
        return False

    log.info("[LOGIN] 检测到 Switch accounts 页面，点击第一个账号...")

    # 尝试多种选择器找到可点击的账号
    account_selectors = [
        'a.cvf-widget-btn-verify-account-switcher',
        'a[data-name="switch_account_request"]',
        '[data-test-id="switchableAccounts"] a:has(.a-fixed-left-grid)',
        '[data-test-id="switchableAccounts"] a.cvf-widget-btn-val',
        '.cvf-account-switcher a[role="button"]',
    ]

    clicked = False
    for sel in account_selectors:
        try:
            link = page.locator(sel).first
            if link.count() > 0 and link.is_visible(timeout=2000):
                log.info("[LOGIN] 点击账号: %s", sel)
                link.click(timeout=5000)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        log.warning("[LOGIN] 未找到可点击的账号元素")
        return False

    # 等待页面跳转（可能进入 OTP/MFA 或直接进入后台）
    time.sleep(3)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(2)

    log.info("[LOGIN] Switch accounts 点击后 URL: %s", page.url[:120])
    return True


def quick_login(page: Page) -> bool:
    """一键登录（推荐使用）。

    带预检：如果在卖家后台/账户选择页 → 跳过；如果没有登录表单 → 跳过。
    新增：自动处理 "Switch accounts"（切换账号）中间页。
    """
    url = page.url

    # 预检1: 是否在登录相关页面？
    on_login_page = any(kw in url for kw in LOGIN_URL_KW)

    # 预检2: 是否已在卖家后台/账户选择页？
    if not on_login_page and (
        "sellercentral" in url
        or "account-switcher" in url
        or "choose-account" in url
    ):
        return True

    # 预检3: 不在登录页也没内嵌表单 → 跳过
    if not on_login_page:
        try:
            has_login_form = page.locator(
                'input[type="password"], #ap_password, form[name="signIn"]'
            ).first.count() > 0
        except Exception:
            has_login_form = False
        if not has_login_form:
            return True

    # ── 新增: 先处理 Switch accounts 中间页 ──
    if _handle_switch_accounts_page(page):
        # 点击账号后可能进入 OTP 页面或直接进入后台
        time.sleep(2)
        url_after = page.url
        # 检查是否已离开登录页
        still_login = any(kw in url_after for kw in LOGIN_URL_KW)
        # 再检查是否是 switch accounts 页（可能点错了）
        still_switch = any(kw in url_after for kw in SWITCH_ACCOUNTS_KW[:2])
        if not still_login or still_switch:
            # 可能还在 switch 页，回退到标准登录
            pass
        else:
            log.info("[LOGIN] Switch accounts → 已进入验证流程")

    # 执行标准登录
    log.info("[LOGIN] 执行登录验证, URL=%s", url[:120])
    try:
        handler = LoginHandler(page)
        handler.handle()
    except Exception as e:
        log.warning("[LOGIN] LoginHandler 异常: %s", e)

    # 登录后判断: 只要离开登录页就算成功
    url_after = page.url
    still_on_login = any(kw in url_after for kw in LOGIN_URL_KW)
    if not still_on_login:
        log.info("[LOGIN] 登录成功（已离开登录页）")
        return True
    log.warning("[LOGIN] 登录未确认，仍在登录页: %s", url_after[:100])
    return False


class LoginHandler:
    """标准亚马逊卖家后台登录流程。

    紫鸟插件自动填入账号密码和 OTP，脚本点击按钮 + 确认。
    """

    def __init__(self, page: Page):
        self.page = page

    def handle(self, timeout: int = 120) -> bool:
        """三步登录：导航 → 登录按钮 → OTP 确认。
        新增 Step 0.5: 处理 Switch accounts 中间页。
        """
        print("[login] 开始处理登录...")

        # Step 0: 确保在卖家后台域
        current_url = self.page.url.lower()
        if "sellercentral" not in current_url:
            print("[login] 导航到卖家后台...")
            try:
                self.page.goto(
                    "https://sellercentral.amazon.com/",
                    timeout=60000, wait_until="domcontentloaded",
                )
                time.sleep(3)
            except Exception as e:
                print(f"[login] 导航失败: {e}")

        # Step 0.5: 处理 Switch accounts 中间页（点击账号卡片）
        _handle_switch_accounts_page(self.page)
        time.sleep(1)

        # Step 1: 已在卖家后台 → 跳过
        current_url = self.page.url.lower()
        if ("sellercentral" in current_url and "signin" not in current_url
                and "mfa" not in current_url
                and "account-switcher" not in current_url):
            print("[login] 已在卖家后台，无需登录")
            return True

        # Step 2: 登录页 → 点击按钮（最多3轮）
        for login_round in range(3):
            current_url = self.page.url.lower()
            if "signin" not in current_url and "ap/signin" not in current_url:
                break

            if login_round == 0:
                print("[login] 检测到登录页面...")
            else:
                print(f"[login] 仍在登录页，第 {login_round + 1} 轮尝试...")

            self._wait_password_filled()
            clicked = self._click_signin_button()

            if not clicked:
                print("  [login] 未找到按钮，尝试密码框按 Enter...")
                try:
                    pw_el = self.page.locator("input[type='password']").first
                    if pw_el.count() > 0:
                        pw_el.press("Enter", timeout=5000)
                        print("  [login] 已按 Enter")
                        time.sleep(4)
                except Exception:
                    pass

        # Step 3: OTP 处理
        return self._handle_otp()

    def _wait_password_filled(self) -> None:
        """等待紫鸟填入密码。"""
        try:
            pw_el = self.page.locator("input[type='password']").first
            if pw_el.count() > 0:
                for _ in range(10):
                    try:
                        val = pw_el.input_value() or ""
                        if len(val) >= 4:
                            print(f"  [login] 密码已填入 ({len(val)} 位)")
                            break
                    except Exception:
                        pass
                    time.sleep(1)
        except Exception:
            pass

    def _click_signin_button(self) -> bool:
        """遍历登录按钮选择器，点击第一个可见的。"""
        for sel in SIGNIN_SELECTORS:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    print(f"  [login] 点击: {sel}")
                    try:
                        btn.click(timeout=5000)
                    except Exception:
                        pass
                    time.sleep(3)
                    try:
                        self.page.wait_for_load_state(
                            "domcontentloaded", timeout=15000,
                        )
                    except Exception:
                        pass
                    time.sleep(1)
                    return True
            except Exception:
                continue
        return False

    def _handle_otp(self) -> bool:
        """处理 OTP/MFA 验证码自动填入和确认。"""
        for otp_sel in ["#auth-mfa-otpcode", "input[name='otpCode']",
                        "input[name='code']"]:
            try:
                otp = self.page.locator(otp_sel).first
                if otp.count() > 0:
                    print("[login] 检测到 OTP 页面，等待紫鸟填入验证码...")
                    for _ in range(30):
                        try:
                            val = otp.input_value()
                            if val and len(val) >= 4:
                                print(f"  [login] OTP 已填入 ({len(val)} 位)")
                                time.sleep(1)

                                for btn_sel in OTP_CONFIRM_SELECTORS:
                                    try:
                                        btn = self.page.locator(btn_sel).first
                                        if (btn.count() > 0
                                                and btn.is_visible(timeout=2000)):
                                            btn.click(timeout=3000)
                                            print(f"  [login] 点击确认: {btn_sel}")
                                            time.sleep(5)
                                            break
                                    except Exception:
                                        continue

                                # 降级：OTP 框 Enter / 表单提交
                                try:
                                    otp.press("Enter", timeout=3000)
                                    print("  [login] 表单按 Enter 提交")
                                except Exception:
                                    try:
                                        self.page.locator("form").first.press(
                                            "Enter", timeout=3000,
                                        )
                                        print("  [login] 表单按 Enter 提交")
                                    except Exception:
                                        pass
                                time.sleep(5)
                                break
                        except Exception:
                            pass
                        time.sleep(2)
                    break
            except Exception:
                continue

        # 最终检查
        time.sleep(3)
        final_url = self.page.url.lower()
        if ("sellercentral" in final_url and "signin" not in final_url
                and "mfa" not in final_url
                and "account-switcher" not in final_url):
            print("[login] 登录成功")
            return True

        print("[login] 登录未完成")
        return False
