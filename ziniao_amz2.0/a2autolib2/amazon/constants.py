#!/usr/bin/env python3
"""
亚马逊站点常量 (V2 增强版)
==========================
域名映射、国家别名、语言参数、付款页面多语言选择器、繁简转换。
"""

# ------------------------------------------------------------------
# 亚马逊域名 → 中文国家名映射
# ------------------------------------------------------------------
DOMAIN_TO_COUNTRY: dict[str, str] = {
    "amazon.com":   "美国",   "amazon.ca":    "加拿大",
    "amazon.com.mx": "墨西哥", "amazon.co.uk": "英国",
    "amazon.de":    "德国",   "amazon.fr":    "法国",
    "amazon.it":    "意大利", "amazon.es":    "西班牙",
    "amazon.nl":    "荷兰",   "amazon.se":    "瑞典",
    "amazon.pl":    "波兰",   "amazon.com.be": "比利时",
    "amazon.ie":    "爱尔兰", "amazon.co.jp": "日本",
    "amazon.com.au": "澳大利亚", "amazon.in":  "印度",
    "amazon.sg":    "新加坡", "amazon.ae":    "阿联酋",
    "amazon.sa":    "沙特阿拉伯", "amazon.com.tr": "土耳其",
    "amazon.eg":    "埃及",   "amazon.com.br": "巴西",
}

# ------------------------------------------------------------------
# 国家中文名 → 卖家后台域名
# ------------------------------------------------------------------
COUNTRY_TO_DOMAIN: dict[str, str] = {}
for _d, _c in DOMAIN_TO_COUNTRY.items():
    COUNTRY_TO_DOMAIN[_c] = f"sellercentral.{_d}"

# ------------------------------------------------------------------
# 站点名 → 账户选择页标签匹配词（英文/本地语）
# ------------------------------------------------------------------
SITE_MATCH_KEYWORDS: dict[str, list[str]] = {
    "英国": ["United Kingdom", "UK", "amazon.co.uk"],
    "德国": ["Germany", "Deutschland", "amazon.de"],
    "法国": ["France", "amazon.fr"],
    "意大利": ["Italy", "Italia", "amazon.it"],
    "西班牙": ["Spain", "España", "amazon.es"],
    "荷兰": ["Netherlands", "Nederland", "amazon.nl"],
    "波兰": ["Poland", "Polska", "amazon.pl"],
    "瑞典": ["Sweden", "Sverige", "amazon.se"],
    "土耳其": ["Turkey", "Türkiye", "amazon.tr"],
    "美国": ["United States", "USA", "amazon.com "],
    "加拿大": ["Canada", "amazon.ca"],
    "墨西哥": ["Mexico", "México", "amazon.mx"],
    "日本": ["Japan", "amazon.co.jp"],
    "印度": ["India", "amazon.in"],
    "澳大利亚": ["Australia", "amazon.au"],
    "新加坡": ["Singapore", "amazon.sg"],
    "阿联酋": ["UAE", "United Arab Emirates", "amazon.ae"],
    "沙特": ["Saudi Arabia", "KSA", "amazon.sa"],
    "巴西": ["Brazil", "Brasil", "amazon.br"],
    "葡萄牙": ["Portugal", "amazon.pt"],
    "比利时": ["Belgium", "België", "amazon.be"],
    "爱尔兰": ["Ireland", "amazon.ie"],
    "香港": ["Hong Kong", "amazon.hk"],
    "中国": ["China", "amazon.cn"],
    # ── 英文名 → 中文名反向映射（兼容飞书配置使用英文站点名）──
    "United Kingdom": ["United Kingdom", "UK"],
    "Germany": ["Germany", "Deutschland"],
    "France": ["France"],
    "Italy": ["Italy", "Italia"],
    "Spain": ["Spain", "España"],
    "Netherlands": ["Netherlands", "Nederland"],
    "Poland": ["Poland", "Polska"],
    "Sweden": ["Sweden", "Sverige"],
    "Turkey": ["Turkey", "Türkiye"],
    "United States": ["United States", "USA"],
    "Canada": ["Canada"],
    "Mexico": ["Mexico", "México"],
    "Japan": ["Japan"],
    "India": ["India"],
    "Australia": ["Australia"],
    "Singapore": ["Singapore"],
    "UAE": ["UAE", "United Arab Emirates"],
    "Saudi Arabia": ["Saudi Arabia", "KSA"],
    "Brazil": ["Brazil", "Brasil"],
    "Portugal": ["Portugal"],
    "Belgium": ["Belgium", "België"],
    "Ireland": ["Ireland"],
}

# ------------------------------------------------------------------
# 站点名 → 默认货币符号
# ------------------------------------------------------------------
SITE_CURRENCY_SYMBOL: dict[str, str] = {
    "英国": "£", "法国": "€", "德国": "€", "意大利": "€",
    "西班牙": "€", "荷兰": "€", "波兰": "zł", "瑞典": "kr",
    "土耳其": "₺", "美国": "US$", "加拿大": "CA$", "墨西哥": "MX$",
    "日本": "JP¥", "印度": "₹", "澳大利亚": "AU$", "新加坡": "S$",
    "阿联酋": "د.إ", "沙特": "﷼", "巴西": "R$", "香港": "HK$",
    "中国": "CN¥", "比利时": "€", "葡萄牙": "€", "挪威": "kr",
}

# ------------------------------------------------------------------
# 繁体中文 → 简体中文归一化
# ------------------------------------------------------------------
TRAD_TO_SIMP: dict[str, str] = {
    "英國": "英国", "德國": "德国", "法國": "法国",
    "義大利": "意大利", "荷蘭": "荷兰", "波蘭": "波兰",
    "美國": "美国", "澳大利亞": "澳大利亚", "阿聯酋": "阿联酋",
    "比利時": "比利时", "中國": "中国", "愛爾蘭": "爱尔兰",
    "紐西蘭": "新西兰", "菲律賓": "菲律宾", "馬來西亞": "马来西亚",
    "泰國": "泰国", "越南": "越南", "印尼": "印尼",
    "韓國": "韩国", "西班牙": "西班牙", "加拿大": "加拿大",
    "墨西哥": "墨西哥", "日本": "日本", "印度": "印度",
    "新加坡": "新加坡", "沙特": "沙特", "巴西": "巴西",
    "香港": "香港", "葡萄牙": "葡萄牙", "土耳其": "土耳其",
    "瑞典": "瑞典", "挪威": "挪威",
    # ── 补充常见变体 / 异体字 ──
    "沙烏地": "沙特", "沙烏地阿拉伯": "沙特", "阿拉伯聯合大公國": "阿联酋",
    "阿拉伯联合酋长国": "阿联酋", "阿拉伯聯合酋長國": "阿联酋",
    "阿拉伯联合大公国": "阿联酋", "UAE": "阿联酋",
    "UK": "英国", "USA": "美国", "KSA": "沙特",
}

# ------------------------------------------------------------------
# 登录按钮选择器（多语言）
# ------------------------------------------------------------------
SIGNIN_SELECTORS: list[str] = [
    "input#signInSubmit", "button[type='submit']", "#signInSubmit",
    "input[name='signInSubmit']", "input[type='submit']",
    "button:has-text('Sign In')", "button:has-text('Anmelden')",
    "button:has-text('Accedi')", "button:has-text('Iniciar sesión')",
    "button:has-text('Se connecter')", "button:has-text('Aanmelden')",
    "button:has-text('Zaloguj się')", "button:has-text('Logga in')",
    "input#continue", "button:has-text('Continue')",
]

# ------------------------------------------------------------------
# OTP/MFA 确认按钮选择器
# ------------------------------------------------------------------
OTP_CONFIRM_SELECTORS: list[str] = [
    "input#auth-signin-button", "button[type='submit']",
    "#auth-signin-button", "input#signInSubmit", "input#continue",
    "button:has-text('Sign In')", "button:has-text('登录')",
    "button:has-text('Continue')", "button:has-text('Weiter')",
    "button:has-text('Verify')", "input[type='submit']",
]

# ------------------------------------------------------------------
# 弹窗关闭选择器
# ------------------------------------------------------------------
POPUP_DISMISS_SELECTORS: list[str] = [
    "button[data-testid='guided-tour-later-button']",
    "button.modal-close",
    "button.vibes-ngs-form__ask-me-later-link",
    "button[aria-label='Close']",
]

# ------------------------------------------------------------------
# 付款页面 — 请求付款按钮（12种语言，kat-button[label] 精确匹配）
# ------------------------------------------------------------------
BTN_REQUEST_PAYMENT = (
    'kat-button[label="Request Payment"],'
    'kat-button[label="请求付款"],'
    'kat-button[label="Solicitar pago"],'
    'kat-button[label="Demander un paiement"],'
    'kat-button[label="Zahlung anfordern"],'
    'kat-button[label="Richiedi pagamento"],'
    'kat-button[label="Betaling aanvragen"],'
    'kat-button[label="Poproś o płatność"],'
    'kat-button[label="Begär betalning"],'
    'kat-button[label="Ödeme talep et"],'
    'kat-button[label="Solicitar pagamento"],'
    'kat-button[label="支払いをリクエスト"]'
)
BTN_REQUEST_PAYMENT_ENABLED = (
    'kat-button[label="Request Payment"]:not([disabled]),'
    'kat-button[label="请求付款"]:not([disabled]),'
    'kat-button[label="Solicitar pago"]:not([disabled]),'
    'kat-button[label="Demander un paiement"]:not([disabled]),'
    'kat-button[label="Zahlung anfordern"]:not([disabled]),'
    'kat-button[label="Richiedi pagamento"]:not([disabled]),'
    'kat-button[label="Betaling aanvragen"]:not([disabled]),'
    'kat-button[label="Poproś o płatność"]:not([disabled]),'
    'kat-button[label="Begär betalning"]:not([disabled]),'
    'kat-button[label="Ödeme talep et"]:not([disabled]),'
    'kat-button[label="Solicitar pagamento"]:not([disabled]),'
    'kat-button[label="支払いをリクエスト"]:not([disabled])'
)
BTN_REQUEST_PAYMENT_DISABLED = (
    'kat-button[label="Request Payment"][disabled],'
    'kat-button[label="请求付款"][disabled],'
    'kat-button[label="Solicitar pago"][disabled],'
    'kat-button[label="Demander un paiement"][disabled],'
    'kat-button[label="Zahlung anfordern"][disabled],'
    'kat-button[label="Richiedi pagamento"][disabled],'
    'kat-button[label="Betaling aanvragen"][disabled],'
    'kat-button[label="Poproś o płatność"][disabled],'
    'kat-button[label="Begär betalning"][disabled],'
    'kat-button[label="Ödeme talep et"][disabled],'
    'kat-button[label="Solicitar pagamento"][disabled],'
    'kat-button[label="支払いをリクエスト"][disabled]'
)

# ------------------------------------------------------------------
# 付款页面 — 偿还余额按钮（12种语言）
# ------------------------------------------------------------------
BTN_REPAY_BALANCE = (
    'kat-button[label="Repay Balance"]:not([disabled]),'
    'kat-button[label="偿还余额"]:not([disabled]),'
    'kat-button[label="Reembolsar saldo"]:not([disabled]),'
    'kat-button[label="Rembourser le solde"]:not([disabled]),'
    'kat-button[label="Guthaben zurückzahlen"]:not([disabled]),'
    'kat-button[label="Rimborsa saldo"]:not([disabled]),'
    'kat-button[label="Saldo terugbetalen"]:not([disabled]),'
    'kat-button[label="Spłać saldo"]:not([disabled]),'
    'kat-button[label="Återbetala saldo"]:not([disabled]),'
    'kat-button[label="Bakiyeyi geri öde"]:not([disabled]),'
    'kat-button[label="Reembolsar saldo"]:not([disabled]),'
    'kat-button[label="残高を返済"]:not([disabled])'
)

# ------------------------------------------------------------------
# 付款页面 — 无付款数据（多语言 kat-alert）
# ------------------------------------------------------------------
NO_PAYMENT_DATA_SELECTORS: list[str] = [
    'kat-alert[description="您在此商城中没有任何可用的付款数据。"]',
    'kat-alert[description="You have no payment data available in this marketplace."]',
    'kat-alert:has-text("datos de pago disponibles")',
    'kat-alert:has-text("aucune donnée de paiement")',
    'kat-alert:has-text("keine Zahlungsdaten")',
    'kat-alert:has-text("dati di pagamento")',
    'kat-alert:has-text("betalingsgegevens beschikbaar")',
    'kat-alert:has-text("danych o płatnościach")',
    'kat-alert:has-text("betalningsdata")',
    'kat-alert:has-text("ödeme verisi")',
    'kat-alert:has-text("dados de pagamento disponíveis")',
]

# ------------------------------------------------------------------
# 付款页面 — 24小时限制文本（多语言 span）
# ------------------------------------------------------------------
TEXT_24H_LIMIT = (
    'span:has-text("二十四小时内只能进行一次付款"),'
    'span:has-text("twenty-four hours"),'
    'span:has-text("24 hours"),'
    'span:has-text("24小时内"),'
    'span:has-text("24 小时内"),'
    'span:has-text("veinticuatro horas"),'
    'span:has-text("24 horas"),'
    'span:has-text("vingt-quatre heures"),'
    'span:has-text("24 heures"),'
    'span:has-text("24 Stunden"),'
    'span:has-text("24 ore"),'
    'span:has-text("24 uur"),'
    'span:has-text("24 godzin"),'
    'span:has-text("24 timmar"),'
    'span:has-text("24 saat"),'
    'span:has-text("24時間")'
)

# ------------------------------------------------------------------
# 付款页面 — 余额不足文本（多语言 span）
# ------------------------------------------------------------------
TEXT_BALANCE_MINIMUM = (
    'span:has-text("只有当您的可用余额大于最低限值时"),'
    'span:has-text("greater than the minimum"),'
    'span:has-text("可用余额大于最低限值"),'
    'span:has-text("saldo disponible"),'
    'span:has-text("superior al mínimo"),'
    'span:has-text("solde disponible"),'
    'span:has-text("supérieur au minimum"),'
    'span:has-text("Guthaben größer"),'
    'span:has-text("Mindestbetrag"),'
    'span:has-text("saldo disponibile"),'
    'span:has-text("superiore al minimo"),'
    'span:has-text("beschikbare saldo"),'
    'span:has-text("hoger dan het minimum"),'
    'span:has-text("dostępne saldo"),'
    'span:has-text("wyższe niż"),'
    'span:has-text("tillgängliga saldot"),'
    'span:has-text("högre än"),'
    'span:has-text("kullanılabilir bakiye"),'
    'span:has-text("minimumdan yüksek"),'
    'span:has-text("saldo disponível"),'
    'span:has-text("superior ao mínimo"),'
    'span:has-text("利用可能な残高"),'
    'span:has-text("最低限度")'
)

# ------------------------------------------------------------------
# 账户选择页元素选择器
# ------------------------------------------------------------------
ACCOUNT_SWITCHER_LABEL = "span.full-page-account-switcher-account-label"
ACCOUNT_SWITCHER_BUTTON = "button.full-page-account-switcher-account-details"
ACCOUNT_SWITCHER_CONFIRM = "kat-button[data-test='confirm-selection']"
CURRENT_MARKET_SELECTOR = ".dropdown-account-switcher-header-label-regional"

# ------------------------------------------------------------------
# 站点语言参数
# ------------------------------------------------------------------
MARKETPLACE_LANG: dict[str, str] = {
    "英国": "en_GB", "德国": "de_DE", "法国": "fr_FR",
    "意大利": "it_IT", "西班牙": "es_ES", "荷兰": "nl_NL",
    "瑞典": "sv_SE", "波兰": "pl_PL", "比利时": "nl_BE",
    "美国": "en_US", "加拿大": "en_CA", "墨西哥": "es_MX",
    "巴西": "pt_BR", "日本": "ja_JP", "澳大利亚": "en_AU",
    "印度": "en_IN", "新加坡": "en_SG", "中国": "zh_CN",
    "阿联酋": "ar_AE", "沙特阿拉伯": "ar_SA", "土耳其": "tr_TR",
}
