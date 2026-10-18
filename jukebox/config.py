"""配置加载：三层深合并 DEFAULTS <- config.yaml <- cache/settings.json。

语义：
- DEFAULTS 内置全部默认值；
- config.yaml 是用户可编辑的基线；
- settings.json 是管理页覆盖层（热生效，永不改动 config.yaml）；
- _deep_merge：dict 递归合并；其他类型（含 list）整体覆盖。
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Any, Optional

import yaml


def is_frozen() -> bool:
    """是否由 PyInstaller 等工具打包运行（sys.frozen）。"""
    return getattr(sys, "frozen", False)


def _project_root() -> str:
    """只读资源根：web/ 等随包分发的静态数据。

    打包为单文件 exe 时，这些数据被解包到 sys._MEIPASS；未打包时取项目根。
    """
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = _project_root()

_DATA_DIR_CACHE: Optional[str] = None


def _writable(path: str) -> bool:
    """目录是否可写（尝试创建 + 写探针文件）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def app_data_dir() -> str:
    """可写数据根：cache/ 等运行时生成文件。

    未打包时取项目根（开发/测试一致）；打包后优先放「原始 exe 所在目录」，
    该目录不可写（如 Program Files）时回退到 %LOCALAPPDATA%/bili-jukebox。
    """
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE:
        return _DATA_DIR_CACHE
    if is_frozen():
        exe = sys.argv[0] if sys.argv and os.path.isabs(sys.argv[0]) else sys.executable
        base = os.path.dirname(os.path.abspath(exe))
        if _writable(base):
            _DATA_DIR_CACHE = base
            return base
        fallback = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "bili-jukebox"
        )
        os.makedirs(fallback, exist_ok=True)
        _DATA_DIR_CACHE = fallback
        return fallback
    _DATA_DIR_CACHE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _DATA_DIR_CACHE


def _resolve_config_path() -> str:
    """config.yaml 路径。

    未打包：项目根；打包后：优先 exe 同级 config.yaml（用户可编辑），
    不存在时把内置默认导出一份到该位置；导出失败则直接读内置默认。
    """
    bundled = os.path.join(PROJECT_ROOT, "config.yaml")
    if not is_frozen():
        return bundled
    user_cfg = os.path.join(app_data_dir(), "config.yaml")
    if os.path.exists(user_cfg):
        return user_cfg
    try:
        import shutil

        shutil.copyfile(bundled, user_cfg)
        return user_cfg
    except Exception:
        return bundled


CONFIG_PATH = _resolve_config_path()

DEFAULTS: dict[str, Any] = {
    # 默认只监听本机：OBS / 管理页都在本机访问，避免同网络下他人操作服务
    "server": {"host": "127.0.0.1", "port": 8788, "open_browser": True},
    "danmaku": {"mode": "mock", "room_id": 0, "credential_file": ""},
    "jukebox": {
        "request_enabled": False,     # 点歌功能默认关闭，主播在管理页手动开启
        "request_prefixes": ["点歌", "求歌", "来首", "我想听"],
        "user_cooldown_seconds": 0,   # 默认无冷却
        "song_cooldown_minutes": 0,   # 默认无冷却
        "max_queue_length": 50,
        "max_per_user_queue": 3,
        "max_song_duration": 900,
        "blacklist": [],
        "allow_user_skip": False,
        "keep_state_on_exit": True,
    },
    "cache": {"dir": "cache", "search_ttl_minutes": 30},
    "login": {"keep_cookies": True},  # False = 重启后清空登录态，需重新扫码
    "live_queue": {  # 直播排队：观众发「排队」入队，灯牌礼物插队
        "keywords": ["排队"],            # 入队口令（弹幕整条归一化后相等才命中）
        "contents": [],                  # 可打内容关键词，「排队，轨外」记录要打的内容
        "gift_names": ["粉丝团灯牌"],     # 优先礼物（按礼物名精确匹配）
        "max_rows": 50,                  # OBS 名单页最多渲染行数
        "max_users": 512,                # 队列/待用上限
    },
    "sources": {
        "active": ["bilibili", "qq", "kugou", "netease", "demo"],
        "bilibili": {"max_results": 8, "max_duration": 900, "cookie": ""},
        "netease": {"cookie": ""},
        "qq": {"cookie": ""},
        "kugou": {"cookie": ""},
        "local": {"music_dir": "./music"},
        "demo": {},
    },
}


def _deep_merge(base: dict, override: Any) -> dict:
    """dict 递归合并；其他类型（含 list）直接覆盖。"""
    if not isinstance(override, dict):
        return override
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Optional[str] = None, overlay: Optional[dict] = None) -> dict:
    """加载配置：深拷贝 DEFAULTS -> 合并 config.yaml -> 合并 overlay。"""
    cfg = copy.deepcopy(DEFAULTS)
    path = path or CONFIG_PATH
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                cfg = _deep_merge(cfg, data)
        except Exception:
            # 配置文件损坏时静默回退到 DEFAULTS（主流程不受影响）
            pass
    if overlay:
        cfg = _deep_merge(cfg, overlay)
    return cfg


if __name__ == "__main__":
    import json

    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
