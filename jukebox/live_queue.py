"""直播排队：观众排队名单的核心业务模块。

与点歌功能共用同一条 B站弹幕连接、同一份扫码登录、同一个直播间配置：

- 弹幕入队：观众发「排队」「排队，轨外」即入队，并记录要打的内容；
- 礼物优先：SEND_GIFT 事件里礼物名命中（默认「粉丝团灯牌」）→ 插队或记录待用资格；
- 管理命令：next / add-名字-位置 / del-序号 / clear / match-文本 等，
  控制台经 POST /api/live-queue/command 调用；
- OBS 名单页：/queue 轮询 /api/live-queue/state 渲染竖版面板；
- 持久化：队列/待用/外观状态写 cache/live_queue.json（原子写）；
  口令/内容关键词/礼物名等规则由 config.yaml + settings.json 覆盖层管理；
- revision 自增驱动前端增量重绘，避免轮询时重播动画。
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Any, Callable, Optional

from .danmaku import Danmu

# 分隔符：「排队，轨外」「排队 轨外」「排队：轨外」都能切出内容
_SPLIT_RE = re.compile(r"([,;:/|\s\-+.，；：、。—])")

_Help = (
    "命令列表：add-名字[-位置] 手动加人/移位 | del-序号 删除 | next 或 下一位 叫号 | "
    "list 列表 | clear 清空队列 | clear-pending 清空待用灯牌 | match-文本 测试口令 | "
    "contents-a,b,c 设置内容关键词 | keywords-a,b,c 设置入队口令 | font-36 字号 | "
    "frame-80 / name-80 透明度 | priority-on/off 插队开关 | autoclear-on/off 开播清空 | "
    "test-queue-名字[,内容] / test-gift-名字 本地测试 | help 帮助"
)


def _normalize(text: str) -> str:
    """归一化口令/弹幕：去掉空白与标点（含全角），「排队！」「 排队 」都归一为「排队」。

    ASCII 只保留字母数字，非 ASCII 去掉标点（Unicode P* 类别）与空白，
    其余（中日韩文字等）保留。
    """
    out = []
    for ch in str(text or ""):
        if ch.isascii():
            if ch.isalnum():
                out.append(ch)
            continue
        if ch.isspace() or unicodedata.category(ch).startswith("P"):
            continue
        out.append(ch)
    return "".join(out)


def _split_head_rest(text: str) -> tuple[str, Optional[str]]:
    """按第一个分隔符切成 (口令, 内容)。没有分隔符时返回 (原文, None)。"""
    m = _SPLIT_RE.search(text)
    if not m:
        return text, None
    return text[: m.start()], text[m.end() :]


class LiveQueue:
    """排队状态机（单线程事件循环内使用，无需加锁）。"""

    def __init__(
        self,
        cfg: dict,
        state_path: str,
        on_change: Optional[Callable[[dict], None]] = None,
    ):
        self.state_path = state_path
        self.on_change = on_change
        self.queue: list[dict] = []      # QueueUser dicts
        self.pending: list[dict] = []    # PendingUser dicts
        self.served_count = 0
        self.revision = 0
        # 观感设置（管理页可调，进状态文件）
        self.font_size = 36
        self.frame_opacity = 100
        self.name_opacity = 100
        self.priority_enabled = True     # False = 已取消插队
        self.clear_on_start = False
        # 规则（config.yaml / settings.json 覆盖层）
        self.keywords: list[str] = ["排队"]
        self.contents: list[str] = []
        self.gift_names: list[str] = ["粉丝团灯牌"]
        self.max_rows = 50
        self.max_users = 512

        self._apply_rules(cfg)
        self._load_state()
        if self.clear_on_start:
            self.queue.clear()
            self.pending.clear()
            self.served_count = 0
            self._changed()

    # ---------- 配置 ----------

    def _apply_rules(self, cfg: dict):
        lq = cfg.get("live_queue", {}) or {}
        self.keywords = [str(k) for k in (lq.get("keywords") or []) if str(k).strip()][:8] or ["排队"]
        self.contents = [str(k) for k in (lq.get("contents") or []) if str(k).strip()][:12]
        self.gift_names = [str(k) for k in (lq.get("gift_names") or []) if str(k).strip()] or ["粉丝团灯牌"]
        self.max_rows = int(lq.get("max_rows") or 50)
        self.max_users = int(lq.get("max_users") or 512)

    def apply_config(self, cfg: dict):
        """设置变更后热更新规则（口令/内容/礼物名/行数上限）。"""
        self._apply_rules(cfg)

    # ---------- 持久化 ----------

    def _load_state(self):
        try:
            import json

            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            self.font_size = int(data.get("font_size") or self.font_size)
            self.frame_opacity = int(data.get("frame_opacity") or self.frame_opacity)
            self.name_opacity = int(data.get("name_opacity") or self.name_opacity)
            self.priority_enabled = bool(data.get("priority_enabled", True))
            self.clear_on_start = bool(data.get("clear_on_start", False))
            self.served_count = int(data.get("served_count") or 0)
            self.revision = int(data.get("revision") or 0)
            self.queue = [u for u in (data.get("queue") or []) if isinstance(u, dict) and u.get("name")]
            self.pending = [u for u in (data.get("pending_priority") or []) if isinstance(u, dict) and u.get("name")]
            for u in self.queue:
                u.setdefault("source", "bilibili")
                u.setdefault("content", "")
                u.setdefault("priority", False)
                u.setdefault("joined_at", int(time.time()))
            for u in self.pending:
                u.setdefault("credits", 1)
                u.setdefault("updated_at", int(time.time()))
        except Exception:
            pass

    def _save_state(self):
        import json

        data = {
            "font_size": self.font_size,
            "frame_opacity": self.frame_opacity,
            "name_opacity": self.name_opacity,
            "priority_enabled": self.priority_enabled,
            "clear_on_start": self.clear_on_start,
            "served_count": self.served_count,
            "revision": self.revision,
            "queue": self.queue,
            "pending_priority": self.pending,
        }
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _changed(self):
        self.revision += 1
        self._save_state()
        if self.on_change:
            try:
                self.on_change(self.snapshot())
            except Exception:
                pass

    # ---------- 匹配 ----------

    def _match(self, text: str) -> Optional[str]:
        """弹幕是否命中入队口令；命中返回要打的内容（可为空串），否则 None。"""
        if not text:
            return None
        head, rest = _split_head_rest(text)
        if _normalize(head) not in self._norm_keywords():
            return None
        content = ""
        if rest:
            nrest = _normalize(rest)
            ncontents = [_normalize(c) for c in self.contents]
            for i, c in enumerate(self.contents):
                if nrest and nrest == ncontents[i]:
                    content = c
                    break
            else:
                for i, c in enumerate(self.contents):
                    if c and ncontents[i] and ncontents[i] in nrest:
                        content = c
                        break
        return content

    def _norm_keywords(self) -> list[str]:
        return [_normalize(k) for k in self.keywords]

    # ---------- 身份 ----------

    @staticmethod
    def _masked_prefix(name: str) -> Optional[str]:
        """脱敏昵称（如「小***」）取 * 前的前缀；没有 * 返回 None。"""
        name = str(name or "")
        if "*" in name:
            return name.split("*", 1)[0]
        return None

    def _unique_pending_by_name(self, name: str) -> int:
        prefix = self._masked_prefix(name)
        if not prefix:
            return -1
        hits = [i for i, p in enumerate(self.pending) if str(p.get("name", "")).startswith(prefix)]
        return hits[0] if len(hits) == 1 else -1

    def _unique_queue_by_name(self, name: str) -> int:
        prefix = self._masked_prefix(name)
        if not prefix:
            return -1
        hits = [i for i, q in enumerate(self.queue) if str(q.get("name", "")).startswith(prefix)]
        return hits[0] if len(hits) == 1 else -1

    @staticmethod
    def _danmu_id(d: Danmu) -> str:
        uid = int(getattr(d, "uid", 0) or 0)
        if uid:
            return str(uid)
        # uid=0（匿名/注入）：用昵称兜底；昵称脱敏时的合并靠 _masked_prefix
        return "name:" + str(d.uname or "匿名")

    # ---------- 弹幕 / 礼物入口 ----------

    def handle_danmu(self, d: Danmu) -> Optional[str]:
        """弹幕入队口令。返回提示文案；未命中返回 None。"""
        content = self._match(str(getattr(d, "text", "") or ""))
        if content is None:
            return None
        ok, msg = self.join(self._danmu_id(d), str(d.uname or "观众"), "bilibili", content)
        return msg if ok else msg

    def handle_gift(self, uid: int, uname: str, gift_name: str) -> Optional[str]:
        """SEND_GIFT 礼物：名字命中优先礼物才处理。"""
        if str(gift_name or "") not in self.gift_names:
            return None
        gid = str(uid) if uid else "name:" + str(uname or "匿名")
        ok, msg = self.gift(gid, str(uname or "观众"))
        return msg or None

    # ---------- 核心操作 ----------

    def join(self, id_: str, name: str, source: str, content: str = "") -> tuple[bool, str]:
        rid, rname = id_, name
        if id_.startswith("name:") or id_.startswith("hash:"):
            m = self._unique_pending_by_name(name)
            if m >= 0:
                rid = str(self.pending[m].get("id") or rid)
                rname = str(self.pending[m].get("name") or rname)

        existing = next((i for i, q in enumerate(self.queue) if str(q.get("id")) == rid), -1)
        if existing >= 0:
            q = self.queue[existing]
            if content and str(q.get("content", "")) != content:
                q["content"] = content
                self._changed()
                return True, f"{rname} 已在队列中，已按最新一条更新内容（{content}）"
            return False, f"{rname} 已在队列中"

        if len(self.queue) >= self.max_users:
            return False, f"队列已达到 {self.max_users} 人上限"

        user = {
            "id": rid,
            "name": rname,
            "source": source,
            "content": content or "",
            "joined_at": int(time.time()),
            "priority": False,
        }
        suffix = f"（要打：{content}）" if content else ""
        p = next((i for i, p in enumerate(self.pending) if str(p.get("id")) == rid), -1)
        if p >= 0 and int(self.pending[p].get("credits", 0)) > 0 and self.priority_enabled:
            user["priority"] = True
            self.queue.insert(0, user)
            if int(self.pending[p].get("credits", 0)) - 1 <= 0:
                self.pending.pop(p)
            else:
                self.pending[p]["credits"] = int(self.pending[p].get("credits", 0)) - 1
            self._changed()
            return True, f"{rname} 使用预存灯牌，插队到第 1 位{suffix}"
        self.queue.append(user)
        self._changed()
        return True, f"{rname} 已加入第 {len(self.queue)} 位{suffix}"

    def gift(self, id_: str, name: str) -> tuple[bool, str]:
        if not self.priority_enabled:
            return False, ""  # 已关闭插队：静默忽略礼物，避免每条都刷提示
        gift_label = self.gift_names[0] if self.gift_names else "灯牌"
        index = next((i for i, q in enumerate(self.queue) if str(q.get("id")) == id_), -1)
        if index < 0:
            index = self._unique_queue_by_name(name)
            if index >= 0:
                self.queue[index]["id"] = id_
                self.queue[index]["name"] = name
        if index >= 0:
            user = self.queue.pop(index)
            user["priority"] = True
            self.queue.insert(0, user)
            self._changed()
            if index > 0:
                return True, f"{name} 送出{gift_label}，已插队到第 1 位"
            return True, f"{name} 已经在第 1 位"
        p = next((i for i, p in enumerate(self.pending) if str(p.get("id")) == id_), -1)
        if p < 0:
            if len(self.pending) >= self.max_users:
                return False, "待用灯牌记录已满"
            self.pending.append({"id": id_, "name": name, "credits": 0, "updated_at": int(time.time())})
            p = len(self.pending) - 1
        self.pending[p]["name"] = name
        self.pending[p]["credits"] = int(self.pending[p].get("credits", 0)) + 1
        self.pending[p]["updated_at"] = int(time.time())
        self._changed()
        return True, f"已记录 {name} 的{gift_label}，等待其发送“排队”"

    def manual_add(self, name: str, position: int = 0) -> tuple[bool, str]:
        if not name:
            return False, "用户名不能为空"
        existing = next((i for i, q in enumerate(self.queue) if str(q.get("name", "")).lower() == name.lower()), -1)
        if existing >= 0:
            user = self.queue.pop(existing)
            verb = "移动"
        else:
            if len(self.queue) >= self.max_users:
                return False, "队列已满"
            user = {
                "id": f"manual:{int(time.time())}:{self.revision + 1}",
                "name": name,
                "source": "manual",
                "content": "",
                "joined_at": int(time.time()),
                "priority": False,
            }
            verb = "加入"
        if not (1 <= position <= len(self.queue) + 1):
            position = len(self.queue) + 1
        self.queue.insert(position - 1, user)
        self._changed()
        return True, f"已将 {name} {verb}到第 {position} 位"

    def next_user(self) -> tuple[bool, str]:
        if not self.queue:
            return False, "队列已空，没有可叫的人"
        user = self.queue.pop(0)
        self.served_count += 1
        self._changed()
        if self.queue:
            return True, f"已叫号：{user['name']}，下一位是 {self.queue[0]['name']}"
        return True, f"已叫号：{user['name']}，队列已空"

    def del_rank(self, n: int) -> tuple[bool, str]:
        if not (1 <= n <= len(self.queue)):
            return False, f"没有第 {n} 位用户"
        user = self.queue.pop(n - 1)
        self._changed()
        return True, f"已删除第 {n} 位：{user['name']}"

    def clear(self) -> tuple[bool, str]:
        n = len(self.queue)
        self.queue.clear()
        self._changed()
        return True, f"已清空 {n} 位用户"

    def clear_pending(self) -> tuple[bool, str]:
        n = len(self.pending)
        self.pending.clear()
        self._changed()
        return True, f"已清空 {n} 条待用灯牌记录"

    # ---------- 管理命令 ----------

    def command(self, cmd: str) -> tuple[bool, str, bool]:
        """执行管理命令，返回 (ok, message, changed)。"""
        cmd = str(cmd or "").strip()
        if not cmd:
            return False, "请输入命令", False
        low = cmd.lower()

        if low.startswith("test-queue-"):
            arg = cmd[len("test-queue-"):]
            head, rest = _split_head_rest(arg)
            content = ""
            if rest:
                c = self._match(f"{self.keywords[0]},{rest}") if self.keywords else None
                content = c or ""
            ok, msg = self.join(f"test:{head}", head, "test", content)
            return ok, msg, ok
        if low.startswith("test-gift-"):
            name = cmd[len("test-gift-"):]
            ok, msg = self.gift(f"test:{name}", name)
            return ok, msg, ok

        if low.startswith("match-"):
            text = cmd[len("match-"):]
            content = self._match(text)
            if content is None:
                return True, f"「{text}」不会入队（当前口令：{'、'.join(self.keywords)}）", False
            if content:
                return True, f"「{text}」会入队，要打：{content}", False
            return True, f"「{text}」会入队（未指定内容）", False

        if low.startswith("contents-"):
            items = [s for s in re.split(r"[,，;；]", cmd[len("contents-"):]) if s.strip()]
            self.contents = items[:12]
            return True, f"可打内容已更新：{'、'.join(self.contents)}", True
        if low.startswith("keywords-"):
            items = [s for s in re.split(r"[,，;；]", cmd[len("keywords-"):]) if s.strip()]
            if not items:
                return False, "至少需要一个口令", False
            self.keywords = items[:8]
            return True, f"入队口令已更新：{'、'.join(self.keywords)}", True

        m = re.match(r"^del-(\d+)$", low)
        if m:
            ok, msg = self.del_rank(int(m.group(1)))
            return ok, msg, ok

        m = re.match(r"^font-(\d+)$", low)
        if m:
            v = int(m.group(1))
            if not (12 <= v <= 160):
                return False, "字体大小须在 12 到 160 之间", False
            self.font_size = v
            self._changed()
            return True, f"字体大小已设为 {v}px", True

        m = re.match(r"^frame-(\d+)$", low)
        if m:
            v = int(m.group(1))
            if not (0 <= v <= 100):
                return False, "透明度须在 0 到 100 之间", False
            self.frame_opacity = v
            self._changed()
            return True, f"外框透明度已设为 {v}%", True

        m = re.match(r"^name-(\d+)$", low)
        if m:
            v = int(m.group(1))
            if not (0 <= v <= 100):
                return False, "透明度须在 0 到 100 之间", False
            self.name_opacity = v
            self._changed()
            return True, f"名单透明度已设为 {v}%", True

        if low in ("next", "下一位"):
            ok, msg = self.next_user()
            return ok, msg, ok

        if low == "autoclear-on":
            self.clear_on_start = True
            self._changed()
            return True, "已开启开播自动清空：每次启动程序都会清空队列与待用灯牌", True
        if low == "autoclear-off":
            self.clear_on_start = False
            self._changed()
            return True, "已关闭开播自动清空", True

        if low == "priority-off":
            self.priority_enabled = False
            self._changed()
            gift_label = self.gift_names[0] if self.gift_names else "灯牌"
            return True, f"已取消插队：{gift_label} 不再插队，也不记录资格（已积累的待用记录会保留）", True
        if low == "priority-on":
            self.priority_enabled = True
            self._changed()
            gift_label = self.gift_names[0] if self.gift_names else "灯牌"
            return True, f"已恢复插队：{gift_label} 可插到第 1 位", True

        if low in ("clear-pending", "清空待用"):
            ok, msg = self.clear_pending()
            return ok, msg, True

        if low == "clear":
            ok, msg = self.clear()
            return ok, msg, True

        if low == "list":
            if not self.queue:
                return True, "当前队列为空", False
            return True, " | ".join(f"{i + 1} -> {q['name']}" for i, q in enumerate(self.queue)), False

        if low == "help":
            return True, _Help, False

        if low.startswith("add-"):
            arg = cmd[len("add-"):]
            m2 = re.search(r"-(\d+)$", arg)
            pos = int(m2.group(1)) if m2 else 0
            name = arg[: m2.start()] if m2 else arg
            name = name.strip()
            ok, msg = self.manual_add(name, pos)
            return ok, msg, ok

        return False, "未知命令，输入 help 查看帮助", False

    # ---------- 快照 ----------

    def snapshot(self) -> dict:
        return {
            "type": "state",
            "queue": [
                {
                    "id": q.get("id", ""),
                    "name": q.get("name", ""),
                    "source": q.get("source", "bilibili"),
                    "content": q.get("content", ""),
                    "joined_at": int(q.get("joined_at") or 0),
                    "priority": bool(q.get("priority")),
                    "position": i + 1,
                }
                for i, q in enumerate(self.queue)
            ],
            "pending_priority": [
                {
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "credits": int(p.get("credits", 0)),
                    "updated_at": int(p.get("updated_at") or 0),
                }
                for p in self.pending
            ],
            "font_size": self.font_size,
            "frame_opacity": self.frame_opacity,
            "name_opacity": self.name_opacity,
            "priority_enabled": self.priority_enabled,
            "clear_on_start": self.clear_on_start,
            "served_count": self.served_count,
            "revision": self.revision,
            "max_rows": self.max_rows,
            "keyword": self.keywords[0] if self.keywords else "",
            "keywords": list(self.keywords),
            "contents": list(self.contents),
            "priority_gifts": list(self.gift_names),
        }
