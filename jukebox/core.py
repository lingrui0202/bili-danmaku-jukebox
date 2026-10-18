"""JukeboxCore：弹幕指令解析、队列、冷却、播放状态机。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Optional

from .cache import CacheManager
from .danmaku import Danmu
from .models import QueueItem, Song, queue_item_to_dict, song_from_dict
from .sources import SongSource

# 弹幕音源别名（SPEC 6.4）
_SOURCE_ALIASES = {
    "b站": "bilibili", "bilibili": "bilibili", "哔哩": "bilibili", "哔哩哔哩": "bilibili",
    "qq": "qq", "qq音乐": "qq", "q音": "qq",
    "酷狗": "kugou", "kugou": "kugou",
    "网易": "netease", "网易云": "netease", "网易云音乐": "netease", "netease": "netease",
    "本地": "local", "local": "local",
    "演示": "demo", "demo": "demo", "测试": "demo",
}
_SEPARATORS = (" ", "：", ":", "，", ",")


def _fmt_mmss(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


class JukeboxCore:
    def __init__(self, config: dict, sources: dict[str, SongSource], cache: Optional[CacheManager] = None):
        self.config = config or {}
        self.sources = sources or {}
        self.cache = cache
        self.apply_config(self.config)

        # 队列与状态
        self.queue: deque[QueueItem] = deque()
        self.current: Optional[QueueItem] = None
        self.history: deque[QueueItem] = deque(maxlen=50)
        self.danmu_log: deque[dict] = deque(maxlen=100)
        self.hot_songs: dict[str, int] = {}
        self.total_requested = 0

        self.started_at = 0.0
        self._paused_at = 0.0
        self.paused = False
        self.playing = False

        self._user_last_request: dict[int, float] = {}
        self._song_last_played: dict[str, float] = {}
        self._user_play_count: dict[int, int] = {}  # 每个用户已播放次数（轮询排序用）

        self._listeners: list[Callable] = []

    # ---------- 配置热更新 ----------
    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        jb = self.config.get("jukebox", {})
        self.request_enabled = bool(jb.get("request_enabled", False))
        self.prefixes = list(jb.get("request_prefixes") or [])
        self.user_cooldown = int(jb.get("user_cooldown_seconds", 30))
        self.song_cooldown_minutes = int(jb.get("song_cooldown_minutes", 10))
        self.max_queue_length = int(jb.get("max_queue_length", 50))
        self.max_per_user_queue = int(jb.get("max_per_user_queue", 3))
        self.max_song_duration = int(jb.get("max_song_duration", 900))
        self.blacklist = list(jb.get("blacklist") or [])
        self.allow_user_skip = bool(jb.get("allow_user_skip", False))
        self.keep_state_on_exit = bool(jb.get("keep_state_on_exit", True))
        self.active_sources = list(self.config.get("sources", {}).get("active") or [])
        self.search_ttl = int(self.config.get("cache", {}).get("search_ttl_minutes", 30)) * 60

    # ---------- 事件广播 ----------
    def on_event(self, cb: Callable) -> None:
        self._listeners.append(cb)

    async def _emit(self, event: str, data: Any) -> None:
        for cb in list(self._listeners):
            try:
                await cb(event, data)
            except Exception:
                pass

    async def broadcast_state(self) -> None:
        await self._emit("state", self.snapshot())

    def _log(self, kind: str, msg: str, **extra) -> None:
        entry = {"kind": kind, "msg": msg, "ts": time.time(), **extra}
        self.danmu_log.appendleft(entry)
        # emit "danmu_log"（_log 是同步方法，用 create_task 异步广播）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit("danmu_log", entry))
        except RuntimeError:
            pass

    async def toast(self, msg: str, kind: str = "info") -> None:
        await self._emit("toast", {"msg": msg, "kind": kind})

    # ---------- 快照 ----------
    def progress(self) -> float:
        if not self.current:
            return 0.0
        if self.paused:
            return max(0.0, self._paused_at - self.started_at)
        return max(0.0, time.time() - self.started_at)

    def snapshot(self) -> dict:
        cur = None
        if self.current:
            cur = queue_item_to_dict(self.current)
            cur["progress"] = round(self.progress(), 1)
            cur["paused"] = self.paused
            cur["playing"] = self.playing
        hot = sorted(self.hot_songs.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "current": cur,
            "queue": [queue_item_to_dict(i) for i in self.queue],
            "history": [queue_item_to_dict(i) for i in self.history],
            "hot_songs": [{"name": k, "count": v} for k, v in hot],
            "sources": [
                {**(src.status() or {}), "active": name in self.active_sources}
                for name, src in self.sources.items()
            ],
            "active_sources": list(self.active_sources),
            "jukebox_cfg": {
                "request_enabled": bool(getattr(self, "request_enabled", False)),
                "request_prefixes": list(self.prefixes),
                "user_cooldown_seconds": self.user_cooldown,
                "song_cooldown_minutes": self.song_cooldown_minutes,
                "max_queue_length": self.max_queue_length,
                "max_per_user_queue": self.max_per_user_queue,
                "max_song_duration": self.max_song_duration,
                "blacklist": list(self.blacklist),
                "allow_user_skip": self.allow_user_skip,
                "keep_state_on_exit": self.keep_state_on_exit,
            },
            "cache_stats": self.cache.search.stats() if self.cache else None,
            "total_requested": self.total_requested,
        }

    # ---------- 状态持久化 ----------
    def state_snapshot(self) -> dict:
        return {
            "queue": [queue_item_to_dict(i) for i in self.queue],
            "current": queue_item_to_dict(self.current) if self.current else None,
            "history": [queue_item_to_dict(i) for i in self.history],
            "hot_songs": [{"name": k, "count": v} for k, v in self.hot_songs.items()],
            "total_requested": self.total_requested,
        }

    @staticmethod
    def _item_from_dict(d: dict) -> QueueItem:
        return QueueItem(
            song=song_from_dict(d),
            requested_by=str(d.get("requested_by", "")),
            requested_by_uid=int(d.get("requested_by_uid") or 0),
            requested_at=float(d.get("requested_at") or time.time()),
            pinned=bool(d.get("pinned", False)),
            failed=bool(d.get("failed", False)),
            manual=bool(d.get("manual", False)),
        )

    def restore_state(self, data: dict) -> None:
        """current 插回队首，queue 依次 append，history appendleft。"""
        if not data:
            return
        cur = data.get("current")
        if cur:
            self.queue.appendleft(self._item_from_dict(cur))
        for d in data.get("queue") or []:
            try:
                self.queue.append(self._item_from_dict(d))
            except Exception:
                continue
        for d in data.get("history") or []:
            try:
                self.history.appendleft(self._item_from_dict(d))
            except Exception:
                continue
        self.hot_songs = {
            h.get("name", ""): int(h.get("count", 0)) for h in (data.get("hot_songs") or [])
        }
        self.total_requested = int(data.get("total_requested") or 0)
        if self.cache:
            self.cache.mark_dirty()

    # ---------- 弹幕入口 ----------
    async def handle_danmu(self, d: Danmu) -> Optional[tuple[bool, str]]:
        self._log(
            "danmu",
            d.text,
            uname=d.uname,
            uid=d.uid,
            medal_level=d.medal_level,
        )
        parsed = self.parse_command(d.text)
        if parsed is None:
            return None
        action, arg = parsed

        if action == "skip":
            if self.allow_user_skip or d.uid == 0:
                self.skip()
                return (True, "已切歌")
            return (False, "只有主播可以切歌哦")

        if action == "help":
            msg = self._help_text()
            self._log("system", msg)
            return (True, msg)

        if action == "list":
            if not self.queue:
                msg = "队列是空的，快来「点歌 歌名」点歌吧~"
            else:
                items = list(self.queue)[:5]
                lines = [f"{i+1}. {it.song.display()}" for i, it in enumerate(items)]
                more = len(self.queue) - len(items)
                msg = "当前队列：\n" + "\n".join(lines) + (f"\n...等{more}首" if more > 0 else "")
            self._log("system", msg)
            return (True, msg)

        if action == "request":
            if not getattr(self, "request_enabled", False):
                self._log("system", "点歌功能未开启，已忽略该指令（设置页可开启）", uname=d.uname, uid=d.uid)
                return None
            return await self.request_song(d.uid, d.uname, arg, from_danmu=True)

        return None

    def _help_text(self) -> str:
        prefixes = "/".join(self.prefixes)
        srcs = "、".join(_SOURCE_ALIASES.get(s, s) for s in self.active_sources)
        return (
            f"点歌方式：发送「{prefixes} 歌名」\n"
            f"可指定音源：如「点歌 qq 七里香」（{srcs}）\n"
            f"其他指令：切歌/下一首/skip、点歌列表/队列、点歌帮助"
        )

    def parse_command(self, text: str) -> Optional[tuple[str, str]]:
        text = (text or "").strip()
        if not text:
            return None
        if text in ("切歌", "下一首", "next", "skip"):
            return ("skip", "")
        if text in ("点歌帮助", "点歌说明"):
            return ("help", "")
        if text in ("点歌列表", "查看队列", "队列"):
            return ("list", "")
        # 最长前缀匹配（按长度倒序，避免「点歌」被「点」抢先）
        for p in sorted(self.prefixes, key=len, reverse=True):
            if p and text.startswith(p):
                kw = text[len(p):].strip()
                if not kw:
                    return ("help", "")
                return ("request", kw)
        return None

    # ---------- 音源别名拆分 ----------
    @staticmethod
    def _split_source_hint(keyword: str) -> tuple[str, str]:
        """别名按长度倒序尝试；分隔符集合命中前缀且剩余非空 → (源, 剩余)。"""
        kw = keyword or ""
        for alias in sorted(_SOURCE_ALIASES, key=len, reverse=True):
            for sep in _SEPARATORS:
                prefix = alias + sep
                if kw.startswith(prefix):
                    rest = kw[len(prefix):].strip()
                    if rest:
                        return _SOURCE_ALIASES[alias], rest
        return ("", keyword)

    # ---------- 点歌主流程 ----------
    async def request_song(
        self, uid: int, uname: str, keyword: str, from_danmu: bool = False
    ) -> tuple[bool, str]:
        # 1. 清洗
        keyword = (keyword or "").strip().strip("。.!！?？")
        if not keyword:
            return (False, "歌名不能为空哦，例如「点歌 海阔天空」")
        if len(keyword) > 50:
            return (False, "歌名太长啦（50字以内）")

        # 2. 黑名单
        for bad in self.blacklist:
            if bad and bad in keyword:
                self._log("request", f"命中黑名单「{bad}」", uname=uname, uid=uid)
                return (False, "这首歌不允许点哦")

        # 3. 音源指定
        hint, keyword = self._split_source_hint(keyword)
        if not keyword:
            return (False, "歌名不能为空哦，例如「点歌 海阔天空」")
        if hint:
            if hint not in self.active_sources:
                return (False, f"{hint}源没有启用，先在管理页打开它吧")
            if hint not in self.sources:
                return (False, f"{hint}源没有启用，先在管理页打开它吧")
            search_order = [hint]
        else:
            search_order = [s for s in self.active_sources if s in self.sources]

        now = time.time()
        # 4. 用户冷却（仅弹幕）
        if from_danmu and self.user_cooldown > 0:
            last = self._user_last_request.get(uid)
            if last is not None and now - last < self.user_cooldown:
                remain = int(self.user_cooldown - (now - last)) + 1
                return (False, f"点歌太频繁啦，{remain}秒后再试~")

        # 5. 每人在队上限（仅弹幕）
        if from_danmu and self.max_per_user_queue > 0 and uid:
            mine = sum(1 for it in list(self.queue) + ([self.current] if self.current else []) if it.requested_by_uid == uid)
            if mine >= self.max_per_user_queue:
                return (False, f"每人最多只能点{self.max_per_user_queue}首哦")

        # 6. 搜歌（按 active 顺序逐源，第一个有结果的源胜出）
        search_kw = keyword.replace("@", " ").replace("＠", " ")
        winner_src = None
        candidates: list[Song] = []
        for sname in search_order:
            src = self.sources.get(sname)
            if not src:
                continue
            songs = None
            if self.cache:
                songs = self.cache.get_search(sname, search_kw)
            if songs is None:
                try:
                    songs = await src.search(search_kw, limit=5)
                except Exception:
                    songs = []
                if songs and self.cache:
                    self.cache.set_search(sname, search_kw, songs)
            if songs:
                winner_src = src
                candidates = songs
                break
        if not winner_src:
            return (False, f"没有找到「{keyword}」，换个关键词试试~")

        # 依次 resolve 前 3 条候选，选第一个真正可播放的
        chosen: Optional[Song] = None
        too_long: Optional[Song] = None
        for song in candidates[:3]:
            if song.duration > 0 and song.duration > self.max_song_duration:
                too_long = too_long or song
                continue
            try:
                ok = await winner_src.resolve(song)
            except Exception:
                continue
            if ok:
                chosen = song
                break
        if not chosen:
            # 7/8. 没找到 / 时长超上限
            if too_long is not None:
                return (
                    False,
                    f"这首太长了（{_fmt_mmss(too_long.duration)}），超过{self.max_song_duration // 60}分钟上限",
                )
            return (False, f"没有找到可播放的「{keyword}」，可能是付费或版权歌曲~")

        # 9. 去重
        queue_keys = {it.song.key() for it in self.queue}
        if self.current:
            queue_keys.add(self.current.song.key())
        if chosen.key() in queue_keys:
            return (False, "已经在队列里啦")
        displays = {it.song.display() for it in self.queue}
        if self.current:
            displays.add(self.current.song.display())
        if chosen.display() in displays:
            return (False, "已经在队列里啦")

        # 10. 歌曲冷却
        if self.song_cooldown_minutes > 0:
            last_played = self._song_last_played.get(chosen.key())
            if last_played is not None:
                cd = self.song_cooldown_minutes * 60
                if now - last_played < cd:
                    remain = int((cd - (now - last_played)) / 60) + 1
                    return (False, f"这首歌{remain}分钟内刚被点过，换一首吧~")

        # 11. 队列满
        if self.max_queue_length > 0 and len(self.queue) >= self.max_queue_length:
            return (False, "队列满啦，等一会再点~")

        # 12. 入队（随后按「先看人、后看顺序」轮询重排）
        self._user_last_request[uid] = now
        item = QueueItem(song=chosen, requested_by=uname or "观众", requested_by_uid=int(uid or 0))
        self.queue.append(item)
        self._auto_arrange()
        self.total_requested += 1
        self._count_hot(chosen.display())
        self._prefetch(chosen)
        trial_tip = f"（注意：{chosen.trial_note}）" if chosen.trial else ""
        self._log(
            "request",
            f"{uname} 点了《{chosen.display()}》{trial_tip}",
            uname=uname,
            uid=uid,
        )
        if self.cache:
            self.cache.mark_dirty()
        await self.broadcast_state()
        if not self.current:
            self.play_next()
        msg = f"点歌成功：《{chosen.display()}》已加入队列{trial_tip}"
        return (True, msg)

    def _count_hot(self, display: str) -> None:
        self.hot_songs[display] = self.hot_songs.get(display, 0) + 1

    # ---------- 队列排序：先看人，后看顺序（按人轮询） ----------
    def _rr_order(self, items: list[QueueItem]) -> list[QueueItem]:
        """把一组队列项按「人轮询」交错排列。

        例：A 点了 1/2/3，B 点了 4/5/6 → 1,4,2,5,3,6。
        人之间的先后：没播过歌的人优先，其次按各自最早点歌时间。
        """
        groups: dict[int, list[QueueItem]] = {}
        for it in items:
            groups.setdefault(it.requested_by_uid, []).append(it)
        for g in groups.values():
            g.sort(key=lambda it: it.requested_at)

        def user_key(u: int):
            return (self._user_play_count.get(u, 0), groups[u][0].requested_at if groups[u] else 0)

        users = sorted(groups.keys(), key=user_key)
        out: list[QueueItem] = []
        rnd = 0
        while True:
            added = False
            for u in users:
                g = groups[u]
                if rnd < len(g):
                    out.append(g[rnd])
                    added = True
            if not added:
                break
            rnd += 1
        return out

    def _auto_arrange(self) -> None:
        """重排待播队列：主播手动调过的（pinned/manual）保持在最前，其余按人轮询。"""
        if not self.queue:
            return
        items = list(self.queue)
        manual = [it for it in items if it.pinned or it.manual]
        auto = [it for it in items if not (it.pinned or it.manual)]
        arranged = manual + self._rr_order(auto)
        self.queue.clear()
        self.queue.extend(arranged)

    def _prefetch(self, song: Song) -> None:
        """后台任务再 resolve 一次保活（播放地址可能过期）。"""
        src = self.sources.get(song.source)
        if not src:
            return

        async def _do():
            try:
                await src.resolve(song)
            except Exception:
                pass

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_do())
        except RuntimeError:
            pass

    # ---------- 队列操作 ----------
    def play_next(self) -> None:
        if self.current:
            self.history.appendleft(self.current)
            self._song_last_played[self.current.song.key()] = time.time()
            uid = self.current.requested_by_uid
            self._user_play_count[uid] = self._user_play_count.get(uid, 0) + 1
        if self.queue:
            self.current = self.queue.popleft()
            self.current.failed = False
            self.started_at = time.time()
            self._paused_at = 0.0
            self.paused = False
            self.playing = True
        else:
            self.current = None
            self.playing = False
            self.started_at = 0.0
        if self.cache:
            self.cache.mark_dirty()

    def skip(self) -> None:
        self.play_next()

    def remove(self, index: int) -> bool:
        if 0 <= index < len(self.queue):
            del self.queue[index]
            if self.cache:
                self.cache.mark_dirty()
            return True
        return False

    def clear(self) -> None:
        self.queue.clear()
        if self.cache:
            self.cache.mark_dirty()

    def move(self, index: int, new_index: int) -> bool:
        n = len(self.queue)
        if not (0 <= index < n):
            return False
        new_index = max(0, min(n - 1, int(new_index)))
        item = self.queue[index]
        del self.queue[index]
        self.queue.insert(new_index, item)
        item.manual = True  # 主播手动排序，自动轮询不再调整它
        if self.cache:
            self.cache.mark_dirty()
        return True

    def pin(self, index: int) -> bool:
        if not (0 <= index < len(self.queue)):
            return False
        item = self.queue[index]
        del self.queue[index]
        item.pinned = True
        self.queue.appendleft(item)
        if self.cache:
            self.cache.mark_dirty()
        return True

    def play_now(self, index: int) -> bool:
        if not (0 <= index < len(self.queue)):
            return False
        if self.current:
            self.history.appendleft(self.current)
            self._song_last_played[self.current.song.key()] = time.time()
        item = self.queue[index]
        del self.queue[index]
        self.current = item
        self.current.failed = False
        self.started_at = time.time()
        self._paused_at = 0.0
        self.paused = False
        self.playing = True
        if self.cache:
            self.cache.mark_dirty()
        return True

    # ---------- 播放控制 ----------
    def pause(self) -> None:
        if self.current and not self.paused:
            self._paused_at = time.time()
            self.paused = True
            if self.cache:
                self.cache.mark_dirty()

    def resume(self) -> None:
        if self.current and self.paused:
            self.started_at += time.time() - self._paused_at
            self.paused = False
            if self.cache:
                self.cache.mark_dirty()

    def seek(self, sec: float) -> None:
        if self.current:
            self.started_at = time.time() - max(0.0, float(sec))
            if self.paused:
                self._paused_at = time.time()
            if self.cache:
                self.cache.mark_dirty()

    # ---------- 前端回报 ----------
    def report_ended(self) -> None:
        self.play_next()

    def report_duration(self, d: float) -> None:
        """仅当 current.song.duration<=0 时回填真实时长。"""
        if self.current and (self.current.song.duration or 0) <= 0 and d > 0:
            self.current.song.duration = float(d)
            if self.cache:
                self.cache.mark_dirty()

    def report_error(self, reason: str = "") -> None:
        if not self.current:
            return
        self.current.failed = True
        self._log("error", f"播放失败：{self.current.song.display()} {reason}".strip())
        self.play_next()

    # ---------- tick ----------
    async def tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                if self.current and self.playing and not self.paused:
                    dur = self.current.song.duration or 0
                    if dur > 0 and self.progress() >= dur + 2:
                        self.play_next()
                if self.cache and self.cache.dirty:
                    self.cache.save_state(self.state_snapshot())
            except Exception:
                pass

    # ---------- 控制台直接添加 ----------
    async def add_song_direct(self, song: Song, uname: str = "主播") -> tuple[bool, str]:
        queue_keys = {it.song.key() for it in self.queue}
        if self.current:
            queue_keys.add(self.current.song.key())
        if song.key() in queue_keys:
            return (False, "已经在队列里啦")
        if self.max_queue_length > 0 and len(self.queue) >= self.max_queue_length:
            return (False, "队列满啦")
        item = QueueItem(song=song, requested_by=uname, requested_by_uid=0)
        self.queue.append(item)
        self._auto_arrange()
        self.total_requested += 1
        self._count_hot(song.display())
        self._prefetch(song)
        if self.cache:
            self.cache.mark_dirty()
        await self.broadcast_state()
        if not self.current:
            self.play_next()
        return (True, f"已添加《{song.display()}》")
