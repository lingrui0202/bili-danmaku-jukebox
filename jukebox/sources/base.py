"""歌曲源抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from ..models import Song


def _fmt_mmss(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}分{s:02d}秒"


class SongSource(ABC):
    name: str = "base"
    label: str = "基础源"

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._client: Optional[httpx.AsyncClient] = None

    # ---------- HTTP 客户端（每源一个，懒创建） ----------
    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15, read=60), follow_redirects=True
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ---------- 抽象接口 ----------
    @abstractmethod
    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        """搜索歌曲，返回候选列表（不含可播放地址）。"""

    async def resolve(self, song: Song) -> bool:
        """解析真实播放地址（原地更新 song.play_url / play_url_expire / stream_headers）。

        默认实现：play_url 存在且未过期 → True。
        """
        import time

        if song.play_url and (not song.play_url_expire or time.time() < song.play_url_expire):
            return True
        return False

    # ---------- 可选接口 ----------
    def stream_headers(self, song: Song) -> dict:
        return song.stream_headers or {}

    def set_cookie(self, cookie: str) -> None:
        """热更新登录态。"""

    def supports_login(self) -> bool:
        return False

    async def check_trial(self, song: Song) -> bool:
        """音频头复核完整度；已标 trial / 无 play_url / duration<=0 直接返回 song.trial。

        判片段 → 置 song.trial + trial_note；探测失败一律 False（放行）。
        """
        import time

        from ..complete import is_trial_duration, probe_mp3_duration

        if song.trial or not song.play_url or song.duration <= 0:
            return song.trial
        actual = await probe_mp3_duration(song.play_url, self.stream_headers(song))
        if actual is None:
            return False
        if is_trial_duration(actual, song.duration):
            song.trial = True
            song.trial_note = f"试听片段（约{_fmt_mmss(actual)}）"
            return True
        return False

    def status(self) -> dict:
        return {"name": self.name, "label": self.label}
