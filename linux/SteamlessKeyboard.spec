# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray_linux.py'],
    pathex=[],
    binaries=[],
    datas=[('/run/media/user/A420301F202FF6C8/Users/Administrator/Desktop/SteamlessKeyboard-main/SteamlessKeyboard-main/linux/data', 'data')],
    hiddenimports=['pynput.keyboard._xorg', 'pynput.mouse._xorg', 'PIL._tkinter_finder', 'pystray._appindicator', 'pystray._util.gtk', 'pystray._util.notify_dbus', 'gi', 'gi.repository.Gtk', 'gi.repository.AyatanaAppIndicator3'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['steamcontroller.winhid', 'vgamepad', 'winreg', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'tray'],
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
    name='SteamlessKeyboard',
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
)
