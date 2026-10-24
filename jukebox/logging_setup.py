"""统一日志：控制台 + 按天滚动的文件（cache/logs/jukebox.log）。

为什么要有这个模块（v1.1）：
    旧版只有控制台窗口，日志随进程消失；用户报「连不上直播间」时，
    远程完全拿不到任何线索（连的是哪个房间、握手到哪一步、报的什么错）。
    现在同时写一份文件，出问题只要把 cache/logs/jukebox.log 发过来。

约定：
- 目录：<可写数据目录>/cache/logs/（打包后＝exe 同目录，只读时回退 %LOCALAPPDATA%）
- 文件：jukebox.log（当天），跨天自动切到 jukebox.log.YYYY-MM-DD，保留 14 天
- 格式：2026-10-24 18:20:01 [INFO] jukebox.danmaku: 已连接直播间 123
- 脱敏：SESSDATA / bili_jct / Cookie 串 / 二维码 key 等一律打码，日志可以随便发人
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time
from typing import Optional

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

BACKUP_DAYS = 14

_state = {"dir": "", "file": "", "level": logging.INFO}


class _RedactFilter(logging.Filter):
    """把日志里的登录凭据打码。

    需要脱敏的形态：
    - HTTP 头/参数里的 Cookie 串：SESSDATA=xxx; bili_jct=yyy;
    - 字段式：cookie=xxx、token=xxx、key=xxx、MUSIC_U=xxx、uin=xxx
    """

    _PATTERNS = [
        re.compile(r"(?i)\b(SESSDATA|bili_jct|DedeUserID__ckMd5|buvid3|buvid4|MUSIC_U|__csrf|qrsig|ptui_loginuin)\s*=\s*[^;,\s\"']+"),
        re.compile(r"(?i)\b(cookie|token|key|password|cookie_str)\s*[=:]\s*[^;,\s\"']{4,}"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        new = msg
        for pat in self._PATTERNS:
            new = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=***", new)
        if new != msg:
            record.msg = new
            record.args = ()
        return True


def log_dir() -> str:
    """日志目录（延迟导入 config，避免循环依赖）。"""
    return _state["dir"]


def log_file() -> str:
    return _state["file"]


def _resolve_dir() -> str:
    try:
        from .config import app_data_dir

        base = os.path.join(app_data_dir(), "cache", "logs")
    except Exception:
        base = os.path.join(os.getcwd(), "cache", "logs")
    try:
        os.makedirs(base, exist_ok=True)
        probe = os.path.join(base, ".w")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return base
    except Exception:
        import tempfile

        alt = os.path.join(tempfile.gettempdir(), "bili-jukebox-logs")
        try:
            os.makedirs(alt, exist_ok=True)
        except Exception:
            pass
        return alt


def setup(level: int = logging.INFO, to_console: bool = True) -> str:
    """安装日志处理器（幂等）。返回日志文件路径。

    必须在任何业务日志之前调用（run.py 第一行）。
    """
    if _state["file"]:
        return _state["file"]

    directory = _resolve_dir()
    path = os.path.join(directory, "jukebox.log")
    _state["dir"] = directory
    _state["file"] = path
    _state["level"] = level

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
    redact = _RedactFilter()

    # --- 文件（主产物，按天切分） ---
    try:
        fh = logging.handlers.TimedRotatingFileHandler(
            path, when="midnight", interval=1, backupCount=BACKUP_DAYS,
            encoding="utf-8", utc=False,
        )
        # 切出来的旧文件叫 jukebox.log.2026-10-24（默认是 .1 .2 这种序号）
        fh.suffix = "%Y-%m-%d"
        fh.setFormatter(fmt)
        fh.addFilter(redact)
        root.addHandler(fh)
    except Exception:
        pass  # 文件不可写也不能拖垮服务，控制台还有一份

    # --- 控制台（保持旧版可见性） ---
    if to_console:
        try:
            stream = sys.stdout
            try:
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
            ch = logging.StreamHandler(stream)
            ch.setFormatter(fmt)
            ch.addFilter(redact)
            root.addHandler(ch)
        except Exception:
            pass

    # 第三方噪音收敛：访问日志每秒一条会把文件刷爆，降级到 WARNING
    for noisy in ("uvicorn.access", "httpx", "httpcore", "websockets", "asyncio", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # bilibili_api 内部日志较多，只留警告
    logging.getLogger("bilibili_api").setLevel(logging.WARNING)

    log = logging.getLogger("jukebox")
    log.info("=" * 60)
    log.info("日志已启用：%s", path)
    return path


def install_exception_hooks() -> None:
    """未捕获异常（含子线程）也写进文件，避免「闪退且无痕迹」。"""
    log = logging.getLogger("jukebox.crash")

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            log.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    try:
        sys.excepthook = _hook
    except Exception:
        pass

    try:
        import threading

        def _thread_hook(args):
            try:
                log.critical(
                    "线程 %s 未捕获异常",
                    getattr(args.thread, "name", "?"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass

        threading.excepthook = _thread_hook  # type: ignore[assignment]
    except Exception:
        pass


def install_asyncio_handler(loop=None) -> None:
    """asyncio 任务里的异常（常见于弹幕/登录后台任务）写文件。"""
    import asyncio

    log = logging.getLogger("jukebox.asyncio")

    def _handler(_loop, context):
        msg = context.get("message") or "asyncio 未处理异常"
        exc = context.get("exception")
        try:
            if exc is not None:
                log.error("%s: %r", msg, exc, exc_info=exc)
            else:
                log.error("%s: %s", msg, context.get("future") or "")
        except Exception:
            pass

    try:
        (loop or asyncio.get_event_loop()).set_exception_handler(_handler)
    except Exception:
        pass


def read_tail(lines: int = 200, max_bytes: int = 256 * 1024, name: str = "") -> list[str]:
    """读取日志文件末尾若干行（给管理页「日志」用）。

    name 为空读当前 jukebox.log，否则读同目录下的滚动文件（jukebox.log.1 等）。
    """
    directory = _state["dir"]
    path = _state["file"]
    if name:
        base = os.path.basename(name)
        if not base.startswith("jukebox.log") or not directory:
            return []
        path = os.path.join(directory, base)
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢掉可能被截断的半行
            data = f.read().decode("utf-8", "replace")
        rows = data.splitlines()
        return rows[-lines:]
    except Exception:
        return []


def list_files() -> list[dict]:
    """日志目录下的文件列表（含滚动文件），按修改时间倒序。"""
    directory = _state["dir"]
    out: list[dict] = []
    if not directory or not os.path.isdir(directory):
        return out
    try:
        for name in os.listdir(directory):
            if not name.startswith("jukebox.log"):
                continue
            full = os.path.join(directory, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append(
                {
                    "name": name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                }
            )
    except Exception:
        return out
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out
