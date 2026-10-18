"""缓存与持久化：TTLCache 搜索缓存 + CacheManager 状态持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Optional

from .models import Song, song_from_dict, song_to_dict


class TTLCache:
    """内存 TTL 缓存；满时淘汰最早过期的条目。"""

    def __init__(self, ttl: float = 1800, max_size: int = 500):
        self.ttl = float(ttl)
        self.max_size = int(max_size)
        self._data: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expire_at, value = item
        if time.time() > expire_at:
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        if len(self._data) >= self.max_size and key not in self._data:
            self._evict_expired()
        self._data[key] = (time.time() + (self.ttl if ttl is None else float(ttl)), value)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._data.items() if now > exp]
        for k in expired:
            self._data.pop(k, None)
        # 仍然满则淘汰最早过期的
        if len(self._data) >= self.max_size:
            for k in sorted(self._data, key=lambda k: self._data[k][0])[: max(1, len(self._data) - self.max_size + 1)]:
                self._data.pop(k, None)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


class CacheManager:
    """搜索缓存 + 播放状态持久化（state.json 原子写）。"""

    def __init__(self, cache_dir: str = "cache", search_ttl: float = 1800):
        cache_dir = self._resolve_dir(cache_dir)
        self.cache_dir = cache_dir
        self.search = TTLCache(ttl=search_ttl, max_size=500)
        self.state_path = os.path.join(cache_dir, "state.json")
        self._dirty = False

    @staticmethod
    def _resolve_dir(cache_dir: str) -> str:
        """确保目录可写；不可写时退回到系统临时目录，避免服务崩溃。"""
        try:
            os.makedirs(cache_dir, exist_ok=True)
            return cache_dir
        except Exception:
            alt = os.path.join(tempfile.gettempdir(), "bili-jukebox-cache")
            try:
                os.makedirs(alt, exist_ok=True)
            except Exception:
                pass
            return alt

    # ---------- 搜索缓存 ----------
    @staticmethod
    def _search_key(source: str, keyword: str) -> str:
        return f"search:{source}:{keyword.strip().lower()}"

    def get_search(self, source: str, keyword: str) -> Optional[list[Song]]:
        data = self.search.get(self._search_key(source, keyword))
        if data is None:
            return None
        return [song_from_dict(d) for d in data]

    def set_search(self, source: str, keyword: str, songs: list[Song]) -> None:
        # 空结果不缓存（防接口临时故障被缓存）
        if not songs:
            return
        self.search.set(self._search_key(source, keyword), [song_to_dict(s) for s in songs])

    # ---------- 状态持久化 ----------
    def save_state(self, state: dict) -> None:
        """原子写：先 .tmp 再 os.replace；写失败静默。"""
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
            self._dirty = False
        except Exception:
            pass

    def load_state(self) -> Optional[dict]:
        if not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def clear_state(self) -> None:
        try:
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
        except Exception:
            pass
        self._dirty = False

    # ---------- 脏标记（tick_loop 每秒检查） ----------
    def mark_dirty(self) -> None:
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty
