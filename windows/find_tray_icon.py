import ctypes, ctypes.wintypes, subprocess, sys, os, time

user32 = ctypes.windll.user32

def fw(cls, title=None): return user32.FindWindowW(cls, title)
def fc(p, cls): return user32.FindWindowExW(p, None, cls, None)

shell  = fw("Shell_TrayWnd")
notify = fc(shell, "TrayNotifyWnd")
pager  = fc(notify, "SysPager")
tb     = fc(pager, "ToolbarWindow32")

if not tb:
    print("ERROR: toolbar not found")
    sys.exit(1)

TB_BUTTONCOUNT = 0x0418
count = user32.SendMessageW(tb, TB_BUTTONCOUNT, 0, 0)
print(f"Notification area toolbar: {count} buttons")

# Get toolbar window rect
class RECT(ctypes.Structure):
    _fields_ = [("left",ctypes.c_int),("top",ctypes.c_int),("right",ctypes.c_int),("bottom",ctypes.c_int)]
r = RECT()
user32.GetWindowRect(tb, ctypes.byref(r))
print(f"Toolbar rect: {r.left},{r.top},{r.right},{r.bottom}")

# Use TB_GETITEMRECT for each button to find positions
TB_GETITEMRECT = 0x041D
class TBBUTTON(ctypes.Structure):
    _fields_ = [("iBitmap",ctypes.c_int),("idCommand",ctypes.c_int),
                ("fsState",ctypes.c_ubyte),("fsStyle",ctypes.c_ubyte),
                ("bReserved",ctypes.c_ubyte*6),("dwData",ctypes.c_size_t),
                ("iString",ctypes.c_ssize_t)]

pid = ctypes.wintypes.DWORD()
tid = user32.GetWindowThreadProcessId(tb, ctypes.byref(pid))
print(f"Toolbar process pid={pid.value} tid={tid}")

k32 = ctypes.windll.kernel32
MEM_COMMIT   = 0x1000
MEM_RESERVE  = 0x2000
PAGE_READWRITE = 0x04
MEM_RELEASE  = 0x8000
PROCESS_ALL  = 0x1F0FFF

h = k32.OpenProcess(PROCESS_ALL, False, pid.value)
buf = k32.VirtualAllocEx(h, None, 256, MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE)

for i in range(count):
    user32.SendMessageW(tb, TB_GETITEMRECT, i, buf)
    remote = (ctypes.c_int*4)()
    read = ctypes.c_size_t()
    k32.ReadProcessMemory(h, buf, remote, ctypes.sizeof(remote), ctypes.byref(read))
    # rect is relative to toolbar client area; convert to screen
    x_center = r.left + (remote[0] + remote[2]) // 2
    y_center = r.top  + (remote[1] + remote[3]) // 2
    print(f"  button {i}: client rect [{remote[0]},{remote[1]},{remote[2]},{remote[3]}]  screen center ({x_center},{y_center})")

k32.VirtualFreeEx(h, buf, 0, MEM_RELEASE)
k32.CloseHandle(h)
