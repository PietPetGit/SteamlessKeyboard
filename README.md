# SteamlessKeyboard

Take full control of your PC with any gamepad. We've reimagined **and improved** upon Steam controllers PC controls, bringing it to every controller out there

## Features
- Works on Windows/Linux without Steam running
- Recreates Steam's on-screen keyboard, customisation and skins, keybind control, simultanious multi typing with every input method!
- Recreates the Steam Controller's default key bindings
- Translates controller input into a Xbox 360 gamepad (has lower latency than SISR and VIIPER)
- Smart gamepad mode — Smooth switching between gamepad and pc controls
- Use Keyboard on Windows Lock screen


![SteamlessKeyboard Screenshot](windows/assets/SteamlessKeyboard.png)
<sub>Press X to open the on-screen keyboard (or Ctrl + Alt + K)</sub>

---

## Installation

### 1. Install the ViGEmBus driver (windows only)

[github.com/nefarius/ViGEmBus/releases](https://github.com/nefarius/ViGEmBus/releases)

Run it and follow the prompts. You only need to do this once. The keyboard will still work without it, but gamepad mode will be unavailable.

### 2. Download SteamlessKeyboard

- Grab `SteamlessKeyboard.exe` from the [Releases page](https://github.com/PietPetGit/SteamlessKeyboard/releases)
- Drop it anywhere on your machine and run it

### 3. Configure settings (optional)
Right-click the <img src="windows/assets/SteamlessController_seethrough.png" width="20" style="vertical-align:middle"> tray icon:

|  |  |
|--------|-------------|
| **Startup → Start with Windows** | Auto-launch on boot |
| **Startup → When Steam Is Running → Pause** | Pause the listener while Steam is active |
| **Startup → When Steam Is Running → Exit** | Fully exit the app when Steam starts |
| **Startup → Advanced Settings** | Reveals the hidden **Advanced Settings** menu (see below) |
||
| **Gamepad Mode → Auto enable** | Automatically activate gamepad mode when a game is in the foreground |
| **Gamepad Mode → Always enable** | Keep gamepad mode on at all times (hold Home/Steam + stick to control the mouse) |
| **Gamepad Mode → Off** | Disable the virtual gamepad entirely |
||
| **Steam Controller/Switch Pro Controller** *(shown while one is connected)* | |
| → Keyboard Sticks/Mouse controls | Turn off to make the stick and mouse controls ignore the keyboard |
| → Keyboard Trigger Actuation | How far to pull the triggers to input keys on the keyboard — Default / Low |
| → PC mode Pointer Speed | Right stick mouse pointer speed — Low / Medium / High |
| → Vibration | Toggle rumble / haptic feedback |
||
| **Keyboard Skin → Size** | On-screen keyboard size — Small / Default / Full Screen |
| **Keyboard Skin → Transparent** | On-screen keyboard transparency — Off / Low / Medium / High |
| **Keyboard Skin →** *(theme list)* | Pick one of Steam's official on-screen keyboard color themes |
||
| **Advanced Settings** *(hidden until enabled via Startup → Advanced Settings)* | |
| → Block SteamInput Steam Controller grab | Stops SteamInput / Big Picture from opening when using media controls |
| → Block SteamInput Xbox Controller grab | Hides the virtual Xbox controller from SteamInput so Steam doesn't take it over |

## Optional: Lock-screen keyboard (windows only)

An optional add-on lets you use the Steam Controller as a keyboard on the
Windows **lock screen**, so you can type your password and sign in without a
physical keyboard. It is **not** part of core SteamlessKeyboard and carries a
real security trade-off — read
[windows/lockscreen-keyboard/README.md](windows/lockscreen-keyboard/README.md)
before installing.

## Optional: Nintendo Switch Pro Controller in gamepad mode (windows only)

**Only for the Switch Pro, to get gamepad mode working** The switch pro controller spams phantom input (buttons 1–8) to fix this you need to isntall **HidHide** driver — a one-time setup:

1. Install **HidHide** — `winget install Nefarius.HidHide` (or the [installer](https://github.com/nefarius/HidHide/releases)) — then **reboot**.
2. Open **HidHide Configuration Client** and:
   - **Applications** tab → **+** → add your `SteamlessKeyboard-windows.exe`.
   - **Devices** tab → tick **Nintendo Co., Ltd. Pro Controller**.
   - Enable **Enable device hiding** at the bottom.

Games now see only the virtual Xbox pad, while SteamlessKeyboard still reads the
controller. To use the Switch Pro normally again, untick **Enable device hiding**.

---

## Controller Keybinds (pc mode)

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
- Rumble implementation adapted from [CrazyCritic89/SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger)
- Battery-status parsing referenced from [samueltoken/Bloss_battery_indicator](https://github.com/samueltoken/Bloss_battery_indicator)
