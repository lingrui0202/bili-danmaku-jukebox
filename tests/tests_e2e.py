"""端到端测试：HTTP API 全链路。

点歌 → 自动播放 → 音频流 → 用户冷却 → 每人在队上限 → 播放控制 →
热歌榜统计 → 搜索缓存 → 重启持久化（keep_state=true）。

为离线确定性，全程使用 demo 演示源。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import (
    start_server, stop_server, restart_server, get, post, run_tests, enable_requests,
)

_proc = None


def _reset():
    """清空队列与当前播放。"""
    post("/api/control", {"action": "clear"})
    st = get("/api/state")
    if st.get("current"):
        post("/api/control", {"action": "skip"})


# ---------------- 点歌 / 播放 / 流 / 热歌榜 / 缓存 ----------------

def test_request_and_autoplay():
    _reset()
    r = post("/api/request", {"keyword": "海阔天空"})
    assert r.get("ok"), f"点歌失败: {r}"
    assert "点歌成功" in r.get("msg", "")
    st = get("/api/state")
    cur = st["current"]
    assert cur and cur["id"] == "demo-0", f"应自动播放 demo-0，实际 {cur}"
    assert cur["playing"] is True
    assert st["total_requested"] == 1


def test_progress_advances():
    time.sleep(1.5)
    st = get("/api/state")
    assert st["current"]["progress"] >= 1.0, f"进度应推进: {st['current']['progress']}"


def test_stream():
    st = get("/api/state")
    url = st["current"]["stream_url"]
    data = get(url)  # 非 JSON → 返回原始字节
    assert isinstance(data, bytes) and data[:4] == b"RIFF", "demo 流应为 WAV 头"
    assert b"WAVE" in data[:12], "WAV 头应含 WAVE"


def test_hot_songs():
    st = get("/api/state")
    assert any(h["name"] == "海阔天空 - Beyond" for h in st["hot_songs"]), (
        f"热歌榜应含海阔天空: {st['hot_songs']}"
    )


def test_cache_stats():
    # 二次点同一关键词：走搜索缓存 + 去重
    r = post("/api/mock-danmu", {"text": "点歌 demo 海阔天空", "uid": 301})
    assert not r.get("ok"), "当前正在播，重复点应去重"
    assert "已经在队列里" in r.get("msg", ""), f"应提示已在队列: {r}"
    cs = get("/api/state")["cache_stats"]
    assert cs and cs.get("hits", 0) >= 1, f"二次点歌应命中缓存: {cs}"


# ---------------- 冷却 / 上限 ----------------

def test_user_cooldown():
    # 基线配置默认无冷却，用例内显式设置 30 秒并自行恢复
    post("/api/settings", {"patch": {"jukebox": {"user_cooldown_seconds": 30}}})
    r1 = post("/api/mock-danmu", {"text": "点歌 demo 光辉岁月", "uid": 100})
    assert r1.get("ok"), f"第一次点歌应成功: {r1}"
    r2 = post("/api/mock-danmu", {"text": "点歌 demo 稻香", "uid": 100})
    assert not r2.get("ok"), "冷却期内第二次点歌应被拒绝"
    assert "太频繁" in r2.get("msg", ""), f"应为冷却提示: {r2}"
    post("/api/settings", {"patch": {"jukebox": {"user_cooldown_seconds": 0}}})


def test_max_per_user_queue():
    # 临时关闭冷却、每人上限 2，隔离测试
    post("/api/settings", {"patch": {"jukebox": {"user_cooldown_seconds": 0, "max_per_user_queue": 2}}})
    post("/api/control", {"action": "clear"})
    post("/api/mock-danmu", {"text": "点歌 demo 夜曲", "uid": 200})   # demo-2
    post("/api/mock-danmu", {"text": "点歌 demo 稻香", "uid": 200})   # demo-3
    r = post("/api/mock-danmu", {"text": "点歌 demo 红豆", "uid": 200})  # demo-4 → 拒绝
    assert not r.get("ok"), "第 3 首应触发每人在队上限"
    assert "最多" in r.get("msg", ""), f"应为上限提示: {r}"
    # 恢复默认
    post("/api/settings", {"patch": {"jukebox": {"user_cooldown_seconds": 30, "max_per_user_queue": 3}}})


# ---------------- 播放控制 ----------------

def test_control_flow():
    st = get("/api/state")
    assert st["current"], "应有当前播放"

    # 暂停 / 恢复
    post("/api/control", {"action": "pause"})
    assert get("/api/state")["current"]["paused"] is True
    post("/api/control", {"action": "resume"})
    assert get("/api/state")["current"]["paused"] is False

    # 清空队列后点两首，测试 move / pin / remove / skip / clear
    post("/api/control", {"action": "clear"})
    post("/api/mock-danmu", {"text": "点歌 demo 光辉岁月", "uid": 210})  # demo-1
    post("/api/mock-danmu", {"text": "点歌 demo 稻香", "uid": 211})      # demo-3
    assert len(get("/api/state")["queue"]) == 2

    post("/api/control", {"action": "move", "index": 1, "new_index": 0})
    assert get("/api/state")["queue"][0]["id"] == "demo-3", "move 后队首应为稻香"

    post("/api/control", {"action": "pin", "index": 1})
    st = get("/api/state")
    assert st["queue"][0]["id"] == "demo-1" and st["queue"][0]["pinned"] is True, "pin 后队首应为光辉岁月"

    post("/api/control", {"action": "remove", "index": 0})
    st = get("/api/state")
    assert len(st["queue"]) == 1 and st["queue"][0]["id"] == "demo-3", "remove 后队列应只剩稻香"

    post("/api/control", {"action": "skip"})
    assert get("/api/state")["current"]["id"] == "demo-3", "skip 后应播放队首稻香"

    post("/api/control", {"action": "clear"})
    assert get("/api/state")["queue"] == []


# ---------------- 重启持久化 ----------------

def _setup_persist():
    post("/api/settings", {"patch": {"jukebox": {"keep_state_on_exit": True}}})
    _reset()
    post("/api/mock-danmu", {"text": "点歌 demo 夜曲", "uid": 320})  # demo-2（新鲜未播）
    time.sleep(2.5)  # 等 tick_loop 落盘 state.json


def test_after_restart_persist():
    st = get("/api/state")
    assert st["current"] and st["current"]["id"] == "demo-2", (
        f"重启后应恢复播放 demo-2，实际 {st['current']}"
    )
    assert st["total_requested"] >= 1, "total_requested 应持久化"
    assert any(h["name"] == "夜曲 - 周杰伦" for h in st["hot_songs"]), "热歌榜应持久化"


if __name__ == "__main__":
    print("== 端到端 tests_e2e ==")
    _proc = start_server()
    # 干净起点：清除上次可能遗留的持久化状态
    post("/api/settings", {"patch": {"jukebox": {"keep_state_on_exit": False}}})
    _proc = restart_server(_proc)
    post("/api/settings/reset", {})
    enable_requests()  # 「恢复默认」会关闭点歌，这里重新开启
    # 仅用 demo 源，保证离线确定性
    post("/api/settings", {"patch": {"sources": {"active": ["demo"]}}})
    _reset()

    failed = run_tests(
        [
            ("点歌并自动播放", test_request_and_autoplay),
            ("进度推进", test_progress_advances),
            ("音频流", test_stream),
            ("热歌榜统计", test_hot_songs),
            ("搜索缓存命中", test_cache_stats),
            ("用户冷却", test_user_cooldown),
            ("每人在队上限", test_max_per_user_queue),
            ("播放控制", test_control_flow),
        ]
    )

    print("\n--- 重启验证持久化 ---")
    _setup_persist()
    _proc = restart_server(_proc)
    failed += run_tests([("重启后恢复队列与热歌榜", test_after_restart_persist)])

    stop_server(_proc)
    sys.exit(1 if failed else 0)
