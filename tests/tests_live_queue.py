"""直播排队模块单元测试（纯内存，不启动服务）。

覆盖：口令归一化 / 内容解析 / 入队去重 / 灯牌插队与待用消耗 /
叫号与增删 / 插队开关 / 命令接口 / revision 与持久化。

运行：python tests/tests_live_queue.py
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jukebox.danmaku import Danmu  # noqa: E402
from jukebox.live_queue import LiveQueue, _normalize  # noqa: E402
from tests.common import run_tests  # noqa: E402

CFG = {
    "live_queue": {
        "keywords": ["排队"],
        "contents": ["通行正义", "轨外", "高危"],
        "gift_names": ["粉丝团灯牌"],
        "max_rows": 50,
        "max_users": 512,
    }
}


def _new(**overrides):
    cfg = {"live_queue": dict(CFG["live_queue"], **overrides)}
    tmp = tempfile.mkdtemp(prefix="lqtest_")
    return LiveQueue(cfg, os.path.join(tmp, "state.json"))


def _danmu(text: str, uid: int = 1, uname: str = "观众") -> Danmu:
    return Danmu(text=text, uid=uid, uname=uname)


# ---------------- 归一化 / 口令解析 ----------------

def test_normalize():
    assert _normalize("排队！") == "排队", _normalize("排队！")
    assert _normalize(" 排 队 ") == "排队"
    assert _normalize("排队，轨外") == "排队轨外"
    assert _normalize("") == ""


def test_match_content():
    q = _new()
    assert q._match("排队") == ""
    assert q._match("排队！") == "", "全角标点应被归一化"
    assert q._match("排队，轨外") == "轨外"
    assert q._match("排队 轨外") == "轨外"
    assert q._match("排队：高危") == "高危"
    assert q._match("点歌 海阔天空") is None


# ---------------- 入队 ----------------

def test_join_and_dedupe():
    q = _new()
    q.handle_danmu(_danmu("排队", uid=11, uname="张三"))
    assert len(q.queue) == 1 and q.queue[0]["name"] == "张三"
    rev = q.revision
    q.handle_danmu(_danmu("排队！", uid=11, uname="张三"))
    assert len(q.queue) == 1, "同一 uid 重复发送不应重复入队"
    assert q.revision == rev, "无变化时不应增加 revision"


def test_join_with_content_updates_existing():
    q = _new()
    q.handle_danmu(_danmu("排队", uid=12, uname="李四"))
    q.handle_danmu(_danmu("排队，轨外", uid=12, uname="李四"))
    assert len(q.queue) == 1
    assert q.queue[0]["content"] == "轨外", "已入队用户应更新内容"


# ---------------- 灯牌优先 ----------------

def test_gift_pending_then_priority():
    q = _new()
    msg = q.handle_gift(21, "王五", "粉丝团灯牌")
    assert msg and len(q.pending) == 1
    ok, msg = q.join("21", "王五", "bilibili", "")
    assert ok and "插队" in msg
    assert q.queue[0]["name"] == "王五" and q.queue[0]["priority"]
    assert not q.pending, "插队后应消耗待用记录"


def test_gift_moves_queued_user_to_front():
    q = _new()
    q.handle_danmu(_danmu("排队", uid=31, uname="甲"))
    q.handle_danmu(_danmu("排队", uid=32, uname="乙"))
    q.handle_gift(32, "乙", "粉丝团灯牌")
    assert q.queue[0]["name"] == "乙", "已排队用户送灯牌应插到第 1 位"


def test_gift_ignored_when_priority_off():
    q = _new()
    q.command("priority-off")
    assert q.handle_gift(41, "丙", "粉丝团灯牌") is None
    assert not q.pending


def test_other_gift_ignored():
    q = _new()
    assert q.handle_gift(42, "丁", "辣条") is None
    assert not q.pending


# ---------------- 叫号 / 增删 ----------------

def test_next_and_served_count():
    q = _new()
    for i in range(3):
        q.join(str(i + 1), f"U{i}", "test", "")
    ok, msg = q.next_user()
    assert ok and q.served_count == 1 and len(q.queue) == 2
    q.next_user()
    q.next_user()
    ok, _ = q.next_user()
    assert not ok, "空队列叫号应失败"


def test_del_add_clear():
    q = _new()
    q.join("1", "A", "test", "")
    q.join("2", "B", "test", "")
    q.manual_add("C", 1)
    assert [u["name"] for u in q.queue] == ["C", "A", "B"]
    ok, msg = q.del_rank(2)
    assert ok and [u["name"] for u in q.queue] == ["C", "B"]
    assert not q.del_rank(9)[0]
    q.clear()
    assert not q.queue
    assert q.clear_pending()[0]


def test_command_interface():
    q = _new()
    ok, msg, _ = q.command("match-排队，轨外")
    assert ok and "轨外" in msg
    ok, msg, changed = q.command("test-queue-小明")
    assert ok and changed and len(q.queue) == 1
    q.command("test-gift-小明")
    assert q.queue[0]["priority"], "已排队用户送灯牌后应优先"
    ok, msg, changed = q.command("contents-通行正义,轨外")
    assert ok and changed and q.contents == ["通行正义", "轨外"]
    assert q.command("不存在的命令")[0] is False
    assert q.command("help")[0] is True


# ---------------- 持久化 / revision ----------------

def test_persistence_roundtrip():
    cfg = {"live_queue": dict(CFG["live_queue"])}
    tmp = tempfile.mkdtemp(prefix="lqtest_")
    path = os.path.join(tmp, "state.json")
    q = LiveQueue(cfg, path)
    q.join("1", "持久化", "test", "")
    q.gift("2", "待用用户")
    q.font_size = 44
    q._changed()

    q2 = LiveQueue(cfg, path)
    assert [u["name"] for u in q2.queue] == ["持久化"]
    assert [p["name"] for p in q2.pending] == ["待用用户"]
    assert q2.font_size == 44


def test_revision_increases_on_change():
    q = _new()
    base = q.revision
    q.join("1", "A", "test", "")
    assert q.revision == base + 1
    q.join("1", "A", "test", "")  # 重复，无变化
    assert q.revision == base + 1


def test_clear_on_start():
    cfg = {"live_queue": dict(CFG["live_queue"])}
    tmp = tempfile.mkdtemp(prefix="lqtest_")
    path = os.path.join(tmp, "state.json")
    q = LiveQueue(cfg, path)
    q.join("1", "A", "test", "")
    q.command("autoclear-on")

    q2 = LiveQueue(cfg, path)
    assert not q2.queue, "开启开播自动清空后，重启应清空队列"


if __name__ == "__main__":
    sys.exit(run_tests([
        ("口令归一化", test_normalize),
        ("内容关键词解析", test_match_content),
        ("入队与去重", test_join_and_dedupe),
        ("重复入队更新内容", test_join_with_content_updates_existing),
        ("灯牌待用后插队", test_gift_pending_then_priority),
        ("已排队送灯牌插队", test_gift_moves_queued_user_to_front),
        ("取消插队忽略礼物", test_gift_ignored_when_priority_off),
        ("非优先礼物忽略", test_other_gift_ignored),
        ("叫号计数", test_next_and_served_count),
        ("增删清空", test_del_add_clear),
        ("命令接口", test_command_interface),
        ("持久化", test_persistence_roundtrip),
        ("revision 变化", test_revision_increases_on_change),
        ("开播自动清空", test_clear_on_start),
    ]))
