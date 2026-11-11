"""亚马逊卖家后台操作模块 — 登录、站点切换、付款处理。"""

from .login import LoginHandler, quick_login
from .marketplace import switch_to_marketplace
from .payment import PaymentHandler, PaymentResult
from .utils import extract_domain_from_url

__all__ = [
    "LoginHandler",
    "quick_login",
    "switch_to_marketplace",
    "PaymentHandler",
    "PaymentResult",
    "extract_domain_from_url",
]
