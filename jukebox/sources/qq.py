"""QQ音乐源（client_search_cp 搜索 + vkey.GetVkeyServer 播放）。

坑点（SPEC 9.2）：
① 搜索/歌词接口必须带 Referer: https://y.qq.com/，否则 403/空；
② vkey 的 purl 为空 = 付费/无版权，返回 False；
③ payplay 语义：0=免费完整；1=试听；2=付费可试听；3=付费不可试听；1000+=免费但需登录；
④ resolve 后用音频头复核完整度（登录后可能从试听变完整）。
"""

from __future__ import annotations

import random
import time
from typing import Optional

from ..models import Song
from .base import SongSource

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
URL_TTL = 30 * 60

# payplay 试听说明
_PAYPLAY_NOTE = {1: "试听片段", 2: "付费歌曲", 3: "付费歌曲"}


class QQSource(SongSource):
    name = "qq"
    label = "QQ音乐"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.cookie = self.config.get("cookie", "") or ""

    # ---------- 搜索 ----------
    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"w": keyword, "format": "json", "n": int(limit), "p": 1}
        headers = {"Referer": "https://y.qq.com/", "User-Agent": UA}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            r = await self.http.get(url, params=params, headers=headers)
            data = r.json()
        except Exception:
            return []
        songs = ((data or {}).get("data") or {}).get("song", {}).get("list") or []
        out: list[Song] = []
        for s in songs:
            try:
                songmid = s.get("songmid")
                if not songmid or not s.get("songname"):
                    continue
                payplay = s.get("pay", {}).get("payplay", 0)
                interval = float(s.get("interval") or 0)
                albummid = s.get("albummid") or ""
                singer = (s.get("singer") or [{}])[0].get("name") or ""
                cover = (
                    f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg"
                    if albummid
                    else ""
                )
                trial = payplay in (1, 2, 3)
                note = _PAYPLAY_NOTE.get(payplay, "") if trial else ""
                out.append(
                    Song(
                        id=str(songmid),
                        source=self.name,
                        title=str(s.get("songname")),
                        artist=str(singer),
                        duration=interval,
                        cover=cover,
                        trial=trial,
                        trial_note=note,
                        ref={
                            "songmid": str(songmid),
                            "media_mid": s.get("media_mid") or "",
                            "albummid": albummid,
                            "albumname": s.get("albumname") or "",
                            "payplay": payplay,
                        },
                    )
                )
            except Exception:
                continue
        # payplay 升序（免费优先）
        out.sort(key=lambda x: x.ref.get("payplay", 0))
        return out

    # ---------- 播放地址（vkey） ----------
    async def resolve(self, song: Song) -> bool:
        songmid = song.ref.get("songmid") or song.id
        payload = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": str(random.randint(10**9, 10**10 - 1)),
                    "songmid": [songmid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0},
        }
        headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": UA,
            "Content-Type": "application/json",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            r = await self.http.post(
                "https://u.y.qq.com/cgi-bin/musicu.fcg", json=payload, headers=headers
            )
            data = r.json()
        except Exception:
            return False
        try:
            req0 = ((data or {}).get("req_0") or {}).get("data") or {}
            midurlinfo = req0.get("midurlinfo") or []
            purl = ""
            if midurlinfo:
                purl = midurlinfo[0].get("purl") or ""
            if not purl:
                return False  # 付费/无版权
            sip = (req0.get("sip") or ["http://aqqmusic.tc.qq.com/"])[0]
            song.play_url = sip + purl
            song.play_url_expire = time.time() + URL_TTL
            song.stream_headers = {"Referer": "https://y.qq.com/", "User-Agent": UA}
        except Exception:
            return False
        # 登录后可能从试听变完整，用音频头复核
        try:
            await self.check_trial(song)
        except Exception:
            pass
        return True

    # ---------- 登录 ----------
    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie or ""

    def supports_login(self) -> bool:
        return True

    def status(self) -> dict:
        d = super().status()
        d["logged_in"] = bool(self.cookie)
        return d
