"""启动入口：python run.py

默认 mock 弹幕模式，默认端口 8788。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback

# 确保项目根在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jukebox import __version__  # noqa: E402
from jukebox.logging_setup import install_exception_hooks, setup as setup_logging  # noqa: E402

# 日志必须最先装好：后面任何一步失败都要在文件里留下痕迹（v1.1 起）
LOG_PATH = setup_logging()
install_exception_hooks()
log = logging.getLogger("jukebox.run")


def _interactive() -> bool:
    """当前是否有真人看着控制台：重定向/自动化时不要弹框、不要等回车。"""
    if os.environ.get("JB_NO_GUI"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _fatal_box(title: str, text: str) -> None:
    """弹一个 Windows 系统消息框。

    为什么需要：主播是双击 exe 用的，出错时黑窗口经常还没看清就被关了
    （或者他根本不看），于是「用不了」永远没线索。异常路径必须弹出来。
    JB_NO_GUI=1 时不弹（自动化测试 / 无人值守用）。
    """
    if sys.platform != "win32" or not _interactive():
        return
    try:
        import ctypes

        MB_OK, MB_ICONERROR, MB_TOPMOST = 0x0, 0x10, 0x40000
        ctypes.windll.user32.MessageBoxW(
            None, text, title, MB_OK | MB_ICONERROR | MB_TOPMOST
        )
    except Exception:
        pass


def _wait_enter(tip: str = "\n按 Enter 关闭窗口...") -> None:
    """出错后停住窗口；JB_NO_GUI=1 时不停（避免卡住自动化）。"""
    if os.environ.get("JB_NO_GUI"):
        return
    try:
        input(tip)
    except Exception:
        pass


def _running_marker() -> str:
    try:
        from jukebox.config import app_data_dir

        return os.path.join(app_data_dir(), "cache", ".running")
    except Exception:
        return ""


def _previous_run_ok() -> bool:
    """上次是否正常退出：marker 还在 = 崩了 / 被强杀 / 直接关窗口。"""
    path = _running_marker()
    try:
        return not (path and os.path.exists(path))
    except Exception:
        return True


def _mark_running() -> None:
    path = _running_marker()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass


def _clear_running() -> None:
    path = _running_marker()
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _set_title(text: str) -> None:
    """把端口写进窗口标题：OBS 空白时，第一眼就能看出程序起在哪个端口。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass



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
                    _clear_running()  # 正常关窗不算异常退出，别下次启动吓人
                except Exception:
                    pass
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
    print(f"  B站弹幕点歌机 + 直播排队  (bili-danmaku-jukebox) v{__version__}")
    print(f"  管理控制台 : http://127.0.0.1:{port}/web/console.html")
    print(f"  OBS 播放页 : http://127.0.0.1:{port}/obs")
    print(f"  OBS 歌词页 : http://127.0.0.1:{port}/stage")
    print(f"  OBS 排队页 : http://127.0.0.1:{port}/queue")
    print(f"  弹幕模式   : {mode}")
    print(f"  运行日志   : {LOG_PATH}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)


def _is_elevated() -> bool:
    """当前进程的令牌**是否真的被提升**（UAC 已同意），而不是「用户属于管理员组」。

    坑（2026-10-24 实测）：`shell32.IsUserAnAdmin()` 在管理员账号下，
    即使进程是普通（已过滤）令牌也返回 **True**——它回答的是「你是不是管理员组的人」，
    不是「这个进程有没有提升」。拿它判断会永远走「管理员分支」，
    结果就是浏览器永远不弹（用户报的「不会自动拉起浏览器」）。
    正确做法：读令牌的 TokenElevation。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class TOKEN_ELEVATION(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wintypes.DWORD)]

        TOKEN_QUERY = 0x0008
        TOKEN_ELEVATION_CLASS = 20

        handle = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(handle)
        ):
            return False
        try:
            info = TOKEN_ELEVATION()
            size = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                handle, TOKEN_ELEVATION_CLASS, ctypes.byref(info),
                ctypes.sizeof(info), ctypes.byref(size),
            )
            return bool(ok and info.TokenIsElevated)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _launch_url(url: str, elevated: bool) -> str:
    """用最合适的方式打开管理页，返回实际用的方式（写日志用）。

    提升权限的进程直接 `webbrowser.open()` 会拉起一个「提升的浏览器实例」，
    Edge/Chrome 会弹「现有实例正在以提升的权限运行，是否重启」——所以提升时
    把 URL 交给 **非提升的 explorer.exe** 去开（资源管理器是普通权限，
    由它调起默认浏览器就不会冲突）。
    """
    import subprocess
    import webbrowser

    if elevated:
        try:
            subprocess.Popen(["explorer.exe", url], close_fds=True)
            return "explorer"
        except Exception as e:
            log.info("交给 explorer 打开失败（%s），改用 webbrowser", e)
    try:
        webbrowser.open(url, new=1, autoraise=True)
        return "webbrowser"
    except Exception as e:
        log.info("自动打开浏览器失败（%s）", e)
        return ""


def _open_console(port: int) -> None:
    """服务起来后自动打开管理页（后台等端口就绪再开，避免抢跑白屏）。

    v1.1.1：修掉两件事——
    1. 提升判定用错 API（IsUserAnAdmin），导致浏览器从不自动开；
    2. 提升时不再「跳过」，而是交给 explorer.exe 打开（不会弹权限冲突框）。
    """
    import threading
    import urllib.request

    url = f"http://127.0.0.1:{port}/web/console.html"
    elevated = _is_elevated()

    def _run():
        for _ in range(30):
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        how = _launch_url(url, elevated)
        if how:
            log.info("已自动打开管理页（%s，提升=%s）：%s", how, elevated, url)
        else:
            print(f"[提示] 没能自动打开浏览器，请手动访问：{url}")
            log.info("自动打开浏览器失败，请手动访问 %s", url)

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
    """命名互斥锁：防止重复启动（两个实例会共用 cache、重复入队、重复播放）。

    实现上**不用 GetLastError 判断 183**：实测同一个进程里，
    `CreateMutexW` 拿到句柄 504、而 `ctypes.get_last_error()` 读出来是 0
    （另一个独立脚本同样调用却正确读到 183），结果就是重复启动被放行，
    两个实例抢 8788、抢 cache —— 表现正是「第二次打开就怪怪的」。
    改成先用 `OpenMutexW` 探测：能打开就是已经有人在跑。
    """
    if sys.platform != "win32":
        return True
    global _MUTEX_HANDLE
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        SYNC = 0x00100000
        name = "Local\bili-jukebox-single-instance"

        existing = kernel32.OpenMutexW(SYNC, False, name)
        if existing:
            kernel32.CloseHandle(existing)
            return False

        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True  # 拿不到锁句柄时不阻断启动
        _MUTEX_HANDLE = handle  # 进程存活期间一直持有
        return True
    except Exception:
        return True


def _open_running_instance(base_port: int) -> bool:
    """已有实例在运行：探测其端口并打开管理页。"""
    import urllib.request

    for p in range(base_port, base_port + 20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/state", timeout=0.6) as r:
                if r.status == 200:
                    url = f"http://127.0.0.1:{p}/web/console.html"
                    how = _launch_url(url, _is_elevated())
                    log.info("已有实例在运行（端口 %s），打开管理页：%s（%s）", p, url, how or "失败")
                    print(f"[info] 程序已在运行，已为你打开管理页：{url}")
                    return True
        except Exception:
            continue
    return False


def main() -> None:
    _install_console_handler()
    _install_parent_watchdog()

    import uvicorn

    from jukebox.config import app_data_dir, load_config, CONFIG_PATH
    from jukebox import server as server_module
    from jukebox.server import app
    from jukebox.settings import SettingsStore

    log.info("启动 bili-jukebox v%s（frozen=%s）", __version__, bool(getattr(sys, "frozen", False)))
    log.info("数据目录：%s", app_data_dir())
    log.info("配置文件：%s", CONFIG_PATH)

    settings = SettingsStore()
    cfg = load_config(CONFIG_PATH, settings.data)

    host = cfg["server"]["host"]
    base_port = int(cfg["server"]["port"])
    dm = cfg.get("danmaku", {})
    log.info(
        "弹幕配置：mode=%s room_id=%s host=%s port=%s",
        dm.get("mode"), dm.get("room_id"), host, base_port,
    )

    last_ok = _previous_run_ok()
    if not last_ok:
        log.warning("上次没有正常退出（崩溃或被强杀），本次是新起的")
    server_module.RUNTIME["last_exit_ok"] = last_ok
    server_module.RUNTIME["pid"] = os.getpid()

    if not _acquire_single_instance():
        if not _open_running_instance(base_port):
            msg = (
                "程序好像已经在运行，但找不到它的管理页面（8788~8807 都没响应）。\n\n"
                "最常见的原因：上一个进程没退干净，卡住了。\n"
                "处理：打开任务管理器 → 详细信息 → 把所有 bili-jukebox.exe 结束掉，再重新双击。\n\n"
                f"日志文件：{LOG_PATH}"
            )
            print(msg)
            log.error("单实例锁被占用，但 8788~8807 全部无响应：疑似上次的残留进程")
            _fatal_box("点歌机没启动：有残留进程", msg)
            _wait_enter()
        return

    port = base_port
    while port < base_port + 20:
        if _port_free(port):
            # 告诉服务层「真正用的端口」：端口顺延后控制台/OBS 地址才不会给错
            server_module.RUNTIME["port"] = port
            server_module.RUNTIME["host"] = host
            _set_title(f"B站弹幕点歌机 v{__version__} · 端口 {port}")
            _mark_running()
            _banner(port, cfg["danmaku"]["mode"])
            log.info("服务地址：http://%s:%s/web/console.html", host, port)
            if cfg["server"].get("open_browser", True):
                _open_console(port)
            try:
                # 直接传 app 对象，便于 PyInstaller 静态分析收集依赖。
                # log_config=None：不替换 logging 配置，让 uvicorn 的日志流进
                # logging_setup 装好的控制台 + 文件两个 handler（v1.1）。
                uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)
            except KeyboardInterrupt:
                pass
            finally:
                _clear_running()
                log.info("服务已停止")
            return
        print(f"[warn] 端口 {port} 被占用，自动尝试 {port + 1}")
        log.warning("端口 %s 被占用，尝试下一个", port)
        port += 1
    msg = (
        f"端口 {base_port}~{base_port + 19} 全被占用了，服务起不来。\n\n"
        "OBS 里会是一片空白。请关掉占用端口的程序（常见是上次没退干净的自己）后重试。\n\n"
        f"日志文件：{LOG_PATH}"
    )
    print("[error] " + msg)
    log.error("端口 %s~%s 均被占用，启动失败", base_port, base_port + 19)
    _fatal_box("点歌机启动失败：端口被占满", msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 关键：任何启动异常都不要静默退出，
        # 否则双击 exe 时控制台一闪而过，用户看不到错误。
        log.critical("启动失败", exc_info=True)
        traceback.print_exc()
        try:
            print(f"\n日志已写入：{LOG_PATH}")
        except Exception:
            pass
        _fatal_box(
            "点歌机启动失败",
            f"程序启动时出错，已经把详细原因写进日志：\n{LOG_PATH}\n\n"
            f"请把这个日志文件发给我（或在控制台「运行日志 → 导出诊断包」）。\n\n"
            f"{traceback.format_exc()[-800:]}",
        )
        _wait_enter()

