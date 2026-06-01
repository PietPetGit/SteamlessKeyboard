# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['lockscreen_osk.py'],
    pathex=[],
    binaries=[('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libavif-16.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libgme.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libogg-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libopus-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libopusfile-0.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libtiff-5.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwavpack-1.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwebp-7.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libwebpdemux-2.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\libxmp.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_gfx.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_image.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_mixer.dll', 'sdl2dll/dll'), ('C:\\Users\\Administrator\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\sdl2dll\\dll\\SDL2_ttf.dll', 'sdl2dll/dll')],
    datas=[('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\data', 'data')],
    hiddenimports=['pynput.keyboard._win32', 'pynput.mouse._win32'],
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
    [],
    exclude_binaries=True,
    name='LockScreenKeyboardUIA',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_uiaccess=True,
    icon=['C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\data\\images\\app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LockScreenKeyboardUIA',
)
