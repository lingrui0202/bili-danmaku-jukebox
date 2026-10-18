"""歌曲源注册表：新增源 = 继承 SongSource + 注册一行。"""

from .base import SongSource
from .bilibili import BilibiliSource
from .qq import QQSource
from .kugou import KugouSource
from .netease import NeteaseSource
from .local import LocalSource
from .demo import DemoSource

SOURCES: dict[str, type[SongSource]] = {
    "bilibili": BilibiliSource,
    "qq": QQSource,
    "kugou": KugouSource,
    "netease": NeteaseSource,
    "local": LocalSource,
    "demo": DemoSource,
}

__all__ = ["SOURCES", "SongSource"]
