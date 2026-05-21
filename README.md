# SteamlessKeyboard

A lightweight on-screen keyboard for Steam Controller users. Press **Steam+X** to instantly bring up a virtual keyboard without leaving your game.

## Features

- **Fast Launch**: Opens in-process with Steam+X chord detection—no subprocess overhead
- **Controller-Driven**: Navigate and type entirely from your Steam Controller via D-Pad and A button
- **Non-Intrusive**: Window stays on top but doesn't steal focus from your target application
- **Portable**: Single-file `.exe` that works anywhere; settings persist next to the binary
- **Game-Friendly**: Includes modes to pause when Steam is running and optionally exit on Steam launch
- **Flexible Layout**: Keyboard layout configured via YAML; customize keys and positioning to your needs

## Installation

1. **Download the prebuilt executable**:
   - Grab `SteamlessKeyboard.exe` from the releases page
   - Drop it anywhere on your machine

2. **Configure startup behavior** (optional):
   - Right-click the tray icon to toggle:
     - "Start with Windows" – auto-launch on boot
     - "Disable While Steam Is Running" – pause listener when Steam is active
     - "Exit on Steam Launch" – fully exit the app when Steam starts

## Usage

### Launching the Keyboard

Press **Steam+X** on your Steam Controller to bring up the keyboard.

### Navigation & Typing

- **D-Pad**: Move the cursor around the virtual keyboard
- **A Button**: Press the key under the cursor
- **Shift+Move**: Cycle the keyboard window through 6 positions (down-mid, down-left, up-left, up-mid, up-right, down-right)

### Closing

- Click the X button on the keyboard, or
- Click the tray icon and select "Exit"

## Building from Source

### Prerequisites

```
python 3.8+
pip install PyYAML pillow pystray pynput psutil pysdl2 pysdl2-dll svglib reportlab PyInstaller sdl2dll
```

### Build Steps

```powershell
python build.py
```

The build script will:
1. Rasterize `keyboard-full2.svg` into a multi-resolution `.ico` file
2. Bundle the `data/` folder with keyboard layouts and glyphs
3. Run PyInstaller to generate `dist/SteamlessKeyboard.exe`

Output: `dist/SteamlessKeyboard.exe` (single-file, no-console executable)

## Architecture

### Key Components

- **tray.py**: Entry point; manages system tray, settings persistence, and Steam detection
- **adusk/adusk.py**: Main OSK renderer and event loop; manages window positioning and input
- **adusk/controller.py**: Steam Controller listener thread; detects D-Pad, A button, and Shift
- **adusk/vkb.py**: Virtual keyboard rendering; cursor positioning and key dispatch
- **adusk/screen.py**: SDL2 window and graphics rendering
- **adusk/state.py**: Shared state between controller thread and main loop (thread-safe queues)

### Data Flow

1. **tray.py** launches and sets up the system tray icon
2. **launcher_thread** watches for Steam+X and manages on-demand keyboard sessions
3. When triggered, **adusk.main()** is called in-process
4. **controller.input_thread** (daemon) watches the Steam Controller
5. D-Pad moves the cursor; A button fires the key under the cursor
6. **screen.py** renders and updates the display each frame

## Configuration

The keyboard layout is defined in `data/keyboard-layout.yaml`. You can customize:
- Key caps and labels
- Keyboard rows and columns
- Special key behaviors (backspace, spacebar, etc.)

## Credits & Dependencies

**Core Rendering**
- [PySDL2](https://github.com/a-hurst/pysdl2) – SDL2 Python bindings
- [SDL2, SDL2_ttf, SDL2_gfx](https://www.libsdl.org/) – Native graphics libraries
- [Pillow](https://python-pillow.org/) – Image processing (PNG/ICO conversion)

**Input & Integration**
- [pynput](https://github.com/moses-palmer/pynput) – Keyboard and mouse injection
- [pystray](https://github.com/moses-palmer/pystray) – System tray icon and menu
- [psutil](https://github.com/giampaolo/psutil) – Process detection (Steam.exe monitoring)

**Configuration & Build**
- [PyYAML](https://pyyaml.org/) – YAML configuration parsing
- [PyInstaller](https://pyinstaller.org/) – Python-to-EXE compilation
- [svglib](https://github.com/deeplook/svglib) – SVG rasterization
- [reportlab](https://www.reportlab.com/) – Graphics rendering

**Project Origins**
- Forked from [NOTtheMessiah/scosk](https://github.com/NOTtheMessiah/scosk)
- Uses the [steamcontroller](https://github.com/Sentdex/steamcontroller) driver
- Steam UI assets (glyphs, color palette) extracted from Steam's JavaScript bundles

## License

GNU LGPL v3 – See LICENSE file for details.

## Troubleshooting

**Keyboard doesn't appear:**
- Ensure the Steam Controller is paired and recognized by Steam
- Check that no other application is holding the device exclusively
- Try running `python adusk_launcher.py` from the source directory to debug

**Window steals focus:**
- On some systems, WS_EX_NOACTIVATE may not prevent focus theft. This is a Windows API limitation.

**Settings don't persist:**
- Ensure the `.exe` directory is writable (not Program Files without admin rights)

## Contributing

Pull requests welcome. For major changes, please open an issue first to discuss.

---

**Questions?** Open an issue on GitHub or check the source code comments for detailed explanations of tricky sections (window positioning, Focus control, controller state management).
