"""新功能测试：设置 GET/POST/reset、音源开关与排序、扫码登录、切直播间、
弹幕指定音源、歌词抓取、keep_state_on_exit 重启语义。

会重启服务两次：一次验证 keep_state=false 重启后空队列，一次验证 keep_state=true
重启后恢复续播。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import start_server, stop_server, restart_server, get, post, run_tests

_proc = None

DEFAULT_ACTIVE = ["bilibili", "qq", "kugou", "netease", "demo"]


def _reset():
    """清空队列与当前播放（不影响冷却等内存态）。"""
    post("/api/control", {"action": "clear"})
    st = get("/api/state")
    if st.get("current"):
        post("/api/control", {"action": "skip"})


# ---------------- 设置 ----------------

def test_settings_get():
    r = get("/api/settings")
    assert r.get("ok"), f"settings GET 失败: {r}"
    cfg = r["config"]
    assert cfg["jukebox"]["user_cooldown_seconds"] == 30, "默认用户冷却应为 30"
    assert r["port"] == 8788, "端口应为 8788"
    assert cfg.get("sources_logged_in") is not None, "应有登录态标记"
    assert cfg["sources"]["qq"].get("cookie", "") == "", "Cookie 不应明文回传"


def test_settings_post():
    r = post(
        "/api/settings",
        {"patch": {"jukebox": {"user_cooldown_seconds": 5, "allow_user_skip": True}}},
    )
    assert r.get("ok"), f"settings POST 失败: {r}"
    cfg = get("/api/settings")["config"]
    assert cfg["jukebox"]["user_cooldown_seconds"] == 5, "修改应热生效"
    assert cfg["jukebox"]["allow_user_skip"] is True
    ov = get("/api/settings")["overlay"]
    assert ov["jukebox"]["user_cooldown_seconds"] == 5, "覆盖层应记录修改"


def test_settings_reset():
    post("/api/settings/reset", {})
    cfg = get("/api/settings")["config"]
    assert cfg["jukebox"]["user_cooldown_seconds"] == 30, "reset 后应回到默认 30"
    assert cfg["jukebox"]["allow_user_skip"] is False


# ---------------- 音源开关与排序 ----------------

def test_source_toggle():
    post("/api/settings", {"patch": {"sources": {"active": ["bilibili", "qq", "kugou", "netease"]}}})
    st = get("/api/state")
    assert "demo" not in st["active_sources"], "关闭 demo 后不应出现在 active"
    # 恢复
    post("/api/settings", {"patch": {"sources": {"active": DEFAULT_ACTIVE}}})
    st = get("/api/state")
    assert st["active_sources"] == DEFAULT_ACTIVE


def test_source_order():
    new_order = ["demo", "qq", "kugou", "netease", "bilibili"]
    post("/api/settings", {"patch": {"sources": {"active": new_order}}})
    st = get("/api/state")
    assert st["active_sources"] == new_order, f"排序应生效: {st['active_sources']}"
    # 恢复
    post("/api/settings", {"patch": {"sources": {"active": DEFAULT_ACTIVE}}})


# ---------------- 扫码登录 ----------------

def test_login_state():
    r = get("/api/login/state")
    assert r.get("ok")
    for k in ("bilibili", "qq", "kugou", "netease"):
        assert r["platforms"][k]["supports_login"] is True, f"{k} 应支持登录"


def test_login_flow():
    # 轻量全流程（真实扫码需人工，这里验证二维码生成 + 轮询 + 取消）
    r = post("/api/login/start", {"source": "qq"})
    assert r.get("ok"), f"qq start 失败: {r}"
    s = r.get("session") or {}
    assert s.get("status") == "waiting"
    assert s.get("qr") or s.get("key"), "应有二维码或 key"
    st = post("/api/login/status", {"source": "qq"})
    sts = (st.get("session") or {}).get("status")
    assert sts in ("waiting", "scanned", "expired", "done"), f"异常状态 {sts}"
    post("/api/login/cancel", {"source": "qq"})


# ---------------- 切直播间 ----------------

def test_danmaku_reconnect():
    # live 模式无房间号 → 拒绝
    r = post("/api/danmaku/reconnect", {"mode": "live"})
    assert not r.get("ok"), "live 模式无房间号应拒绝"
    assert "房间号" in r.get("msg", "")
    # room_id 非整数 → 拒绝
    r = post("/api/danmaku/reconnect", {"room_id": "abc"})
    assert not r.get("ok"), "非整数房间号应拒绝"
    # mock 模式 + 房间号 → 成功
    r = post("/api/danmaku/reconnect", {"mode": "mock", "room_id": 12345})
    assert r.get("ok"), f"mock 切换失败: {r}"
    st = post("/api/danmu-status", {})
    assert st.get("mode") == "mock" and st.get("room_id") == 12345
    # 恢复
    post("/api/danmaku/reconnect", {"mode": "mock", "room_id": 0})


# ---------------- 弹幕指定音源 ----------------

def test_danmu_source_hint_demo():
    _reset()
    r = post("/api/mock-danmu", {"text": "点歌 demo 海阔天空", "uid": 1})
    assert r.get("ok"), f"demo 指定失败: {r}"
    st = get("/api/state")
    assert st["current"] and st["current"]["source"] == "demo", "应来自 demo 源"
    # 别名「测试」→ demo（换 uid 避开冷却）
    r = post("/api/mock-danmu", {"text": "点歌 测试 光辉岁月", "uid": 2})
    assert r.get("ok"), f"别名「测试」失败: {r}"
    st = get("/api/state")
    assert st["queue"] and st["queue"][0]["source"] == "demo", "别名指定应走 demo"


def test_danmu_source_hint_disabled():
    # 关闭 demo 后指定 demo → 拒绝
    post("/api/settings", {"patch": {"sources": {"active": ["bilibili", "qq", "kugou", "netease"]}}})
    r = post("/api/mock-danmu", {"text": "点歌 demo 稻香", "uid": 3})
    assert not r.get("ok"), "关闭的源指定应拒绝"
    assert "没有启用" in r.get("msg", ""), f"应为未启用提示: {r}"
    post("/api/settings", {"patch": {"sources": {"active": DEFAULT_ACTIVE}}})


def test_danmu_source_hint_network():
    # qq / b站 指定音源（需网络，容错：仅校验返回结构合法，成功时校验来源）
    for i, (hint, src) in enumerate((("qq", "qq"), ("b站", "bilibili"))):
        _reset()
        r = post("/api/mock-danmu", {"text": f"点歌 {hint} 七里香", "uid": 101 + i})
        assert isinstance(r.get("msg"), str), "应返回消息"
        if r.get("ok"):
            st = get("/api/state")
            found = bool(st["current"] and st["current"]["source"] == src)
            if not found:
                found = any(it["source"] == src for it in st["queue"])
            assert found, f"指定 {hint} 成功后歌曲来源应为 {src}"
        else:
            print(f"      {hint} 指定：网络/版权受限（{r['msg'][:40]}）")


# ---------------- 歌词 ----------------

def test_lyric():
    _reset()
    post("/api/mock-danmu", {"text": "点歌 demo 稻香", "uid": 11})
    st = get("/api/state")
    assert st["current"], "应先有播放歌曲"
    cur = st["current"]
    r = get(f"/api/lyric?source={cur['source']}&id={cur['id']}")
    # demo 源无歌词，但接口应返回结构化结果并带 song 字段
    assert "song" in r, "歌词接口应附 song 字段"
    assert r["song"]["id"] == cur["id"], "song 字段应匹配"
    assert isinstance(r.get("ok"), bool)
    # 未找到歌曲
    r2 = get("/api/lyric?source=demo&id=nonexist")
    assert r2.get("ok") is False, "未找到歌曲应返回 ok=False"


# ---------------- keep_state_on_exit 重启语义 ----------------

def _setup_keep_state_false():
    post("/api/settings/reset", {})
    post("/api/settings", {"patch": {"jukebox": {"keep_state_on_exit": False}}})
    _reset()
    post("/api/mock-danmu", {"text": "点歌 demo 海阔天空", "uid": 21})
    time.sleep(2.5)  # 等 tick_loop 落盘 state.json


def test_after_restart_keep_false():
    st = get("/api/state")
    assert st["current"] is None, "keep_state=false 重启后 current 应为空"
    assert st["queue"] == [], "keep_state=false 重启后队列应为空"


def _setup_keep_state_true():
    post("/api/settings", {"patch": {"jukebox": {"keep_state_on_exit": True}}})
    _reset()
    post("/api/mock-danmu", {"text": "点歌 demo 海阔天空", "uid": 22})
    time.sleep(2.5)


def test_after_restart_keep_true():
    st = get("/api/state")
    assert st["current"] is not None, "keep_state=true 重启后应恢复播放"
    assert st["current"]["source"] == "demo" and st["current"]["id"] == "demo-0", (
        f"恢复的歌曲应为 demo-0，实际 {st['current']}"
    )


if __name__ == "__main__":
    print("== 新功能 tests_features_v4 ==")
    _proc = start_server()
    # 清理上次运行可能遗留的持久化状态（keep_state=true 会恢复 current 导致歌曲被标记已点过）
    post("/api/settings", {"patch": {"jukebox": {"keep_state_on_exit": False}}})
    _proc = restart_server(_proc)  # 重启 → clear_state，得到干净起点
    post("/api/settings/reset", {})  # 恢复默认设置（keep_state 默认 true）
    _reset()

    failed = run_tests(
        [
            ("设置 GET", test_settings_get),
            ("设置 POST 热生效", test_settings_post),
            ("设置 reset 恢复默认", test_settings_reset),
            ("音源开关", test_source_toggle),
            ("音源排序", test_source_order),
            ("登录态查询", test_login_state),
            ("扫码登录 start+status+cancel", test_login_flow),
            ("切直播间", test_danmaku_reconnect),
            ("弹幕指定音源 demo", test_danmu_source_hint_demo),
            ("弹幕指定音源未启用拒绝", test_danmu_source_hint_disabled),
            ("弹幕指定音源 qq/b站（容错）", test_danmu_source_hint_network),
            ("歌词接口", test_lyric),
        ]
    )

    print("\n--- 重启验证 keep_state=false ---")
    _setup_keep_state_false()
    _proc = restart_server(_proc)
    failed += run_tests([("keep_state=false 重启空队列", test_after_restart_keep_false)])

    print("\n--- 重启验证 keep_state=true ---")
    _setup_keep_state_true()
    _proc = restart_server(_proc)
    failed += run_tests([("keep_state=true 重启恢复续播", test_after_restart_keep_true)])

    stop_server(_proc)
    sys.exit(1 if failed else 0)
