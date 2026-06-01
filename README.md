# SteamlessKeyboard
The goal is to **replicate how the steam controller (2026) behaves under Steam's default configuration** — so that with Steam closed the controller works like your used to

> ⚠️ **Requires the May 22, 2026 Steam Controller firmware update.** This program will not work with earlier firmware.

## Features
- Works on Windows without Steam running
- Recreates Steam's on-screen keyboard
- Recreates Steam controllers default key bindings
- Translates Steam Controller inputs into a Xbox 360 gamepad
- Smart gamepad mode — Smooth switching between gamepad and lizard mode


![SteamlessKeyboard Screenshot](windows/assets/SteamlessKeyboard.png)
<sub>Press X to open the on-screen keyboard (or Ctrl + Alt + K)</sub>

### To do
- Add emoji menu support
- Expanded real-world testing
- Linux release
- Multiple language support?

---

## Installation

### 1. Install the ViGEmBus driver

[github.com/nefarius/ViGEmBus/releases](https://github.com/nefarius/ViGEmBus/releases)

Run it and follow the prompts. You only need to do this once. The keyboard will still work without it, but gamepad mode will be unavailable.

### 2. Download SteamlessKeyboard

- Grab `SteamlessKeyboard.exe` from the [Releases page](https://github.com/PietPetGit/SteamlessKeyboard/releases)
- Drop it anywhere on your machine and run it

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
| **Debug** *(hidden — toggle Vibration 4× in a row to reveal/hide)* | **Block Steam controller grab** — made for stopping SteamInput / Big Picture from opening when using media controls |

## Optional: Lock-screen keyboard

An optional add-on lets you use the Steam Controller as a keyboard on the
Windows **lock screen**, so you can type your password and sign in without a
physical keyboard. It is **not** part of core SteamlessKeyboard and carries a
real security trade-off — read
[windows/lockscreen-keyboard/README.md](windows/lockscreen-keyboard/README.md)
before installing.

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
| <img src="windows/assets/sd_button_aux_md.png" width="32" align="middle"> + <img src="windows/data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Play / pause (click the left stick in) |

---

## Credits

- Forked from [archshift/adusk](https://github.com/archshift/adusk)
- Gamepad translation inspired by [ddeverill/SteamlessController](https://github.com/ddeverill/SteamlessController)
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Rumble implementation adapted from [CrazyCritic89/SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger)
