# -*- mode: python ; coding: utf-8 -*-

import os
import sys

block_cipher = None

# Datas: include assets/ and data/
datas = [
    ('assets/gateway-minimap.png', 'assets'),
    ('assets/gateway-base-optimized.png', 'assets'),
    ('data/gateway-overlay.json', 'data'),
    ('data/config.json', 'data'),
    ('data/waypoints.json', 'data'),
]

# Check existing files to avoid missing file errors during packaging
valid_datas = []
for src_p, dst_d in datas:
    if os.path.exists(src_p):
        valid_datas.append((src_p, dst_d))

a = Analysis(
    ['main.py'],
    pathex=['.', 'src'],
    binaries=[],
    datas=valid_datas,
    hiddenimports=[
        'PyQt5',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'requests',
        'json',
        'socket',
        'threading',
        'ctypes',
        'ctypes.wintypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['evdev', 'tkinter', 'unittest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='VoiceHUD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None
)
