# -*- mode: python ; coding: utf-8 -*-
# One-file build with ffmpeg/ffprobe baked in. PyInstaller can't cross-compile:
# build on Windows for the .exe, on Linux for the Linux binary.
import sys

ext = '.exe' if sys.platform == 'win32' else ''

a = Analysis(
    ['mazvid.py'],
    pathex=[],
    binaries=[('ffmpeg' + ext, '.'), ('ffprobe' + ext, '.')],
    datas=[],
    hiddenimports=[],
    hookspath=[],
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
    name='mazvid',
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
)
