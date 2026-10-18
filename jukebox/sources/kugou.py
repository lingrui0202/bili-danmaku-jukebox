"""酷狗源（song_search_v2 搜索 + trackercdn 播放）。

坑点（SPEC 9.3）：
① trackercdn 的 key = md5(hash + "kgcloudv2")，盐值不是通用签名盐；
② url 字段可能是 list，取第一个 http 开头元素；
③ time_len 是实际可播放时长，< duration - 10 判试听；
④ 登录时 URL 追加 &cookie=（URL 编码）。
"""

from __future__ import annotations

import hashlib
import time
from typing import Optional
from urllib.parse import quote

from ..models import Song
from .base import SongSource, _fmt_mmss

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
URL_TTL = 25 * 60


class KugouSource(SongSource):
    name = "kugou"
    label = "酷狗"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.cookie = self.config.get("cookie", "") or ""

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        url = "https://songsearch.kugou.com/song_search_v2"
        params = {"keyword": keyword, "page": 1, "pagesize": int(limit)}
        headers = {"User-Agent": UA}
        try:
            r = await self.http.get(url, params=params, headers=headers)
            data = r.json()
        except Exception:
            return []
        lists = (data or {}).get("data", {}).get("lists") or []
        out: list[Song] = []
        for s in lists:
            try:
                file_hash = s.get("FileHash")
                if not file_hash:
                    continue
                title = s.get("OriSongName") or s.get("SongName") or ""
                if not title:
                    continue
                artist = s.get("SingerName") or ""
                duration = float(s.get("Duration") or 0)
                out.append(
                    Song(
                        id=str(file_hash),
                        source=self.name,
                        title=str(title),
                        artist=str(artist),
                        duration=duration,
                        cover="",
                        ref={
                            "FileHash": str(file_hash),
                            "HQFileHash": str(s.get("HQFileHash") or ""),
                            "AlbumName": s.get("AlbumName") or "",
                        },
                    )
                )
            except Exception:
                continue
        return out

    async def resolve(self, song: Song) -> bool:
        hq = song.ref.get("HQFileHash") or ""
        normal = song.ref.get("FileHash") or song.id
        # 优先 HQ 音质 hash，失败回退普通 hash
        for h in filter(None, [hq, normal]):
            ok = await self._resolve_hash(song, h)
            if ok:
                return True
        return False

    async def _resolve_hash(self, song: Song, file_hash: str) -> bool:
        key = hashlib.md5((file_hash + "kgcloudv2").encode()).hexdigest()
        url = (
            f"https://trackercdn.kugou.com/i/v2/?hash={file_hash}&key={key}"
            f"&appid=1005&pid=2&cmd=25&behavior=play"
        )
        if self.cookie:
            url += f"&cookie={quote(self.cookie)}"
        headers = {"Referer": "https://www.kugou.com/", "User-Agent": UA}
        try:
            r = await self.http.get(url, headers=headers)
            data = r.json()
        except Exception:
            return False
        try:
            if (data or {}).get("status") != 1:
                return False
            urls = data.get("url") or []
            if isinstance(urls, str):
                urls = [urls]
            play_url = next((u for u in urls if isinstance(u, str) and u.startswith("http")), "")
            if not play_url:
                return False
            song.play_url = play_url
            song.play_url_expire = time.time() + URL_TTL
            song.stream_headers = {"Referer": "https://www.kugou.com/", "User-Agent": UA}
        except Exception:
            return False
        # 完整度：time_len（实际可播放时长）< duration - 10 → 试听
        try:
            time_len = float(data.get("time_len") or 0)
            if time_len > 0 and song.duration > 0 and time_len < song.duration - 10:
                song.trial = True
                song.trial_note = f"试听片段（约{_fmt_mmss(time_len)}）"
                return True
            await self.check_trial(song)
        except Exception:
            pass
        return True

    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie or ""

    def supports_login(self) -> bool:
        return True

    def status(self) -> dict:
        d = super().status()
        d["logged_in"] = bool(self.cookie)
        return d
