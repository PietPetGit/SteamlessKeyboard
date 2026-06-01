"""Linux diagnostic: scan every Steam Controller 2026 HID interface and report
which input report IDs each one streams, so we can see whether the 0x43 battery
report arrives on the interface we open for game input (0x45) or somewhere else
(or not at all). Run on the Linux machine, with the controller ON and charging
on the puck. Stop the SteamlessKeyboard tray/binary first (HID handle conflict).

    cd <this dir>
    python battery_probe.py

If a 'permission denied' shows for a path, you likely need the udev rule from
the README (idVendor 28de, idProduct 1304/1302, MODE 0660, TAG+ uaccess).
"""
import time
from collections import Counter

import hid

from steamcontroller import (
    _enumerate_data_interfaces, TRITON_BATTERY_REPORT_ID,
    TRITON_INPUT_REPORT_ID, TRITON_WIRELESS_STATUS_IDS, _parse_battery,
)

READ_SECONDS = 6.0

ifaces = _enumerate_data_interfaces()
print(f"found {len(ifaces)} data interface(s)\n")

for cand in ifaces:
    path = cand["path"]
    iface = cand.get("interface_number")
    pid = cand.get("product_id", 0)
    print(f"=== iface {iface}  pid=0x{pid:04X}  path={path!r}")
    try:
        dev = hid.device()
        dev.open_path(path)
    except Exception as e:
        print(f"    open failed: {e!r}\n")
        continue
    try:
        dev.set_nonblocking(0)
        ids = Counter()
        first_batt = None
        deadline = time.time() + READ_SECONDS
        while time.time() < deadline:
            try:
                data = dev.read(64, 200)
            except Exception as e:
                print(f"    read error: {e!r}")
                break
            if not data:
                continue
            ids[data[0]] += 1
            if data[0] == TRITON_BATTERY_REPORT_ID and first_batt is None:
                first_batt = _parse_battery(bytes(data))
        summary = ", ".join(f"0x{r:02X}:{n}" for r, n in sorted(ids.items()))
        print(f"    report IDs seen: {summary or '(none)'}")
        if TRITON_INPUT_REPORT_ID in ids:
            print(f"    -> game-input interface (0x{TRITON_INPUT_REPORT_ID:02X})")
        if TRITON_BATTERY_REPORT_ID in ids:
            print(f"    -> battery 0x43 streams here. parsed: {first_batt}")
        else:
            print("    -> no 0x43 battery report on this interface")
        link = [r for r in ids if r in TRITON_WIRELESS_STATUS_IDS]
        if link:
            print(f"    -> link-status report(s): {[hex(r) for r in link]}")
    finally:
        dev.close()
    print()
