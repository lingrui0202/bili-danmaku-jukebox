"""四平台扫码登录：二维码生成 + 轮询状态（真机扫码需人工）。

覆盖 /api/login/start、/api/login/status、/api/login/state、/api/login/cancel、/api/login/logout。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import start_server, stop_server, get, post, run_tests

PLATFORMS = ["bilibili", "qq", "kugou", "netease"]
_proc = None


def _setup():
    global _proc
    _proc = start_server()
    post("/api/settings/reset", {})
    post("/api/control", {"action": "clear"})


def _login_test(src):
    def _inner():
        r = post("/api/login/start", {"source": src})
        assert r.get("ok"), f"start 失败: {r}"
        s = r.get("session") or {}
        assert s.get("status") == "waiting", f"应为 waiting，实际 {s.get('status')}"
        assert s.get("qr") or s.get("key"), "应有二维码或 key"
        # 未扫码轮询
        st = post("/api/login/status", {"source": src})
        sts = (st.get("session") or {}).get("status")
        assert sts in ("waiting", "scanned", "expired", "done"), f"异常状态 {sts}"
        print(f"      {src} 二维码 OK（{sts}）")
        post("/api/login/cancel", {"source": src})

    return _inner


def test_login_state_supports():
    r = get("/api/login/state")
    assert r.get("ok")
    for k in PLATFORMS:
        assert r["platforms"][k]["supports_login"] is True, f"{k} 应支持登录"


def test_login_bad_source():
    r = post("/api/login/start", {"source": "nope"})
    assert not r.get("ok"), "未知平台应拒绝"


def test_login_cancel():
    post("/api/login/start", {"source": "qq"})
    r = post("/api/login/cancel", {"source": "qq"})
    assert r.get("ok")


def test_login_logout():
    r = post("/api/login/logout", {"source": "netease"})
    assert r.get("ok")


def _build_tests():
    t = [("登录态支持查询", test_login_state_supports)]
    for p in PLATFORMS:
        t.append((f"{p} 二维码生成+轮询", _login_test(p)))
    t += [
        ("未知平台拒绝", test_login_bad_source),
        ("取消登录", test_login_cancel),
        ("退出登录", test_login_logout),
    ]
    return t


if __name__ == "__main__":
    print("== 扫码登录 tests_login_qr ==")
    _setup()
    failed = run_tests(_build_tests())
    stop_server(_proc)
    sys.exit(1 if failed else 0)
