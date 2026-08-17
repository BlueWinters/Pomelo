# -*- coding: utf-8 -*-
"""安装 / 卸载 Claude Code hooks 到 ~/.claude/settings.json。

- command 用当前 Python 虚拟环境的绝对路径（sys.executable），避免环境硬编码。
- 保留用户已有 settings（permissions / theme 等）合并写入，不覆盖。
- 卸载只移除引用本模块 cc_hooks.py 的条目，不影响用户其他 hook。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# 卸载时用于识别"是否我们写入的 hook"
MARKER = (BASE_DIR / "cc_hooks.py").as_posix().replace("\\", "/")


def build_commands(python: str, decision_timeout: float) -> dict:
    """构建 hooks 配置：扁平格式 hooks.{事件} -> [{matcher, hooks:[command]}]。

    注意：本版引擎（2.1.x）只认扁平结构，旧的 hooks.commands.{事件} 包裹形式
    会被静默忽略（debug 显示 "Found 0 total hooks in registry"）。每个事件项必须
    是 matcher 包裹形式（无工具匹配的事件用空 matcher "" 匹配一切）。
    """
    script = (BASE_DIR / "cc_hooks.py").as_posix()
    cmd = f'"{python}" "{script}"'

    def wrapped(**kw) -> dict:
        return {"matcher": "", "hooks": [{"type": "command", "command": cmd, **kw}]}

    # PermissionRequest hook 会长时间长轮询，超时必须 > 决策窗口
    permission_timeout = int(decision_timeout) + 30

    return {
        "SessionStart": [wrapped()],
        "SessionEnd": [wrapped()],
        "UserPromptSubmit": [wrapped()],
        "Stop": [wrapped()],
        "Notification": [
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": cmd}]},
            {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": cmd}]},
        ],
        "PermissionRequest": [wrapped(timeout=permission_timeout)],
    }


def _load_settings(path: Path) -> dict:
    if path.is_file():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            print(f"[cc_install] 警告：无法解析 {path}，将合并空配置写入（原内容备份为 .bak）")
            try:
                path.rename(str(path) + ".bak")
            except Exception:
                pass
    return {}


def _save_settings(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _entry_references(entry, marker: str) -> bool:
    """判断一个 hook 条目（或 wrapper）是否引用我们的脚本。"""
    def scan(obj) -> bool:
        if isinstance(obj, str):
            return marker in obj
        if isinstance(obj, list):
            return any(scan(x) for x in obj)
        if isinstance(obj, dict):
            return any(scan(v) for v in obj.values())
        return False
    return scan(entry)


def _flat_events(hooks: dict) -> dict:
    """返回扁平的事件->条目 映射。兼容迁移旧 hooks.commands 结构。"""
    events: dict = {}
    for key, val in (hooks or {}).items():
        if key == "commands" and isinstance(val, dict):
            # 旧包裹形式：提升为扁平
            for ev, entries in val.items():
                events.setdefault(ev, entries)
        elif key != "commands":
            events[key] = val
    return events


def install(settings_path: Path, decision_timeout: float) -> dict:
    data = _load_settings(settings_path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}

    # 兼容迁移：把旧 hooks.commands 提升为扁平 hooks.{event}
    flat = _flat_events(hooks)
    for ev in list(hooks.keys()):
        if ev != "commands":
            flat.setdefault(ev, hooks[ev])

    our = build_commands(sys.executable, decision_timeout)
    # 保留用户已有但不由本工具管理的事件；已管理的事件整体覆盖
    for event, entries in list(flat.items()):
        if event in our:
            continue
        if isinstance(entries, list):
            kept = [e for e in entries if not _entry_references(e, MARKER)]
            flat[event] = kept
    flat.update(our)

    hooks.clear()
    hooks.update(flat)
    data["hooks"] = hooks
    _save_settings(settings_path, data)
    return data


def uninstall(settings_path: Path) -> dict:
    data = _load_settings(settings_path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    flat = _flat_events(hooks)
    for event in list(flat.keys()):
        entries = flat[event]
        if isinstance(entries, list):
            kept = [e for e in entries if not _entry_references(e, MARKER)]
            if kept:
                flat[event] = kept
            else:
                del flat[event]
        else:
            # 异常结构，不碰
            pass
    hooks.clear()
    hooks.update(flat)
    data["hooks"] = hooks
    _save_settings(settings_path, data)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code → 飞书 hooks 安装/卸载")
    ap.add_argument("action", nargs="?", choices=["install", "uninstall"], default="install")
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS), help="settings.json 路径")
    ap.add_argument("--decision-timeout", type=float, default=None,
                    help="决策窗口（秒），用于设置 PermissionRequest hook 超时；默认读 config.json")
    args = ap.parse_args()

    try:
        from .cc_config import load_config  # noqa: PLC0415
        timeout = args.decision_timeout or float(load_config().get("decision", {}).get("timeout_sec", 60))
    except Exception:
        timeout = args.decision_timeout or 60

    path = Path(args.settings)
    if args.action == "install":
        data = install(path, timeout)
        events = _flat_events(data.get("hooks") or {})
        print(f"[cc_install] hooks 已安装 -> {path}（扁平格式 hooks.{{事件}}）")
        print(f"[cc_install] python = {sys.executable}")
        for ev, entries in events.items():
            n = len(entries) if isinstance(entries, list) else 0
            print(f"  {ev}: {n} 条")
    else:
        data = uninstall(path)
        events = _flat_events(data.get("hooks") or {})
        print(f"[cc_install] hooks 已卸载 -> {path}")
        print(f"[cc_install] 剩余事件: {list(events.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
