"""设置覆盖层 SettingsStore：<可写数据目录>/cache/settings.json 读写。

语义：
- config.yaml 是基线；管理页所有修改只写 settings.json，永不改动 config.yaml；
- patch() 与现有覆盖层深合并后原子写（先 .tmp 再 os.replace）；
- 「恢复默认」清空并删除文件。

关键：路径必须用 app_data_dir()（打包后 = exe 同目录，可写、跨重启保留）。
不能用 PROJECT_ROOT——打包后 PROJECT_ROOT 是 PyInstaller 的 _MEIPASS 临时解压目录，
每次启动重建，写进去的设置（含登录 Cookie）重启就丢，表现为「登录态消失」。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from .config import app_data_dir, _deep_merge


class SettingsStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(app_data_dir(), "cache", "settings.json")
        self.data: dict = {}
        self.load()

    # ---------- 基础读写 ----------
    def load(self) -> dict:
        self.data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.data = data
            except Exception:
                self.data = {}
        return self.data

    def save(self) -> None:
        """原子写：先写 .tmp 再 os.replace；写失败静默。"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- 操作 ----------
    def patch(self, patch: dict) -> dict:
        """与现有覆盖层再深合并后原子写，返回合并后的覆盖层。"""
        self.data = _deep_merge(self.data, patch or {})
        self.save()
        return self.data

    def set_cookie(self, source: str, cookie: str) -> dict:
        return self.patch({"sources": {source: {"cookie": cookie or ""}}})

    def clear_cookies(self) -> dict:
        """清空覆盖层中所有平台的登录 Cookie（「重启不保留登录态」用）。"""
        names = ("bilibili", "qq", "kugou", "netease")
        had = any(
            ((self.data.get("sources", {}).get(n) or {}).get("cookie", "") for n in names)
        )
        self.data = _deep_merge(self.data, {"sources": {n: {"cookie": ""} for n in names}})
        if had:
            self.save()
        return self.data

    def reset(self) -> None:
        """清空覆盖层并删除文件，回到 config.yaml 基线。"""
        self.data = {}
        self.save()
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass

    def apply_to(self, cfg: dict) -> dict:
        """把覆盖层合并到给定配置上（返回新 dict）。"""
        if not self.data:
            return cfg
        return _deep_merge(cfg, self.data)
