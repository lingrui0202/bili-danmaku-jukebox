"""6 源 search/resolve 回归（不依赖服务，直接调用源实现）。

local 源无 music 目录时 search 为空属预期，标记为 SKIP（不计失败）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.common import run_tests
from jukebox.sources import SOURCES

KW = "海阔天空"


def _source_test(name: str, kw: str, resolve_required: bool = True):
    async def _inner():
        src = SOURCES[name]({})
        try:
            songs = await src.search(kw, limit=5)
            if not songs:
                raise AssertionError(f"{name} 搜索无结果")
            if resolve_required:
                ok = False
                first_url = ""
                for s in songs:
                    try:
                        if await src.resolve(s) and s.play_url:
                            ok = True
                            first_url = s.play_url
                            break
                    except Exception:
                        continue
                assert ok, f"{name} resolve 全部失败"
                print(f"      首条: {songs[0].display()} | play_url: {first_url[:60]}")
            else:
                print(f"      首条: {songs[0].display()}")
        finally:
            try:
                await src.aclose()
            except Exception:
                pass

    return _inner


def local_expected_empty():
    """local 源：无 music 目录属预期失败，有目录则正常回归。"""
    src = SOURCES["local"]({})
    d = src.music_dir
    if not os.path.isdir(d):
        print(f"      无 music 目录（{d}），local 回归预期跳过")
        return  # 视为通过（SKIP）
    # 有目录则跑真实回归
    return _source_test("local", "", resolve_required=True)


TESTS = [
    ("demo 源 search+resolve", _source_test("demo", KW)),
    ("bilibili 源 search+resolve", _source_test("bilibili", KW)),
    ("qq 源 search+resolve", _source_test("qq", KW)),
    ("kugou 源 search+resolve", _source_test("kugou", KW)),
    ("netease 源 search+resolve", _source_test("netease", KW)),
    ("local 源（无目录预期跳过）", local_expected_empty),
]


if __name__ == "__main__":
    print("== 源回归 tests_regression_sources ==")
    sys.exit(1 if run_tests(TESTS) else 0)
