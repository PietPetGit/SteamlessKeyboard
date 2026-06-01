# SteamlessKeyboard
The goal is to **replicate how the steam controller (2026) behaves under Steam's default configuration** — so that with Steam closed the controller works like your used to

> ⚠️ **Requires the May 22, 2026 Steam Controller firmware update.** This program will not work with earlier firmware.

## Download

SteamlessKeyboard is distributed as per-platform builds attached to each GitHub
[Release](https://github.com/PietPetGit/SteamlessKeyboard/releases). There are no
per-platform branches — everything lives on `main`, and each Release carries a
prebuilt asset for each OS. Grab the one for your OS:

| Platform | Asset |
|----------|-------|
| **Windows** | `SteamlessKeyboard-<version>-windows.zip` |
| **Linux** *(experimental — OSK only)* | `SteamlessKeyboard-<version>-linux.tar.gz` |

Both assets are prebuilt and attached directly to each Release — the Windows
`.zip` and the Linux `.tar.gz`.

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
- Linux release *(experimental — see below)*
- Multiple language support?

---

## Linux (experimental, OSK only)

Only the on-screen keyboard is ported so far. The Windows tray, autostart,
Steam-running detection, and ViGEm virtual gamepad are not on Linux yet.
Targets X11 (or XWayland under a Wayland session — the no-focus-steal
behavior may degrade depending on the compositor).

System packages (Debian/Ubuntu names; translate for your distro):

```
sudo apt install python3-dev libsdl2-2.0-0 libsdl2-image-2.0-0 \
                 libsdl2-gfx-1.0-0 libhidapi-hidraw0
```

Python packages:

```
pip install pyinstaller pysdl2 pillow pynput hidapi pyyaml
```

To talk to the controller without root, drop a udev rule:

```
# /etc/udev/rules.d/99-steam-controller-2026.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="28de", ATTRS{idProduct}=="1304", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="28de", ATTRS{idProduct}=="1302", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload-rules && sudo udevadm trigger`.

Build the single-file binary:

```
cd linux
python build_linux.py
```

Produces `linux/dist/SteamlessKeyboard-linux.exe`. Usage:

```
./dist/SteamlessKeyboard-linux.exe               # open the OSK once
./dist/SteamlessKeyboard-linux.exe --watch       # daemon, Ctrl+Alt+K opens it
./dist/SteamlessKeyboard-linux.exe --controller  # + Steam+X chord on the controller
```

---

## Installation

### 1. Install the ViGEmBus driver

[github.com/nefarius/ViGEmBus/releases](https://github.com/nefarius/ViGEmBus/releases)

Run it and follow the prompts. You only need to do this once. The keyboard will still work without it, but gamepad mode will be unavailable.

### 2. Download SteamlessKeyboard

- Grab the `SteamlessKeyboard-<version>-windows.zip` asset from the [Releases page](https://github.com/PietPetGit/SteamlessKeyboard/releases)
- Unzip it anywhere on your machine and run `SteamlessKeyboard.exe`

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

## Repository layout

```
.
├─ windows/                     # Windows app: tray, ViGEm gamepad, lock-screen keyboard
│  ├─ adusk/  steamcontroller/  #   core (shared logic, Windows variants)
│  ├─ data/  assets/            #   bundled fonts, glyphs, images
│  ├─ tray.py  adusk_launcher.py
│  ├─ lockscreen-keyboard/      #   lock-screen OSK component + installer
│  └─ build.py  *.spec
├─ linux/                       # Linux app (experimental): OSK + AppIndicator tray
│  ├─ adusk/  steamcontroller/  #   core (shared logic, Linux variants)
│  ├─ data/  assets/
│  ├─ tray_linux.py  battery_probe.py
│  └─ build_linux.py
├─ requirements.txt             # deps for both platforms (PEP 508 markers)
├─ README.md  LICENSE  HACKING.md
```

Most of the core (`adusk/`, `steamcontroller/`) is shared, but a few modules
hard-fork per platform — e.g. `steamcontroller/uinput.py` is ViGEm on Windows
and `/dev/uinput` on Linux — so each platform folder keeps its own copy rather
than risking a runtime-merged module.

### Building from source

**Windows:**
```
cd windows
pip install -r ../requirements.txt
python build.py            # -> windows/dist/SteamlessKeyboard-windows.exe
```

**Linux:**
```
cd linux
pip install -r ../requirements.txt
python build_linux.py      # -> linux/dist/SteamlessKeyboard-linux.exe
```

---

## Credits

- Forked from [archshift/adusk](https://github.com/archshift/adusk)
- Gamepad translation inspired by [ddeverill/SteamlessController](https://github.com/ddeverill/SteamlessController)
- Virtual gamepad driver by [Nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus)
- Rumble implementation adapted from [CrazyCritic89/SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger)
