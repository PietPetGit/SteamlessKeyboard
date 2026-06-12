# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data', 'data')]
binaries = [('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\sdl3w\\dll\\SDL3.dll', 'sdl3w/dll'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\sdl3w\\dll\\SDL3_ttf.dll', 'sdl3w/dll')]
hiddenimports = ['pystray._win32', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'PIL._tkinter_finder', 'sdl3w']
tmp_ret = collect_all('vgamepad')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['tray.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='SteamlessKeyboard-windows',
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
    icon=['C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data\\images\\app_icon.ico'],
)
