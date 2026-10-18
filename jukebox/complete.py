"""歌曲完整度检测：MP3 帧头 + Xing/Info 头探测反推精确时长。

三层策略（SPEC 6.13）：
1. 元数据：QQ pay.payplay / 网易云 freeTrialInfo / 酷狗 time_len（在各源内处理）；
2. 音频头探测：本模块 parse_mp3_duration；
3. 兜底：探测失败一律放行（返回 False / None），不影响主流程。
"""

from __future__ import annotations

from typing import Optional

import httpx

TRIAL_TOLERANCE = 8.0   # 秒：实际短于标称超过它判试听
PROBE_BYTES = 262144    # Range 前 256KB
PROBE_TIMEOUT = 4.0

# MPEG1 Layer3 码率表（kbps），index 0/15 非法
_BR_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
# MPEG1 采样率
_SR_V1 = {0: 44100, 1: 48000, 2: 32000}


def _skip_id3(data: bytes) -> int:
    """跳过 ID3v2 头（syncsafe 长度）。"""
    if len(data) >= 10 and data[:3] == b"ID3":
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        return 10 + size
    return 0


def parse_mp3_duration(head: bytes) -> Optional[float]:
    """从音频头部解析 MP3 帧头 + Xing/Info 头的 frames 字段反推精确时长（秒）。

    无 Xing/Info 头（如 CBR 无头）返回 None（无法从头单独算出总时长）。
    """
    pos = _skip_id3(head)
    n = len(head)
    while pos + 4 <= n:
        if head[pos] == 0xFF and (head[pos + 1] & 0xE0) == 0xE0:
            h = int.from_bytes(head[pos : pos + 4], "big")
            version = (h >> 19) & 3   # 3 = MPEG1
            layer = (h >> 17) & 3     # 1 = Layer III
            br_i = (h >> 12) & 15
            sr_i = (h >> 10) & 3
            ch = (h >> 6) & 3         # 3 = mono
            if version == 3 and layer == 1 and br_i not in (0, 15) and sr_i != 3:
                srate = _SR_V1[sr_i]
                # Xing/Info 偏移 = 帧头后 21（mono）/36（stereo）
                xing_off = pos + (21 if ch == 3 else 36)
                if head[xing_off : xing_off + 4] in (b"Xing", b"Info"):
                    flags = int.from_bytes(head[xing_off + 4 : xing_off + 8], "big")
                    if flags & 1:
                        frames = int.from_bytes(head[xing_off + 8 : xing_off + 12], "big")
                        if frames > 0 and srate > 0:
                            return frames * 1152 / srate
                return None
        pos += 1
    return None


async def probe_mp3_duration(url: str, headers: Optional[dict] = None) -> Optional[float]:
    """Range 拉前 PROBE_BYTES 字节探测真实时长；失败返回 None（放行）。"""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(
                url, headers={**(headers or {}), "Range": f"bytes=0-{PROBE_BYTES - 1}"}
            )
            if r.status_code not in (200, 206):
                return None
            return parse_mp3_duration(r.content)
    except Exception:
        return None


def is_trial_duration(actual: float, nominal: float) -> bool:
    """实际时长明显短于标称 → 判定为试听片段。"""
    if not nominal or nominal <= 0:
        return False
    return actual < nominal - TRIAL_TOLERANCE
