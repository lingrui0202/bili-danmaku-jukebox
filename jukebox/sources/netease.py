"""网易云源（api/search/get 搜索 + weapi 加密的 player/url 播放）。

坑点（SPEC 9.4）：
① 必须用 pycryptodomex（Cryptodome 包）；
② RSA 是「明文大数直接模幂」（无 PKCS#1 填充），encSecKey 定长 256 位 hex；
③ 随机 key 用于第二层 AES，反转后 RSA；
④ 第一层 preset key 固定 0CoJUm6Qyw8W8jud；
⑤ 搜索 duration 是毫秒，/1000；
⑥ freeTrialInfo 非空 → 试听，end 秒数为试听结束点。
"""

from __future__ import annotations

import base64
import json
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

_NETEASE_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa7"
    "6d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46b"
    "ee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
_NETEASE_EXPONENT = "010001"
_NETEASE_PRESET_KEY = "0CoJUm6Qyw8W8jud"
_NETEASE_IV = b"0102030405060708"


def _aes_cbc_encrypt_b64(text: str, key: bytes, iv: bytes) -> str:
    from Cryptodome.Cipher import AES

    pad = 16 - len(text.encode()) % 16
    text = text + chr(pad) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, iv).encrypt(text.encode())).decode()


def _rsa_encrypt_hex(text: str) -> str:
    """text 无填充，直接大数模幂；返回 256 位定长 hex。"""
    num = int.from_bytes(text.encode(), "big")
    r = pow(num, int(_NETEASE_EXPONENT, 16), int(_NETEASE_MODULUS, 16))
    return format(r, "x").zfill(256)


def weapi_encrypt(params: dict) -> dict:
    """网易云 weapi 双重加密。"""
    key = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=16))
    text = json.dumps(params, separators=(",", ":"))
    enc1 = _aes_cbc_encrypt_b64(text, _NETEASE_PRESET_KEY.encode(), _NETEASE_IV)
    enc_text = _aes_cbc_encrypt_b64(enc1, key.encode(), _NETEASE_IV)
    enc_sec_key = _rsa_encrypt_hex(key[::-1])
    return {"params": enc_text, "encSecKey": enc_sec_key}


class NeteaseSource(SongSource):
    name = "netease"
    label = "网易云"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.cookie = self.config.get("cookie", "") or ""

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        url = "https://music.163.com/api/search/get"
        params = {"s": keyword, "type": 1, "offset": 0, "limit": int(limit)}
        headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            r = await self.http.get(url, params=params, headers=headers)
            data = r.json()
        except Exception:
            return []
        songs = (data or {}).get("result", {}).get("songs") or []
        out: list[Song] = []
        for s in songs:
            try:
                sid = s.get("id")
                if not sid or not s.get("name"):
                    continue
                artists = "、".join(a.get("name", "") for a in (s.get("artists") or []))
                cover = (s.get("album") or {}).get("picUrl") or ""
                if cover.startswith("http://"):
                    cover = "https://" + cover[len("http://") :]
                duration = float(s.get("duration") or 0) / 1000.0  # 毫秒 -> 秒
                fee = s.get("fee", 0)
                popularity = s.get("popularity", 0)
                out.append(
                    Song(
                        id=str(sid),
                        source=self.name,
                        title=str(s.get("name")),
                        artist=str(artists),
                        duration=duration,
                        cover=cover,
                        ref={"song_id": str(sid), "fee": fee, "popularity": popularity},
                    )
                )
            except Exception:
                continue
        # 排序：免费优先，再按热度降序
        def _sort_key(x: Song):
            free = 1 if x.ref.get("fee", 0) in (0, 8) else 0
            return (-free, -x.ref.get("popularity", 0))

        out.sort(key=_sort_key)
        return out

    async def resolve(self, song: Song) -> bool:
        sid = song.ref.get("song_id") or song.id
        url = "https://music.163.com/api/song/enhance/player/url/v1"
        params = {"ids": json.dumps([sid]), "level": "standard", "encodeType": "mp3"}
        headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            r = await self.http.get(url, params=params, headers=headers)
            data = r.json()
        except Exception:
            return False
        try:
            arr = (data or {}).get("data") or []
            if not arr:
                return False
            d0 = arr[0]
            play_url = d0.get("url") or ""
            if not play_url:
                return False
            song.play_url = play_url
            song.play_url_expire = time.time() + URL_TTL
            song.stream_headers = {"Referer": "https://music.163.com/", "User-Agent": UA}
            # 完整度：freeTrialInfo 非空 → 试听
            fti = d0.get("freeTrialInfo") or {}
            if fti:
                end = fti.get("end")
                if end:
                    song.trial = True
                    song.trial_note = f"试听片段（约{int(end)}秒）"
                else:
                    song.trial = True
                    song.trial_note = "VIP试听片段"
        except Exception:
            return False
        return True

    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie or ""

    def supports_login(self) -> bool:
        return True

    def status(self) -> dict:
        d = super().status()
        d["logged_in"] = bool(self.cookie)
        return d
