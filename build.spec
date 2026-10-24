# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建脚本：将弹幕点歌机打包为单文件 exe。

用法（在项目根目录执行）：
    pyinstaller build.spec
产物：dist/bili-jukebox.exe （单文件，可直接发给别人，无需安装 Python）

说明：
- 只读资源（web/、config.yaml）随包进入 _MEIPASS，由 jukebox/config.py 的
  _project_root() 自动定位；
- 运行时可写数据（cache/）放在「原始 exe 同目录」，由 app_data_dir() 定位，
  保证设置与播放列表多次运行间持久化。
"""

import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# 收集带动态导入 / 数据包的第三方库（bilibili_api、Cryptodome）
datas = []
hiddenimports = [
    "Cryptodome.Cipher",
    "Cryptodome.Util.Padding",
    "Cryptodome.PublicKey.RSA",
    "qrcode",
    "qrcode.image.pil",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "websockets",
    "aiohttp",
    "orjson",
    "curl_cffi",
    "curl_cffi.requests",
]

for pkg in ("bilibili_api", "Cryptodome", "curl_cffi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["run.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=datas + [("web", "web"), ("config.yaml", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "pydoc_data"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="bili-jukebox",
    icon="docs/icon.ico",           # 图标（v1.1.2 起）
    version="version_info.txt",     # 文件属性里显示版本号（v1.1.2 起）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 关闭 UPX：避免部分机器杀软误报/加壳失败
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
