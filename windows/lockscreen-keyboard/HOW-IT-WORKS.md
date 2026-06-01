# Lock-screen keyboard — how it works

Optional add-on — **not recommended** (see Security below and the README).
Lets the Steam Controller act as a keyboard on the Windows lock screen so you
can unlock without a physical keyboard, on a trusted home PC you own.
`install.bat` / `uninstall.bat` do everything below for you.

## How

The lock screen is the **secure desktop** (`winlogon`) — normal apps can't draw
or type there; only `SYSTEM` processes on that desktop can. Windows launches the
built-in accessibility tools there via the **Ease of Access** button. We ride
that: an **IFEO "Debugger"** entry on `Utilman.exe` makes that button launch our
`LockScreenKeyboard.exe` (a no-tray build that opens the keyboard immediately)
instead. The controller's firmware "lizard mode" already gives you a mouse on
the lock screen (right trackpad) to click the button.

What `install.bat` does:
1. Copy the EXE to `C:\LockScreenKeyboard\` (admin-only, stable path).
2. Add a Defender exclusion so it isn't quarantined.
3. Set `HKLM\…\Image File Execution Options\Utilman.exe` → `Debugger` = that EXE.

Close the keyboard = it exits. Press Ease of Access again to relaunch.

## Security

This is the well-known "utilman backdoor": anyone at your locked PC can open the
keyboard as `SYSTEM` before sign-in. Fine for a trusted home PC; **not** for a
laptop you carry or a work/shared machine. Defender flags the registry change —
hence the exclusion. `uninstall.bat` removes all three changes.

## Undo manually (if needed)

```powershell
# (Administrator)
Remove-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\Utilman.exe" -Name Debugger -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionPath "C:\LockScreenKeyboard" -ErrorAction SilentlyContinue
Remove-Item "C:\LockScreenKeyboard" -Recurse -Force -ErrorAction SilentlyContinue
```

## Troubleshooting

- **Nothing happens / normal menu appears:** Defender removed the entry —
  reinstall (the exclusion should stick on the second run).
- **Keyboard appears but typing doesn't register:** the password box must have
  focus. The launcher auto-focuses it by locating the box with UI Automation and
  synthesizing a real mouse click on it (works because we run as SYSTEM, same
  integrity as LogonUI). If a key highlights on the keyboard but no character
  appears, the click missed — move the cursor onto the box and click it once
  with the right trackpad, then type.
- **No mouse on lock screen:** something is holding the controller. Make sure
  the tray app or "Block Steam exclusive HID" isn't grabbing it while locked.

## Rebuild the EXE

```powershell
cd "...\SteamlessKeyboard-main\SteamlessKeyboard-main"
python build_lockscreen.py   # -> dist\LockScreenKeyboard.exe
```
