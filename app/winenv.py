# -*- coding: utf-8 -*-
"""WebView2 进程卫生：清掉「父进程已死」的僵尸 msedgewebview2。

背景（实测结论）：
  Lumen被强杀 / 崩溃退出后，它的 msedgewebview2 子进程会残留。残留进程占着
  WebView2 的全局资源，导致**下一次**启动卡死在 CreateCoreWebView2Environment，
  表现为窗口白屏、系统判定「未响应」。清理掉残留后立刻恢复正常——这一点用
  连续两次启动实验验证过（第 1 次 300 张卡片正常，第 2 次必卡，清完又好）。

所以：启动前清孤儿、退出时清自己的子进程，两头都堵住。
"""
import ctypes
import ctypes.wintypes as wt

TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = -1
TARGET = "msedgewebview2.exe"


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.CHAR * 260),
    ]


def _snapshot():
    """返回 [(pid, ppid, exe_name)]。"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    out = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID,
                        entry.th32ParentProcessID,
                        entry.szExeFile.decode("mbcs", "ignore")))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _alive(pid: int) -> bool:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wt.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _kill(pid: int) -> bool:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def orphans() -> list:
    """父进程已不存在的 msedgewebview2 进程。"""
    procs = _snapshot()
    live = {p for p, _, _ in procs}
    me = ctypes.windll.kernel32.GetCurrentProcessId()
    out = []
    for pid, ppid, exe in procs:
        if exe.lower() != TARGET or pid == me:
            continue
        if ppid not in live or not _alive(ppid):
            out.append(pid)
    return out


def clean_orphans() -> int:
    """杀掉孤儿 WebView2，返回清掉的数量。"""
    try:
        pids = orphans()
        n = 0
        for pid in pids:
            if _kill(pid):
                n += 1
        return n
    except Exception:
        return 0


def kill_my_children() -> int:
    """杀掉挂在当前进程下的 msedgewebview2（退出时调用，不留残骸）。"""
    try:
        me = ctypes.windll.kernel32.GetCurrentProcessId()
        n = 0
        for pid, ppid, exe in _snapshot():
            if ppid == me and exe.lower() == TARGET and _kill(pid):
                n += 1
        return n
    except Exception:
        return 0


def pid_alive(pid: int) -> bool:
    return _alive(pid)


def find_app_window(title: str = "Lumen"):
    """找运行中的Lumen主窗口（按标题 + WinForms 窗口类认）。"""
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        b = ctypes.create_unicode_buffer(200)
        u.GetWindowTextW(h, b, 200)
        c = ctypes.create_unicode_buffer(200)
        u.GetClassNameW(h, c, 200)
        if u.IsWindowVisible(h) and b.value.strip() == title and "WindowsForms10" in c.value:
            found.append(h)
        return True

    u.EnumWindows(cb, 0)
    return found[0] if found else None


def activate_app(title: str = "Lumen") -> bool:
    """把已运行的Lumen拉到前台。"""
    h = find_app_window(title)
    if not h:
        return False
    try:
        u = ctypes.windll.user32
        u.ShowWindow(h, 9)          # SW_RESTORE
        u.SetForegroundWindow(h)
        return True
    except Exception:
        return False
