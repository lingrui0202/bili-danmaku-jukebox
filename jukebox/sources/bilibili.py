"""B站站内搜索源（DASH 音轨）。

坑点（SPEC 9.1）：
① 搜索标题带 HTML 高亮标签，需去标签；
② 老视频无 DASH 音轨（只有 durl/flv），resolve 返回 False 让流程试下一首；
③ 播放地址 Referer 必须 https://www.bilibili.com/（服务端代理转发时加）；
④ CDN 地址约 2 小时有效，保守 TTL 110 分钟；
⑤ 登录 Cookie 是 JSON 字符串，Credential.from_cookies 需要 json.loads 后的 dict。
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from ..models import Song
from .base import SongSource

URL_TTL = 110 * 60  # B站 CDN 约 2 小时，保守 110 分钟
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HTML_TAG = re.compile(r"<[^>]+>")
_NOISE_RE = re.compile(r"在听|在看|棚|试听|现场|MV|专辑|翻唱|【.*?】|\[.*?\]")


def _to_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    if not s or s in ("--", "-"):
        return 0
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        return int(float(s))
    except Exception:
        return 0


def _parse_duration(s) -> float:
    """"5:27" / "1:02:03" -> 秒。"""
    parts = str(s or "").split(":")
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return float(nums[0]) if nums else 0.0


def _clean_artist(s: str) -> str:
    s = s or ""
    m = _NOISE_RE.search(s)
    if m:
        s = s[: m.start()]
    return s.strip(" -—–·、,，")


def _split_title_artist(title: str, author: str, keyword: str = "") -> tuple[str, str]:
    """从 B站视频标题拆歌名/歌手。

    ① 《歌名》模式（容忍未闭合；《》内容与关键词不命中时按关键词回退截取；
       歌手取《》后 "- XXX" 或 XXX《 前词，过滤「在听看唱棚试现场MV专辑」等字样）；
    ② 无《》按 - 拆（多段取最短中段做歌手）；
    ③ 兜底整段。
    """
    raw = _HTML_TAG.sub("", title or "").strip()
    kw = (keyword or "").strip()
    kw_l = kw.lower()

    m = re.search(r"《([^》]*)", raw)
    if m:
        content = m.group(1).strip()
        if content and (not kw_l or kw_l in content.lower()):
            pre = raw[: m.start()].strip()
            post = raw[m.end() :].lstrip("》 -—–·").strip()
            artist = _clean_artist(post) or _clean_artist(pre)
            return content, artist
        if kw_l and kw_l in raw.lower():
            i = raw.lower().find(kw_l)
            return raw[i:].strip(), author
        return raw, author

    if "-" in raw:
        segs = [s.strip() for s in raw.split("-") if s.strip()]
        if len(segs) >= 2:
            artist = min(segs[1:], key=len)
            if segs[0] and artist:
                return segs[0], _clean_artist(artist)

    return raw, author


def _rank(item: dict, keyword: str) -> tuple:
    """智能排序：匹配度高、播放量高、收藏量高优先。"""
    title = (item.get("title") or "").lower()
    kw = (keyword or "").strip().lower()
    exact = 1 if kw and kw in title else 0
    play = _to_int(item.get("play"))
    favorites = _to_int(item.get("favorites"))
    like = _to_int(item.get("like"))
    review = _to_int(item.get("review"))
    return (exact, play + favorites * 10, like + review)


class BilibiliSource(SongSource):
    name = "bilibili"
    label = "B站搜索"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.cookie = ""
        self.credential = None
        self.logged_in = False

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        try:
            from bilibili_api import search as bili_search
        except Exception:
            return []
        try:
            result = await bili_search.search_by_type(
                keyword, search_type=bili_search.SearchObjectType.VIDEO
            )
        except Exception:
            return []
        items = (result or {}).get("result") or []
        max_results = int(self.config.get("max_results", 8))
        max_duration = int(self.config.get("max_duration", 900))

        ranked: list[tuple[Song, dict]] = []
        for it in items:
            try:
                if not it.get("title") or not it.get("bvid"):
                    continue
                duration = _parse_duration(it.get("duration"))
                if duration < 20 or duration > max_duration:
                    continue
                author = it.get("author") or ""
                t, a = _split_title_artist(it.get("title"), author, keyword)
                if not a:
                    a = author
                if not t:
                    continue
                cover = it.get("pic") or it.get("cover") or ""
                if cover.startswith("//"):
                    cover = "https:" + cover
                song = Song(
                    id=str(it.get("bvid")),
                    source=self.name,
                    title=t,
                    artist=a,
                    duration=duration,
                    cover=cover,
                    ref={
                        "bvid": str(it.get("bvid")),
                        "url": f"https://www.bilibili.com/video/{it.get('bvid')}",
                        "play": _to_int(it.get("play")),
                        "favorites": _to_int(it.get("favorites")),
                    },
                )
                ranked.append((song, it))
            except Exception:
                continue

        ranked.sort(key=lambda p: _rank(p[1], keyword), reverse=True)
        # (title, artist) 去重，排序后先到先得
        seen: set[tuple[str, str]] = set()
        out: list[Song] = []
        for song, _ in ranked:
            k = (song.title.lower(), song.artist.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(song)
        cap = max_results if not limit else min(max_results, int(limit))
        return out[:cap]

    async def resolve(self, song: Song) -> bool:
        try:
            from bilibili_api import video as bili_video
        except Exception:
            return False
        bvid = song.ref.get("bvid") or song.id
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            data = await v.get_download_url(page_index=0)
        except Exception:
            return False
        try:
            audios = ((data or {}).get("dash") or {}).get("audio") or []
            if not audios:
                return False  # 老视频只有 durl/flv → False，试下一首
            best = max(audios, key=lambda a: a.get("bandwidth") or 0)
            url = best.get("baseUrl") or best.get("base_url") or ""
            if not url:
                return False
            song.play_url = url
            song.play_url_expire = time.time() + URL_TTL
            song.stream_headers = {"Referer": "https://www.bilibili.com/", "User-Agent": UA}
            return True
        except Exception:
            return False

    # ---------- 登录 ----------
    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie or ""
        self.credential = None
        self.logged_in = False
        if cookie:
            try:
                from bilibili_api import Credential

                self.credential = Credential.from_cookies(json.loads(cookie))
                self.logged_in = True
            except Exception:
                self.credential = None
                self.logged_in = False

    def supports_login(self) -> bool:
        return True

    def status(self) -> dict:
        d = super().status()
        d.update(
            {
                "logged_in": self.logged_in,
                "max_results": self.config.get("max_results", 8),
                "max_duration": self.config.get("max_duration", 900),
            }
        )
        return d
