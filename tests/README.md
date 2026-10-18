# 测试套件

无框架脚本式测试，每个脚本可独立运行（`python tests/xxx.py`），服务由脚本内
subprocess 启动/停止（默认端口 **8788**）。公共辅助见 `tests/common.py`。

## 运行方式

```bash
# 单个脚本
python tests/tests_regression_sources.py   # 6 源 search/resolve 回归（联网）
python tests/tests_edge_cases.py           # LRC/weapi/wav/local/网易云 边界（联网）
python tests/tests_login_qr.py             # 四平台二维码生成与轮询（联网）
python tests/tests_features_v4.py          # 设置/登录/切直播间/歌词/keep_state
python tests/tests_e2e.py                  # HTTP API 全链路 + 重启持久化（离线，demo 源）

# 一次性跑全部（按顺序，端口复用 8788，脚本内各自起停服务）
for f in tests/tests_*.py; do python "$f" || break; done
```

## 脚本说明

| 脚本 | 覆盖点 | 依赖网络 |
|---|---|---|
| `tests_regression_sources.py` | bilibili/qq/kugou/netease/demo/local 的 search+resolve | 是（local 无目录时跳过） |
| `tests_edge_cases.py` | LRC 多标签/逐字/毫秒、weapi 加密、demo wav、local 扫描、网易云多候选 | 是（网易云一项） |
| `tests_login_qr.py` | 四平台 start/status/state/cancel/logout | 是（二维码接口） |
| `tests_features_v4.py` | 设置 GET/POST/reset、音源开关排序、扫码登录、切直播间、弹幕指定音源、歌词、keep_state 重启语义 | 部分（登录/指定音源容错） |
| `tests_e2e.py` | 点歌→播放→流→冷却→上限→控制→热歌榜→缓存→重启持久化 | 否（纯 demo 源） |

## 注意

- 端口固定 **8788**；脚本会 `kill_port()` 后重启服务，**不要同时跑多个脚本**。
- 状态文件落在 `cache/state.json`、`cache/settings.json`；`keep_state` 相关用例
  会重启服务验证持久化语义，脚本自身在启动时会清理遗留状态，可重复运行。
- 联网用例对网络/版权有容错（搜索无结果视为优雅失败），核心断言不依赖具体歌曲可播放性。
