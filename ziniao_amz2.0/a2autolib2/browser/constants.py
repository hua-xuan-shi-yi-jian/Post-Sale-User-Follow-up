#!/usr/bin/env python3
"""
紫鸟浏览器常量
==============
"""

# HTTP API 默认端口
DEFAULT_SOCKET_PORT = 18888

# API 请求超时（秒），文档要求 ≥120s
ZIGNIAO_API_TIMEOUT = 130

# 紫鸟默认搜索路径（按优先级排列）
SUPERBROWSER_DEFAULT_SEARCH_PATHS = [
    r"D:\紫鸟\SuperBrowser\starter.exe",
    r"D:\软件\SuperBrowser\starter.exe",
    r"C:\Program Files\SuperBrowser\starter.exe",
    r"C:\Program Files (x86)\SuperBrowser\starter.exe",
]

# 通用品牌名称（不是店铺名，用于店铺名称验证过滤）
GENERIC_STORE_NAMES = {
    "amazon", "亚马逊", "amazon seller central", "seller central",
    "amazon.co.uk", "amazon.com", "sellercentral", "后台",
}
