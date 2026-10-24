"""弹幕监听：Danmu 数据类 + DANMU_MSG 解析 + DanmakuListener（mock/live）。"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Optional, Tuple

import httpx

_EMOJI_RE = re.compile(r"^\[[^\[\]]{1,30}\]$")

_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DanmakuError(RuntimeError):
    """弹幕连接异常（带可读原因，直接展示在控制台状态里）。"""


class DanmakuAuthError(DanmakuError):
    """认证没成功/超时：token 被风控、房间号错、房间不允许匿名等。"""


def describe_error(e: BaseException) -> str:
    """把异常变成给主播看的一句话（日志里保留原始堆栈）。"""
    if isinstance(e, DanmakuError):
        return str(e)
    name = type(e).__name__
    msg = str(e) or name
    if "ConnectionClosed" in name or "ConnectionClosed" in msg:
        # 拿旧 token / 被风控时 B站 的直接反应就是不回包并断开
        return "弹幕服务器直接断开了连接（token 无效或被风控，也可能是房间号不对）——" \
               "请核对房间号、稍等几分钟再试，或先扫码登录 B站"
    if name in ("InvalidHandshake", "InvalidStatus", "InvalidStatusCode"):
        return f"弹幕服务器拒绝了握手（{name}）：网络可能被代理/防火墙拦截"
    if isinstance(e, TimeoutError) or "Timeout" in name:
        return f"连接 B站超时（{msg}）——检查网络/代理，B站接口可能需要直连"
    if "Connect" in name or "Connection" in name or "SSL" in name:
        return f"连不上 B站服务器（{name}: {msg}）——多半是网络/代理/VPN 拦截"
    if isinstance(e, OSError):
        return f"网络错误（{name}: {msg}）"
    return f"{name}: {msg}"



@dataclass
class Danmu:
    text: str
    uid: int
    uname: str
    medal_level: int = 0
    is_emoji: bool = False
    ts: float = 0.0


def _stable_hash_id(extra_str: str) -> int:
    """uid=0 的新结构：从 extra.user_hash 生成稳定数字 ID。"""
    try:
        payload = json.loads(extra_str)
        user_hash = payload.get("extra", {}).get("user_hash", "") if isinstance(payload, dict) else ""
        if not user_hash and isinstance(payload, dict):
            user_hash = str(payload.get("user_hash", ""))
    except Exception:
        user_hash = extra_str
    return abs(hash(user_hash)) % 10**10 or 1


@dataclass
class Gift:
    """SEND_GIFT 事件摘要（直播排队用来识别灯牌礼物）。"""
    uid: int
    uname: str
    gift_name: str
    num: int = 1
    ts: float = 0.0


def parse_gift_event(data: Any) -> Optional[Gift]:
    """从 B站 SEND_GIFT 事件的 data 解析礼物摘要。"""
    try:
        d = data.get("data") or {}
        uid = int(d.get("uid") or 0)
        uname = str(d.get("uname") or "")
        gift_name = str(d.get("giftName") or "")
        if not gift_name:
            gift_name = str((d.get("gift_info") or {}).get("name") or "")
        num = int(d.get("num") or 1)
    except Exception:
        return None
    if not gift_name:
        return None
    return Gift(uid=uid, uname=uname, gift_name=gift_name, num=num, ts=time.time())


def parse_danmu_event(data: Any) -> Optional[Danmu]:
    """从 B站 DANMU_MSG 事件的 data 解析。"""
    try:
        info = data["info"]
        text = info[1]
        user = info[2]  # [uid, uname, ...]
        uid = user[0]
        uname = user[1]
        medal_level = 0
        try:
            medal_level = user[5] if len(user) > 5 else 0
        except Exception:
            medal_level = 0
    except Exception:
        return None

    if not uid:
        # 新结构 uid=0：遍历 info 嵌套 list 找 {"extra": "..."}
        try:
            for item in info:
                if isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, dict) and "extra" in sub:
                            uid = _stable_hash_id(str(sub.get("extra", "")))
                            break
        except Exception:
            pass

    if not uname:
        uname = f"用户{uid}"

    text = str(text)
    return Danmu(
        text=text,
        uid=int(uid or 0),
        uname=str(uname),
        medal_level=int(medal_level or 0),
        is_emoji=bool(_EMOJI_RE.match(text)),
        ts=time.time(),
    )


_LOGGER = __import__("logging").getLogger("jukebox.danmaku")
log = _LOGGER  # 别名：连接/重连路径上的普通日志


def _dbg(msg: str, *args) -> None:
    """弹幕连接调试日志（排查收不到弹幕时用）。

    v1.1 起真正可见：logging_setup 给 root 装了 INFO 级 handler，
    日志同时进 cache/logs/jukebox.log。旧版这里用 INFO 但没人配 handler，
    Python 的 lastResort 只处理 WARNING+，所以这些行一条都打不出来。
    """
    try:
        _LOGGER.info("[弹幕] " + msg, *args)
    except Exception:
        pass


# ---------------- 自研 B站直播弹幕客户端 ----------------

def _cookie_from_credential(credential: Any) -> str:
    """把 bilibili_api Credential / dict / JSON 字符串 / 原始 Cookie 串统一成 Cookie 头。"""
    if credential is None:
        return ""
    raw = ""
    try:
        d = credential.get_cookies()  # bilibili_api.Credential
        raw = "; ".join(f"{k}={v}" for k, v in (d or {}).items() if v)
    except Exception:
        raw = credential if isinstance(credential, str) else ""
    raw = (raw or "").strip()
    if raw.startswith("{"):  # JSON 字符串形式的 cookie dict
        try:
            d = json.loads(raw)
            raw = "; ".join(f"{k}={v}" for k, v in (d or {}).items() if v)
        except Exception:
            pass
    return raw.replace("\n", " ").replace("\r", " ").strip("; ")


def _cookie_uid(cookie: str) -> int:
    """从 Cookie 串里取 DedeUserID（登录 uid，匿名=0）。"""
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().lower() == "dedeuserid":
                try:
                    return int(v.strip())
                except Exception:
                    return 0
    return 0


async def _fetch_danmu_conf(room_id: int, cookie: str = "") -> Tuple[str, str, int]:
    """拿弹幕 token 与 ws 地址。失败时抛 DanmakuError（带可读原因）。

    注意：bilibili_api 用的 xlive/.../getDanmuInfo 现在对无登录的非浏览器请求
    一律返回 -352 风控（拿不到 token → 认证包发出后服务器不回 → connect() 永久挂起）。
    旧接口 room/v1/Danmu/getConf 仍然正常工作，这里用它。
    带上 B站登录 Cookie 可提升通过率，并可能解锁未开播房间的消息推送。
    """
    headers = {
        "User-Agent": _BILI_UA,
        "Referer": f"https://live.bilibili.com/{int(room_id)}",
        "Origin": "https://live.bilibili.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as h:
            r = await h.get(
                "https://api.live.bilibili.com/room/v1/Danmu/getConf",
                params={"room_id": int(room_id), "platform": "pc", "player": "web"},
                headers=headers,
            )
    except Exception as e:  # 网络层：DNS/代理/TLS
        raise DanmakuError(f"获取弹幕服务器配置失败：{describe_error(e)}") from e

    if r.status_code != 200:
        raise DanmakuError(f"获取弹幕服务器配置失败：HTTP {r.status_code}（可能被风控/代理拦截）")
    try:
        body = r.json() or {}
    except Exception as e:
        raise DanmakuError("获取弹幕服务器配置失败：返回不是 JSON（可能被代理/运营商劫持）") from e

    code = body.get("code")
    if code not in (0, None):
        hint = {
            -352: "被B站风控（-352），过几分钟再试，或在「音源与登录」扫码登录后重试",
            -400: "房间号不对（-400），请填直播间地址里的数字",
            -412: "请求过频被限流（-412），稍等再试",
        }.get(int(code) if isinstance(code, int) else 0, f"接口返回 code={code}: {body.get('message', '')}")
        raise DanmakuError(f"获取弹幕服务器配置失败：{hint}")

    d = body.get("data") or {}
    token = str(d.get("token") or "")
    if not token:
        raise DanmakuError("B站没返回弹幕 token（房间可能不存在/已被封禁，或被风控），无法建立弹幕连接")

    host, port = "", 0
    for hp in d.get("host_server_list") or []:
        if hp.get("host") and (hp.get("wss_port") or hp.get("port")):
            host, port = str(hp["host"]), int(hp.get("wss_port") or hp.get("port"))
            break
    if not host:
        host = str(d.get("host") or "broadcastlv.chat.bilibili.com")
        port = int(d.get("port") or 2245)
    return token, host, port


async def check_room(room_id: int) -> Tuple[str, Optional[dict]]:
    """校验直播间号，返回 (状态, 信息)。

    状态：
    - "ok"      ：B站确认这个房间存在；
    - "invalid" ：B站明确说没有这个房间（最常见的错法：填成了主播 UID）；
    - "unknown" ：查询本身失败（断网/被墙），此时不应阻断用户操作。
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as h:
            r = await h.get(
                "https://api.live.bilibili.com/room/v1/Room/get_info",
                params={"room_id": int(room_id)},
                headers={"User-Agent": _BILI_UA, "Referer": "https://live.bilibili.com/"},
            )
            body = r.json() or {}
    except Exception as e:
        _dbg("查询直播间信息失败 room=%s：%s", room_id, describe_error(e))
        return "unknown", None
    if body.get("code") not in (0, None):
        return "invalid", None
    d = body.get("data") or {}
    if not d:
        return "invalid", None
    return "ok", {
        "room_id": int(d.get("room_id") or 0),
        "uid": int(d.get("uid") or 0),
        "title": str(d.get("title") or ""),
        "live_status": int(d.get("live_status") or 0),
        "anchor": str(d.get("uname") or ""),
    }


async def fetch_room_info(room_id: int) -> Optional[dict]:
    """查询直播间基本信息；查询失败或房间不存在返回 None（仅用于打日志）。"""
    state_name, info = await check_room(room_id)
    return info if state_name == "ok" else None




def _pack(op: int, body: bytes) -> bytes:
    """B站弹幕协议封包：16 字节头 + 正文。"""
    return struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1) + body


def _iter_packets(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """解一帧数据里的所有包（可能粘包），返回 (op, 正文)。

    关键：protover=2(zlib)/3(brotli) 时，一个 op=5 包的正文是**压缩后的连续多个协议包**，
    必须解压后再按协议包结构递归解析——直接 json.loads 解压结果会失败（多个 JSON 连在一起），
    表现为「认证成功但永远收不到弹幕」。
    """
    i = 0
    while i + 16 <= len(data):
        try:
            total, hlen, ver, op, _seq = struct.unpack(">IHHII", data[i : i + 16])
        except Exception:
            break
        if total < 16 or i + total > len(data):
            break
        body = data[i + hlen : i + total]
        i += total
        if ver == 2:  # zlib：正文是压缩后的连续协议包
            try:
                yield from _iter_packets(zlib.decompress(body))
            except Exception:
                pass
        elif ver == 3:  # brotli
            try:
                import brotli  # type: ignore

                yield from _iter_packets(brotli.decompress(body))
            except Exception:
                pass
        else:
            yield op, body


class BiliLiveWS:
    """直连 B站直播弹幕服务器（不依赖 bilibili_api）。

    协议：认证(op=7) -> 服务器回认证成功(op=8) -> 每 30s 心跳(op=2) -> 消息(op=5)。
    """

    OP_HEARTBEAT, OP_MESSAGE, OP_AUTH, OP_AUTH_OK = 2, 5, 7, 8
    #: 认证回包最长等待（秒）；超时按失败处理并给出人话原因
    _AUTH_TIMEOUT = 15.0
    #: 协议版本：1=不压缩，2=zlib。B站新版只对 protover>=2 的客户端下发消息，
    #: 用 1 会出现「认证成功但永远收不到弹幕」。
    _PROTOVER = 2

    def __init__(self, room_id: int, on_event: Callable[[dict], Awaitable[None]], cookie: str = ""):
        self.room_id = int(room_id)
        self.on_event = on_event
        self.cookie = _cookie_from_credential(cookie)
        self.uid = _cookie_uid(self.cookie)
        self._ws = None
        self._hb: Optional[asyncio.Task] = None
        self._closed = False
        self._frames = 0
        #: 最后一次收到任何数据（含心跳回包）的时刻，给上层看门狗判断「假连接」
        self.last_activity = time.monotonic()

    def _buvid_from_cookie(self) -> str:
        for part in (self.cookie or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip().lower() == "buvid3":
                    return v.strip()
        return ""

    async def _ensure_buvid(self) -> Tuple[str, str]:
        """返回 (buvid3, buvid4)。匿名连接需要真实 buvid（从 /x/frontend/finger/spi 取），
        否则 B站在未开播房间可能不推送消息。登录态则直接用 Cookie 里的 buvid3。"""
        buvid = self._buvid_from_cookie()
        if buvid:
            return buvid, ""
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as h:
                r = await h.get(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    headers={"User-Agent": _BILI_UA},
                )
                d = ((r.json() or {}).get("data") or {})
                return str(d.get("b_3") or ""), str(d.get("b_4") or "")
        except Exception:
            return "", ""

    async def run(self) -> None:
        import websockets

        token, host, port = await _fetch_danmu_conf(self.room_id, self.cookie)
        buvid, buvid4 = await self._ensure_buvid()
        url = f"wss://{host}:{port}/sub"
        _dbg(f"房间 {self.room_id} 弹幕服务器 {url} token_len={len(token)} "
             f"登录uid={self.uid or '匿名'} buvid3={'有' if buvid else '无'}")
        # 握手带上浏览器式请求头 + Cookie（含 buvid3/buvid4/CURRENT_FNVAL，对齐官方 web 端）
        headers = {
            "User-Agent": _BILI_UA,
            "Origin": "https://live.bilibili.com",
        }
        cookie = self.cookie or ""
        if buvid and "buvid3=" not in cookie:
            cookie = (cookie + ";" if cookie else "") + f"buvid3={buvid};"
            if buvid4:
                cookie += f"buvid4={buvid4};"
            cookie += "CURRENT_FNVAL=4048"
        if cookie:
            headers["Cookie"] = cookie
        try:
            ws_ctx = websockets.connect(url, open_timeout=15, ping_interval=None,
                                        additional_headers=headers)
        except TypeError:  # 旧版 websockets 参数名不同
            ws_ctx = websockets.connect(url, open_timeout=15, ping_interval=None,
                                        extra_headers=headers)
        try:
            ws = await ws_ctx.__aenter__()
        except Exception as e:
            raise DanmakuError(
                f"连不上弹幕服务器 {host}:{port}（{describe_error(e)}）"
            ) from e
        try:
            self._ws = ws
            body = json.dumps(
                {
                    "uid": self.uid,
                    "roomid": self.room_id,
                    "protover": self._PROTOVER,
                    "platform": "web",
                    "type": 2,
                    "key": token,
                    "buvid": buvid,
                },
                separators=(",", ":"),
            ).encode()
            _dbg(f"发送认证包 protover={self._PROTOVER} uid={self.uid} buvid={'有' if buvid else '无'}")
            await ws.send(_pack(self.OP_AUTH, body))
            self._hb = asyncio.create_task(self._heartbeat())

            # 等 AUTH_OK：旧版没有超时，token 被风控时服务器**什么都不回**，
            # 界面就永远停在「连接中…」，用户只能看到「连不上」。
            deadline = time.monotonic() + self._AUTH_TIMEOUT
            authed = False
            while True:
                timeout = None if authed else max(0.0, deadline - time.monotonic())
                if timeout is not None and timeout <= 0:
                    raise DanmakuAuthError(
                        f"弹幕认证超时（{self._AUTH_TIMEOUT}s 没收到 AUTH_OK）："
                        "常见原因是 B站风控拿不到有效 token、房间号填错、或该房间禁止匿名连接；"
                        "请确认房间号，并在「音源与登录」扫码登录 B站 后重试"
                    )
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise DanmakuAuthError(
                        f"弹幕认证超时（{self._AUTH_TIMEOUT}s 没收到 AUTH_OK）："
                        "常见原因是 B站风控拿不到有效 token、房间号填错、或该房间禁止匿名连接"
                    )
                except DanmakuError:
                    raise
                except Exception as e:
                    # 服务器直接断开（旧 token / 被风控）也换算成人话
                    raise DanmakuError(describe_error(e)) from e
                if isinstance(msg, str):
                    continue
                self.last_activity = time.monotonic()
                for op, payload in _iter_packets(bytes(msg)):
                    if op == self.OP_AUTH_OK and not authed:
                        # 认证回包正文是 {"code":0}；非 0 说明 token/房间有问题
                        code = None
                        try:
                            code = json.loads(payload.decode("utf-8", "ignore")).get("code")
                        except Exception:
                            pass
                        if code not in (None, 0):
                            raise DanmakuAuthError(
                                f"弹幕认证被拒绝（code={code}）：token 失效或房间号错误，请重新扫码登录后重试"
                            )
                        authed = True
                        _dbg(f"认证成功（房间 {self.room_id}），开始接收消息")
                        await self.on_event({"cmd": "__AUTH_OK__"})
                        continue
                    self._frames += 1
                    # 前 20 帧 + 之后每 200 帧记一条：热闹的直播间每秒几十条，
                    # 全量写文件会把日志撑爆，反而看不见真正的报错
                    if self._frames <= 20 or self._frames % 200 == 0:
                        _dbg(f"收到第 {self._frames} 帧 op={op} len={len(payload)} "
                             f"{payload[:160].decode('utf-8', 'ignore')!r}")
                    else:
                        _LOGGER.debug("帧 %s op=%s len=%s", self._frames, op, len(payload))
                    await self._handle(op, payload)
        finally:
            self._closed = True
            if self._hb:
                self._hb.cancel()
            try:
                await ws_ctx.__aexit__(None, None, None)
            except Exception:
                pass

    async def _heartbeat(self) -> None:
        try:
            while not self._closed and self._ws is not None:
                await asyncio.sleep(10)
                await self._ws.send(_pack(self.OP_HEARTBEAT, b"[object Object]"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 心跳断了说明连接其实已经不可用；交给读循环/重连处理，但要留日志
            _dbg(f"心跳失败（{describe_error(e)}），准备重连")

    async def _handle(self, op: int, payload: bytes) -> None:
        if op == self.OP_AUTH_OK:
            await self.on_event({"cmd": "__AUTH_OK__"})
            return
        if op != self.OP_MESSAGE or not payload:
            return
        try:
            info = json.loads(payload.decode("utf-8", "ignore"))
        except Exception:
            return
        if isinstance(info, dict) and info.get("cmd"):
            await self.on_event(info)

    async def close(self) -> None:
        self._closed = True
        if self._hb:
            self._hb.cancel()
            try:
                await self._hb
            except (asyncio.CancelledError, Exception):
                pass
            self._hb = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


class DanmakuListener:
    """弹幕监听器：mock 模式只注入；live 模式直连 B站弹幕服务器。"""

    def __init__(
        self,
        room_id: int,
        on_danmu: Callable[[Danmu], Awaitable],
        on_status: Optional[Callable[[dict], Awaitable]] = None,
        credential: Any = None,
        mock: bool = False,
        on_gift: Optional[Callable[[Gift], Awaitable]] = None,
    ):
        self.room_id = int(room_id or 0)
        self.on_danmu = on_danmu
        self.on_status = on_status
        self.credential = credential
        self.mock = mock
        self.on_gift = on_gift
        self.connected = False
        self.total_received = 0
        self.status = "idle"
        self.live_status: Optional[int] = None  # 0=未开播 1=直播中 2=轮播
        self.last_error = ""
        self._task: Optional[asyncio.Task] = None
        self._session = None  # LiveDanmaku 实例
        self._closed = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._forced_reconnect = False
        self._seen: dict[str, float] = {}   # 事件去重：key -> 最近一次到达时间
        self._dup_count = 0                 # 累计压掉的重复事件数（诊断用）

    async def _emit_status(self) -> None:
        if self.on_status:
            try:
                await self.on_status(self.status_dict())
            except Exception:
                pass

    def status_dict(self) -> dict:
        return {
            "mode": "mock" if self.mock else "live",
            "room_id": self.room_id,
            "connected": self.connected,
            "total_received": self.total_received,
            "live_status": self.live_status,
            "error": self.last_error,
            "duplicates_dropped": self._dup_count,
        }

    async def _mark_connected(self) -> None:
        if not self.connected:
            self.connected = True
            self.status = "connected"
            self.last_error = ""
            log.info("已连接直播间 %s（模式 live）", self.room_id)
            await self._emit_status()

    def _on_task_done(self, task: "asyncio.Task") -> None:
        """连接任务结束回调：把异常/断开真实反映到状态里（以前是被静默吞掉）。"""
        if task.cancelled():
            return
        err = None
        try:
            err = task.exception()
        except Exception:
            return
        self.connected = False
        if err is not None:
            self.last_error = describe_error(err)
            self.status = f"error: {self.last_error}"
            log.warning("弹幕连接结束：%s", self.last_error, exc_info=err)
        else:
            self.status = "disconnected"
        try:
            asyncio.get_running_loop().create_task(self._emit_status())
        except RuntimeError:
            pass

    async def start(self) -> None:
        if self.mock:
            self.status = "mock"
            await self._emit_status()
            return
        self._closed = False
        self._task = asyncio.create_task(self._run_loop())
        self._task.add_done_callback(self._on_task_done)
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    #: 连接「看着是好的」但超过这个秒数没收到任何数据（心跳回包也算）→ 判定假连接
    _IDLE_LIMIT = 90.0

    async def _watchdog_loop(self) -> None:
        """连接看门狗：B站偶发「TCP 还在、但不再推任何消息」的假连接。

        旧版只能靠用户发现「怎么没弹幕了」；这里主动断开逼重连，
        并以一条明确日志记录下来（日志页一眼可见）。
        """
        while not self._closed:
            await asyncio.sleep(15)
            try:
                if self._closed or not self.connected or self._session is None:
                    continue
                last = getattr(self._session, "last_activity", 0.0)
                idle = time.monotonic() - last
                if idle > self._IDLE_LIMIT:
                    log.warning(
                        "直播间 %s 已 %.0fs 收不到任何数据（含心跳回包），判定为假连接，主动重连",
                        self.room_id, idle,
                    )
                    self._forced_reconnect = True
                    self.last_error = f"连接假死（{int(idle)} 秒无任何数据），已自动重连"
                    await self._session.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("看门狗检查异常（忽略）：%s", describe_error(e))

    async def _run_loop(self) -> None:
        """连接 + 自动重连循环（网络抖动时无限重试）。

        网络抖动/服务器断开时回到这里重连，而不是静默停在 disconnected。
        total_received 在同一 listener 实例内累计，重连不清零。

        v1.1：每次尝试都写日志（第几次、房间号、失败原因），
        并且连上超过 60 秒后把退避重新压回 3 秒——旧版退避只涨不降，
        断过一次之后要等 30 秒才重连，主播会以为「彻底连不上了」。
        """
        backoff = 3
        attempt = 0
        while not self._closed:
            attempt += 1
            t0 = time.monotonic()
            try:
                # 主播身份连接：未开播房间的弹幕推送很可能要求登录态
                cookie = _cookie_from_credential(self.credential)
                self._session = BiliLiveWS(self.room_id, self._on_event, cookie=cookie)
                self.status = "connecting"
                await self._emit_status()
                uid = _cookie_uid(cookie)
                if cookie and not uid:
                    # 有 Cookie 但缺 DedeUserID：B站 会按匿名处理 → 观众昵称全是星号
                    log.warning(
                        "弹幕凭据里有 Cookie 却缺少 DedeUserID，B站会按匿名连接处理"
                        "（表现：所有观众名字都是星号）——建议重新扫码登录 B站"
                    )
                log.info(
                    "连接直播间 %s（第 %s 次尝试，%s）",
                    self.room_id, attempt,
                    f"已登录 uid={_cookie_uid(cookie)}" if cookie else "匿名（昵称会脱敏成星号）",
                )
                await self._session.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._forced_reconnect:
                    # 看门狗主动断开的，别把它报成「服务器拒绝/网络错误」
                    log.info("按看门狗决定重连（原异常：%s）", describe_error(e))
                else:
                    self.last_error = describe_error(e)
                    log.warning("弹幕连接失败（第 %s 次）：%s", attempt, self.last_error, exc_info=True)
                self._forced_reconnect = False
            else:
                # run() 正常返回 = 服务器断开（少见）
                self.last_error = "连接被服务器关闭"
                log.info("弹幕连接被服务器关闭（已连 %.1fs）", time.monotonic() - t0)
            finally:
                self.connected = False
                if self._session is not None:
                    try:
                        await self._session.close()
                    except Exception:
                        pass
                    self._session = None
            if self._closed:
                break
            # 连了足够久：这是「偶发断开」，退避回到最短，避免长时间空等
            if time.monotonic() - t0 >= 60:
                backoff = 3
            self.status = "reconnecting" + (f": {self.last_error}" if self.last_error else "")
            await self._emit_status()
            log.info("将在 %ss 后重连直播间 %s", backoff, self.room_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


    async def _on_event(self, info: dict) -> None:
        """弹幕服务器事件分发（自研客户端回调）。"""
        cmd = (info or {}).get("cmd") or ""
        if cmd == "__AUTH_OK__":
            # 认证成功：没开播/没人发弹幕时也能正确显示「已连接」
            await self._mark_connected()
            await self._refresh_live_status()
            return
        if cmd == "DANMU_MSG":
            if self._is_duplicate("danmu", info):
                return
            await self._handle(info)
            return
        if cmd == "SEND_GIFT":
            # 礼物事件：直播排队的灯牌插队依赖它
            await self._mark_connected()
            if self._is_duplicate("gift", info):
                return
            g = parse_gift_event(info)
            if g is not None and self.on_gift:
                try:
                    await self.on_gift(g)
                except Exception:
                    pass
            return
        # 其他消息（进场/点赞/礼物等）也说明连接是健康的
        if cmd:
            await self._mark_connected()

    #: 同一条事件在这么多秒内再次到达 → 判定重复（B站会重发，多连接也会各来一份）
    _DEDUP_WINDOW = 10.0

    def _is_duplicate(self, kind: str, info: dict) -> bool:
        """按消息 id 去重：一次发送只处理一次。

        为什么需要（2026-10-24 用户反馈）：主播明明只发了一条弹幕，
        控制台「弹幕日志」里出现三条一模一样的，排队/灯牌也会被重复计数。
        B站 在重连、或多条连接时会把同一条事件重复下发；这里按
        「弹幕 id / 礼物 uid+id+ts」压掉，只保留第一次。
        """
        now = time.monotonic()
        try:
            seen = self._seen
            if len(seen) > 2000:
                for k, t in list(seen.items()):
                    if now - t > self._DEDUP_WINDOW:
                        seen.pop(k, None)
            key = self._event_key(kind, info)
            last = seen.get(key)
            if last is not None and now - last <= self._DEDUP_WINDOW:
                self._dup_count += 1
                if self._dup_count <= 5 or self._dup_count % 100 == 0:
                    log.info("忽略重复%s（10 秒内第 %s 次）", "礼物" if kind == "gift" else "弹幕", self._dup_count)
                return True
            seen[key] = now
            return False
        except Exception:
            return False

    @staticmethod
    def _event_key(kind: str, info: dict) -> str:
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        if kind == "gift":
            return "gift:{}:{}:{}:{}".format(
                data.get("uid"), data.get("giftId") or data.get("giftName"),
                data.get("timestamp"), data.get("num"),
            )
        try:
            arr = info.get("info") or []
            uid = arr[2][0]
            text = arr[1]
            dm_id = arr[0][5] if len(arr[0]) > 5 else ""
            return f"danmu:{dm_id}" if dm_id else f"danmu:{uid}:{text}"
        except Exception:
            return f"danmu:raw:{id(info)}"

    async def _refresh_live_status(self) -> None:
        """查询直播间开播状态（0=未开播 1=直播中 2=轮播），用于 UI 提示。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as h:
                r = await h.get(
                    f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={self.room_id}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                data = (r.json() or {}).get("data") or {}
                self.live_status = data.get("live_status")
                log.info(
                    "直播间 %s 状态：%s（标题：%s）",
                    self.room_id,
                    {0: "未开播", 1: "直播中", 2: "轮播"}.get(self.live_status, f"未知({self.live_status})"),
                    (data.get("title") or "")[:40],
                )
                await self._emit_status()
        except Exception as e:
            log.info("查询直播间开播状态失败：%s", describe_error(e))

    async def _handle(self, data: Any) -> None:
        d = parse_danmu_event(data)
        if d is None:
            return
        await self._mark_connected()
        self.total_received += 1
        try:
            await self.on_danmu(d)
        except Exception:
            pass

    async def stop(self) -> None:
        self._closed = True  # 先让重连循环退出，再取消任务，避免又自动连上
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # cancel() 后 await 会抛 CancelledError（3.8+ 是 BaseException，
                # except Exception 抓不到），漏掉它会让重连/关停整个接口 500
                pass
            self._task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self.connected = False
        self.status = "stopped"

    async def inject(self, text: str, uname: str = "测试用户", uid: int = 0) -> None:
        """注入模拟弹幕（mock 调试用）。"""
        d = Danmu(text=text, uid=int(uid or 0), uname=uname, ts=time.time())
        self.total_received += 1
        await self.on_danmu(d)
