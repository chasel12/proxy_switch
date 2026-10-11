# -*- mode: python ; coding: utf-8 -*-
import os


a = Analysis(
    ['webui.py'],
    pathex=[],
    binaries=[],
    # controller.html 必须打包进来：frozen 时 HTML_PATH 从 sys._MEIPASS 取
    datas=[('controller.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter 必须保留：cc_switch_launcher 顶层导入 + --tk 兜底界面
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
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
    name='控制器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, 'assets', 'controller.ico'),
)
