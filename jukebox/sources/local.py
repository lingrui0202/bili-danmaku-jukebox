"""本地音乐目录源。

- os.walk(music_dir) 扫描（sorted）；文件名 "歌手 - 歌名" 拆 artist/title；
- id = 扫描序号字符串；ref = {"path": 绝对路径}；
- resolve：play_url = /api/stream?source=local&id={id}（服务端读文件转发）；
- search：title/artist 子串匹配（lower）；_index 空时允许重扫（运行中加歌）。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..models import Song
from .base import SongSource

EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".wma")
URL_TTL = 3600


class LocalSource(SongSource):
    name = "local"
    label = "本地音乐"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self._index: list[Song] = []

    @property
    def music_dir(self) -> str:
        return os.path.abspath(self.config.get("music_dir", "./music"))

    def _scan(self) -> list[Song]:
        root = self.music_dir
        songs: list[Song] = []
        if not os.path.isdir(root):
            return songs
        idx = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                if not fn.lower().endswith(EXTS):
                    continue
                path = os.path.join(dirpath, fn)
                stem = os.path.splitext(fn)[0]
                if " - " in stem:
                    artist, title = stem.split(" - ", 1)
                else:
                    artist, title = "", stem
                songs.append(
                    Song(
                        id=str(idx),
                        source=self.name,
                        title=title.strip(),
                        artist=artist.strip(),
                        duration=0.0,
                        ref={"path": os.path.abspath(path)},
                    )
                )
                idx += 1
        return songs

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        if not self._index:
            self._index = self._scan()
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        out = [
            s
            for s in self._index
            if kw in s.title.lower() or (s.artist and kw in s.artist.lower())
        ]
        return out[:limit]

    async def resolve(self, song: Song) -> bool:
        song.play_url = f"/api/stream?source=local&id={song.id}"
        song.play_url_expire = time.time() + URL_TTL
        song.stream_headers = {}
        return True

    def status(self) -> dict:
        d = super().status()
        if not self._index:
            self._index = self._scan()
        d["count"] = len(self._index)
        d["music_dir"] = self.music_dir
        return d
