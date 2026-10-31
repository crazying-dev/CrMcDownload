# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：文件夹分发（onedir），避免单文件运行时解压临时目录失败。"""

from PyInstaller.utils.hooks import collect_submodules

# 皮肤校验 / 头像裁切用到 Pillow，onedir 产物必须显式收齐 PIL 子模块与 _imaging 扩展；
# 官方内置插件经 importlib 动态导入，静态分析抓不到，须一并收集 offcraft.plugins.builtin。
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/icon.ico", "assets")],
    hiddenimports=[
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        *collect_submodules("PIL"),
        *collect_submodules("offcraft.plugins.builtin"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "paramiko", "pytest", "numpy", "pandas", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrMCDownload",
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
    version="version_info.txt",
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CrMCDownload",
)
