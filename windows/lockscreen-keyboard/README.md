# Optional: Lock-screen keyboard (windows only)

Use the Steam Controller as a keyboard on the Windows **lock screen**, so you
can type your password and sign in without a physical keyboard.

> [!WARNING]
> **This is optional, and I do not recommend it.** It is not part of core
> SteamlessKeyboard — it's a separate convenience hack with a real security
> cost. It installs the well-known "Utilman" accessibility trick, which lets
> **anyone** standing at your locked PC open a SYSTEM-level keyboard *before*
> sign-in. Only consider it on a private home PC you fully trust — **never** on
> a laptop, work, or shared machine. Uninstalling reverses every change.

## Install

1. Double-click **`install.bat`** that is located in this folder
2. Click **Yes** on the admin prompt.

## Use

1. Press **Win + L** to lock the PC.
2. The right trackpad moves the mouse — click **Ease of Access** <img src="../data/images/glyphs/glyph_easeofaccess.png width="18" alt="Ease of Access" style="vertical-align:middle"> (bottom-right).
3. The keyboard appears. Type your password and press **R2** to sign in.

## Uninstall

Double-click **`uninstall.bat`** and click **Yes**. The lock screen returns to
normal — all changes are removed.

## How it works

The mechanism, the security implications, and manual undo steps are in
[HOW-IT-WORKS.md](HOW-IT-WORKS.md).
