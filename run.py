"""启动入口：python run.py

默认 mock 弹幕模式，默认端口 8788。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# 确保项目根在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install_console_handler() -> None:
    """关掉命令行窗口（或 Ctrl+C）时确保进程一定退出，避免残留。

    默认情况下控制台程序关窗会收到 CTRL_CLOSE_EVENT 并由系统终结，
    但有时因后台线程/子进程残留而“看起来没关掉”。这里显式注册处理器，
    事件一到直接 os._exit(0)，保证窗口一关进程就没。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.BOOL, ctypes.DWORD)

        def _handler(event: int) -> bool:
            # 0=CTRL_C_EVENT, 2=CTRL_CLOSE_EVENT（关窗口）, 5=注销, 6=关机
            if event in (0, 2, 5, 6):
                try:
                    os._exit(0)
                except Exception:
                    pass
            return False

        kernel32.SetConsoleCtrlHandler(HandlerRoutine(_handler), True)
    except Exception:
        pass


def _parent_pid_of(pid: int) -> int:
    """查询指定进程的父进程 PID（Windows，NtQueryInformationProcess）。"""
    import ctypes
    from ctypes import wintypes

    class _PBI(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore
    ntdll = ctypes.windll.ntdll  # type: ignore
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 0
    try:
        info = _PBI()
        ret = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(
            wintypes.HANDLE(handle), 0, ctypes.byref(info),
            ctypes.sizeof(info), ctypes.byref(ret),
        )
        return int(info.InheritedFromUniqueProcessId or 0)
    except Exception:
        return 0
    finally:
        kernel32.CloseHandle(handle)


def _install_parent_watchdog() -> None:
    """父进程（启动它的命令行终端）结束后立即退出本程序，绝不留后台残留。

    - 命令行里 `python run.py`：父进程是终端 shell，关掉终端 → 程序退出；
    - 双击 exe：父进程是资源管理器（常驻），不受影响；
    - 单文件 exe 由 bootloader 释放子进程运行：除 bootloader 外再盯住
      shell 一层，任何一个先退出都会带走本进程。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import threading

        kernel32 = ctypes.windll.kernel32  # type: ignore
        SYNCHRONIZE = 0x00100000

        pids: list[int] = []
        ppid = os.getppid()
        if ppid and ppid > 0:
            pids.append(int(ppid))
        if getattr(sys, "frozen", False) and ppid:
            gp = _parent_pid_of(int(ppid))
            if gp and gp not in pids:
                pids.append(int(gp))
        handles = []
        for p in pids:
            h = kernel32.OpenProcess(SYNCHRONIZE, False, p)
            if h:
                handles.append(h)
        if not handles:
            return

        def _waiter() -> None:
            try:
                arr = (ctypes.c_void_p * len(handles))(*handles)
                # 等任一父进程句柄有信号（= 父进程结束）即退出
                kernel32.WaitForMultipleObjects(len(handles), arr, False, 0xFFFFFFFF)
            finally:
                try:
                    os._exit(0)
                except Exception:
                    pass

        threading.Thread(target=_waiter, daemon=True, name="parent-watchdog").start()
    except Exception:
        pass


def _banner(port: int, mode: str) -> None:
    print("=" * 56)
    print("  B站弹幕点歌机 + 直播排队  (bili-danmaku-jukebox)")
    print(f"  管理控制台 : http://127.0.0.1:{port}/web/console.html")
    print(f"  OBS 播放页 : http://127.0.0.1:{port}/obs")
    print(f"  OBS 歌词页 : http://127.0.0.1:{port}/stage")
    print(f"  OBS 排队页 : http://127.0.0.1:{port}/queue")
    print(f"  弹幕模式   : {mode}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)


def _open_console(port: int) -> None:
    """服务起来后自动打开管理页（后台等待端口就绪再开，避免抢跑白屏）。"""
    import threading
    import urllib.request
    import webbrowser

    url = f"http://127.0.0.1:{port}/web/console.html"

    def _run():
        for _ in range(30):
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _port_free(port: int) -> bool:
    """探测端口是否可绑定（uvicorn 在端口被占用时内部吞掉异常导致静默退出，
    因此先用一次试绑定判断，避免一启动就崩）。

    同时探测 0.0.0.0 与 127.0.0.1：Windows 下两者被视为不同地址，
    仅测 0.0.0.0 会漏掉“只占用 127.0.0.1”的情况。
    """
    import socket

    for addr in ("0.0.0.0", "127.0.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


_MUTEX_HANDLE = None


def _acquire_single_instance() -> bool:
    """命名互斥锁：防止重复启动（两个实例会共用 cache、重复入队、重复播放）。"""
    if sys.platform != "win32":
        return True
    global _MUTEX_HANDLE
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore
        handle = kernel32.CreateMutexW(None, False, "Local\\bili-jukebox-single-instance")
        if not handle:
            return True  # 拿不到锁句柄时不阻断启动
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle  # 进程存活期间一直持有
        return True
    except Exception:
        return True


def _open_running_instance(base_port: int) -> bool:
    """已有实例在运行：探测其端口并打开管理页。"""
    import urllib.request
    import webbrowser

    for p in range(base_port, base_port + 20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/state", timeout=0.6) as r:
                if r.status == 200:
                    url = f"http://127.0.0.1:{p}/web/console.html"
                    webbrowser.open(url, new=1)
                    print(f"[info] 程序已在运行，已为你打开管理页：{url}")
                    return True
        except Exception:
            continue
    return False


def main() -> None:
    _install_console_handler()
    _install_parent_watchdog()

    import uvicorn

    from jukebox.config import load_config, CONFIG_PATH
    from jukebox.server import app
    from jukebox.settings import SettingsStore

    settings = SettingsStore()
    cfg = load_config(CONFIG_PATH, settings.data)

    host = cfg["server"]["host"]
    base_port = int(cfg["server"]["port"])

    if not _acquire_single_instance():
        if not _open_running_instance(base_port):
            print("[warn] 程序已有实例在运行（未找到其端口），本次启动已取消。")
        return

    port = base_port
    while port < base_port + 20:
        if _port_free(port):
            _banner(port, cfg["danmaku"]["mode"])
            if cfg["server"].get("open_browser", True):
                _open_console(port)
            try:
                # 直接传 app 对象，便于 PyInstaller 静态分析收集依赖
                uvicorn.run(app, host=host, port=port, log_level="info")
            except KeyboardInterrupt:
                pass
            return
        print(f"[warn] 端口 {port} 被占用，自动尝试 {port + 1}")
        port += 1
    print("[error] 端口 8788~8799 均被占用，无法启动服务。请关闭占用端口的程序后重试。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 关键：任何启动异常都不要静默退出，
        # 否则双击 exe 时控制台一闪而过，用户看不到错误。
        traceback.print_exc()
        try:
            input("\n程序异常退出，请截图上面的报错，然后按 Enter 关闭窗口...")
        except Exception:
            pass

