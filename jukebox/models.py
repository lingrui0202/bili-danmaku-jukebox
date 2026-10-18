"""Song / QueueItem 数据模型 + 序列化。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Song:
    """一首歌（来自某个源的搜索结果或队列项）。"""

    id: str
    source: str
    title: str
    artist: str = ""
    duration: float = 0.0  # 标称时长（秒）；0 = 未知（播放时前端回填）
    cover: str = ""
    ref: dict = field(default_factory=dict)
    playable: bool = True
    trial: bool = False
    trial_note: str = ""
    play_url: str = ""
    play_url_expire: float = 0.0
    stream_headers: dict = field(default_factory=dict)

    def key(self) -> str:
        """去重指纹。"""
        return f"{self.source}:{self.id}"

    def display(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title


@dataclass
class QueueItem:
    """队列中的一首歌。注意：没有 id 字段，序列化里的 id 是 song.id。"""

    song: Song
    requested_by: str
    requested_by_uid: int = 0
    requested_at: float = field(default_factory=time.time)
    pinned: bool = False
    failed: bool = False
    manual: bool = False  # 主播手动排过序（拖动/置顶），自动轮询排序不再动它


# ---------------- 序列化 ----------------

def song_to_dict(song: Song) -> dict:
    d = {
        "id": song.id,
        "source": song.source,
        "title": song.title,
        "artist": song.artist,
        "duration": song.duration,
        "cover": song.cover,
        "display": song.display(),
        "playable": song.playable,
        "trial": song.trial,
        "trial_note": song.trial_note,
        "stream_url": f"/api/stream?source={song.source}&id={song.id}",
    }
    return d


def queue_item_to_dict(item: QueueItem) -> dict:
    d = song_to_dict(item.song)
    d.update(
        {
            "requested_by": item.requested_by,
            "requested_by_uid": item.requested_by_uid,
            "requested_at": item.requested_at,
            "pinned": item.pinned,
            "failed": item.failed,
            "manual": item.manual,
        }
    )
    return d


def song_from_dict(d: dict) -> Song:
    """反序列化（缓存/状态恢复用），恢复 trial/trial_note。"""
    return Song(
        id=str(d.get("id", "")),
        source=str(d.get("source", "")),
        title=str(d.get("title", "")),
        artist=str(d.get("artist", "")),
        duration=float(d.get("duration") or 0.0),
        cover=str(d.get("cover", "")),
        ref=dict(d.get("ref") or {}),
        playable=bool(d.get("playable", True)),
        trial=bool(d.get("trial", False)),
        trial_note=str(d.get("trial_note", "")),
        play_url=str(d.get("play_url", "")),
        play_url_expire=float(d.get("play_url_expire") or 0.0),
        stream_headers=dict(d.get("stream_headers") or {}),
    )
