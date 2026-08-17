# -*- coding: utf-8 -*-
"""cc_monitor 配置加载：读取 config.json（可被 CC_CONFIG 环境变量覆盖），
缺失字段用默认值兜底。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "max_sessions": 30,  # 内存中最多保留的会话数（超出清理最旧已结束的），防止常驻进程累积
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "target_chat_id": "",
        "target_open_id": "",
        "domain": "feishu.cn",  # 国内开放平台用 feishu.cn；国际版用 larksuite.com
    },
    "forward_url": "http://127.0.0.1:8000/api/im/feishu/websocket",
    "card": {
        "max_panels": 12,
        "window_lines": 30,
        "debounce_sec": 2.5,
        "max_batch_wait_sec": 5.0,
        "min_patch_interval_sec": 2.0,
        "max_messages_per_card": 16,         # 每张卡片消息条数上限（超出时面板由 build_card 压缩兜底）
        "title_brief_len": 10,               # 折叠标题后追加的本轮内容摘要截断长度
        "persist_mapping": True,             # 会话↔卡片映射写文件（重启后仍 PATCH 同一张卡）
        "data_dir": "~/.agent-assist",       # 映射文件目录，可改（Windows 例：C:\\Users\\xxx\\.agent-assist）
        "sync_list_max": 30,                 # 「同步历史」列表卡最多显示的会话数（超出只取最近 N 内最新）
        "sync_recent_max": 10,               # 「同步历史」只同步最近有更新的 N 个会话（需求：默认 10）
        "unknown_source_default": "vscode",  # 无法确认来源环境的历史会话默认归入的环境（vscode/cli）
        "exec_probe_window_sec": 45,         # 执行探针：transcript 距今 N 秒内有写入视为「正在执行」
    },
    "decision": {
        "timeout_sec": 60,
    },
}

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> Dict[str, Any]:
    """读取配置。path 未指定时用 CC_CONFIG 环境变量，否则用 config.json。"""
    cfg = _deep_merge(DEFAULTS, {})
    p = Path(path) if path else (
        Path(os.environ["CC_CONFIG"]) if os.environ.get("CC_CONFIG") else BASE_DIR / "config.json"
    )
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:  # 配置损坏不能阻断服务启动
            print(f"[cc_config] 读取配置失败({p}): {e}", file=__import__("sys").stderr)
    return cfg
