# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

import playwright


root = Path(SPECPATH)
driver = Path(playwright.__file__).parent / "driver"
tcl = Path(sys.base_prefix) / "tcl"

a = Analysis(
    [str(root / "amazon_bulk_send.py")],
    pathex=[str(root), str(root / "ziniao_amz2.0")],
    binaries=[(str(driver / "node.exe"), "playwright/driver")],
    datas=[
        (str(driver / "package"), "playwright/driver/package"),
        (str(tcl / "tcl8.6"), "_tcl_data"),
        (str(tcl / "tk8.6"), "_tk_data"),
        (str(root / "amazon_mail_tool.ico"), "."),
    ],
    hiddenimports=[
        "playwright.sync_api",
        "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk",
        "a2autolib2.amazon.login", "a2autolib2.amazon.marketplace",
        "a2autolib2.amazon.constants", "a2autolib2.amazon.utils",
        "a2autolib2.common.logging",
        "amazon_selectors", "task_models", "feishu_sync", "ziniao_browser",
    ],
    hookspath=[str(root / "packaging_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="亚马逊批量发送邮件",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "amazon_mail_tool.ico"),
)
