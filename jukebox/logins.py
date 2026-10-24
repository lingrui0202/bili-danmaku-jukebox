"""四平台扫码登录：B站 / QQ音乐 / 酷狗 / 网易云。

统一接口：
    mgr = LoginManager(http_client)
    session = await mgr.start("qq")   # 生成二维码 -> {"qr": data-url, "key": ...}
    st = await mgr.poll("qq")         # {"status": "waiting/scanned/confirmed/expired/done/error", ...}
    mgr.cancel("qq")

状态机：pending（创建中）-> waiting（等待扫码）-> scanned（已扫码待确认）
        -> done（成功，带 cookie）/ expired（过期）/ error（失败）。

坑点（SPEC 第 9 章）逐一实现，详见各类注释。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

# ---------------- 通用基类 ----------------

_PENDING_STATES = ("pending", "waiting", "scanned", "confirmed")


_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _new_login_client(impersonate: bool = False):
    """创建登录会话的 HTTP 客户端。

    impersonate=True 时优先用 curl_cffi 伪装 Chrome 的 TLS/HTTP2 指纹：
    网易云等平台会按客户端指纹拦截第三方——无风险的轮询能过，
    但扫码确认后签发登录票据那一步会返回 8821「请切换其他登录方式或升级新版本」。
    """
    if impersonate:
        try:
            from curl_cffi.requests import AsyncSession

            s = AsyncSession(impersonate="chrome")
            try:
                s.headers.update({"User-Agent": _DEFAULT_UA})
            except Exception:
                pass
            return s
        except Exception:
            pass  # 没装就退回 httpx，功能不缺失（只是可能被风控）
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, read=60), follow_redirects=True,
        headers={"User-Agent": _DEFAULT_UA},
    )


def _parse_ptuicb(text: str) -> list:
    """解析 ptlogin 的 ptuiCB('0','url','msg',...) 回调。

    用引号感知的分割，避免 URL/msg 内含逗号或括号时截断。
    """
    if not text:
        return []
    m = re.search(r"ptuiCB\((.*)\)\s*;?\s*$", text, re.S)
    if not m:
        return []
    body = m.group(1)
    args: list = []
    cur: list = []
    in_q = False
    for ch in body:
        if ch == "'":
            in_q = not in_q
            cur.append(ch)
        elif ch == "," and not in_q:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return [a.strip().strip("'\"") for a in args]


class QrLoginSession:
    """扫码登录会话基类。"""

    #: 二维码有效期（秒），客户端过期判断
    QR_TTL = 180
    #: 真实轮询最小间隔（秒）：前端可以随便调 API，但打向平台的请求被节流，
    #: 防止高频轮询触发平台风控（网易云 8821 之类）
    POLL_MIN_INTERVAL = 0.0
    #: 是否用伪装浏览器的 TLS 指纹（对客户端指纹做风控的平台需要）
    IMPERSONATE = False

    def __init__(self, http_client=None):
        # 每个登录会话持有独立的客户端：Cookie 作用域隔离，
        # 避免与其他源/其他平台会话互相污染（QQ qrsig / 网易 unikey 等都靠 Cookie 关联）
        self._impersonated = bool(self.IMPERSONATE) and http_client is None
        self.http = http_client or _new_login_client(self.IMPERSONATE)
        self._owns_http = http_client is None
        self.qr = ""          # data URL
        self.key = ""
        self.status = "pending"
        self.detail = ""
        self.cookie = ""
        self.created_at = time.time()
        self.expires_at = time.time() + self.QR_TTL
        self._last_poll = 0.0
        self._last_poll_result = {"status": self.status, "detail": "", "key": ""}

    async def aclose(self) -> None:
        """关闭会话持有的客户端（会话结束时调用）。兼容 httpx 与 curl_cffi。"""
        if self._owns_http and self.http is not None:
            try:
                close = getattr(self.http, "aclose", None) or getattr(self.http, "close", None)
                if close is not None:
                    r = close()
                    if asyncio.iscoroutine(r):
                        await r
            except Exception:
                pass
            self.http = None

    async def _get(self, url, follow: bool = True, **kw):
        """统一 httpx(follow_redirects) 与 curl_cffi(allow_redirects) 的差异。"""
        if self._impersonated:
            return await self.http.get(url, allow_redirects=follow, **kw)
        return await self.http.get(url, follow_redirects=follow, **kw)

    async def _post(self, url, follow: bool = True, **kw):
        if self._impersonated:
            return await self.http.post(url, allow_redirects=follow, **kw)
        return await self.http.post(url, follow_redirects=follow, **kw)

    def result(self, with_qr: bool = True) -> dict:
        d = {"status": self.status, "detail": self.detail, "key": self.key}
        if with_qr and self.status in _PENDING_STATES:
            d["qr"] = self.qr
        # done 时才带 cookie（Cookie 不明文回传给前端 API 的语义）
        if self.status == "done" and self.cookie:
            d["cookie"] = self.cookie
        return d

    async def start(self) -> None:
        """生成二维码（子类实现）。"""
        raise NotImplementedError

    async def poll(self) -> dict:
        """轮询状态：带节流，间隔内直接返回上次结果。"""
        now = time.time()
        if now - self._last_poll < self.POLL_MIN_INTERVAL:
            return self._last_poll_result
        self._last_poll = now
        r = await self._do_poll()
        self._last_poll_result = r
        return r

    async def _do_poll(self) -> dict:
        """真实轮询（子类实现）。"""
        raise NotImplementedError

    def _check_expired(self) -> bool:
        if self.status in ("done", "expired", "error"):
            return False
        if time.time() > self.expires_at:
            self.status = "expired"
            self.detail = "二维码已过期，请刷新"
            return True
        return False


# ---------------- B站 ----------------

def _qr_data_url(text: str) -> str:
    """把二维码内容渲染成 data URL（前端直接 <img src=...>）。"""
    import qrcode

    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class BilibiliQrSession(QrLoginSession):
    """B站扫码登录（直连官方 passport web 二维码接口）。

    为什么不用 bilibili_api 的 login_v2（2026-10-24 实测踩坑，必须记下来）：
        `qr_login.get_credential()` 返回的 Credential 里 **SESSDATA/bili_jct/
        DedeUserID 全是空字符串**，只有 ac_time_value 有值。我们当初把
        `json.dumps(cred.get_cookies())` 存下来，看起来是一串非空 JSON，
        于是「已登录」判断通过、界面显示登录成功，但弹幕连接 uid=0 → 匿名，
        观众昵称全是星号（用户反馈：明明登录了还是匿名）。
    现在改成直连官方接口，Cookie 从 Set-Cookie 与回调 URL 里取，
    并且**校验 SESSDATA 非空**才算登录成功。
    """

    QR_TTL = 180
    POLL_MIN_INTERVAL = 1.5

    GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    HEADERS = {
        "User-Agent": _DEFAULT_UA,
        "Referer": "https://www.bilibili.com/",
    }

    async def start(self) -> None:
        try:
            r = await self._get(self.GEN_URL, headers=self.HEADERS)
            data = (r.json() or {}).get("data") or {}
            url = str(data.get("url") or "")
            key = str(data.get("qrcode_key") or "")
            if not url or not key:
                raise RuntimeError(f"接口没返回二维码（code={((r.json() or {}).get('code'))}）")
            self.key = key
            self.qr = _qr_data_url(url)
            self.status = "waiting"
            self.detail = "请使用哔哩哔哩 APP 扫码"
        except Exception as e:
            self.status = "error"
            self.detail = f"二维码生成失败: {e}"

    async def _do_poll(self) -> dict:
        if self.status in ("done", "error"):
            return self.result()
        if self._check_expired():
            return self.result()
        try:
            r = await self._get(
                self.POLL_URL,
                params={"qrcode_key": self.key, "source": "main-fe-header"},
                headers=self.HEADERS,
            )
            body = r.json() or {}
            data = body.get("data") or {}
            code = data.get("code")

            if code == 0:
                cookies: dict[str, str] = {}
                # ① Set-Cookie（官方就是把登录票据发在这里）
                try:
                    for c in r.cookies.jar:
                        cookies[c.name] = c.value
                except Exception:
                    pass
                # ② 回调 URL 的 query 里也带一份（跨域跳转场景）
                try:
                    from urllib.parse import parse_qs, urlparse

                    q = parse_qs(urlparse(str(data.get("url") or "")).query)
                    for k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                        if k in q and not cookies.get(k):
                            cookies[k] = q[k][0]
                except Exception:
                    pass

                if not (cookies.get("SESSDATA") or "").strip():
                    # 最坑的情况：接口说成功但没给票据 —— 宁可报错，也不要假装登录成功
                    self.status = "error"
                    self.detail = "登录接口没有返回 SESSDATA（B站风控或接口变更），请重新扫码"
                    return self.result()

                self.cookie = json.dumps(cookies, ensure_ascii=False)
                self.status = "done"
                self.detail = f"登录成功（uid {cookies.get('DedeUserID') or '未知'}）"
            elif code in (86101, 86090, 86038):
                if code == 86090:
                    self.status = "scanned"
                    self.detail = "已扫码，请在手机上确认"
                elif code == 86038:
                    self.status = "expired"
                    self.detail = "二维码已过期"
                else:
                    self.status = "waiting"
                    self.detail = "等待扫码"
            else:
                self.status = "error"
                self.detail = f"轮询返回异常 code={code} {data.get('message') or ''}".strip()
        except Exception as e:
            self.status = "error"
            self.detail = f"轮询失败: {e}"
        return self.result()


# ---------------- QQ音乐 ----------------

class QQMusicQrSession(QrLoginSession):
    """QQ音乐扫码登录（ptlogin2 扫码 + QQConnect 换 Cookie）。

    坑：① qrsig 只在二维码接口的 Set-Cookie 里，必须显式收；
        ② 授权码在 302 的 Location 里，不能开 follow_redirects；
        ③ gtk 算法是 h=5381; for ch: h += (h<<5)+ord(ch); h & 0x7FFFFFFF。
    """

    QR_TTL = 180
    POLL_MIN_INTERVAL = 2.0
    # QQ 用普通 httpx 客户端已实测完整跑通（u1 修复 + check_sig 取参修复后），
    # 这里不再改用伪装指纹，避免引入回归
    IMPERSONATE = False
    AID = "716027609"
    PT_3RD_AID = "100497308"
    DAID = "383"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    @staticmethod
    def _ptqrtoken(qrsig: str) -> int:
        e = 0
        for ch in qrsig:
            e += (e << 5) + ord(ch)
        return 2147483647 & e

    @staticmethod
    def _gtk(p_skey: str) -> int:
        h = 5381
        for ch in p_skey:
            h += (h << 5) + ord(ch)
        return h & 0x7FFFFFFF

    async def start(self) -> None:
        try:
            # 坑：带 u1 参数会 403，实测去掉才能拿到二维码图片
            params = {
                "appid": self.AID,
                "e": 2,
                "l": "M",
                "s": 3,
                "d": 72,
                "v": 4,
                "t": str(round(time.time() * 1000)),
                "daid": self.DAID,
                "pt_3rd_aid": self.PT_3RD_AID,
            }
            r = await self._get(
                "https://ssl.ptlogin2.qq.com/ptqrshow", params=params
            )
            if r.status_code != 200 or not r.content:
                self.status = "error"
                self.detail = "二维码获取失败"
                return
            self.qr = "data:image/png;base64," + base64.b64encode(r.content).decode()
            # qrsig 只在 Set-Cookie 里
            qrsig = r.cookies.get("qrsig", "")
            if not qrsig:
                self.status = "error"
                self.detail = "未拿到 qrsig"
                return
            self.qrsig = qrsig
            self.ptqrtoken = self._ptqrtoken(qrsig)
            self.status = "waiting"
            self.detail = "请使用 QQ 扫码"
        except Exception as e:
            self.status = "error"
            self.detail = f"二维码获取失败: {e}"

    async def _do_poll(self) -> dict:
        if self.status in ("done", "error"):
            return self.result()
        if self._check_expired():
            return self.result()
        try:
            params = {
                # 坑：u1 传 "login_jump" 会被 ptqrlogin 拒绝（HTTP 403 空响应），
                # 导致轮询永远解析不出状态码（扫没扫根本不知道）；必须传正常跳转地址。
                "u1": "https://graph.qq.com/login_success.html",
                "ptqrtoken": self.ptqrtoken,
                "ptredirect": 0,
                "h": 1,
                "t": 1,
                "g": 1,
                "from_ui": 1,
                "ptlang": 2052,
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": 23111510,
                "js_type": 1,
                "aid": self.AID,
                "daid": self.DAID,
                "pt_3rd_aid": self.PT_3RD_AID,
                "o1vId": secrets.token_hex(16),
                "pt_js_version": "v1.48.1",
            }
            headers = {"Cookie": f"qrsig={self.qrsig}", "Referer": "https://ssl.ptlogin2.qq.com/"}
            r = await self._get(
                "https://ssl.ptlogin2.qq.com/ptqrlogin",
                params=params,
                headers=headers,
                follow=False,
            )
            args = _parse_ptuicb(r.text or "")
            code = args[0] if args else ""
            if code == "0":
                # ptuiCB('0','0','https://...check_sig?...','0','登录成功！','昵称')
                # check_sig 地址在第 3 个参数（index 2）；稳妥起见直接找 http 开头的参数
                check_sig_url = next((a for a in args if str(a).startswith("http")), "")
                if not check_sig_url:
                    self.status = "error"
                    self.detail = "缺少 check_sig_url"
                else:
                    try:
                        self.cookie = await self._exchange(check_sig_url)
                        self.status = "done"
                        self.detail = "登录成功"
                    except Exception as e:
                        # 换票失败单独标错，弹窗能看到具体卡在哪一步
                        self.status = "error"
                        self.detail = f"换票失败：{e}"
            elif code == "65":
                self.status = "expired"
                self.detail = "二维码已过期"
            elif code == "67":
                self.status = "scanned"
                self.detail = "已扫码，请在手机上确认"
            else:
                # 带原始 code（66=未扫码 / 68=…），便于排查「扫完没反应」
                self.status = "waiting"
                self.detail = f"等待扫码（code={code}）"
        except Exception as e:
            self.status = "error"
            self.detail = f"轮询失败: {e}"
        return self.result()

    async def _exchange(self, check_sig_url: str) -> str:
        """确认后换 Cookie：check_sig 收登录 Cookie -> gtk -> 授权码 -> QQLogin。

        Cookie 全部走会话 jar（与浏览器一致），避免手工合并漏掉跳转链上的 Set-Cookie。
        """
        # 1. GET check_sig_url（跟随跳转），uin/skey/p_skey 等登录 Cookie 进 jar
        await self._get(check_sig_url)
        jar = {k: v for k, v in self.http.cookies.items() if k}
        p_skey = jar.get("p_skey", "")
        if not p_skey:
            raise RuntimeError("check_sig 未返回 p_skey（登录 Cookie 不完整）")
        gtk = self._gtk(p_skey)

        # 2. POST graph.qq.com/oauth2.0/authorize（Cookie 由 jar 自动携带），302 Location 取授权码
        auth_params = {
            "response_type": "code",
            "client_id": self.PT_3RD_AID,
            "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
            "scope": "get_user_info,get_app_friends",
            "state": "state",
            "from_ptlogin": 1,
            "src": 1,
            "update_auth": 1,
            "openapi": "1010_1030",
            "g_tk": gtk,
            "auth_time": str(round(time.time() * 1000)),
            "ui": secrets.token_hex(16),
        }
        r2 = await self._post(
            "https://graph.qq.com/oauth2.0/authorize",
            data=auth_params,
            headers={"Referer": "https://graph.qq.com/"},
            follow=False,
        )
        m = re.search(r"[?&]code=([^&]+)", r2.headers.get("location", ""))
        if not m:
            raise RuntimeError("授权码获取失败（Location 无 code）")
        auth_code = m.group(1)

        # 3. POST musicu.fcg QQLogin，换取 QQ 音乐登录 Cookie
        payload = {
            "comm": {"g_tk": gtk, "platform": "yqq", "ct": 24, "cv": 0},
            "req": {
                "module": "QQConnectLogin.LoginServer",
                "method": "QQLogin",
                "param": {"code": auth_code},
            },
        }
        await self._post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json=payload,
            headers={"Referer": "https://y.qq.com/"},
        )
        out = {k: v for k, v in self.http.cookies.items() if k}
        return "; ".join(f"{k}={v}" for k, v in out.items())


# ---------------- 酷狗 ----------------

class KugouQrSession(QrLoginSession):
    """酷狗扫码登录（login-user.kugou.com v2 二维码）。

    坑：① 必须带完整参数 + 签名：appid/srcappid/clientver/clienttime(毫秒)/mid/uuid/dfid，
          签名 md5(salt + 排序 k=v 串 + salt)，盐值出现两次；
        ② data 可能是字符串（参数错），要判类型；
        ③ status 4=成功（拿 token/userid + 响应 Cookie）。
    """

    QR_TTL = 180
    SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    APPID = "1005"
    SRCAPPID = "2919"
    CLIENTVER = "8131"
    H5 = "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=1005&"

    def _headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

    @staticmethod
    def _sign(params: dict) -> str:
        s = "".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.md5(
            (KugouQrSession.SALT + s + KugouQrSession.SALT).encode()
        ).hexdigest()

    def _base_params(self, **extra) -> dict:
        """生成/轮询二维码共用的基础参数；mid/uuid 会话内保持一致。"""
        if not getattr(self, "mid", ""):
            self.mid = hashlib.md5(
                f"{time.time()}{secrets.token_hex(8)}".encode()
            ).hexdigest()
        p = {
            "appid": self.APPID,
            "clienttime": str(int(time.time() * 1000)),
            "clientver": self.CLIENTVER,
            "dfid": "-",
            "mid": self.mid,
            "plat": "4",
            "srcappid": self.SRCAPPID,
            "uuid": self.mid,
        }
        p.update(extra)
        return p

    async def start(self) -> None:
        try:
            params = self._base_params(type="1", qrcode_txt=self.H5)
            params["signature"] = self._sign(params)
            r = await self._get(
                "https://login-user.kugou.com/v2/qrcode", params=params, headers=self._headers()
            )
            data = (r.json() or {}).get("data")
            if not isinstance(data, dict):
                self.status = "error"
                self.detail = "二维码获取失败（data 异常）"
                return
            self.key = str(data.get("qrcode") or "")
            img = data.get("qrcode_img") or ""
            if not self.key or not img:
                self.status = "error"
                self.detail = "二维码获取失败"
                return
            if img.startswith("data:"):
                self.qr = img
            else:
                self.qr = "data:image/png;base64," + img
            self.status = "waiting"
            self.detail = "请使用酷狗 APP 扫码"
        except Exception as e:
            self.status = "error"
            self.detail = f"二维码获取失败: {e}"

    async def _do_poll(self) -> dict:
        if self.status in ("done", "error"):
            return self.result()
        if self._check_expired():
            return self.result()
        try:
            params = self._base_params(qrcode=self.key)
            params["signature"] = self._sign(params)
            r = await self._get(
                "https://login-user.kugou.com/v2/get_userinfo_qrcode",
                params=params,
                headers=self._headers(),
            )
            data = (r.json() or {}).get("data")
            if not isinstance(data, dict):
                # data 可能是字符串（参数错）
                self.status = "error"
                self.detail = "轮询返回异常"
                return self.result()
            st = data.get("status")
            if st == 4:
                token = data.get("token") or ""
                userid = data.get("userid") or ""
                parts = {k: v for k, v in r.cookies.items()}
                if token:
                    parts["token"] = str(token)
                if userid:
                    parts["userid"] = str(userid)
                self.cookie = "; ".join(f"{k}={v}" for k, v in parts.items() if k)
                self.status = "done"
                self.detail = "登录成功"
            elif st == 0:
                self.status = "expired"
                self.detail = "二维码已过期"
            elif st == 2:
                self.status = "scanned"
                self.detail = "已扫码，请在手机上确认"
            else:
                self.status = "waiting"
        except Exception as e:
            self.status = "error"
            self.detail = f"轮询失败: {e}"
        return self.result()


# ---------------- 网易云 ----------------

class NeteaseQrSession(QrLoginSession):
    """网易云扫码登录（weapi unikey + client/login）。

    坑：① 必须用 weapi 双重加密（Cryptodome 包）；
        ② code 803=成功（响应 cookie 字段即登录 Cookie 字符串）、800=过期、802=已扫码、801=等待。
    """

    QR_TTL = 180
    POLL_MIN_INTERVAL = 3.0  # 网易云风控敏感，节流到 3s（官方网页也是 3s 轮询）
    IMPERSONATE = True  # 用 Chrome 指纹，规避扫码后 8821「请切换其他登录方式」
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
            "User-Agent": self.UA,
        }

    async def _weapi_post(self, path: str, params: dict):
        from .sources.netease import weapi_encrypt

        form = weapi_encrypt(params)
        r = await self._post(
            f"https://music.163.com/weapi/{path}", data=form, headers=self._headers()
        )
        ck = {k: v for k, v in r.cookies.items()}
        try:
            return r.json(), ck
        except Exception:
            return {}, ck

    async def start(self) -> None:
        try:
            data, _ = await self._weapi_post(
                "login/qrcode/unikey", {"type": 1, "noCheckToken": True}
            )
            unikey = (data or {}).get("unikey")
            if not unikey:
                self.status = "error"
                self.detail = "unikey 获取失败"
                return
            self.key = unikey
            url = f"https://music.163.com/login?codekey={unikey}"
            import qrcode

            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            self.status = "waiting"
            self.detail = "请使用网易云音乐 APP 扫码"
        except Exception as e:
            self.status = "error"
            self.detail = f"二维码获取失败: {e}"

    async def _do_poll(self) -> dict:
        if self.status in ("done", "error"):
            return self.result()
        if self._check_expired():
            return self.result()
        try:
            data, resp_cookies = await self._weapi_post(
                "login/qrcode/client/login", {"key": self.key, "type": 1}
            )
            code = (data or {}).get("code")
            if code == 803:
                # 合并响应头 Cookie（MUSIC_U 等有时只在 Set-Cookie 里）
                base = (data or {}).get("cookie", "") or ""
                merged = base
                if resp_cookies:
                    extra = "; ".join(f"{k}={v}" for k, v in resp_cookies.items())
                    merged = (base + "; " + extra).strip("; ")
                self.cookie = merged
                self.status = "done"
                self.detail = "登录成功"
            elif code == 800:
                self.status = "expired"
                self.detail = "二维码已过期"
            elif code == 802:
                self.status = "scanned"
                self.detail = "已扫码，请在手机上确认"
            elif code == 801:
                self.status = "waiting"
            else:
                # 把原始 code 和网易云返回的 message 一起暴露出来，便于排查
                msg = (data or {}).get("message", "")
                self.detail = f"等待扫码（code={code} {msg}）".strip()
                if code == 8821 or "登录方式" in msg or "频繁" in msg:
                    self.detail += "｜网易风控/频率限制：请勿继续扫码（会延长限制），可用账号页「粘贴Cookie」方式登录，或24小时后再试"
                self.status = "waiting"
        except Exception as e:
            self.status = "error"
            self.detail = f"轮询失败: {e}"
        return self.result()


# ---------------- 管理器 ----------------

_SESSION_CLASSES: dict[str, type[QrLoginSession]] = {
    "bilibili": BilibiliQrSession,
    "qq": QQMusicQrSession,
    "kugou": KugouQrSession,
    "netease": NeteaseQrSession,
}


class LoginManager:
    """同平台同时只允许一个会话；每次 start 都生成全新二维码并新建会话。"""

    def __init__(self, http_client=None):
        # 兼容旧签名；实际每会话各自持有独立客户端
        self.http = http_client
        self.sessions: dict[str, QrLoginSession] = {}

    def get(self, source: str) -> Optional[QrLoginSession]:
        return self.sessions.get(source)

    async def start(self, source: str) -> Optional[QrLoginSession]:
        cls = _SESSION_CLASSES.get(source)
        if cls is None:
            return None
        # 总是生成新的二维码：既能支持「刷新二维码」重新生成，也避免复用
        # 已过期/失败/残留的进行中会话（旧逻辑复用会导致刷新无效）。
        old = self.sessions.pop(source, None)
        if old is not None:
            await old.aclose()
        session = cls(None)
        self.sessions[source] = session
        await session.start()
        return session

    async def poll(self, source: str) -> dict:
        s = self.sessions.get(source)
        if s is None:
            return {"status": "none"}
        return await s.poll()

    def cancel(self, source: str) -> None:
        old = self.sessions.pop(source, None)
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass
