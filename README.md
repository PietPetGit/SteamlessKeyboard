# SteamlessKeyboard
SteamlessKeyboard aims to make the Steam Controller (2026) more useful when Steam is not running.

The goal is to **replicate how the controller behaves under Steam's default configuration** — every button, chord, trackpad, and stick mapping — so that with Steam closed the controller works the same way it does with Steam running.

> ⚠️ **Requires the May 22, 2026 Steam Controller firmware update.** This program will not work with earlier firmware.

## Features
- Works on Windows without Steam running
- Recreates Steam's on-screen keyboard
- Translates Steam Controller inputs into a Xbox 360 gamepad
- Smart gamepad mode — Smooth switching between gamepad and lizard mode
- Desktop controls — left stick as arrow keys, right stick as mouse, plus handy face-button / back-paddle keys
- Audible chime when gamepad mode turns on and off


![SteamlessKeyboard Screenshot](assets/SteamlessKeyboard.png)

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
Right-click the <img src="assets/SteamlessController_seethrough.png" width="20" style="vertical-align:middle"> tray icon to toggle:
|  |  |
|--------|-------------|
| **Start with Windows** | Auto-launch on boot |
| **When Steam Is Running → Pause SteamlessKeyboard** | Pause the listener while Steam is active (lets Steam grab the controller) |
| **When Steam Is Running → Exit SteamlessKeyboard** | Fully exit the app when Steam starts |
| | *The two "When Steam Is Running" options are mutually exclusive — only one can be on.* |
| **Gamepad Mode → Auto enable** | Automatically activate gamepad mode when a game is in the foreground *(default)* |
| **Gamepad Mode → Always enable** | Keep gamepad mode on at all times (hold Steam + trackpad to control the mouse) |
| **Gamepad Mode → Off** | Disable the virtual gamepad entirely |
| **Vibration** | Toggle rumble / haptic feedback |
| **Debug** *(hidden — toggle Vibration 4× in a row to reveal/hide)* | Advanced HID options: **Block Steam controller grab**, **Block Steam Xbox Gamepad grab** |

---

## Controller Keybinds

### Desktop mode (controller as mouse & keyboard)
*Active when the on-screen keyboard is closed and you're not in gamepad mode.*

| Input | Action |
|-------|--------|
| <img src="assets/shared_button_x_md.png" width="32" align="middle"> | Open the on-screen keyboard |
| <img src="assets/shared_button_y_md.png" width="32" align="middle"> | Space |
| Left stick | Arrow keys (↑ ↓ ← →), auto-repeat while held |
| Right stick | Move the mouse cursor |
| R4 (right upper paddle) | Page Up |
| R5 (right lower paddle) | Page Down |
| L4 (left upper paddle) | Shift *(held)* |
| L5 (left lower paddle) | Windows key *(held)* |

> **Ctrl + Alt + K** (PC keyboard) opens or closes the on-screen keyboard — handy for previewing it without a controller.

### Hold the Steam button +…
| Input | Action |
|-------|--------|
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/sd_button_menu_md.png" width="32" align="middle"> | Alt+Tab — hold Steam to keep the switcher open; each VIEW press advances one slot |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/sd_rtrackpad_md.png" width="32" align="middle"> | Use the trackpad as a mouse while in gamepad mode |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_lstick_up_md.png" width="32" align="middle"> | Volume up — tap for one step, hold to ramp |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_lstick_down_md.png" width="32" align="middle"> | Volume down — tap for one step, hold to ramp |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_lstick_left_md.png" width="32" align="middle"> | Previous song |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_lstick_right_md.png" width="32" align="middle"> | Next song |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="data/images/glyphs/glyph_l3.png" width="32" align="middle"> | Play / pause (click the left stick in) |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_button_b_md.png" width="32" align="middle"> | Force-shutdown the foreground game (leaves the launcher running) |
| <img src="assets/sc_button_steam_md.png" width="32" align="middle"> + <img src="assets/shared_button_y_md.png" width="32" align="middle"> | Turn off the controller |


---

## Credits

- Forked from [archshift/adusk](https://github.com/archshift/adusk)
- Gamepad translation inspired by [ddeverill/SteamlessController](https://github.com/ddeverill/SteamlessController)
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Rumble implementation adapted from [CrazyCritic89/SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger)
