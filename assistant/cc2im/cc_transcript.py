# -*- coding: utf-8 -*-
"""读取 Claude Code 会话 transcript(JSONL)，提取一轮轮「用户 → agent」对话。

- extract_turns_since：从指定行号开始逐轮分组，供「获取以前消息」时按一条条
  用户-agent 对话补卡（不把多条历史合并成一条消息）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple


def extract_turns_since(transcript_path: str, from_line: int = 0) -> Tuple[list, int]:
    """从 from_line 读取 transcript，按轮次分组返回 (turns, 新行数)。

    turns 顺序与 transcript 一致：先用户输入，随后是该轮 agent 的全部输出
    （跨工具调用聚合为一条 assistant）。每条：
      {"role": "user",      "text": <用户文本>, "ts": <该消息时间戳>}
      {"role": "assistant", "text": <agent 全部文本>, "tools": <工具名顿号连接>,
                              "ts": <该轮最后一条消息的时间戳>}
    - 用户条目只取文本块；tool_result 回显不视为用户输入；
    - 同一轮内 agent 多次输出（含工具调用间的续写）聚合为一条 assistant；
    - ts 来自 transcript 记录的 timestamp（ISO），供卡片按真实时间显示。
    文件不存在/解析失败时静默返回空，绝不抛错（hook 不能因此受影响）。
    """
    turns: list[dict] = []
    total = from_line
    cur_agent = None  # 正在聚合的当前轮 agent
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return [], from_line
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        if total <= from_line:
            return [], total
        for line in lines[from_line:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            ts = rec.get("timestamp")  # ISO，如 2026-08-08T10:28:37.074Z
            text_parts: list[str] = []
            tools: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    text_parts.append(str(block["text"]).strip())
                elif btype == "tool_use" and block.get("name"):
                    tools.append(str(block["name"]))
            if role == "user":
                text = "\n".join(p for p in text_parts if p)
                if not text:
                    continue  # tool_result 等回显，不属于用户输入
                # 新用户输入 = 新轮次：先收尾上一轮的 agent
                if cur_agent is not None:
                    turns.append(cur_agent)
                    cur_agent = None
                turns.append({"role": "user", "text": text, "ts": ts})
            else:  # assistant
                if cur_agent is None:
                    cur_agent = {"role": "assistant", "text": "", "tools": [], "ts": ts}
                joined = "\n".join(p for p in text_parts if p)
                if joined:
                    cur_agent["text"] = (
                        cur_agent["text"] + "\n" + joined if cur_agent["text"] else joined
                    )
                cur_agent["tools"].extend(tools)
                if ts:
                    cur_agent["ts"] = ts  # 取该轮最后一条消息的时间
        if cur_agent is not None:
            turns.append(cur_agent)
    except Exception:
        return turns, total

    for t in turns:
        if t.get("role") == "assistant":
            t["tools"] = "、".join(dict.fromkeys(t["tools"])) if t.get("tools") else ""
    return turns, total
