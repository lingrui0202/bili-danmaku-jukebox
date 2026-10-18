"""演示源：内置假数据 + 服务端实时生成正弦波 wav（离线可用）。"""

from __future__ import annotations

import io
import math
import struct
import wave
from typing import Optional

from ..models import Song
from .base import SongSource

# 12 首内置演示歌曲：(歌名, 歌手, 时长秒, 基频Hz)
DEMO_SONGS: list[tuple[str, str, int, float]] = [
    ("海阔天空", "Beyond", 30, 261.63),
    ("光辉岁月", "Beyond", 28, 293.66),
    ("夜曲", "周杰伦", 25, 329.63),
    ("稻香", "周杰伦", 24, 349.23),
    ("红豆", "王菲", 22, 392.00),
    ("匆匆那年", "王菲", 23, 440.00),
    ("小幸运", "田馥甄", 21, 493.88),
    ("告白气球", "周杰伦", 26, 523.25),
    ("成都", "赵雷", 27, 587.33),
    ("理想三旬", "陈鸿宇", 29, 659.25),
    ("消愁", "毛不易", 25, 698.46),
    ("起风了", "买辣椒也用券", 32, 783.99),
]

URL_TTL = 3600  # 演示源 URL 1 小时
SAMPLE_RATE = 22050
FADE_SEC = 4  # 首尾淡入淡出（秒）


def _find_song(song_id: str) -> Optional[tuple[str, str, int, float]]:
    """按 id 查找；找不到时按索引现场构造（允许直接按索引播放）。"""
    for i, s in enumerate(DEMO_SONGS):
        if f"demo-{i}" == song_id:
            return s
    if song_id.startswith("demo-"):
        try:
            idx = int(song_id.split("-", 1)[1])
            if 0 <= idx < len(DEMO_SONGS):
                return DEMO_SONGS[idx]
        except Exception:
            pass
    return None


def generate_wav(freq: float, seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """22050Hz 单声道 16bit wav：正弦波 = 基频 + 2/3 倍频泛音（0.6/0.25 权重），
    5.5Hz 颤音（±0.4%），首尾 4s 淡入淡出，总幅 0.7。"""
    buf = io.BytesIO()
    total = int(sample_rate * seconds)
    fade = min(int(sample_rate * FADE_SEC), total // 2)
    norm = 1.0 + 0.6 + 0.25  # 归一化系数
    frames = bytearray()
    two_pi = 2.0 * math.pi
    for i in range(total):
        t = i / sample_rate
        vibrato = 1.0 + 0.004 * math.sin(two_pi * 5.5 * t)  # 5.5Hz 颤音 ±0.4%
        f = freq * vibrato
        v = (
            math.sin(two_pi * f * t)
            + 0.6 * math.sin(two_pi * 2 * f * t)
            + 0.25 * math.sin(two_pi * 3 * f * t)
        )
        v = v / norm * 0.7
        if fade > 0:
            if i < fade:
                v *= i / fade
            elif i > total - fade:
                v *= (total - i) / fade
        v = max(-1.0, min(1.0, v))
        frames += struct.pack("<h", int(v * 32767))
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class DemoSource(SongSource):
    name = "demo"
    label = "演示数据"

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        out: list[Song] = []
        for i, (title, artist, dur, freq) in enumerate(DEMO_SONGS):
            if kw in title.lower() or kw in artist.lower():
                out.append(
                    Song(
                        id=f"demo-{i}",
                        source=self.name,
                        title=title,
                        artist=artist,
                        duration=float(dur),
                        ref={"freq": freq},
                    )
                )
        return out[:limit]

    async def resolve(self, song: Song) -> bool:
        import time

        song.play_url = f"/api/demo-audio/{song.id}.wav"
        song.play_url_expire = time.time() + URL_TTL
        song.stream_headers = {}
        return True

    def status(self) -> dict:
        d = super().status()
        d["count"] = len(DEMO_SONGS)
        d["offline"] = True
        return d
