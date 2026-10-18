"""测试公共辅助：服务生命周期管理 + HTTP 客户端 + 断言运行器。

脚本式测试（无框架），每个脚本可 `python tests/xxx.py` 直接运行，
服务由脚本内 subprocess 启动/停止（默认端口 8788）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8788
BASE = f"http://127.0.0.1:{PORT}"
LOG = os.path.join(ROOT, "tests", "server_test.log")


# ---------------- HTTP ----------------

def get(path: str, timeout: int = 90):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        body = r.read()
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            return json.loads(body.decode("utf-8"))
        return body


def post(path: str, body: dict, timeout: int = 120):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 服务生命周期 ----------------

def _wait_ready(timeout: int = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            get("/api/state", timeout=3)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def start_server() -> subprocess.Popen:
    """启动服务并等待就绪。"""
    kill_port()
    logf = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=ROOT,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    if not _wait_ready():
        proc.kill()
        raise RuntimeError("服务启动超时，请查看 tests/server_test.log")
    return proc


def stop_server(proc: "subprocess.Popen | None" = None):
    """停服（尽量优雅 terminate，兜底按端口强杀）。"""
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    time.sleep(0.5)
    kill_port()
    time.sleep(0.5)


def kill_port():
    """按端口强杀占用进程（Windows PowerShell）。"""
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-NetTCPConnection -LocalPort {PORT} -State Listen "
                "-ErrorAction SilentlyContinue | ForEach-Object "
                "{ Stop-Process -Id $_.OwningProcess -Force }",
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def restart_server(proc) -> subprocess.Popen:
    """停旧起新，模拟重启。"""
    stop_server(proc)
    time.sleep(1)
    return start_server()


# ---------------- 断言运行器 ----------------

def run_tests(tests: list) -> int:
    """tests: [(name, callable)]，返回失败数。支持同步与 async 测试函数。"""
    import asyncio

    passed = failed = 0
    failures = []
    for name, fn in tests:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failures.append((name, str(e)))
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failures.append((name, f"{type(e).__name__}: {e}"))
            failed += 1
    print(f"\n结果：{passed} 通过 / {failed} 失败（共 {passed + failed}）")
    if failures:
        print("失败项：")
        for n, e in failures:
            print(f"  - {n}: {e}")
    return failed
