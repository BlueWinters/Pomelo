# -*- coding: utf-8 -*-
"""Claude Code hook 入口（独立快进程，由 settings.json 调用）。

职责：
  1. 读 stdin JSON → POST http://127.0.0.1:8790/hook 上报事件
  2. Stop 事件 → 秒回 {"decision":"approve"}（绝不阻塞回合结束）
  3. PermissionRequest → 长轮询 /decision/{id}，输出 {"permissionDecision": allow|deny|ask}

任何异常都静默失败、快速退出（exit 0），绝不拖累 Claude Code 本身。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

SERVICE_URL = os.environ.get("CC_SERVICE_URL", "http://127.0.0.1:8790")
DECISION_TIMEOUT = float(os.environ.get("CC_DECISION_TIMEOUT", "90"))

# 客户端来源：Claude Code 按 CLAUDE_CODE_ENTRYPOINT 区分启动入口
# （VSCode 扩展=claude-vscode；纯终端默认 cli）。hook 子进程继承该环境变量，
# 随事件上报给服务端，用于卡片主标题区分 [VSCode] / [CLI]。
ENTRYPOINT = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").lower()


def _client_source() -> str:
    if "vscode" in ENTRYPOINT:
        return "vscode"
    return "cli"

# 调试：每次 hook 被调用都追加一行，用于区分「hook 没触发」还是「触发了没转发」
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "local", "_cc_hooks_debug.log")
# 调试：PermissionRequest 等事件的完整 JSON 落盘（排查 AskUserQuestion 等结构）
EVENT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "local", "_cc_permission_events.jsonl")


def _log_debug(line: str):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_event(event: dict):
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _post_json(url: str, payload: dict, timeout: float = 5) -> dict | None:
    """POST JSON，失败返回 None（服务不可达等）。"""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _get_json(url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def main() -> int:
    # Windows 中文环境下 stdout 默认按 GBK 编码（含中文的 JSON 输出会被 Claude Code
    # 当 UTF-8 解析失败）。必须强制 stdout 用 UTF-8，与 stdin 侧的 UTF-8 读取对称。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # Windows 中文环境下 stdin 默认按 GBK 解码，会破坏 Claude Code 写入的 UTF-8 JSON
    # （含中文时直接解码失败 → 事件被丢弃）。必须按 UTF-8 字节读取。
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}

    name = event.get("hook_event_name", "")

    # 附加客户端来源（VSCode 扩展 / 终端 CLI），随事件上报服务端
    if isinstance(event, dict):
        event["client_source"] = _client_source()
        event["client_entrypoint"] = ENTRYPOINT

    # 调试：PermissionRequest 事件完整落盘，便于排查 AskUserQuestion 等结构
    if name in ("PermissionRequest", "Notification") or event.get("tool_name"):
        _log_event(event)

    # 1) 上报事件（快速，不等待）
    resp = _post_json(f"{SERVICE_URL}/hook", event, timeout=5)
    _log_debug(f"hook fired: {name} | sid={event.get('session_id','')} | "
               f"post_ok={resp is not None} | did={(resp or {}).get('decision_id','')} | raw_len={len(raw)}")

    # 2) AskUserQuestion / 一般权限：长轮询决策
    # AskUserQuestion 有两条触发路径，都要处理：
    #   - PreToolUse（matcher AskUserQuestion）：VSCode 扩展官方 headless 机制，
    #     答案须包在 hookSpecificOutput 里（hookEventName=PreToolUse），在原生 UI
    #     渲染前注入；PermissionRequest 路径在该插件里可能不被应用（#13203 等）。
    #   - PermissionRequest（tool_name=AskUserQuestion）：纯 CLI 下可靠的路径。
    # 服务端按问题内容去重，两条路径共享同一决策，点一次按钮两边都拿到答案。
    if name in ("PermissionRequest", "PreToolUse"):
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        is_question = (tool == "AskUserQuestion" or
                       (isinstance(tool_input, dict) and isinstance(tool_input.get("questions"), list)
                        and bool(tool_input["questions"])))
        if not is_question:
            # PreToolUse 非 AskUserQuestion：只上报，不决策（如 Bash/Edit 等普通工具调用）
            if name == "PreToolUse":
                return 0
            # PermissionRequest 一般权限（Bash/Read 等）：长轮询 allow/deny
            decision_id = (resp or {}).get("decision_id")
            if not decision_id:
                # 服务不可达 / 未拿到决策 token → 回 ask，走原生确认，绝不自动放行
                print(json.dumps({"permissionDecision": "ask"}))
                return 0
            d = _get_json(f"{SERVICE_URL}/decision/{decision_id}", timeout=DECISION_TIMEOUT)
            if not d:
                # 等待期间服务不可达 → ask（原生确认兜底）
                print(json.dumps({"permissionDecision": "ask"}))
                return 0
            action = d.get("action") or d.get("decision") or "ask"
            decision = "allow" if action in ("allow", "timeout") else "deny"
            print(json.dumps({"permissionDecision": decision}))
            return 0

        # ---- AskUserQuestion 长轮询 ----
        decision_id = (resp or {}).get("decision_id")
        if not decision_id:
            print(json.dumps({"permissionDecision": "ask"}))
            return 0
        d = _get_json(f"{SERVICE_URL}/decision/{decision_id}", timeout=DECISION_TIMEOUT)
        if not d:
            print(json.dumps({"permissionDecision": "ask"}))
            return 0
        action = d.get("action") or d.get("decision") or "ask"
        answer = d.get("answer")
        # 用户已在飞书选择了选项 / 输入文本。纯 allow 不会回答问题（原生输入框仍会阻塞），
        # 必须把答案经 updatedInput.answers 回填——questions 原样透传，answers 的键是
        # 问题全文（本版本二进制确认：question text -> answer string；多选用逗号拼接）。
        if action == "answer" and answer:
            qs = (event.get("tool_input") or {}).get("questions")
            if isinstance(qs, list) and qs:
                q0 = qs[0] if isinstance(qs[0], dict) else {}
                qtext = str(q0.get("question", "") or q0.get("header", "") or "")
                updated = {
                    "questions": qs,  # 引擎依赖原样 questions，不能改写
                    "answers": {qtext: str(answer)},
                }
                if name == "PreToolUse":
                    out = {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": updated,
                        }
                    }
                else:
                    out = {"permissionDecision": "allow", "updatedInput": updated}
                _log_debug(f"answer AskUserQuestion[{name}] {qtext!r} -> {answer!r}")
                print(json.dumps(out, ensure_ascii=False))
                return 0
            _log_debug(f"answer 但 tool_input 无 questions 可透传: {answer!r}")
        decision = "allow" if action in ("allow", "timeout") else "deny"
        print(json.dumps({"permissionDecision": decision}))
        return 0

    # 3) Stop：秒回 approve，不阻塞回合结束
    if name == "Stop":
        print(json.dumps({"decision": "approve"}))
        return 0

    # 其他事件：无决策输出，退出 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
