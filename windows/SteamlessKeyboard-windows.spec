# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data', 'data')]
binaries = [('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libavif-16.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libgme.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libogg-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libopus-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libopusfile-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libtiff-5.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwavpack-1.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwebp-7.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwebpdemux-2.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libxmp.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_gfx.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_image.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_mixer.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_ttf.dll', 'sdl2dll/dll')]
hiddenimports = ['pystray._win32', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'PIL._tkinter_finder']
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
