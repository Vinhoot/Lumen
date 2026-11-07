# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— Lumen（Liquid Glass Web UI 版）。

产物：dist/Lumen/Lumen.exe（onedir，启动快）
数据来源：main_web.py → app/（pyapi/server/workspace + 复用 db/scanner/thumbs/backup）
"""
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.getcwd())
ICON = os.path.join(ROOT, "app", "assets", "icon.ico")

a = Analysis(
    ["main_web.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "app", "web"), "app/web"),
        (ICON, "app/assets"),
        (os.path.join(ROOT, "runtime", "pythonnet.runtimeconfig.json"), "."),
    ]
    + collect_data_files("webview")          # WebView2 互操作 DLL（已替换为 .NET 8 兼容版）
    + collect_data_files("pythonnet")        # Python.Runtime.dll
    + collect_data_files("clr_loader")       # ClrLoader.dll
    + collect_data_files("imageio_ffmpeg", includes=["binaries/*"]),
    hiddenimports=[
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "pythonnet",
        "clr_loader",
        "defusedxml",
        "defusedxml.ElementTree",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "lib2to3", "xmlrpc", "pdb",
        "PySide6", "PyQt5", "PyQt6", "PySide2",
        "numpy", "pandas", "matplotlib",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Lumen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Lumen",
)
