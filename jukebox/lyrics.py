"""歌词模块：LRC 解析 + 各平台歌词抓取。

SPEC 6.12：
- parse_lrc 逐行解析，元信息标签跳过，一行多时间标签处理（连续标签组共享其后文本）。
- fetch_lyric 按 song.key() 会话级缓存（仅缓存成功）。
- None（无歌词）与空串（纯音乐）要区分（_wrap）。
"""

from __future__ import annotations

import base64
import os
import re
import time

from .models import Song

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_LRC_TIME = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_META = re.compile(
    r"^\[(ti|ar|al|by|offset|re|ve|hash|sign|qq|total|language|id)[:=]", re.I
)

# 会话级缓存：key -> (ts, result)，仅缓存成功结果
_LRC_CACHE: dict = {}


def _parse_time(m: "re.Match") -> float:
    mm = int(m.group(1))
    ss = int(m.group(2))
    frac = (m.group(3) or "0").ljust(3, "0")[:3]
    return mm * 60 + ss + int(frac) / 1000.0


def parse_lrc(text: str) -> list[dict]:
    """解析 LRC 文本 -> [{"t": 秒, "text": 文本}]。"""
    if not text:
        return []
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "[":
            continue
        if _LRC_META.match(line):
            continue
        tags = list(_LRC_TIME.finditer(line))
        if not tags:
            continue
        # 每个时间标签对应「其后到下一标签之前」的文本段；
        # 紧邻标签（中间无文本）不产生空条目，共享后续文本段。
        for i, m in enumerate(tags):
            start = m.end()
            end = tags[i + 1].start() if i + 1 < len(tags) else len(line)
            seg = line[start:end].strip()
            if not seg:
                continue
            entries.append({"t": _parse_time(m), "text": seg})
    entries.sort(key=lambda x: x["t"])
    return entries


def _wrap(lrc: "str | None", source: str) -> dict:
    """区分 None（无歌词）与空串（纯音乐）。"""
    if lrc is None:
        return {"ok": True, "lrc": None, "source": source, "lines": [], "msg": "没有找到歌词"}
    if lrc == "":
        return {"ok": True, "lrc": "", "source": source, "lines": [], "msg": "这首是纯音乐，没有歌词"}
    return {"ok": True, "lrc": lrc, "source": source, "lines": parse_lrc(lrc)}


# ---------------- 各平台抓取 ----------------

async def _netease(song: Song, http, cookies: dict) -> dict:
    sid = song.ref.get("song_id") or song.id
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    cookie = cookies.get("netease", "")
    if cookie:
        headers["Cookie"] = cookie
    r = await http.get(
        "https://music.163.com/api/song/lyric",
        params={"id": sid, "lv": -1, "tv": -1},
        headers=headers,
    )
    data = r.json()
    lyric = ((data or {}).get("lrc") or {}).get("lyric")
    if not lyric:
        if (data or {}).get("nolyric") or (data or {}).get("uncollected"):
            return _wrap("", "netease")  # 纯音乐
        return _wrap(None, "netease")
    return _wrap(lyric, "netease")


async def _qq(song: Song, http, cookies: dict) -> dict:
    mid = song.id
    # 必须带 Referer: y.qq.com，否则被拒
    r = await http.get(
        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
        params={"songmid": mid, "format": "json", "nobase64": 1, "g_tk": 0},
        headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
    )
    data = r.json()
    lyric = (data or {}).get("lyric") or ""
    if not lyric:
        return _wrap(None, "qq")
    return _wrap(lyric, "qq")


def _kugou_pick(candidates: list, target: float) -> dict:
    """候选里选时长最接近（diff<=3s 优先，等价于取最小 diff）。"""

    def diff(c):
        try:
            dur = float(c.get("duration", 0))
            if dur > 1000:  # 坑：kugou 歌词候选的 duration 是毫秒
                dur /= 1000.0
            return abs(dur - (target or 0))
        except (TypeError, ValueError):
            return float("inf")

    return min(candidates, key=diff)


def _kugou_decode(content: str) -> str:
    """base64 内容按 utf-8/gbk/gb18030 依次试解码，并去掉 UTF-8 BOM。"""
    try:
        raw = base64.b64decode(content)
    except Exception:
        return ""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc).lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


async def _kugou(song: Song, http, cookies: dict) -> dict:
    h = song.id  # FileHash
    headers = {"User-Agent": UA}
    # 1. 按 hash 搜索歌词
    r = await http.get(
        "https://lyrics.kugou.com/search",
        params={"hash": h, "clientver": "20489", "ver": 1, "man": "yes"},
        headers=headers,
    )
    candidates = (r.json() or {}).get("candidates") or []
    if not candidates:
        return _wrap(None, "kugou")
    best = _kugou_pick(candidates, song.duration or 0)
    accesskey = best.get("accesskey")
    iid = best.get("id")
    if not accesskey or iid is None:
        return _wrap(None, "kugou")
    # 2. 下载 LRC（坑：必须带 ver=1，否则 content 为空）
    r2 = await http.get(
        "https://lyrics.kugou.com/download",
        params={"accesskey": accesskey, "id": iid, "fmt": "lrc", "clientver": "20489", "ver": 1},
        headers=headers,
    )
    content = (r2.json() or {}).get("content") or ""
    if not content:
        return _wrap(None, "kugou")
    lrc = _kugou_decode(content)
    if not lrc:
        return _wrap(None, "kugou")
    return _wrap(lrc, "kugou")


async def _bilibili(song: Song, http, cookies: dict) -> dict:
    """B站无歌词接口，用 {title} {artist} 调网易云搜索，选歌名完全相等者。"""
    keyword = f"{song.title} {song.artist}".strip()
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    r = await http.get(
        "https://music.163.com/api/search/get",
        params={"s": keyword, "type": 1, "offset": 0, "limit": 10},
        headers=headers,
    )
    songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    for s in songs:
        if str(s.get("name") or "") == song.title:
            sid = str(s.get("id"))
            fake = Song(
                id=sid,
                source="netease",
                title=str(s.get("name")),
                artist=song.artist,
                duration=song.duration,
                ref={"song_id": sid},
            )
            return await _netease(fake, http, cookies)
    return _wrap(None, "bilibili")


async def _local(song: Song, http, cookies: dict) -> dict:
    path = song.ref.get("path") or ""
    if not path:
        return _wrap(None, "local")
    base = os.path.splitext(path)[0]
    candidates = [base + ".lrc", os.path.join(os.path.dirname(path), song.title + ".lrc")]
    for cand in candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8", errors="replace") as f:
                    return _wrap(f.read(), "local")
            except Exception:
                continue
    return _wrap(None, "local")


async def _demo(song: Song, http, cookies: dict) -> dict:
    return {"ok": False, "msg": "演示音源没有歌词"}


_HANDLERS = {
    "netease": _netease,
    "qq": _qq,
    "kugou": _kugou,
    "bilibili": _bilibili,
    "local": _local,
    "demo": _demo,
}


async def fetch_lyric(song: Song, http, cookies: dict | None = None) -> dict:
    """抓取歌词。成功结果按 song.key() 会话级缓存。"""
    cookies = cookies or {}
    key = song.key()
    cached = _LRC_CACHE.get(key)
    if cached:
        ts, res = cached
        if time.time() - ts < 3600:
            return res
    handler = _HANDLERS.get(song.source)
    if handler is None:
        return {"ok": False, "msg": f"不支持的音源 {song.source}"}
    try:
        res = await handler(song, http, cookies)
    except Exception as e:
        return {"ok": False, "msg": f"歌词抓取失败: {e}"}
    if res.get("ok"):
        _LRC_CACHE[key] = (time.time(), res)
    return res
