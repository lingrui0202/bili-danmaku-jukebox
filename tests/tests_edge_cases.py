"""边界/疑点测试：LRC 解析、weapi 格式、demo wav、local 目录扫描、网易云多候选解析。"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import run_tests
from jukebox.lyrics import parse_lrc
from jukebox.sources.demo import generate_wav
from jukebox.sources.local import LocalSource
from jukebox.sources.netease import weapi_encrypt
from jukebox.sources import SOURCES


# ---------- LRC 解析 ----------

def test_parse_lrc_basic():
    lines = parse_lrc("[ti:测试]\n[00:01.50]第一句\n[00:05]第二句")
    assert len(lines) == 2, f"应 2 行，实际 {len(lines)}"
    assert abs(lines[0]["t"] - 1.5) < 1e-6
    assert lines[0]["text"] == "第一句"
    assert lines[1]["text"] == "第二句"


def test_parse_lrc_multitag_shared():
    # 两个连续标签共享其后文本
    lines = parse_lrc("[00:10][00:20]共享文本")
    assert len(lines) == 1, f"应 1 行，实际 {len(lines)}"
    assert lines[0]["text"] == "共享文本"


def test_parse_lrc_word_by_word():
    lines = parse_lrc("[00:10]我[00:11]爱[00:12]你")
    assert [l["text"] for l in lines] == ["我", "爱", "你"]


def test_parse_lrc_millis_colon():
    lines = parse_lrc("[00:10:80]毫秒冒号")
    assert len(lines) == 1
    assert abs(lines[0]["t"] - 10.8) < 1e-6


def test_parse_lrc_empty():
    assert parse_lrc("") == []
    assert parse_lrc("[ti:无歌词]") == []


# ---------- weapi 格式 ----------

def test_weapi_format():
    enc = weapi_encrypt({"type": 1, "csrf_token": ""})
    assert len(enc["encSecKey"]) == 256, "encSecKey 应定长 256 位 hex"
    assert all(c in "0123456789abcdef" for c in enc["encSecKey"])
    import base64

    raw = base64.b64decode(enc["params"])
    assert len(raw) % 16 == 0, "params 应为 AES 分块长度"


# ---------- demo wav ----------

def test_demo_wav():
    wav = generate_wav(440, 0.5)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) > 44  # 头 + 数据


# ---------- local 目录扫描 ----------

def test_local_scan():
    with tempfile.TemporaryDirectory() as d:
        music = os.path.join(d, "music")
        os.makedirs(music)
        open(os.path.join(music, "Beyond - 海阔天空.mp3"), "wb").close()
        open(os.path.join(music, "周杰伦 - 七里香.flac"), "wb").close()
        src = LocalSource({"music_dir": music})
        songs = src._scan()
        assert len(songs) == 2, f"应扫到 2 首，实际 {len(songs)}"
        assert songs[0].artist == "Beyond" and songs[0].title == "海阔天空"
        assert songs[0].ref["path"].endswith(".mp3")
        res = asyncio.run(src.search("海阔天空"))
        assert len(res) == 1 and res[0].title == "海阔天空"


# ---------- 网易云多候选解析 ----------

def test_netease_resolve_multi_candidate():
    async def _inner():
        src = SOURCES["netease"]({})
        try:
            songs = await src.search("海阔天空", limit=3)
            assert songs, "网易云搜索无结果"
            # 多候选：遍历到可解析的
            resolved = None
            for s in songs:
                if await src.resolve(s) and s.play_url:
                    resolved = s
                    break
            assert resolved is not None, "网易云多候选解析全部失败"
            # duration 毫秒换算：应在 100~600 秒合理范围
            assert 100 <= resolved.duration <= 600, f"时长异常: {resolved.duration}"
            print(f"      解析成功: {resolved.title} | trial={resolved.trial} | url={resolved.play_url[:50]}")
        finally:
            try:
                await src.aclose()
            except Exception:
                pass

    return _inner


TESTS = [
    ("LRC 基本解析", test_parse_lrc_basic),
    ("LRC 多标签共享文本", test_parse_lrc_multitag_shared),
    ("LRC 逐字歌词", test_parse_lrc_word_by_word),
    ("LRC 毫秒冒号", test_parse_lrc_millis_colon),
    ("LRC 空输入", test_parse_lrc_empty),
    ("weapi 加密格式", test_weapi_format),
    ("demo 正弦波 wav", test_demo_wav),
    ("local 目录扫描", test_local_scan),
    ("网易云多候选解析", test_netease_resolve_multi_candidate),
]


if __name__ == "__main__":
    print("== 边界测试 tests_edge_cases ==")
    sys.exit(1 if run_tests(TESTS) else 0)
