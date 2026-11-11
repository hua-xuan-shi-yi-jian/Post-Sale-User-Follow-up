#!/usr/bin/env python3
"""
亚马逊操作通用工具函数
======================
"""

import re


def extract_domain_from_url(url: str) -> str:
    """从 URL 提取 sellercentral 域名后缀。

    例如: "https://sellercentral.amazon.co.uk/home" → ".amazon.co.uk"
    """
    m = re.search(r"sellercentral(.+?)/", url)
    if m:
        return m.group(1)
    m = re.search(r"sellercentral([^/]*)", url)
    return m.group(1) if m else ".amazon.com"


def normalize_site_name(site_name: str) -> str:
    """繁体中文/英文缩写站点名 → 简体中文归一化.

    从 constants.TRAD_TO_SIMP 中查找映射表。
    同时使用 SITE_MATCH_KEYWORDS 做反向查找（英文→中文）。
    """
    from .constants import TRAD_TO_SIMP, SITE_MATCH_KEYWORDS

    # 1. 繁→简转换
    simplified = TRAD_TO_SIMP.get(site_name)
    if simplified is not None:
        return simplified

    # 2. 英文缩写/英文名 → 中文名（反向查找 SITE_MATCH_KEYWORDS）
    site_lower = site_name.strip()
    for cn_name, keywords in SITE_MATCH_KEYWORDS.items():
        if site_name == cn_name or site_lower in keywords:
            return cn_name

    # 3. 已经是简体或未知，保持原样
    return site_name


def matches_site_label(label_text: str, site_name: str) -> bool:
    """检查账户选择页标签是否匹配目标站点.

    账户选择页显示英文/本地语。匹配分两步:
      1. site_name 或其 SITE_MATCH_KEYWORDS 关键词直接子串命中
      2. URL 域名关键词命中（如 amazon.pl 出现在页面 URL 上下文中）
    """
    from .constants import SITE_MATCH_KEYWORDS
    terms = [site_name] + SITE_MATCH_KEYWORDS.get(site_name, [])
    return any(t in label_text for t in terms)
