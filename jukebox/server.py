"""FastAPI 应用：全部 API + WS 广播 + 音频流代理 + 页面路由。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .cache import CacheManager
from .config import CONFIG_PATH, PROJECT_ROOT, app_data_dir, load_config
from .core import JukeboxCore
from .danmaku import Danmu, DanmakuListener, check_room, describe_error, fetch_room_info
from .live_queue import LiveQueue
from .logging_setup import install_asyncio_handler, list_files as list_log_files, log_dir, log_file, read_tail
from .models import Song, song_to_dict
from .settings import SettingsStore
from .sources import SOURCES

log = logging.getLogger("jukebox.server")

WEB_DIR = os.path.join(PROJECT_ROOT, "web")

#: 运行时信息（run.py 绑定端口后写入实际端口；端口顺延时也能给出正确地址）
RUNTIME: dict = {"port": 0, "host": "127.0.0.1"}


def _base_url() -> str:
    port = int(RUNTIME.get("port") or state.cfg.get("server", {}).get("port") or 8788)
    host = str(RUNTIME.get("host") or state.cfg.get("server", {}).get("host") or "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _urls() -> dict:
    base = _base_url()
    return {
        "console": f"{base}/web/console.html",
        "obs": f"{base}/obs",
        "stage": f"{base}/stage",
        "queue": f"{base}/queue",
    }


LOGIN_LABELS = {"bilibili": "B站", "qq": "QQ音乐", "kugou": "酷狗", "netease": "网易云"}


# ---------------- WebSocket 连接管理 ----------------

class ConnectionManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, event: str, data):
        msg = json.dumps({"type": event, "data": data}, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# ---------------- 应用状态单例 ----------------

class AppState:
    def __init__(self):
        self.settings = SettingsStore()
        self.cfg = load_config(CONFIG_PATH, self.settings.data)
        self.ws = ConnectionManager()
        self.http: Optional[httpx.AsyncClient] = None
        self.cache: Optional[CacheManager] = None
        self.core: Optional[JukeboxCore] = None
        self.sources: dict = {}
        self.listener: Optional[DanmakuListener] = None
        self.live_queue: Optional[LiveQueue] = None  # 直播排队（与点歌共用弹幕/登录）
        self.login_mgr = None  # logins.LoginManager（阶段6 懒加载）
        self._listener_lock = asyncio.Lock()   # 保证任何时刻只有一个弹幕监听
        self._last_listener_key = None          # (mode, room_id, credential)

    # ---------- 启动 / 关闭 ----------
    async def startup(self):
        install_asyncio_handler(asyncio.get_running_loop())
        # uvicorn 会按 log_level 把 access 日志调回 INFO；OBS 页面每秒轮询会把日志刷爆，
        # 这里统一压到 WARNING（5xx 仍会由 uvicorn.error 记录）
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        cache_dir = self.cfg["cache"]["dir"]
        if not os.path.isabs(cache_dir):
            cache_dir = os.path.join(app_data_dir(), cache_dir)
        self.cache = CacheManager(
            cache_dir, search_ttl=int(self.cfg["cache"]["search_ttl_minutes"]) * 60
        )
        log.info("缓存目录：%s", cache_dir)
        # 「重启不保留登录态」开关：启动即清空持久化的平台 Cookie
        if not self.cfg.get("login", {}).get("keep_cookies", True):
            self.settings.clear_cookies()
        self._reload_sources()
        self.core = JukeboxCore(self.cfg, self.sources, self.cache)
        self.core.on_event(self._broadcast)

        # 直播排队：与点歌共用弹幕连接/登录/直播间配置
        self.live_queue = LiveQueue(
            self.cfg,
            os.path.join(cache_dir, "live_queue.json"),
            on_change=self._live_queue_changed,
        )
        log.info(
            "排队模块就绪：%s 人 / 待用灯牌 %s 条 / 口令 %s",
            len(self.live_queue.queue), len(self.live_queue.pending), self.live_queue.keywords,
        )

        # 播放状态恢复 / 清理
        if self.cfg["jukebox"].get("keep_state_on_exit", True):
            data = self.cache.load_state()
            if data:
                self.core.restore_state(data)
                if not self.core.current and self.core.queue:
                    self.core.play_next()
                log.info("已恢复播放状态：队列 %s 首", len(self.core.queue))
        else:
            self.cache.clear_state()

        await self._start_listener(self.cfg)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(15, read=60), follow_redirects=True
        )
        asyncio.create_task(self.core.tick_loop())
        asyncio.create_task(self._broadcast_state_loop())
        log.info("服务启动完成（v%s）", __version__)


    async def shutdown(self):
        try:
            if self.listener:
                await self.listener.stop()
        except Exception:
            pass
        try:
            if self.cfg["jukebox"].get("keep_state_on_exit", True):
                if self.cache and self.core:
                    self.cache.save_state(self.core.state_snapshot())
            else:
                if self.cache:
                    self.cache.clear_state()
        except Exception:
            pass
        if self.http:
            try:
                await self.http.aclose()
            except Exception:
                pass

    # ---------- 内部 ----------
    async def _broadcast(self, event: str, data):
        await self.ws.broadcast(event, data)

    def _live_queue_changed(self, snapshot: dict):
        """排队状态变化：WS 推送 live_queue 事件（管理台实时刷新）。"""
        try:
            asyncio.get_running_loop().create_task(self.ws.broadcast("live_queue", snapshot))
        except RuntimeError:
            pass

    async def handle_danmu_shared(self, d: Danmu):
        """弹幕统一入口：点歌指令 + 排队口令共用一条连接。"""
        result = await self.core.handle_danmu(d)
        if self.live_queue is not None:
            try:
                self.live_queue.handle_danmu(d)
            except Exception:
                pass
        return result

    async def handle_gift_shared(self, g):
        """礼物事件入口：灯牌优先礼物的插队/待用记录。"""
        if self.live_queue is None:
            return
        try:
            msg = self.live_queue.handle_gift(g.uid, g.uname, g.gift_name)
        except Exception:
            return
        if msg:
            await self.ws.broadcast("toast", {"msg": msg, "kind": "queue"})

    async def _broadcast_state_loop(self):
        """每秒广播全量状态，前端进度条靠它驱动。"""
        while True:
            await asyncio.sleep(1)
            try:
                await self.ws.broadcast("state", self.core.snapshot())
            except Exception:
                pass

    def _reload_sources(self, drop_cookies=()):
        """按当前配置重建全部源实例（含未启用的，供控制台展示/热切换）。

        v1.1 修正：把旧实例里的 Cookie 带过来。关闭「重启后保留登录态」时
        Cookie 只存在内存里，旧版每次改设置都会重建源 → 登录态被静默清掉
        （表现为「扫码登录完，改个设置就又要重新登录」）。
        drop_cookies 里的音源显式不带（退出登录用）。
        """
        old_cookies = {}
        for name, src in (self.sources or {}).items():
            if name in drop_cookies:
                continue
            try:
                ck = getattr(src, "cookie", "") or ""
            except Exception:
                ck = ""
            if ck:
                old_cookies[name] = ck
        self.sources = {}
        for name, cls in SOURCES.items():
            try:
                src_cfg = self.cfg.get("sources", {}).get(name, {}) or {}
                src = cls(src_cfg)
                cookie = src_cfg.get("cookie", "") or old_cookies.get(name, "")
                if cookie:
                    src.set_cookie(cookie)
                self.sources[name] = src
            except Exception as e:
                log.warning("音源 %s 初始化失败：%s", name, e)
                print(f"[warn] 音源 {name} 初始化失败: {e}")
        if self.core is not None:
            self.core.sources = self.sources


    def reload_config(self):
        self.cfg = load_config(CONFIG_PATH, self.settings.data)

    def apply_settings(self, drop_cookies=()):
        """设置变更后：重载 config -> core.apply_config -> 重建源 -> 广播。"""
        self.reload_config()
        self.core.apply_config(self.cfg)
        self._reload_sources(drop_cookies=drop_cookies)
        if self.live_queue is not None:
            self.live_queue.apply_config(self.cfg)

    def _bili_credential(self, cfg: dict) -> str:
        """弹幕用的 B站凭据：优先 sources.bilibili.cookie，其次 credential_file。

        统一返回**原始 Cookie 字符串**（可能是 JSON 串或 "k=v; k=v" 串），由弹幕侧
        _cookie_from_credential 解析。这样手动粘贴的原始 Cookie 也能用（旧版只对
        JSON 串做 json.loads，粘贴串会被静默丢弃 -> 弹幕仍匿名）。
        """
        raw = cfg.get("sources", {}).get("bilibili", {}).get("cookie", "") or ""
        if raw:
            return raw
        path = cfg.get("danmaku", {}).get("credential_file", "")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return ""

    async def _start_listener(
        self, cfg: dict, cookie_override: Optional[str] = None, force: bool = False
    ):
        """创建（或替换）弹幕监听。**全程持锁**，保证任何时刻只有一个监听在跑。

        v1.1.6 修的真 bug：控制台在扫码时会高频轮询 `/api/login/status`，
        扫码成功那一刻，同时在途的几个请求都会走到「登录成功 → 用新凭据重连」，
        于是并发起了 5~6 个监听（日志里能看到同一秒 6 次「连接直播间…第 1 次尝试」）。
        每条弹幕被 6 个连接各收一份 → 用户「只发了一条，日志里六条」。
        现在：① 重连走 asyncio.Lock 串行；② 配置+凭据没变就跳过重复重连；
        ③ 退出锁之前确保 self.listener 指向的就是刚起来的那个。
        """
        dm = cfg.get("danmaku", {})
        mode = dm.get("mode", "mock")
        room_id = int(dm.get("room_id", 0) or 0)
        # cookie_override 优先（登录时直接传入，覆盖 cfg；"" 表示显式匿名/退出登录）
        credential = self._bili_credential(cfg) if cookie_override is None else (cookie_override or "")
        key = (mode, room_id, credential)

        async with self._listener_lock:
            if not force and self.listener is not None and key == self._last_listener_key:
                log.info("弹幕监听已在用同一份配置/凭据，跳过重复重连（mode=%s room=%s）", mode, room_id)
                return
            if self.listener is not None:
                try:
                    log.info("替换已有的弹幕监听（旧连接先停掉）")
                    await self.listener.stop()
                except Exception as e:
                    log.warning("停止旧弹幕监听失败：%s", e)
                self.listener = None
                # stop() 是异步收尾，给事件循环一拍，确保旧 socket 真的关掉
                await asyncio.sleep(0)

            log.info(
                "启动弹幕监听：mode=%s room_id=%s 凭据=%s",
                mode, room_id, "已登录" if credential else "匿名",
            )
            if mode == "live" and room_id:
                info = await fetch_room_info(room_id)
                if info is None:
                    log.warning(
                        "直播间 %s 查不到（B站没这个房间）：可能填成了主播 UID，"
                        "请填 live.bilibili.com/ 后面的房间号", room_id,
                    )
                else:
                    log.info(
                        "直播间信息：room_id=%s 主播=%s 状态=%s 标题=%s",
                        info["room_id"], info["anchor"] or f"uid{info['uid']}",
                        {0: "未开播", 1: "直播中", 2: "轮播"}.get(info["live_status"], info["live_status"]),
                        info["title"][:40],
                    )

            async def _on_status(st: dict):
                await self.ws.broadcast("danmaku_status", st)

            listener = DanmakuListener(
                room_id=room_id,
                on_danmu=self.handle_danmu_shared,
                on_gift=self.handle_gift_shared,
                on_status=_on_status,
                credential=credential,
                mock=(mode == "mock"),
            )
            await listener.start()
            self.listener = listener
            self._last_listener_key = key
            log.info("弹幕监听已就绪（mode=%s room=%s）", mode, room_id)


    async def _sync_danmaku_credential(self, cookie_override: Optional[str] = None):
        """B站登录态变化后，若弹幕在 live 模式，重连以套用新凭据（完整昵称 + uid）。

        登录完成后 apply_settings() 已把 sources.bilibili.cookie 写进 state.cfg，
        但运行中的弹幕监听器还拿着旧凭据（匿名，昵称被脱敏成首字母）。
        必须停掉旧连接、用新凭据重建，才能读到完整观众名字。
        cookie_override 传入本次登录/退出的实际 Cookie，使「重启不保留登录态」
        （Cookie 只进内存源、不进 cfg）也能同步弹幕。
        """
        lis = getattr(self, "listener", None)
        if lis is None or getattr(lis, "mock", True) or not getattr(lis, "room_id", 0):
            log.info("当前不在 live 模式，登录凭据留到下次切换直播间时生效")
            return
        self.reload_config()
        await self._start_listener(self.cfg, cookie_override=cookie_override)
        await self.ws.broadcast("danmaku_status", self.listener.status_dict())

    def _get_login_mgr(self):
        """logins.LoginManager 懒加载（阶段6）。"""
        if self.login_mgr is None:
            try:
                from .logins import LoginManager

                self.login_mgr = LoginManager(None)  # 每个登录会话独立 http 客户端，Cookie 隔离
            except Exception as e:
                print(f"[warn] 登录模块不可用: {e}")
                return None
        return self.login_mgr

    def _cookies_map(self) -> dict:
        out = {}
        for name in ("bilibili", "qq", "kugou", "netease"):
            out[name] = self.cfg.get("sources", {}).get(name, {}).get("cookie", "") or ""
        return out


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await state.startup()
    yield
    await state.shutdown()


app = FastAPI(title="B站弹幕点歌机", lifespan=lifespan)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _origin_allowed(origin: str, request: Optional[Request] = None, host_header: str = "") -> bool:
    """同源校验：只放行本机页面，或与本次请求 Host 相同的页面（局域网自用）。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if request is not None and (request.url.hostname or "").lower() == host:
        return True
    if host_header:
        hh = host_header
        if hh.startswith("["):          # IPv6：[::1]:8788
            hh = hh.split("]")[0].lstrip("[")
        elif ":" in hh:                 # IPv4/域名：192.168.1.5:8788
            hh = hh.rsplit(":", 1)[0]
        if host == hh.lower():
            return True
    return False


@app.middleware("http")
async def _same_origin_guard(request: Request, call_next):
    """拦截跨站写操作：浏览器带了 Origin 且不是本机/自身来源 → 403。

    命令行/OBS 等非浏览器客户端不带 Origin，照常放行。
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin is not None and not _origin_allowed(origin, request=request):
            return JSONResponse(
                {"ok": False, "msg": "请求来源不受信任，已拒绝（防盗链式跨站调用）"},
                status_code=403,
            )
    return await call_next(request)


class NoCacheMiddleware:
    """纯 ASGI 中间件：页面与常规接口禁用缓存，升级 exe 后浏览器/OBS 拿到的永远是新页面。

    刻意不用 @app.middleware("http")（BaseHTTPMiddleware）：它在长耗时请求上
    （如 /api/danmaku/reconnect 要停旧连接再重连）会抛 No response returned，
    把正常接口打成 500。纯 ASGI 只是包装 send，不碰请求/响应流，没有这个坑。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        skip = path.startswith("/api/stream") or path.startswith("/api/demo-audio")

        async def send_wrapper(message):
            if not skip and message.get("type") == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"cache-control", b"no-store, no-cache, must-revalidate"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(NoCacheMiddleware)


# ---------------- 页面路由 ----------------

@app.get("/")
async def index():
    return RedirectResponse("/web/console.html")


@app.get("/obs")
async def obs_page():
    return FileResponse(os.path.join(WEB_DIR, "obs.html"))


@app.get("/stage")
async def stage_page():
    return FileResponse(os.path.join(WEB_DIR, "stage.html"))


@app.get("/queue")
async def queue_page():
    """OBS 排队名单页（票据式竖版面板，透明背景）。"""
    return FileResponse(os.path.join(WEB_DIR, "queue.html"))


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


# ---------------- 工具函数 ----------------

def _find_song(source: str, song_id: str) -> Optional[Song]:
    """在 current + queue + history 中找歌。"""
    for it in [state.core.current] + list(state.core.queue) + list(state.core.history):
        if it is None:
            continue
        if it.song.source == source and str(it.song.id) == str(song_id):
            return it.song
    return None


def _queue_index(body: dict) -> Optional[int]:
    """优先 id（+可选 source 过滤），回退 index。"""
    sid = body.get("id")
    source = body.get("source")
    if sid is not None and str(sid) != "":
        for i, it in enumerate(state.core.queue):
            if str(it.song.id) == str(sid) and (not source or it.song.source == source):
                return i
        return None
    idx = body.get("index")
    if idx is not None:
        try:
            i = int(idx)
            if 0 <= i < len(state.core.queue):
                return i
        except Exception:
            pass
    return None


def _cookie_ok(name: str, raw: str) -> bool:
    """这个 Cookie 能不能真的当登录态用。

    v1.1.5：B站 必须校验 **SESSDATA 非空**。之前只看「cookie 字符串非空」，
    而 bilibili_api 的扫码登录会返回一个**所有字段都是空串**的 Credential，
    序列化后是一串非空 JSON → 界面显示“已登录”，弹幕连接却是匿名
    （用户反馈：明明登录了还是星号）。
    """
    raw = (raw or "").strip()
    if not raw:
        return False
    if name != "bilibili":
        return True
    try:
        from .danmaku import _cookie_from_credential

        converted = _cookie_from_credential(raw)
        for part in converted.split(";"):
            k, _, v = part.partition("=")
            if k.strip().lower() == "sessdata" and v.strip():
                return True
        return False
    except Exception:
        return False


def _sanitize_overlay(overlay: dict) -> dict:
    """设置覆盖层脱敏：登录 Cookie 不回传明文，只标记是否已登录。"""
    o = copy.deepcopy(overlay or {})
    srcs = o.get("sources")
    if isinstance(srcs, dict):
        for name in ("bilibili", "qq", "kugou", "netease"):
            sub = srcs.get(name)
            if isinstance(sub, dict):
                sub["cookie"] = ""
                sub["has_cookie"] = bool((overlay.get("sources") or {}).get(name, {}).get("cookie"))
    return o


def _public_config(cfg: dict) -> dict:
    """Cookie 不明文回传：置空 + sources_logged_in 标记。"""
    c = copy.deepcopy(cfg)
    srcs = c.get("sources", {})
    logged = {}
    for name in ("bilibili", "qq", "kugou", "netease"):
        sub = srcs.get(name) or {}
        logged[name] = _cookie_ok(name, sub.get("cookie", ""))
        if name in srcs:
            srcs[name]["cookie"] = ""
    c["sources_logged_in"] = logged
    return c


# ---------------- 基础 API ----------------

@app.get("/api/state")
async def api_state():
    return state.core.snapshot()


@app.post("/api/request")
async def api_request(body: dict):
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        return {"ok": False, "msg": "关键词为空"}
    ok, msg = await state.core.request_song(0, "主播", keyword)
    return {"ok": ok, "msg": msg}


@app.post("/api/search")
async def api_search(body: dict):
    keyword = (body.get("keyword") or "").strip()
    source = (body.get("source") or "").strip()
    if not keyword:
        return {"ok": False, "msg": "关键词为空", "results": []}
    names = [source] if source else list(state.core.active_sources)
    results = []
    seen = set()
    for name in names:
        src = state.sources.get(name)
        if not src:
            continue
        try:
            songs = await src.search(keyword, limit=20)
        except Exception:
            songs = []
        for s in songs:
            if s.key() in seen:
                continue
            seen.add(s.key())
            results.append(
                {
                    "source": s.source,
                    "id": s.id,
                    "title": s.title,
                    "artist": s.artist,
                    "duration": s.duration,
                    "cover": s.cover,
                    "display": s.display(),
                    "trial": s.trial,
                    "trial_note": s.trial_note,
                    "ref": s.ref,
                }
            )
        if len(results) >= 20:
            break
    return {"ok": True, "results": results[:20]}


@app.post("/api/add")
async def api_add(body: dict):
    try:
        song = Song(
            id=str(body.get("id") or ""),
            source=str(body.get("source") or ""),
            title=str(body.get("title") or ""),
            artist=str(body.get("artist") or ""),
            duration=float(body.get("duration") or 0),
            cover=str(body.get("cover") or ""),
            ref=dict(body.get("ref") or {}),
            trial=bool(body.get("trial", False)),
            trial_note=str(body.get("trial_note") or ""),
        )
    except Exception as e:
        return {"ok": False, "msg": f"参数错误: {e}"}
    if not song.id or not song.source or not song.title:
        return {"ok": False, "msg": "缺少 id/source/title"}
    ok, msg = await state.core.add_song_direct(song)
    return {"ok": ok, "msg": msg}


@app.post("/api/control")
async def api_control(body: dict):
    core = state.core
    action = str(body.get("action") or "")
    if action == "skip":
        core.skip()
    elif action == "pause":
        core.pause()
    elif action == "resume":
        core.resume()
    elif action == "seek":
        try:
            core.seek(float(body.get("sec", 0)))
        except Exception:
            return {"ok": False, "msg": "sec 参数错误"}
    elif action in ("remove", "move", "pin", "play_now"):
        idx = _queue_index(body)
        if idx is None:
            return {"ok": False, "msg": "未找到该歌曲"}
        if action == "remove":
            core.remove(idx)
        elif action == "move":
            try:
                core.move(idx, int(body.get("new_index", idx)))
            except Exception:
                return {"ok": False, "msg": "new_index 参数错误"}
        elif action == "pin":
            core.pin(idx)
        elif action == "play_now":
            core.play_now(idx)
    elif action == "clear":
        core.clear()
    elif action == "report_ended":
        core.report_ended()
    elif action == "report_error":
        core.report_error(str(body.get("reason") or ""))
    elif action == "report_duration":
        try:
            core.report_duration(float(body.get("sec", 0)))
        except Exception:
            return {"ok": False, "msg": "sec 参数错误"}
    else:
        return {"ok": False, "msg": f"未知操作 {action}"}
    await core.broadcast_state()
    return {"ok": True}


@app.post("/api/mock-danmu")
async def api_mock_danmu(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "msg": "弹幕内容为空"}
    uname = str(body.get("uname") or "测试用户")
    try:
        uid = int(body.get("uid") or 0)
    except Exception:
        uid = 0
    result = await state.handle_danmu_shared(Danmu(text=text, uid=uid, uname=uname, ts=time.time()))
    if state.listener:
        state.listener.total_received += 1
    if result is None:
        return {"ok": True, "msg": "已记录（非指令弹幕）"}
    return {"ok": bool(result[0]), "msg": result[1]}


@app.post("/api/danmu-status")
async def api_danmu_status():
    lst = state.listener
    if not lst:
        return {"mode": "mock", "room_id": 0, "connected": False, "total_received": 0}
    return lst.status_dict()


# ---------------- 日志（v1.1：用户报障时直接把日志发过来） ----------------

@app.get("/api/logs")
async def api_logs(lines: int = 200, file: str = ""):
    """日志位置 + 文件列表 + 末尾若干行（管理页「运行日志」用）。"""
    try:
        n = int(lines)
    except Exception:
        n = 200
    n = max(10, min(n, 2000))
    return {
        "ok": True,
        "version": __version__,
        "dir": log_dir(),
        "file": log_file(),
        "files": list_log_files(),
        "lines": read_tail(n, name=file or ""),
    }


@app.get("/api/logs/download")
async def api_logs_download(file: str = ""):
    """下载日志文件（默认当前 jukebox.log，也可指定 jukebox.log.1）。"""
    name = os.path.basename(file or "jukebox.log") or "jukebox.log"
    if not name.startswith("jukebox.log"):
        name = "jukebox.log"
    path = os.path.join(log_dir(), name)
    if not log_dir() or not os.path.isfile(path):
        return JSONResponse({"ok": False, "msg": "日志文件不存在"}, status_code=404)
    log.info("管理页下载日志：%s", name)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)


@app.post("/api/logs/open-folder")
async def api_logs_open_folder():
    """在资源管理器里打开日志目录（仅本机、Windows）。"""
    directory = log_dir()
    if not directory or not os.path.isdir(directory):
        return {"ok": False, "msg": "日志目录不存在"}
    try:
        if os.name == "nt":
            os.startfile(directory)  # type: ignore[attr-defined]
            return {"ok": True, "msg": f"已打开：{directory}"}
        return {"ok": False, "msg": directory}
    except Exception as e:
        return {"ok": False, "msg": f"打开失败：{e}（路径：{directory}）"}


# ---------------- 启动体检（v1.1.2） ----------------

@app.get("/api/health")
async def api_health():
    """四项体检：弹幕连接 / B站登录 / 音源 / OBS 地址。"""
    lst = state.listener
    dm = lst.status_dict() if lst else {"mode": "mock", "connected": False, "room_id": 0}
    mode = dm.get("mode", "mock")
    room = dm.get("room_id") or 0

    if mode == "mock":
        dm_item = ("warn", "模拟弹幕模式：不连直播间，观众弹幕不会进来")
    elif dm.get("connected"):
        live = dm.get("live_status")
        if live == 0:
            dm_item = ("ok", f"已连接直播间 {room}（当前未开播，开播后才有真实弹幕）")
        else:
            dm_item = ("ok", f"已连接直播间 {room}，累计收到 {dm.get('total_received', 0)} 条")
    elif dm.get("error"):
        dm_item = ("error", f"直播间 {room} 连不上：{dm.get('error')}")
    else:
        dm_item = ("warn", f"直播间 {room} 正在连接…")

    bili_cookie = state.cfg.get("sources", {}).get("bilibili", {}).get("cookie", "") or ""
    login_uid = 0
    if bili_cookie:
        try:
            from .danmaku import _cookie_from_credential, _cookie_uid

            login_uid = _cookie_uid(_cookie_from_credential(bili_cookie))
        except Exception:
            login_uid = 0
    if not bili_cookie:
        login_item = ("warn", "B站未登录：观众昵称会显示成星号，灯牌插队可能也认不出人")
    elif not _cookie_ok("bilibili", bili_cookie):
        login_item = ("warn", "保存的 B站 登录态无效（没有 SESSDATA）：实际会按匿名连接，请重新扫码登录")
    elif not login_uid:
        login_item = ("warn", "有 B站 SESSDATA 但缺 DedeUserID：可能仍被当匿名，建议重新扫码登录")
    else:
        login_item = ("ok", f"B站已登录（uid {login_uid}）：完整昵称 + 灯牌插队可用")

    active = [n for n in state.core.active_sources if n in state.sources]
    if active:
        src_item = ("ok", f"已启用音源：{'、'.join(active)}")
    else:
        src_item = ("error", "一个音源都没启用，搜歌一定失败")

    port = int(RUNTIME.get("port") or 0) or int(state.cfg.get("server", {}).get("port") or 8788)
    cfg_port = int(state.cfg.get("server", {}).get("port") or 8788)
    if port != cfg_port:
        obs_item = ("warn", f"端口已顺延到 {port}（配置里是 {cfg_port}）：OBS 源地址要改成新端口")
    else:
        obs_item = ("ok", f"OBS 地址可用（端口 {port}）")

    items = [
        {"key": "弹幕连接", "level": dm_item[0], "text": dm_item[1]},
        {"key": "B站登录", "level": login_item[0], "text": login_item[1]},
        {"key": "音源", "level": src_item[0], "text": src_item[1]},
        {"key": "OBS 地址", "level": obs_item[0], "text": obs_item[1]},
    ]

    # 程序自身状态：OBS 一片空白时，先确认「它到底在不在、在哪个端口、上次退得干不干净」
    pid = RUNTIME.get("pid") or "-"
    last_ok = RUNTIME.get("last_exit_ok", True)
    proc_text = f"正在运行（PID {pid}，端口 {port}）"
    if not last_ok:
        proc_text += "；上次没有正常退出（崩溃或被强杀），这一次是新起的"
    items.append({
        "key": "程序状态",
        "level": "ok" if last_ok else "warn",
        "text": proc_text,
    })
    return {
        "ok": True,
        "version": __version__,
        "port": port,
        "configured_port": cfg_port,
        "urls": _urls(),
        "items": items,
        "worst": "error" if any(i["level"] == "error" for i in items)
        else ("warn" if any(i["level"] == "warn" for i in items) else "ok"),
    }


# ---------------- 诊断包（v1.1.2） ----------------

@app.get("/api/diagnostics")
async def api_diagnostics():
    """打包一份可外发的诊断包：日志 + 脱敏配置 + 环境信息。

    日志里 Cookie/token 已脱敏；覆盖层里的登录 Cookie 在这里也会被清空。
    """
    import platform
    import shutil
    import sys
    import tempfile
    import zipfile

    from fastapi import BackgroundTasks

    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"bili-jukebox-diag-{ts}.zip"
    tmpdir = tempfile.mkdtemp(prefix="jb-diag-")
    path = os.path.join(tmpdir, name)

    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in list_log_files():
                full = os.path.join(log_dir(), f["name"])
                try:
                    z.write(full, arcname=f"logs/{f['name']}")
                except Exception as e:
                    log.info("诊断包跳过日志 %s：%s", f["name"], e)

            info = {
                "version": __version__,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "frozen": bool(getattr(sys, "frozen", False)),
                "python": sys.version,
                "platform": platform.platform(),
                "executable": sys.executable,
                "data_dir": app_data_dir(),
                "pid": RUNTIME.get("pid"),
                "last_exit_ok": RUNTIME.get("last_exit_ok"),
                "port": _base_url(),
                "urls": _urls(),
                "danmaku": state.listener.status_dict() if state.listener else None,
                "live_queue": {
                    "people": len(state.live_queue.queue) if state.live_queue else 0,
                    "pending_gifts": len(state.live_queue.pending) if state.live_queue else 0,
                    "keywords": state.live_queue.keywords if state.live_queue else [],
                },
                "queue": {
                    "current": (state.core.current.song.display() if state.core and state.core.current else None),
                    "pending": len(state.core.queue) if state.core else 0,
                },
                "sources": {n: bool(getattr(s, "cookie", "")) for n, s in state.sources.items()},
                "config_file": CONFIG_PATH,
            }
            z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2, default=str))
            z.writestr(
                "config.public.json",
                json.dumps(_public_config(state.cfg), ensure_ascii=False, indent=2, default=str),
            )
            z.writestr(
                "settings.public.json",
                json.dumps(_sanitize_overlay(state.settings.data), ensure_ascii=False, indent=2, default=str),
            )
            z.writestr("README.txt", "这是 B站弹幕点歌机的诊断包。\n"
                                     "logs/ 是运行日志；info.json 是环境信息；\n"
                                     "config/settings 里的登录 Cookie 已清空，可以安全外发。\n")
        log.info("已生成诊断包：%s", path)
    except Exception as e:
        log.warning("生成诊断包失败：%s", e)
        return JSONResponse({"ok": False, "msg": f"生成失败：{e}"}, status_code=500)

    background = BackgroundTasks()
    background.add_task(shutil.rmtree, tmpdir, True)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=name,
        background=background,
    )



# ---------------- 设置 API ----------------

@app.get("/api/settings")
async def api_get_settings():
    return {
        "ok": True,
        "version": __version__,
        "config": _public_config(state.cfg),
        "overlay": _sanitize_overlay(state.settings.data),
        "config_file": CONFIG_PATH,
        "port": int(RUNTIME.get("port") or state.cfg["server"]["port"]),
        "urls": _urls(),
        "has_backup": state.settings.has_backup(),
        "note": "管理页修改只写入 cache/settings.json，不影响 config.yaml；「恢复默认」清空覆盖层",
    }


@app.post("/api/settings")
async def api_post_settings(body: dict):
    patch = body.get("patch")
    if not isinstance(patch, dict):
        return {"ok": False, "msg": "patch 必须是对象"}
    state.settings.patch(patch)
    state.apply_settings()
    # 只记顶层键名与关键值，绝不把 Cookie 写进日志
    changed = ", ".join(sorted(patch.keys()))
    log.info("设置已更新：%s", changed or "(空)")
    for key in ("danmaku", "live_queue", "jukebox"):
        if key in patch and isinstance(patch[key], dict):
            log.info("  设置 %s → %s", key, json.dumps(patch[key], ensure_ascii=False)[:300])
    await state.core.broadcast_state()
    return {"ok": True, "msg": "已保存并生效"}


@app.post("/api/settings/reset")
async def api_reset_settings():
    state.settings.reset()
    # 恢复默认包含清掉登录态，所以内存里的 Cookie 也不能带过来
    state.apply_settings(drop_cookies=tuple(LOGIN_LABELS.keys()))
    log.info("设置已恢复默认（覆盖层已清空，登录态已清除）")
    await state.core.broadcast_state()
    return {"ok": True, "msg": "已恢复默认设置"}


@app.post("/api/settings/rollback")
async def api_settings_rollback():
    """撤销上一次保存（settings.json.bak）。"""
    if not state.settings.has_backup():
        return {"ok": False, "msg": "没有可回滚的上一版设置（只在保存过一次之后才有）"}
    ok = state.settings.rollback()
    if not ok:
        return {"ok": False, "msg": "回滚失败：备份文件读不出来"}
    state.apply_settings()
    log.info("设置已回滚到上一版（settings.json.bak）")
    await state.core.broadcast_state()
    return {"ok": True, "msg": "已恢复到上一次保存之前的设置"}


# ---------------- 登录 API（logins.py 见阶段6） ----------------

@app.post("/api/login/start")
async def api_login_start(body: dict):
    source = str(body.get("source") or "")
    if source not in LOGIN_LABELS:
        return {"ok": False, "msg": "不支持的登录平台"}
    mgr = state._get_login_mgr()
    if mgr is None:
        return {"ok": False, "msg": "登录模块不可用"}
    try:
        session = await mgr.start(source)
        if session is None:
            return {"ok": False, "msg": "无法创建登录会话"}
        log.info("开始扫码登录：%s", LOGIN_LABELS.get(source, source))
        return {"ok": True, "session": session.result(with_qr=True)}
    except Exception as e:
        log.warning("登录启动失败（%s）：%s", source, describe_error(e), exc_info=True)
        return {"ok": False, "msg": f"登录启动失败: {e}"}


@app.post("/api/login/status")
async def api_login_status(body: dict):
    source = str(body.get("source") or "")
    mgr = state._get_login_mgr()
    if mgr is None:
        return {"ok": False, "msg": "登录模块不可用"}
    session = mgr.get(source)
    if session is None:
        return {"ok": False, "msg": "没有进行中的登录会话"}
    st = await session.poll()
    if st.get("status") == "done" and st.get("cookie"):
        # 控制台会连续轮询：同一次登录只处理一遍（保存 Cookie / 重连弹幕）
        if getattr(session, "_consumed", False):
            return {"ok": True, "session": session.result(with_qr=True), "logged_in": True}
        session._consumed = True
        log.info("扫码登录成功：%s（持久化=%s）", LOGIN_LABELS.get(source, source),
                 bool(state.cfg.get("login", {}).get("keep_cookies", True)))
        # 「重启不保留登录态」关闭时：只热更新到内存，不写入 settings.json
        if state.cfg.get("login", {}).get("keep_cookies", True):
            state.settings.set_cookie(source, st["cookie"])
            state.apply_settings()
        else:
            src = state.sources.get(source)
            if src is not None:
                src.set_cookie(st["cookie"])
        # 弹幕同步放在分支外：无论是否持久化，都把登录 Cookie 传给弹幕连接
        if source == "bilibili":
            await state._sync_danmaku_credential(st["cookie"])
        await state.core.broadcast_state()
        return {"ok": True, "session": session.result(with_qr=True), "logged_in": True}
    return {"ok": True, "session": session.result(with_qr=False)}


@app.post("/api/login/cancel")
async def api_login_cancel(body: dict):
    source = str(body.get("source") or "")
    mgr = state._get_login_mgr()
    if mgr is None:
        return {"ok": False, "msg": "登录模块不可用"}
    mgr.cancel(source)
    return {"ok": True}


@app.post("/api/login/logout")
async def api_login_logout(body: dict):
    source = str(body.get("source") or "")
    if source not in LOGIN_LABELS:
        return {"ok": False, "msg": "不支持的登录平台"}
    state.settings.set_cookie(source, "")
    state.apply_settings(drop_cookies=(source,))
    log.info("退出登录：%s", LOGIN_LABELS.get(source, source))
    # 退出也要清掉内存源里的登录态（「重启不保留登录态」模式下 Cookie 只存在于内存源）
    src = state.sources.get(source)
    if src is not None:
        try:
            src.set_cookie("")
        except Exception:
            pass
    if source == "bilibili":
        await state._sync_danmaku_credential("")  # 显式传空 -> 弹幕回退匿名
    await state.core.broadcast_state()
    return {"ok": True, "msg": "已退出登录"}


@app.post("/api/login/manual")
async def api_login_manual(body: dict):
    """扫码被平台风控挡住时的保底：直接粘贴浏览器里的 Cookie（如 MUSIC_U / uin）。"""
    source = str(body.get("source") or "")
    cookie = str(body.get("cookie") or "").strip()
    if source not in LOGIN_LABELS:
        return {"ok": False, "msg": "不支持的登录平台"}
    if not cookie:
        return {"ok": False, "msg": "Cookie 不能为空"}
    cookie = cookie.replace("\n", " ").replace("\r", " ").strip("; ")
    if state.cfg.get("login", {}).get("keep_cookies", True):
        state.settings.set_cookie(source, cookie)
        state.apply_settings()
    else:
        src = state.sources.get(source)
        if src is not None:
            src.set_cookie(cookie)
    # 弹幕同步放在分支外：手动粘贴的原始 Cookie 也直接传给弹幕连接
    if source == "bilibili":
        await state._sync_danmaku_credential(cookie)
    await state.core.broadcast_state()
    return {"ok": True, "msg": "登录态已保存"}


@app.get("/api/login/state")
async def api_login_state():
    mgr = state._get_login_mgr()
    out = {}
    for name, label in LOGIN_LABELS.items():
        src = state.sources.get(name)
        cookie = state.cfg.get("sources", {}).get(name, {}).get("cookie", "") or ""
        entry = {
            "label": label,
            "supports_login": bool(src.supports_login()) if src else False,
            "logged_in": _cookie_ok(name, cookie),
            "session": None,
        }
        if mgr is not None:
            s = mgr.get(name)
            if s is not None:
                entry["session"] = s.result(with_qr=False)
        out[name] = entry
    return {"ok": True, "platforms": out}


# ---------------- 直播间切换 ----------------

@app.post("/api/danmaku/reconnect")
async def api_danmaku_reconnect(body: dict):
    dm = {}
    room_id = body.get("room_id")
    mode = body.get("mode")
    if room_id is not None:
        if isinstance(room_id, bool):
            return {"ok": False, "msg": "room_id 必须是整数"}
        try:
            room_id = int(room_id)
        except Exception:
            return {"ok": False, "msg": "room_id 必须是整数"}
        dm["room_id"] = room_id
    if mode:
        mode = str(mode)
        if mode not in ("mock", "live"):
            return {"ok": False, "msg": "mode 只能是 mock/live"}
        dm["mode"] = mode
    if not dm:
        return {"ok": False, "msg": "没有需要修改的参数"}
    # live 模式必须有房间号
    new_mode = dm.get("mode") or state.cfg.get("danmaku", {}).get("mode", "mock")
    new_room = dm.get("room_id")
    if new_room is None:
        new_room = state.cfg.get("danmaku", {}).get("room_id", 0)
    if new_mode == "live" and not new_room:
        return {"ok": False, "msg": "live 模式需要先填写直播间房间号"}

    # 房间号校验（v1.1）：主播最常见的错法是把「主播 UID」当房间号，
    # 旧版会照单全收 → 连接一直卡在「连接中」。这里提前拦下并说清楚。
    if new_mode == "live" and new_room:
        room_state, room_info = await check_room(int(new_room))
        if room_state == "invalid":
            log.warning("切换直播间被拒：%s 不是有效房间号", new_room)
            return {
                "ok": False,
                "msg": f"B站查不到直播间 {new_room}：请填直播间地址 live.bilibili.com/ 后面的数字（不是主播 UID）",
            }
        if room_state == "ok" and room_info:
            log.info(
                "直播间校验通过：%s 主播=%s 状态=%s",
                room_info["room_id"], room_info["anchor"] or f"uid{room_info['uid']}",
                {0: "未开播", 1: "直播中", 2: "轮播"}.get(room_info["live_status"], room_info["live_status"]),
            )

    log.info("切换直播间：mode=%s room_id=%s", new_mode, new_room or 0)
    state.settings.patch({"danmaku": dm})

    state.reload_config()
    state.core.apply_config(state.cfg)
    # 停旧 listener -> 起新 listener
    if state.listener:
        try:
            await state.listener.stop()
        except Exception:
            pass
    await state._start_listener(state.cfg, force=True)
    await state.ws.broadcast("danmaku_status", state.listener.status_dict())
    return {"ok": True, "msg": f"已切换到 {new_mode} 模式（房间号 {new_room or '无'}）"}


# ---------------- 直播排队 ----------------

def _live_queue_snapshot() -> dict:
    snap = state.live_queue.snapshot() if state.live_queue else {
        "queue": [], "pending_priority": [], "font_size": 36, "frame_opacity": 100,
        "name_opacity": 100, "priority_enabled": True, "clear_on_start": False,
        "served_count": 0, "revision": 0, "max_rows": 50, "keyword": "排队",
        "keywords": ["排队"], "contents": [], "priority_gifts": [],
    }
    lst = state.listener
    if lst is not None:
        st = lst.status_dict()
        snap["room_id"] = st.get("room_id", 0)
        snap["mode"] = st.get("mode", "mock")
        if st.get("mode") == "mock":
            live = "mock"
        elif st.get("connected"):
            live = "connected"
        elif str(st.get("error") or ""):
            live = "reconnecting"
        else:
            live = "connecting"
        snap["live_status"] = {"state": live, "message": st.get("error") or ""}
    return snap


@app.get("/api/live-queue/state")
async def api_live_queue_state():
    """OBS 名单页轮询 + 管理台初次加载。"""
    return _live_queue_snapshot()


@app.post("/api/live-queue/command")
async def api_live_queue_command(body: dict):
    """管理命令（对齐原排队工具的命令接口）。"""
    if state.live_queue is None:
        return {"ok": False, "message": "排队模块未启用", "changed": False}
    cmd = str(body.get("command") or "").strip()
    ok, msg, changed = state.live_queue.command(cmd)
    low = cmd.lower()
    # 口令/内容关键词持久化到设置覆盖层（config.yaml 不动，热生效）
    if changed:
        if low.startswith("contents-"):
            state.settings.patch({"live_queue": {"contents": list(state.live_queue.contents)}})
        elif low.startswith("keywords-"):
            state.settings.patch({"live_queue": {"keywords": list(state.live_queue.keywords)}})
    return {"ok": ok, "message": msg, "changed": changed}


# ---------------- 歌词 ----------------

@app.get("/api/lyric")
async def api_lyric(source: str = "", id: str = ""):
    song = _find_song(source, id)
    if song is None:
        return {"ok": False, "msg": "未找到歌曲"}
    try:
        from . import lyrics as lyrics_mod
    except Exception as e:
        return {"ok": False, "msg": f"歌词模块不可用: {e}"}
    try:
        result = await lyrics_mod.fetch_lyric(song, state.http, state._cookies_map())
    except Exception as e:
        result = {"ok": False, "msg": f"歌词抓取失败: {e}"}
    result["song"] = song_to_dict(song)
    return result


# ---------------- 音频流 ----------------

async def _demo_audio(song_id: str) -> Response:
    from urllib.parse import quote

    from .sources.demo import _find_song, generate_wav

    sid = song_id.replace(".wav", "")
    found = _find_song(sid)
    if found is None:
        return Response(status_code=404, content="not found")
    title, artist, dur, freq = found
    wav = generate_wav(freq, dur)
    # Content-Disposition 头不能含非 latin-1 字符，用 RFC 5987 filename* 编码
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(f"{title}.wav")
        },
    )


@app.get("/api/demo-audio/{song_id}.wav")
async def api_demo_audio(song_id: str):
    return await _demo_audio(song_id)


@app.get("/api/stream")
async def api_stream(request: Request, source: str = "", id: str = ""):
    song = _find_song(source, id)
    if song is None:
        return JSONResponse({"ok": False, "msg": "未找到歌曲"}, status_code=404)
    src = state.sources.get(song.source)
    if src is None:
        return JSONResponse({"ok": False, "msg": "音源不存在"}, status_code=404)

    if song.source == "local":
        path = song.ref.get("path")
        if not path or not os.path.isfile(path):
            return JSONResponse({"ok": False, "msg": "本地文件不存在"}, status_code=404)
        return FileResponse(path)  # FileResponse 自带 Range 支持

    if song.source == "demo":
        return await _demo_audio(str(song.id))

    # 远程：必要时 resolve，然后流式代理
    try:
        ok = await src.resolve(song)
    except Exception:
        ok = False
    if not ok or not song.play_url:
        return JSONResponse({"ok": False, "msg": "无法解析播放地址"}, status_code=502)

    headers = dict(src.stream_headers(song) or {})
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    client = state.http
    own_client = False
    if client is None:
        # 兜底（正常不会发生）：临时客户端必须自己关，否则每次播放泄漏一个连接池
        client = httpx.AsyncClient(follow_redirects=True)
        own_client = True
    try:
        req = client.build_request("GET", song.play_url, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as e:
        if own_client:
            try:
                await client.aclose()
            except Exception:
                pass
        log.warning("拉取音频流失败：%s", describe_error(e))
        return JSONResponse({"ok": False, "msg": f"拉取音频流失败：{e}"}, status_code=502)

    async def _gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            if own_client:
                try:
                    await client.aclose()
                except Exception:
                    pass

    resp_headers = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        v = resp.headers.get(h)
        if v:
            resp_headers[h] = v
    return StreamingResponse(
        _gen(),
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )


# ---------------- WebSocket ----------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin is not None and not _origin_allowed(origin, host_header=ws.headers.get("host", "")):
        await ws.close(code=1008)  # policy violation：拒绝跨站页面订阅状态
        return
    await state.ws.connect(ws)
    try:
        await ws.send_text(
            json.dumps(
                {"type": "state", "data": state.core.snapshot()},
                ensure_ascii=False,
                default=str,
            )
        )
    except Exception:
        state.ws.disconnect(ws)
        return
    try:
        while True:
            await ws.receive_text()  # 客户端仅需保活
    except WebSocketDisconnect:
        state.ws.disconnect(ws)
    except Exception:
        state.ws.disconnect(ws)
