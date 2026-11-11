#!/usr/bin/env python3
"""
紫鸟 CDP 端口诊断工具
=====================
扫描所有端口，检测哪些是真正的 Chrome DevTools Protocol 端点。
"""

import socket
import urllib.request
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

SCAN_RANGE = range(9200, 9600)  # 扩大扫描范围


def check_cdp_endpoint(port: int) -> tuple[int, dict] | None:
    """验证端口是否是真正的 CDP 端点（通过读取 /json/version）"""
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


def main():
    print("=" * 60)
    print("  紫鸟 CDP 端口诊断工具")
    print("=" * 60)
    print()
    print(f"扫描范围: {SCAN_RANGE.start} ~ {SCAN_RANGE.stop - 1}")
    print("正在扫描（大约需要 30 秒）...")
    print()

    # 先用 TCP 快速扫描找开放端口
    print("[第1步] TCP 端口扫描...")
    open_ports = []
    with ThreadPoolExecutor(max_workers=30) as pool:
        def tcp_check(p):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                if s.connect_ex(('127.0.0.1', p)) == 0:
                    s.close()
                    return p
                s.close()
            except Exception:
                pass
            return None

        futures = {pool.submit(tcp_check, p): p for p in SCAN_RANGE}
        for f in as_completed(futures):
            result = f.result()
            if result:
                open_ports.append(result)

    open_ports.sort()
    print(f"  发现 {len(open_ports)} 个开放端口: {open_ports}")
    print()

    # 验证哪些是 CDP 端点
    print("[第2步] CDP 端点验证...")
    if not open_ports:
        print("  [FAIL] 没有发现任何开放端口！")
        print()
        print("  请确认：")
        print("  1. 紫鸟浏览器已启动")
        print("  2. 已在紫鸟中打开了目标店铺的浏览器窗口")
        print("  3. 紫鸟中该账号已开启 WebDriver/调试模式")
        return

    cdp_ports = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(check_cdp_endpoint, p): p for p in open_ports}
        for f in as_completed(futures):
            result = f.result()
            if result:
                port, info = result
                cdp_ports.append((port, info))

    print()
    if cdp_ports:
        print(f"  发现 {len(cdp_ports)} 个 CDP 端点：")
        print()
        for port, info in cdp_ports:
            print(f"    端口: {port}")
            print(f"    浏览器: {info.get('Browser', '?')}")
            print(f"    版本: {info.get('Product', '?')}")
            print(f"    WebSocket: {info.get('webSocketDebuggerUrl', '?')[:80]}...")
            print(f"    → Playwright 连接: playwright.chromium.connect_over_cdp('http://127.0.0.1:{port}')")
            print()

        # 给出建议
        main_port = cdp_ports[0][0]
        print(f"  建议在 config.json 中设置 CDP 端口为: {main_port}")
    else:
        print(f"  [WARN] {len(open_ports)} 个开放端口都不是 CDP 端点")
        print()

        # 列出开放端口及其用途
        print("  开放端口列表（非 CDP）：")
        for p in open_ports:
            try:
                url = f"http://127.0.0.1:{p}/"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    content = resp.read()[:200].decode(errors='replace')
                    print(f"    {p}: HTTP {resp.status} → {content[:80]}")
            except Exception as e:
                print(f"    {p}: {type(e).__name__}")

    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
