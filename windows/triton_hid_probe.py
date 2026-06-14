"""Triton (Steam Controller 2026) HID wire-format probe / firmware-diff tool.

Run this BEFORE a firmware update to capture a baseline of exactly what the
controller streams on its vendor HID interface, then run it AGAIN after the
update. Diffing the two text files tells us in seconds what the firmware
changed — which is the only thing that ever breaks this app's Steam Controller
support:

  * the input "state" report ID (history: 0x42 -> 0x45; a 0x47 timestamp
    variant also exists in Valve's headers), and/or
  * the report length (history: 54-byte USB -> 46-byte BLE), and/or
  * the byte offsets of buttons / triggers / sticks / trackpads.

The app accepts a *set* of state report IDs (steamcontroller.TRITON_INPUT_REPORT_IDS)
and parses fixed offsets after the id byte; if any of the three things above
changes, input dies until those are updated. This tool surfaces all three.

USAGE (from the windows/ directory, with Python on PATH):

    1. QUIT the tray app first (right-click tray icon -> Exit) so it isn't
       fighting this probe for the device / lizard state.
    2. python triton_hid_probe.py            # 12 s capture (default)
       python triton_hid_probe.py 20         # custom capture seconds
    3. While it counts down, MASH EVERYTHING: every button, both sticks (full
       circles + click), both triggers (soft + full), both trackpads (slide +
       hard click), all four grips/paddles, Steam + "..." buttons.
    4. It writes triton_probe_<timestamp>.txt next to this script and prints a
       summary. Save the file. Re-run after the firmware update and send both.

It restores desktop (lizard) mode on exit, so the controller works normally
afterwards. Read-only beyond the lizard enable/disable it already does itself.
"""

import sys
import time
import datetime
from collections import Counter

import hid

from steamcontroller import (
    _enumerate_data_interfaces,
    DISABLE_LIZARD_REPORT,
    ENABLE_LIZARD_REPORT,
    TRITON_INPUT_REPORT_IDS,
    TRITON_BATTERY_REPORT_ID,
    TRITON_WIRELESS_STATUS_IDS,
    present_product_ids,
)


def _fmt_hex(data):
    return " ".join(f"{b:02X}" for b in data)


class ReportStats:
    """Per-report-id accumulator: count, observed lengths, a sample frame, and
    the per-byte min/max across every frame (so bytes that ever changed — the
    buttons/axes/pads — stand out from constant header/padding bytes)."""

    def __init__(self):
        self.count = 0
        self.lengths = Counter()
        self.first = None
        self.byte_min = {}
        self.byte_max = {}

    def add(self, data):
        self.count += 1
        self.lengths[len(data)] += 1
        if self.first is None:
            self.first = list(data)
        for i, b in enumerate(data):
            if i not in self.byte_min:
                self.byte_min[i] = b
                self.byte_max[i] = b
            else:
                if b < self.byte_min[i]:
                    self.byte_min[i] = b
                if b > self.byte_max[i]:
                    self.byte_max[i] = b

    def changed_indices(self):
        """Byte indices whose value varied during the capture (skip byte 0, the
        report id). These are where live input lives."""
        return [i for i in sorted(self.byte_min)
                if i != 0 and self.byte_max[i] != self.byte_min[i]]


def _summarize_changed(stats):
    """Compress the changed-byte list into contiguous ranges for readability,
    e.g. [2,3,4,5, 10,11] -> '2-5, 10-11'. Those ranges should line up with the
    known field layout (buttons 2-5, triggers 6-9, sticks 10-17, pads 18-29)."""
    idx = stats.changed_indices()
    if not idx:
        return "(none changed — did you press anything? is this the input iface?)"
    ranges = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
            continue
        ranges.append((start, prev))
        start = prev = i
    ranges.append((start, prev))
    return ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in ranges)


def probe_interface(cand, seconds, out):
    """Probe ONE vendor interface. The dongle exposes up to 4 slots and only the
    paired/live controller streams; silent slots are skipped fast (a short
    discovery wait) so we don't burn the whole capture window on each. Returns
    True only for the live slot that actually streamed (caller then stops)."""
    path = cand["path"]
    iface = cand.get("interface_number")
    pid = cand.get("product_id", 0)

    try:
        dev = hid.device()
        dev.open_path(path)
    except Exception as e:
        line = f"--- interface {iface}  pid=0x{pid:04X}: open failed: {e!r}"
        print(line); out.append(line); out.append("")
        return False

    try:
        # Ask the firmware to stop pretending to be a keyboard/mouse so it
        # streams full game-input state reports on this interface.
        try:
            dev.send_feature_report(DISABLE_LIZARD_REPORT)
        except Exception:
            pass

        dev.set_nonblocking(0)

        # --- Discovery: is THIS the live slot? Wait briefly for any report. ---
        first = None
        discover_deadline = time.time() + 2.0
        while time.time() < discover_deadline:
            try:
                data = dev.read(64, 200)
            except Exception:
                data = None
            if data:
                first = data
                break
        if first is None:
            line = f"--- interface {iface}  pid=0x{pid:04X}: silent (unpaired slot) — skipping"
            print(line); out.append(line); out.append("")
            return False

        # --- Live slot: capture + analyze while the user mashes inputs. ---
        header = f"=== interface {iface}  pid=0x{pid:04X}  path={path!r}  [LIVE]"
        print(header); out.append(header)

        stats = {}            # report id -> ReportStats
        id_counts = Counter()
        id_counts[first[0]] += 1
        stats.setdefault(first[0], ReportStats()).add(first)

        print(f"    >>> MASH EVERY INPUT NOW for {seconds:g}s <<<")
        deadline = time.time() + seconds
        last_print = 0
        while time.time() < deadline:
            sec_left = int(deadline - time.time()) + 1
            if sec_left != last_print:
                last_print = sec_left
                print(f"    capturing... mash all inputs — {sec_left:>2d}s left", end="\r")
            try:
                data = dev.read(64, 200)
            except Exception as e:
                line = f"    read error: {e!r}"
                print(line); out.append(line)
                break
            if not data:
                continue
            rid = data[0]
            id_counts[rid] += 1
            stats.setdefault(rid, ReportStats()).add(data)
        print(" " * 60, end="\r")  # clear the countdown line

        summary = ", ".join(f"0x{r:02X}:{n}" for r, n in sorted(id_counts.items()))
        line = f"    report IDs seen: {summary}"
        print(line); out.append(line)

        for rid in sorted(stats):
            st = stats[rid]
            kind = _classify(rid)
            lens = ", ".join(f"{L}B x{n}" for L, n in sorted(st.lengths.items()))
            out.append("")
            out.append(f"    --- report 0x{rid:02X}  [{kind}]")
            out.append(f"        frames:  {st.count}")
            out.append(f"        lengths: {lens}")
            out.append(f"        first:   {_fmt_hex(st.first)}")
            if rid in TRITON_INPUT_REPORT_IDS or kind.startswith("STATE"):
                out.append(f"        changed bytes (live input): {_summarize_changed(st)}")
                # Per-changed-byte min..max helps spot which axis/button moved.
                changed = st.changed_indices()
                if changed:
                    detail = "  ".join(
                        f"[{i}]={st.byte_min[i]:02X}..{st.byte_max[i]:02X}"
                        for i in changed)
                    out.append(f"        ranges: {detail}")
            # Echo a compact per-report line to console too.
            print(f"    0x{rid:02X} [{kind}]: {st.count} frames, lengths {lens}")

        out.append("")
        return True
    finally:
        # Restore desktop (lizard) mode so the controller works normally after.
        try:
            dev.send_feature_report(ENABLE_LIZARD_REPORT)
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass


def _classify(rid):
    if rid in TRITON_INPUT_REPORT_IDS:
        return "STATE (input) — ACCEPTED"
    if rid == 0x47:
        return "STATE_TIMESTAMP 0x47 — NOT accepted (layout may differ!)"
    if rid == TRITON_BATTERY_REPORT_ID:
        return "battery 0x43"
    if rid in TRITON_WIRELESS_STATUS_IDS:
        return "wireless status"
    return "unknown"


def main():
    seconds = 12.0
    if len(sys.argv) > 1:
        try:
            seconds = float(sys.argv[1])
        except ValueError:
            pass

    out = []
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner = f"Triton HID probe — {stamp} — {seconds:g}s capture on the live controller"
    print(banner); out.append(banner)

    pids = present_product_ids()
    line = ("present Steam Controller PIDs: "
            + (", ".join(f"0x{p:04X}" for p in sorted(pids)) or "(none found!)"))
    print(line); out.append(line)

    out.append("EXPECTED BASELINE (current known-good): state report id in "
               + ", ".join(f"0x{r:02X}" for r in TRITON_INPUT_REPORT_IDS)
               + "; length ~46 (BLE) or 54 (USB); after the id byte — "
               "buttons=bytes 2-5, triggers=6-9, sticks=10-17, trackpads=18-29.")
    out.append("If input breaks after the firmware update, compare the NEW "
               "'report IDs seen' and 'changed bytes' against this file: a new "
               "state id -> add it to TRITON_INPUT_REPORT_IDS; a shorter length "
               "-> lower TRITON_INPUT_MIN_LEN; shifted 'changed' ranges -> the "
               "field offsets in _parse_triton moved.")
    out.append("")

    ifaces = _enumerate_data_interfaces()
    line = f"found {len(ifaces)} vendor interface(s)\n"
    print(line); out.append(line)
    if not ifaces:
        line = ("No Steam Controller vendor HID interface found. Is it powered/"
                "paired and not held exclusively by Steam? (Quit Steam + this "
                "app's tray, then retry.)")
        print(line); out.append(line)

    any_stream = False
    for cand in ifaces:
        if probe_interface(cand, seconds, out):
            any_stream = True
            break  # captured the live controller — stop, don't probe the rest

    if not any_stream and ifaces:
        out.append("NOTE: interfaces existed but none streamed. In lizard/"
                   "desktop mode an unpaired dongle slot stays silent; the live "
                   "controller's slot should have produced 0x45/0x42 frames.")

    fname = "triton_probe_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        print(f"\nwrote {fname} — SAVE THIS. Re-run after the firmware update and compare.")
    except Exception as e:
        print(f"\n(could not write {fname}: {e!r})")
        print("\n".join(out))


if __name__ == "__main__":
    main()
