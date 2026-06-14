# SteamlessKeyboard
The goal is to **replicate how the Steam Controller (2026) behaves under Steam's default configuration** so that, with Steam closed, the controller works like you are used to.

> ⚠️ **Requires the May 22, 2026 Steam Controller firmware update.** This program will not work with earlier firmware.

## Features
- Works on Windows/Linux without Steam running
- Recreates Steam's on-screen keyboard
- Recreates Steam controllers default key bindings
- Translates Steam Controller inputs into a Xbox 360 gamepad (has lower latency than SISR and VIIPER)
- Smart gamepad mode — Smooth switching between gamepad and lizard mode
- Use Keyboard on Windows Lock screen


![SteamlessKeyboard Screenshot](windows/assets/SteamlessKeyboard.png)
<sub>Press X to open the on-screen keyboard (or Ctrl + Alt + K)</sub>

---

## Installation

### 1. Install the ViGEmBus driver (windows only)

[github.com/nefarius/ViGEmBus/releases](https://github.com/nefarius/ViGEmBus/releases)

Run it and follow the prompts. You only need to do this once. The keyboard will still work without it, but gamepad mode will be unavailable.

### 2. Download SteamlessKeyboard

- Windows: download `SteamlessKeyboard-windows.zip` from the [Releases page](https://github.com/PietPetGit/SteamlessKeyboard/releases), extract it, and run `SteamlessKeyboard-windows.exe`.
- Linux: download `SteamlessKeyboard-linux.tar.gz`, extract it, and run `SteamlessKeyboard`.

### 3. Configure startup behavior (optional)
Right-click the <img src="windows/assets/SteamlessController_seethrough.png" width="20" style="vertical-align:middle"> tray icon to toggle:
|  |  |
|--------|-------------|
| **Start with Windows** | Auto-launch on boot |
| **When Steam Is Running → Pause** | Pause the listener while Steam is active (lets Steam grab the controller) |
| **When Steam Is Running → Exit** | Fully exit the app when Steam starts |
||
| **Gamepad Mode → Auto enable** | Automatically activate gamepad mode when a game is in the foreground *(default)* |
| **Gamepad Mode → Always enable** | Keep gamepad mode on at all times (hold Steam + trackpad to control the mouse) |
| **Gamepad Mode → Off** | Disable the virtual gamepad entirely |
| **Vibration** | Toggle rumble / haptic feedback |
| **Debug** *(hidden — toggle Vibration 4× in a row to reveal/hide)* | **Block SteamInput Steam Controller grab** — made for stopping SteamInput / Big Picture from opening when using media controls |

## Optional: Lock-screen keyboard (windows only)

An optional add-on lets you use the Steam Controller as a keyboard on the
Windows **lock screen**, so you can type your password and sign in without a
physical keyboard. It is **not** part of core SteamlessKeyboard and carries a
real security trade-off — read
[windows/lockscreen-keyboard/README.md](windows/lockscreen-keyboard/README.md)
before installing. The installer files are included in the Windows release zip.

## Optional: Nintendo Switch Pro Controller in gamepad mode (windows only)

**Only for the Switch Pro, and optional.** In gamepad mode the physical Switch
Pro stays visible to games and spams phantom input (buttons 1–8) on top of our
virtual Xbox pad. Hide it with the free **HidHide** driver — a one-time setup:

1. Install **HidHide** — `winget install Nefarius.HidHide` (or the [installer](https://github.com/nefarius/HidHide/releases)) — then **reboot**.
2. Open **HidHide Configuration Client** and:
   - **Applications** tab → **+** → add your `SteamlessKeyboard-windows.exe`.
   - **Devices** tab → tick **Nintendo Co., Ltd. Pro Controller**.
   - Enable **Enable device hiding** at the bottom.

Games now see only the virtual Xbox pad, while SteamlessKeyboard still reads the
controller. To use the Switch Pro normally again, untick **Enable device hiding**.

---

## Controller Keybinds

| Input | Action |
|-------|--------|
| <img src="windows/assets/shared_button_x_md.png" width="32" align="middle"> | Open the on-screen keyboard (or Ctrl + Alt + K) |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_button_b_md.png" width="32" align="middle"> | Force-shutdown game |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_button_y_md.png" width="32" align="middle"> | Turn off the controller |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/sd_button_menu_md.png" width="32" align="middle"> | Alt+Tab — hold Steam to keep the switcher open; each VIEW press advances one slot |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/sd_rtrackpad_md.png" width="32" align="middle"> | Use the trackpad as a mouse while in gamepad mode |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_up_md.png" width="32" align="middle"> | Volume up — tap for one step, hold to ramp |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_down_md.png" width="32" align="middle"> | Volume down — tap for one step, hold to ramp |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_left_md.png" width="32" align="middle"> | Previous song |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/assets/shared_lstick_right_md.png" width="32" align="middle"> | Next song |
| <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Middle click — click the left stick in (e.g. open a link in a new tab, or close a tab) |
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Play / pause (click the left stick in) |

---

## Credits

- Forked from [archshift/adusk](https://github.com/archshift/adusk)
- Gamepad translation inspired by [ddeverill/SteamlessController](https://github.com/ddeverill/SteamlessController)
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Windows virtual gamepad wrapper vendored from [yannbouteiller/vgamepad](https://github.com/yannbouteiller/vgamepad)
- Rumble implementation adapted from [CrazyCritic89/SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger)
- Battery-status parsing referenced from [samueltoken/Bloss_battery_indicator](https://github.com/samueltoken/Bloss_battery_indicator)

## Fonts

The on-screen keyboard bundles these open-license fonts (full license texts ship
alongside them in `windows/data/fonts/` and `linux/data/fonts/`):

- **Selawik Semibold** — © 2015 Microsoft Corporation, licensed under the
  [SIL Open Font License 1.1](https://github.com/microsoft/Selawik). An open,
  Segoe-UI-metric-compatible typeface; used for the key labels.
- **DejaVu Sans (Condensed Bold)** — [DejaVu Fonts license](https://dejavu-fonts.github.io/License.html)
  (a permissive Bitstream Vera / public-domain derivative); used for the arrow-key
  shapes (◀ ▶ ▲ ▼) and as a fallback.

> Note: Steam's own keyboard uses *Motiva Sans*, and Windows' *Segoe UI* is a
> close match — but both are proprietary and are **not** bundled or redistributed.
> Selawik is the open, metric-compatible stand-in.

## Building from source

Install Python 3.12+ dependencies, then build from the platform folder:

```bash
python -m pip install -r requirements.txt
cd windows && python build.py
cd linux && python build.py
```

Release automation is documented in [RELEASE.md](RELEASE.md).
