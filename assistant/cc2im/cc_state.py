# -*- coding: utf-8 -*-
"""Session 状态机 + 去抖/节流 + 决策 broker（超时自动允许）。

一个 session 始终只有一张飞书卡片：首张 send，之后事件攒批、PATCH 原地更新
（不再自动滚动新卡，多卡 bug 修复）。显示内容由 build_card 的 max_panels 压缩兜底。
会话↔卡片映射按配置写入数据目录，服务重启后仍能续上同一张卡片。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from .cc_card import (
    build_card,
    build_deleted_card,
    build_delete_confirm_card,
    build_session_list_card,
    display_status,
)
from .cc_feishu import FeishuClient
from .cc_transcript import extract_turns_since


# ---------------------------------------------------------------------------
# 决策 broker：跨线程（WS 线程 -> 主循环）安全地 resolve 待批准决策
# ---------------------------------------------------------------------------
class DecisionBroker:
    def __init__(self, loop: asyncio.AbstractEventLoop, timeout_sec: float = 60):
        self.loop = loop
        self.timeout_sec = timeout_sec
        self._futures: Dict[str, asyncio.Future] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}

    def create(self, decision_id: str, meta: Dict[str, Any]) -> asyncio.Future:
        fut = self.loop.create_future()
        self._futures[decision_id] = fut
        self._meta[decision_id] = meta
        return fut

    def resolve(self, decision_id: str, action: str, answer: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """返回 meta 表示这次是"首次"解决（future 尚在等待）；返回 None 表示已解决过。
        answer：AskUserQuestion 场景下用户选中的答案（选项文本 / 自定义输入）。"""
        fut = self._futures.pop(decision_id, None)
        meta = self._meta.pop(decision_id, None)
        if fut is not None and not fut.done():
            self.loop.call_soon_threadsafe(fut.set_result, (action, answer))
            return meta
        return None

    async def wait(self, decision_id: str, timeout: Optional[float] = None):
        """返回 (action, answer)。未知/已结束的决策默认 allow（不让 Claude 挂死）。"""
        fut = self._futures.get(decision_id)
        if fut is None:
            return ("allow", None)
        t = timeout or (self.timeout_sec + 5)
        # 不能取消底层 future：真正的超时统一由 _auto_allow 解决并清理状态，
        # 这里只是"旁观等待"。wait_for 会取消 future，导致后续清理失效。
        timer = asyncio.create_task(asyncio.sleep(t))
        done, _ = await asyncio.wait({fut, timer}, return_when=asyncio.FIRST_COMPLETED)
        if not timer.done():
            timer.cancel()
        if fut in done:
            return fut.result()
        return ("timeout", None)


# ---------------------------------------------------------------------------
# Session 状态
# ---------------------------------------------------------------------------
@dataclass
class SessionState:
    session_id: str
    cwd: str
    project_name: str
    short_id: str
    created_at: datetime = field(default_factory=datetime.now)
    last_activity_at: datetime = field(default_factory=datetime.now)
    status: str = "running"
    step_count: int = 0
    # 当前卡片的折叠面板（一条消息一个面板）；滚动到新卡时只保留溢出部分。
    # 每张卡片的消息条数受 max_messages_per_card 约束。
    panels: list = field(default_factory=list)
    # 本轮已由 UserPromptSubmit 实时上卡的「用户请求」文本；Stop 补历史时用它去重，
    # 避免同一条 prompt 既实时上卡又被 transcript 补一遍。Stop 后清空。
    pending_user_prompts: list = field(default_factory=list)
    pending_decision: Optional[Dict[str, Any]] = None
    last_result_note: str = ""
    feishu_message_id: Optional[str] = None
    update_count: int = 0
    transcript_offset: int = 0
    # 客户端来源（vscode / cli）与 claude-code 显示标题（SessionStart 携带）
    client_source: str = "cli"
    claude_code_title: str = ""
    # 去抖状态
    dirty: bool = False
    dirty_since: float = 0.0
    last_event_at: float = 0.0
    last_patch_at: float = 0.0
    flush_task: Optional[asyncio.Task] = None
    # 本次会话累计发过的卡片数（不再自动滚动，仅「立即刷新」时递增）
    card_seq: int = 1


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------
class SessionManager:
    def __init__(self, cfg: Dict[str, Any], feishu: FeishuClient, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.feishu = feishu
        self.loop = loop
        self.sessions: Dict[str, SessionState] = {}
        self.max_sessions = int(cfg.get("max_sessions", 30))
        card_cfg = cfg.get("card", {})
        self.debounce = float(card_cfg.get("debounce_sec", 2.5))
        self.max_batch_wait = float(card_cfg.get("max_batch_wait_sec", 5.0))
        self.min_interval = float(card_cfg.get("min_patch_interval_sec", 2.0))
        self.max_messages = int(card_cfg.get("max_messages_per_card", 16))  # 每卡消息条数上限
        self.title_brief_len = int(card_cfg.get("title_brief_len", 10))
        self.persist_mapping = bool(card_cfg.get("persist_mapping", True))
        self.data_dir = os.path.expanduser(card_cfg.get("data_dir", "~/.agent-assist"))
        self.decision_timeout = float(cfg.get("decision", {}).get("timeout_sec", 60))
        self.broker = DecisionBroker(loop, self.decision_timeout)
        self._auto_tasks: set = set()  # 持有引用，避免被 GC
        # 远程指令（卡片底部输入框→claude --resume）每会话一把锁，防止并发写同一 transcript
        self._command_locks: Dict[str, asyncio.Lock] = {}
        # 会话↔卡片映射（持久化，重启后可续接同一张卡片）
        self.registry: Dict[str, Dict[str, Any]] = self._load_registry()
        # 上一张「最近会话」列表卡 message_id（删除会话后原地 PATCH 刷新，行消失）
        self._last_list_card_id: Optional[str] = None
        # 待确认删除：session_id → 确认卡 message_id（二次确认，删除不可逆）
        self._pending_delete_cards: Dict[str, str] = {}

    # ---------------- 事件入口 ----------------
    async def on_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        name = event.get("hook_event_name", "")
        session_id = event.get("session_id", "")
        cwd = event.get("cwd", "")
        if not session_id:
            return {"ok": False, "msg": "missing session_id"}

        if name == "SessionStart":
            return await self._on_session_start(session_id, cwd, event)
        # 未见过该 session 就来了事件（缺 SessionStart 时兜底建，含持久化恢复）
        s = self._ensure_session(session_id, cwd or "未知目录", event)

        s.last_activity_at = datetime.now()

        if name == "UserPromptSubmit":
            s.status = "running"  # 改进1：新任务开始 → 运行中
            prompt = str(event.get("prompt", ""))
            self._add_panel(s, "👤 用户请求", prompt)
            if prompt:
                s.pending_user_prompts.append(prompt)
                if len(s.pending_user_prompts) > 30:
                    s.pending_user_prompts = s.pending_user_prompts[-30:]
            s.step_count += 1
            self._mark_dirty(s)

        elif name in ("Stop", "SubagentStop"):
            self._on_stop(s, event)
            s.status = "idle"  # 改进1：任务完成 → 空闲中（下一条 UserPromptSubmit/PreToolUse 会切回运行中）

        elif name in ("PermissionRequest", "PreToolUse"):
            tool = event.get("tool_name", "?")
            ti = event.get("tool_input", {})
            s.status = "running"  # 改进1：有工具调用 → 运行中（提问分支下面会切成 awaiting）
            # AskUserQuestion 双触发（PreToolUse + PermissionRequest），去重后共享同一决策
            if tool == "AskUserQuestion" or self._is_question_input(ti):
                decision_id = await self._create_question(s, ti)
                return {"ok": True, "decision_id": decision_id}
            if name == "PermissionRequest":
                reason = self._summarize_input(ti)
                decision_id = await self._create_decision(s, tool, reason)
                return {"ok": True, "decision_id": decision_id}
            # PreToolUse 非 AskUserQuestion（Bash/Edit 等普通工具调用）：只上报，不决策
            return {"ok": True}

        elif name == "Notification":
            ntype = event.get("notification_type", "")
            msg = event.get("message", "") or event.get("title", "") or ""
            if ntype == "permission_prompt" or (
                not ntype and "permission" in msg.lower()):
                # 仅通知类确认（无 PermissionRequest 决策），提供"去终端处理"
                if s.pending_decision is None:
                    s.status = "awaiting"
                    s.pending_decision = {"decision_id": None, "tool": "终端确认",
                                         "reason": msg or "Claude 需要确认"}
                    self._add_panel(s, "⏳ 等待确认", msg or "Claude 需要确认")
                    self._mark_dirty(s)
            elif ntype == "idle_prompt" or (
                not ntype and ("空闲" in msg or "idle" in msg.lower())):
                if s.pending_decision is None:
                    s.status = "idle"
                    self._mark_dirty(s)

        elif name == "SessionEnd":
            s.status = "ended"
            self._mark_dirty(s)
            self._gc_ended_sessions()

        return {"ok": True}

    # ---------------- Session 生命周期 ----------------
    def _new_session(self, session_id: str, cwd: str) -> SessionState:
        short = session_id[:8]
        project = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "workspace"
        s = SessionState(session_id=session_id, cwd=cwd, project_name=project, short_id=short)
        self.sessions[session_id] = s
        return s

    def _ensure_session(self, session_id: str, cwd: str, event: Dict[str, Any]) -> SessionState:
        """取 session；不存在则新建，若持久化映射里有记录则恢复（续接同一张卡片）。"""
        s = self.sessions.get(session_id)
        if s:
            return s
        s = self._new_session(session_id, cwd)
        mapping = self.registry.get(session_id)
        if mapping:
            self._restore_session(s, mapping, event.get("transcript_path", ""))
        return s

    def _restore_session(self, s: SessionState, mapping: Dict[str, Any], transcript_path: str):
        """服务重启后恢复：还原卡片映射，并从 transcript 按一轮轮对话重建当前卡内容。"""
        s.feishu_message_id = (mapping.get("feishu_message_id") or "").strip() or None
        s.card_seq = int(mapping.get("card_seq", 1))
        s.client_source = str(mapping.get("client_source", "cli") or "cli")
        s.claude_code_title = str(mapping.get("claude_code_title", "") or "")
        if not s.claude_code_title and transcript_path:
            # 映射里没标题（历史会话刚被磁盘同步补进来）→ 从 transcript 读真实标题
            s.claude_code_title = self._transcript_title(transcript_path)
        # 面板数 cap 到 max_messages：历史 registry 里的 card_messages 可能是旧滚动
        # 残留的大值（多卡 bug 时期），直接用它重建会让恢复后的卡再次超限滚动。
        want = min(int(mapping.get("card_messages", 0) or 0), self.max_messages)
        if transcript_path and want > 0:
            turns, offset = extract_turns_since(transcript_path, 0)
            s.transcript_offset = offset
            s.panels = self._panels_from_turns(turns[-want:])
            if s.panels:
                s.panels[-1]["expanded"] = True
        s.status = "running"

    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        """读取持久化的 会话↔卡片 映射文件。"""
        if not self.persist_mapping:
            return {}
        try:
            p = os.path.join(self.data_dir, "session_cards.json")
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception as e:
            logger.warning(f"加载持久化映射失败: {e}")
        return {}

    def _registry_entry(self, s: SessionState) -> Dict[str, Any]:
        return {
            "feishu_message_id": s.feishu_message_id or "",
            "card_seq": s.card_seq,
            "card_messages": len(s.panels),
            "step_count": s.step_count,
            "client_source": s.client_source,
            "claude_code_title": s.claude_code_title,
            "cwd": s.cwd,
            "project_name": s.project_name,
            "status": s.status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _dump_registry(self):
        """把整个 registry 原子写入数据目录 session_cards.json。"""
        try:
            cap = self.max_sessions * 2
            if len(self.registry) > cap:
                for sid in list(self.registry)[: len(self.registry) - cap]:
                    self.registry.pop(sid, None)
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = os.path.join(self.data_dir, "session_cards.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.registry, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(self.data_dir, "session_cards.json"))
        except Exception as e:
            logger.warning(f"持久化映射失败: {e}")

    def _persist(self, s: SessionState):
        """把该 session 的卡片映射写入数据目录（原子替换），重启后据此续接。"""
        if not self.persist_mapping:
            return
        try:
            self.registry[s.session_id] = self._registry_entry(s)
            self._dump_registry()
        except Exception as e:
            logger.warning(f"持久化映射失败 {s.session_id}: {e}")

    def _persist_all(self, include_disk: bool = False, prune: bool = False,
                     disk: Optional[Dict[str, Dict[str, Any]]] = None,
                     all_disk_sids: Optional[set] = None):
        """同步历史：把当前所有 session 的映射一并落盘（sync_history 菜单用）。

        include_disk=True 时还把磁盘 transcript 会话补进 registry
        （不覆盖已有的实时/registry 条目），实现「一次性获取所有 session」。
        disk 缺省时自己粗扫（light），也可由调用方传入已算好的元信息
        （如 sync_all_sessions 只把最近 N 个的全量 meta 传进来）。
        prune=True 时为同步（reconcile）模式（需求4）：删除 registry 里
        磁盘已不存在、又不活跃、也不在内存的条目——和 claude-code 真实
        session 对齐，而不是无限 add。all_disk_sids 提供「磁盘全部 sid」，
        以便区分「磁盘已不存在」（清理）与「只是不在最近 N 个」（保留）。
        """
        if not self.persist_mapping:
            return
        try:
            for s in self.sessions.values():
                self.registry[s.session_id] = self._registry_entry(s)
            if include_disk:
                if disk is None:
                    disk = self._scan_disk_sessions(light=True)
                for sid, dmeta in disk.items():
                    existing = self.registry.get(sid)
                    if existing is None:
                        self.registry[sid] = {
                            "feishu_message_id": "",
                            "card_seq": 1,
                            "card_messages": dmeta.get("msg_count", 0),
                            "step_count": 0,
                            "client_source": "",
                            "claude_code_title": dmeta.get("title", ""),
                            "cwd": dmeta.get("cwd", ""),
                            "project_name": dmeta.get("project", ""),
                            "status": "ended",
                            "updated_at": dmeta.get("mtime", ""),
                        }
                        continue
                    # 刷新磁盘来源元数据（标题/cwd/条数/时间），不覆盖实时状态与来源
                    if dmeta.get("title"):
                        existing["claude_code_title"] = dmeta["title"]
                    if dmeta.get("cwd"):
                        existing["cwd"] = dmeta["cwd"]
                    if dmeta.get("project"):
                        existing["project_name"] = dmeta["project"]
                    if dmeta.get("msg_count"):
                        existing["card_messages"] = dmeta["msg_count"]
                    if dmeta.get("mtime"):
                        existing["updated_at"] = dmeta["mtime"]
                if prune:
                    active = self._scan_active_entrypoints()
                    live = set(self.sessions.keys())
                    known = all_disk_sids if all_disk_sids is not None else set(disk.keys())
                    stale = [sid for sid in self.registry.keys()
                             if sid not in known and sid not in live and sid not in active]
                    if stale:
                        for sid in stale:
                            self.registry.pop(sid, None)
                        logger.info(f"同步清理：移除 {len(stale)} 个磁盘已不存在的会话条目")
            self._dump_registry()
        except Exception as e:
            logger.warning(f"持久化全部会话失败: {e}")

    def _gc_ended_sessions(self):
        """保留最近 max_sessions 个会话，清理更早已结束的，防止常驻进程内存累积。"""
        if len(self.sessions) <= self.max_sessions:
            return
        ended = [s for s in self.sessions.values() if s.status == "ended"]
        if len(ended) <= self.max_sessions:
            return
        ended.sort(key=lambda s: s.last_activity_at)
        for s in ended[:-self.max_sessions]:
            self.sessions.pop(s.session_id, None)

    async def _on_session_start(self, session_id: str, cwd: str, event: Dict[str, Any]):
        s = self._ensure_session(session_id, cwd or "未知目录", event)
        # claude-code 显示标题（SessionStart 携带）+ 客户端来源，用于卡片标题/副标题
        if event.get("session_title"):
            s.claude_code_title = str(event["session_title"]).strip()
        if not s.claude_code_title:
            # hook 不携带标题时从 transcript 读 aiTitle/customTitle（需求：卡片标题跟随会话真实标题）
            s.claude_code_title = self._transcript_title(event.get("transcript_path", ""))
        src = str(event.get("client_source", "cli") or "cli").lower()
        s.client_source = "vscode" if src in ("vscode", "claude-vscode") else "cli"
        await self._flush(s)  # 首张卡 / 恢复后 PATCH 原卡
        return {"ok": True}

    # ---------------- 面板 ----------------
    @staticmethod
    def _brief(text: str, limit: int) -> str:
        """折叠标题后追加的本轮内容摘要：压平换行/空白，取前 limit 字。"""
        text = (text or "").strip()
        one = " ".join(text.split())
        if not one:
            return ""
        return one[:limit] + "…" if len(one) > limit else one

    @staticmethod
    def _fmt_panel_ts(ts_iso: Optional[str]) -> str:
        """把 transcript 的 ISO 时间戳格式化为面板标题用的时间。

        当天只显示 HH:MM:SS；跨天显示 MM-DD HH:MM:SS，避免歧义。
        解析失败/缺省时退回当前时间。"""
        if ts_iso:
            try:
                dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
                dt = dt.astimezone()
                if dt.date() == datetime.now().date():
                    return dt.strftime("%H:%M:%S")
                return dt.strftime("%m-%d %H:%M:%S")
            except Exception:
                pass
        return datetime.now().strftime("%H:%M:%S")

    def _make_panel(self, title: str, body: str, ts_iso: Optional[str] = None,
                    expanded: bool = True) -> Dict[str, Any]:
        """构造折叠面板：标题 = 时间 · 原标题 + 本轮内容摘要（需求4）。

        ts_iso 传 transcript 的真实时间戳时用真实时间（历史会话补卡/物化时）；
        缺省用当前时间（实时事件）。expanded 控制默认折叠/展开。"""
        ts = self._fmt_panel_ts(ts_iso)
        brief = self._brief(body, self.title_brief_len)
        full_title = f"🕐{ts} · {title}" + (f" — {brief}" if brief else "")
        return {"title": full_title, "body": body, "expanded": expanded}

    def _panels_from_turns(self, turns: list, *, expanded: bool = True) -> List[Dict[str, Any]]:
        """把一轮轮对话 turns 转成折叠面板（历史补卡/恢复/刷新共用，避免三处重复逻辑）。"""
        panels: List[Dict[str, Any]] = []
        for t in turns:
            if t.get("role") == "user":
                text = (t.get("text") or "").strip()
                if text:
                    panels.append(self._make_panel("👤 用户请求", text, t.get("ts"), expanded=expanded))
            else:
                title = "🤖 回复" + (f" · 🔧 {t.get('tools')}" if t.get("tools") else "")
                panels.append(self._make_panel(title, t.get("text") or "（无输出）", t.get("ts"), expanded=expanded))
        return panels

    def _add_panel(self, s: SessionState, title: str, body: str, ts_iso: Optional[str] = None):
        s.panels.append(self._make_panel(title, body, ts_iso))
        # 防御：flush 持续失败等极端情况下不无限增长（正常由滚动控制在 max_messages 内）
        if len(s.panels) > self.max_messages * 2:
            s.panels = s.panels[-(self.max_messages):]
        # 除最新一条外都折叠
        for p in s.panels[:-1]:
            p["expanded"] = False

    def _on_stop(self, s: SessionState, event: Dict[str, Any]):
        """Stop：读 transcript 增量，按一轮轮「用户-agent」逐条补卡（需求2）。

        - agent 跨工具调用的多次输出聚合为一条「🤖 回复」；
        - 本轮用户请求已由 UserPromptSubmit 实时上卡，文本匹配的跳过，避免重复；
        - 服务中断期间错过的历史轮次在此补上，各成一条，绝不合并成一条消息。
        """
        turns, offset = extract_turns_since(event.get("transcript_path", ""), s.transcript_offset)
        if offset > s.transcript_offset:
            s.transcript_offset = offset
        pending = set(s.pending_user_prompts)
        added = False
        for t in turns:
            if t.get("role") == "user":
                text = (t.get("text") or "").strip()
                if not text:
                    continue
                if text in pending:
                    continue  # 已由 UserPromptSubmit 实时上卡
                self._add_panel(s, "👤 用户请求", text, t.get("ts"))
                added = True
            else:
                title = "🤖 回复" + (f" · 🔧 {t.get('tools')}" if t.get("tools") else "")
                self._add_panel(s, title, t.get("text") or "", t.get("ts"))
                added = True
        s.pending_user_prompts.clear()  # 本轮已结算
        if not added and event.get("last_assistant_message"):
            # transcript 读不到（文件缺失等）时用事件自带的文本兜底
            self._add_panel(s, "🤖 回复", str(event["last_assistant_message"]))
            added = True
        if added:
            s.step_count += 1
            self._mark_dirty(s)

    def _summarize_input(self, tool_input: Any, limit: int = 120) -> str:
        try:
            if isinstance(tool_input, dict):
                for k in ("command", "file_path", "path", "url"):
                    if k in tool_input:
                        v = str(tool_input[k])
                        return (v[:limit] + "…") if len(v) > limit else v
                return str(tool_input)[:limit]
            if tool_input is None:
                return ""
            return str(tool_input)[:limit]
        except Exception:
            return ""

    # ---------------- 决策 ----------------
    async def _create_decision(self, s: SessionState, tool: str, reason: str) -> str:
        decision_id = uuid.uuid4().hex[:16]
        meta = {"session_id": s.session_id, "tool": tool, "reason": reason}
        self.broker.create(decision_id, meta)
        s.pending_decision = {"decision_id": decision_id, "tool": tool, "reason": reason}
        s.status = "awaiting"
        self._add_panel(s, f"⏳ 请求批准 {tool}", reason)
        self._mark_dirty(s)
        task = asyncio.create_task(self._auto_allow(s, decision_id, meta))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)
        return decision_id

    async def _auto_allow(self, s: SessionState, decision_id: str, meta: Dict[str, Any]):
        await asyncio.sleep(self.decision_timeout)
        got = self.broker.resolve(decision_id, "timeout")
        if got:  # 未被按钮解决，超时自动允许
            s.pending_decision = None
            s.status = "running"
            s.last_result_note = f"⏰ {int(self.decision_timeout)}s 未收到飞书决策，已自动允许 {meta['tool']}"
            self._mark_dirty(s)

    # ---------------- AskUserQuestion ----------------
    @staticmethod
    def _is_question_input(tool_input: Any) -> bool:
        try:
            if isinstance(tool_input, dict) and isinstance(tool_input.get("questions"), list):
                return bool(tool_input["questions"])
        except Exception:
            pass
        return False

    async def _create_question(self, s: SessionState, tool_input: Any) -> str:
        """AskUserQuestion：把选项结构存进 pending_decision，卡片渲染成可点选项。

        PreToolUse 与 PermissionRequest 会对同一次 AskUserQuestion 双触发，
        按问题内容去重：15s 窗口内同会话同问题复用同一决策，避免发两张重复卡片。
        两个 hook 长轮询同一决策，点一次按钮两边都拿到答案。
        """
        qs = []
        if isinstance(tool_input, dict) and isinstance(tool_input.get("questions"), list):
            qs = [q for q in tool_input["questions"] if isinstance(q, dict)]
        q = qs[0] if qs else {}
        key = (json.dumps(tool_input.get("questions") or [], ensure_ascii=False, sort_keys=True)
               if isinstance(tool_input, dict) else "")
        now = time.time()
        pd = s.pending_decision
        if (pd and pd.get("kind") == "question" and pd.get("question_key") == key
                and now - pd.get("created_ts", 0) < 15):
            return pd["decision_id"]
        decision_id = uuid.uuid4().hex[:16]
        question_text = str(q.get("question", "") or q.get("header", "") or "请选择")
        meta = {"session_id": s.session_id, "tool": "AskUserQuestion", "reason": question_text}
        self.broker.create(decision_id, meta)
        s.pending_decision = {
            "decision_id": decision_id,
            "kind": "question",
            "tool": "AskUserQuestion",
            "reason": question_text,
            "question_key": key,
            "created_ts": now,
            "question": {
                "text": question_text,
                "header": str(q.get("header", "") or ""),
                "multiSelect": bool(q.get("multiSelect", False)),
                "options": [
                    {"label": str(o.get("label", "")), "description": str(o.get("description", "") or "")}
                    for o in q.get("options", []) if isinstance(o, dict) and o.get("label")
                ],
            },
        }
        s.status = "awaiting"
        self._add_panel(s, "❓ 请选择", question_text)
        self._mark_dirty(s)
        task = asyncio.create_task(self._auto_allow(s, decision_id, meta))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)
        return decision_id

    async def resolve_decision(self, decision_id: str, action: str, answer: Optional[str] = None) -> bool:
        meta = self.broker.resolve(decision_id, action, answer)
        if not meta:
            return False
        s = self.sessions.get(meta["session_id"])
        if s:
            s.pending_decision = None
            s.status = "running"
            if action == "answer":
                s.last_result_note = f"✅ 已远程选择：{answer or '（空）'}"
            elif action == "allow":
                s.last_result_note = f"✅ 已远程允许 {meta['tool']}"
            elif action == "deny":
                s.last_result_note = f"❌ 已远程拒绝 {meta['tool']}"
            self._mark_dirty(s)
        return True

    # ---------------- 远程指令（卡片底部输入框 → claude --resume）----------------
    @staticmethod
    def _resolve_claude_bin() -> str:
        """定位 claude CLI：优先 PATH，其次 npm 全局常见位置；找不到退回裸命令名。"""
        import shutil
        p = shutil.which("claude")
        if p:
            return p
        for cand in (
            os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.expandvars(r"%APPDATA%\npm\claude"),
            r"C:\Program Files\nodejs\claude.cmd",
        ):
            if os.path.isfile(cand):
                return cand
        return "claude"

    async def send_command_to_session(self, session_id: str, text: str) -> bool:
        """把卡片底部输入框的文字下发给对应会话执行（改进2）。

        机制：后台 `claude --resume <session_id> -p "<text>"` 恢复该会话并执行，
        执行过程中的 hooks（UserPromptSubmit/Stop 等）照常上报 → 本卡正常更新。
        每会话一把锁防止并发写同一 transcript；claude -p 的 stdout 作为兜底
        （hooks 已更新卡片时不重复上卡）。"""
        text = (text or "").strip()
        if not text:
            return False
        s = self.sessions.get(session_id)
        if s is None:
            await self.feishu.send_text(f"⚠️ 会话 {session_id[:8]} 不存在或已被清理，无法下发指令。")
            return False
        lock = self._command_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            await self.feishu.send_text(f"⏳ 会话 {session_id[:8]} 已有指令在运行，请等待完成后再发送。")
            return False
        async with lock:
            s.status = "running"
            self._add_panel(s, "🚀 远程指令", text)
            s.step_count += 1
            self._mark_dirty(s)
            before = len(s.panels)
            claude_bin = self._resolve_claude_bin()
            cmd = [claude_bin, "--resume", session_id, "--print", text]
            logger.info(f"下发远程指令: {cmd}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=s.cwd or None,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, err = await proc.communicate()
                stdout = (out or b"").decode("utf-8", "replace").strip()
                stderr = (err or b"").decode("utf-8", "replace").strip()
                if proc.returncode == 0 and len(s.panels) == before and stdout:
                    # 钩子没补上卡（子进程 hooks 未触发等）时用 claude -p 的 stdout 兜底
                    self._add_panel(s, "🤖 回复（远程指令）", stdout[:2000])
                    s.step_count += 1
                    self._mark_dirty(s)
                elif proc.returncode != 0:
                    self._add_panel(s, "⚠️ 远程指令失败", stderr or f"退出码 {proc.returncode}")
                    s.step_count += 1
                    self._mark_dirty(s)
                logger.info(f"远程指令完成 {session_id} exit={proc.returncode} stdout={len(stdout)}B stderr={len(stderr)}B")
                return True
            except Exception as e:
                self._add_panel(s, "⚠️ 远程指令启动失败", f"{type(e).__name__}: {e}")
                s.step_count += 1
                self._mark_dirty(s)
                logger.exception(f"远程指令启动失败 {session_id}")
                return False

    # ---------------- 同步历史（飞书菜单 sync_history）----------------
    def _transcript_paths(self, session_id: str) -> List[str]:
        """在 ~/.claude/projects/*/ 下定位某 session 的所有 transcript 文件
        （同一 sid 可能出现在多个项目目录，删除时全部清理）。"""
        projects = os.path.expanduser("~/.claude/projects")
        out: List[str] = []
        if not os.path.isdir(projects):
            return out
        try:
            for d in os.listdir(projects):
                sub = os.path.join(projects, d)
                if os.path.isdir(sub):
                    p = os.path.join(sub, session_id + ".jsonl")
                    if os.path.isfile(p):
                        out.append(p)
        except Exception:
            pass
        return out

    def _transcript_path(self, session_id: str) -> str:
        """在 ~/.claude/projects/*/ 下定位某 session 的 transcript 文件（取第一个）。"""
        paths = self._transcript_paths(session_id)
        return paths[0] if paths else ""

    @staticmethod
    def _transcript_title(path: str) -> str:
        """从 transcript 提取会话标题：customTitle（用户重命名）优先，
        其次 aiTitle（AI 自动生成），最后 session_title。各取最后一次出现（最新）。
        只读文件开头 1MB（summary 记录在文件顶部），历史会话区分真实标题的关键。
        """
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(1_000_000)
        except Exception:
            return ""

        def _last(key: str) -> str:
            val = ""
            try:
                pat = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key
                for mm in re.finditer(pat, raw):
                    try:
                        val = json.loads('"' + mm.group(1) + '"')
                    except Exception:
                        val = mm.group(1)
            except Exception:
                pass
            return val

        return (_last("customTitle") or _last("aiTitle") or _last("session_title")).strip()

    def _transcript_meta(self, path: str, session_id: str, light: bool = False) -> Dict[str, Any]:
        """从 transcript 提取 cwd / 标题(aiTitle/customTitle) / 消息轮数 / 用户提问数 / 最近内容摘要 / 最后活动时间。

        light=True 时只读开头 1MB（标题/cwd/mtime），跳过全量读取
        （msg_count/user_count/brief 置 0），供「先粗扫全部、再对最近 N 个做全量」的两段式同步。"""
        meta = {"title": "", "cwd": "", "project": "", "msg_count": 0,
                "user_count": 0, "brief": "", "mtime": ""}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(1_000_000)
            # cwd：取第一处；标题：customTitle > aiTitle > session_title（最新一次出现）
            m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            if m:
                try:
                    meta["cwd"] = json.loads('"' + m.group(1) + '"')
                except Exception:
                    meta["cwd"] = m.group(1)
            meta["title"] = self._transcript_title(path)
            if not light:
                # 全量：按一轮轮对话统计消息轮数与用户提问条数（不含 assistant 轮）
                turns, _total = extract_turns_since(path, 0)
                meta["msg_count"] = len(turns)
                meta["user_count"] = sum(1 for t in turns if t.get("role") == "user")
                if turns:
                    last = turns[-1]
                    brief = (last.get("text") or "").strip().replace("\n", " ")
                    prefix = "👤" if last.get("role") == "user" else "🤖"
                    meta["brief"] = f"{prefix} {brief[:80]}"
            st = os.stat(path)
            meta["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
            meta["project"] = (meta["cwd"] or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        except Exception:
            pass
        return meta

    def _scan_disk_sessions(self, light: bool = False) -> Dict[str, Dict[str, Any]]:
        """扫描 ~/.claude/projects/ 下所有 transcript，返回 sid → 元信息。
        这是「VSCode Claude Code 中存在的所有 session」的权威来源。
        light=True 时只做粗扫（标题/cwd/mtime），供「先粗扫再取最近 N 个全量」。
        每个 meta 附私有键 `_path`（transcript 绝对路径，仅内部两段式同步用）。"""
        found: Dict[str, Dict[str, Any]] = {}
        projects = os.path.expanduser("~/.claude/projects")
        if not os.path.isdir(projects):
            return found
        try:
            for d in sorted(os.listdir(projects)):
                sub = os.path.join(projects, d)
                if not os.path.isdir(sub):
                    continue
                try:
                    for fn in os.listdir(sub):
                        if fn.endswith(".jsonl"):
                            sid = fn[:-6]
                            p = os.path.join(sub, fn)
                            meta = self._transcript_meta(p, sid, light=light)
                            meta["_path"] = p
                            found[sid] = meta
                except Exception:
                    continue
        except Exception:
            pass
        return found

    def _recent_disk_sessions(self, n: int):
        """两段式：轻量粗扫全部 transcript，按最后活动倒序取最近 n 个，
        再对这 n 个做全量元信息（含用户提问数）。
        返回 (recent: {sid: 全量meta}, all_sids: 磁盘全部 sid 集合)，
        all_sids 供 registry 清理时区分「磁盘已不存在」与「只是不在最近 N 个」。"""
        light = self._scan_disk_sessions(light=True)
        all_sids = set(light.keys())
        recent: Dict[str, Dict[str, Any]] = {}
        for sid in sorted(light, key=lambda x: light[x].get("mtime") or "", reverse=True)[:max(1, int(n))]:
            meta = light[sid]
            path = meta.get("_path") or self._transcript_path(sid)
            recent[sid] = self._transcript_meta(path, sid) if path else meta
        return recent, all_sids

    @staticmethod
    def _scan_active_entrypoints() -> Dict[str, str]:
        """扫描 ~/.claude/sessions/*.json（当前活跃会话的 PID 文件），
        返回 sessionId → entrypoint（如 claude-vscode / cli）。仅覆盖正在运行的会话。"""
        out: Dict[str, str] = {}
        sess_dir = os.path.expanduser("~/.claude/sessions")
        if not os.path.isdir(sess_dir):
            return out
        try:
            for fn in os.listdir(sess_dir):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(sess_dir, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    sid = d.get("sessionId")
                    ep = str(d.get("entrypoint") or "")
                    if sid and ep:
                        out[str(sid)] = ep
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _active_pid_map(self) -> Dict[str, List[int]]:
        """扫描 ~/.claude/sessions/*.json（活跃会话 PID 文件），返回 sessionId → pid 列表。
        同一会话可能有多个 pid 文件（多进程/多入口），全部收集；与 _scan_active_entrypoints
        同源，供执行探针与删除守卫判断进程是否存活。"""
        out: Dict[str, List[int]] = {}
        sess_dir = os.path.expanduser("~/.claude/sessions")
        if not os.path.isdir(sess_dir):
            return out
        try:
            for fn in os.listdir(sess_dir):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(sess_dir, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    sid = d.get("sessionId")
                    pid = d.get("pid")
                    if sid and pid:
                        try:
                            out.setdefault(str(sid), []).append(int(pid))
                        except (TypeError, ValueError):
                            pass
                except Exception:
                    continue
        except Exception:
            pass
        return out

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """判断进程是否存活。os.kill(pid, 0) 用信号 0 探测存在性：
        Windows 下 winerror 87 表示进程不存在；无权限等无法探测时保守视为存活。"""
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError as e:
            if getattr(e, "winerror", None) == 87:
                return False
            return True
        except Exception:
            return False

    def _execution_hint(self, sid: str, transcript_path: str,
                        pid_map: Optional[Dict[str, List[int]]] = None) -> Optional[str]:
        """基于「进程存活 + transcript 写入时间」评估会话当前是否在执行（Q1）。

        返回 'running'（进程在且 transcript 最近在写）/ 'idle'（进程在但停写）
        / 'ended'（pid 文件在但进程已死）/ None（无 pid 记录，无法判断）。
        只做 stat / 存活探测，不开文件读正文，适合列表卡批量调用。"""
        window = float(self.cfg.get("card", {}).get("exec_probe_window_sec", 45) or 45)
        pids = (pid_map or {}).get(sid) or []
        alive = any(self._pid_alive(int(p)) for p in pids if p)
        recent = False
        if transcript_path and os.path.isfile(transcript_path):
            try:
                recent = (time.time() - os.path.getmtime(transcript_path)) < window
            except OSError:
                pass
        if not pids:
            return None
        if alive and recent:
            return "running"
        if alive:
            return "idle"
        return "ended"

    def _classify_client_source(self, sid: str, s: Optional[SessionState],
                                reg: Optional[Dict[str, Any]],
                                active: Optional[Dict[str, str]]) -> str:
        """判断某 session 属于 vscode 还是 cli，优先级：
        1) 内存实时状态里记录的 client_source（hook 上报，最准）；
        2) 活跃会话 PID 文件里的 entrypoint（当前正在运行）；
        3) 持久化 registry 里记录的 client_source；
        4) 兜底 card.unknown_source_default（默认 vscode）——transcript 无来源字段，
           历史会话无法分辨，按配置归入多数用户所在环境（本仓库主要用 VSCode）。
        """
        if s is not None and s.client_source:
            return str(s.client_source)
        ep = (active or {}).get(sid) or ""
        if ep:
            return "vscode" if "vscode" in ep.lower() else "cli"
        cs = (reg or {}).get("client_source") or ""
        if cs:
            return str(cs)
        return str(self.cfg.get("card", {}).get("unknown_source_default", "vscode") or "vscode")

    @staticmethod
    def _source_unknown(sid: str, s: Optional[SessionState], reg: Optional[Dict[str, Any]],
                        active: Optional[Dict[str, str]]) -> bool:
        """该会话的来源是否靠兜底默认猜的（True 时列表卡会给提示）。"""
        if s is not None and s.client_source:
            return False
        if (active or {}).get(sid):
            return False
        if (reg or {}).get("client_source"):
            return False
        return True

    def _merge_info(self, sid: str, s: Optional[SessionState], reg: Optional[Dict[str, Any]],
                    dmeta: Dict[str, Any], active: Optional[Dict[str, str]],
                    pid_map: Optional[Dict[str, List[int]]] = None) -> Dict[str, Any]:
        """把 实时(s) / registry(reg) / 磁盘转录(dmeta) 三个来源合并成一条列表卡信息。
        dmeta 为 transcript 全量元信息时 user_count/msg_count 才可靠；
        实时会话未拿到全量元信息时退回 pending_user_prompts / panels 条数。
        状态收敛为 3 态（running/idle/ended），并叠加执行探针（进程存活 + transcript
        写入时间）修正：进程已死的 running 显示为 ended；进程在且最近在写的 idle 显示为 running。"""
        cs = self._classify_client_source(sid, s, reg, active)
        if s is not None:
            updated = s.last_activity_at.strftime("%Y-%m-%dT%H:%M:%S")
            base_raw = s.status
            user_count = int(dmeta.get("user_count") or 0) or len(s.pending_user_prompts)
            msg_count = int(dmeta.get("msg_count") or 0) or len(s.panels)
            step_count = s.step_count
            card_seq = s.card_seq
            project = s.project_name or dmeta.get("project") or ""
            title = (s.claude_code_title or "").strip() or dmeta.get("title") or project
        else:
            updated = (reg or {}).get("updated_at") or dmeta.get("mtime", "")
            base_raw = (reg or {}).get("status") or "ended"
            user_count = int(dmeta.get("user_count") or 0) or int((reg or {}).get("card_messages", 0) or 0)
            msg_count = int(dmeta.get("msg_count") or 0) or int((reg or {}).get("card_messages", 0) or 0)
            step_count = int((reg or {}).get("step_count", 0) or 0)
            card_seq = int((reg or {}).get("card_seq", 1) or 1)
            project = dmeta.get("project") or (reg or {}).get("project_name") or ""
            title = (reg or {}).get("claude_code_title") or dmeta.get("title") or project or "未命名会话"
        # 三态归一 + 执行探针修正（awaiting 固定显示为 idle，绝不被探针翻成 running）
        status = display_status(base_raw)
        if base_raw != "awaiting" and status in ("running", "idle"):
            hint = self._execution_hint(sid, dmeta.get("_path") or "", pid_map)
            if hint == "ended":
                status = "ended"
            elif hint == "running":
                status = "running"
            elif hint == "idle" and base_raw == "running":
                status = "idle"  # 进程在但 transcript 已停写：hook 标 running 实际已空闲
        return {
            "session_id": sid,
            "title": title,
            "project": project,
            "status": status,
            "step_count": step_count,
            "card_messages": msg_count,
            "user_count": user_count,
            "msg_count": msg_count,
            "card_seq": card_seq,
            "updated_at": updated,
            "brief": dmeta.get("brief", ""),
            "client_source": cs,
            "source_unknown": self._source_unknown(sid, s, reg, active),
        }

    def _all_session_infos(self, env: Optional[str] = None) -> List[Dict[str, Any]]:
        """汇总所有 session 基本信息（/debug/sessions 用）：内存实时状态 + 磁盘全部 transcript + registry 兜底。

        env 传 "vscode"/"cli" 时只返回该来源环境的会话（调试过滤）；传 None 返回全部。"""
        disk = self._scan_disk_sessions()  # 全量元信息（含用户提问数）
        active = self._scan_active_entrypoints()
        pid_map = self._active_pid_map()
        infos: List[Dict[str, Any]] = []
        seen = set()
        # 1) 内存中的实时会话（状态最准）
        for sid, s in self.sessions.items():
            seen.add(sid)
            info = self._merge_info(sid, s, self.registry.get(sid) or {}, disk.get(sid) or {},
                                    active, pid_map)
            if env is None or info["client_source"] == env:
                infos.append(info)
        # 2) 磁盘上存在但不在内存的会话（重启后未收到 hooks 的历史会话）
        for sid, dmeta in disk.items():
            if sid in seen:
                continue
            seen.add(sid)
            info = self._merge_info(sid, None, self.registry.get(sid) or {}, dmeta, active, pid_map)
            if env is None or info["client_source"] == env:
                infos.append(info)
        # 3) registry 兜底（磁盘扫描漏掉的）
        for sid, reg in self.registry.items():
            if sid in seen:
                continue
            seen.add(sid)
            info = self._merge_info(sid, None, reg, {}, active, pid_map)
            if env is None or info["client_source"] == env:
                infos.append(info)
        infos.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return infos

    def _recent_list_infos(self, n: int) -> List[Dict[str, Any]]:
        """同步与删除后刷新共用的列表数据装配：两段式取最近 N 个 → 持久化(带清理) →
        合并实时/registry/磁盘成列表行。返回按最后活动倒序的 infos。"""
        recent, all_sids = self._recent_disk_sessions(n)
        self._persist_all(include_disk=True, prune=True, disk=recent, all_disk_sids=all_sids)
        active = self._scan_active_entrypoints()
        pid_map = self._active_pid_map()
        infos: List[Dict[str, Any]] = []
        for sid, dmeta in recent.items():
            s = self.sessions.get(sid)
            infos.append(self._merge_info(sid, s, self.registry.get(sid) or {}, dmeta, active, pid_map))
        infos.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return infos

    async def sync_all_sessions(self) -> int:
        """菜单 sync_history（需求：合并回单个 Claude Code，事件 sync_history@claude_code）。

        只同步最近有更新的 N 个会话（sync_recent_max，默认 10）：
        轻量粗扫全部 transcript → 按最后活动倒序取前 N 做全量元信息（含用户提问数）→
        只把这 N 个写入 session_cards.json，同时清理磁盘已不存在的条目；
        最后回发最近 N 个的列表卡（状态｜标题｜提问条数｜刷新｜删除，逐列对齐）。
        记录列表卡 message_id，删除会话后原地 PATCH 让对应行消失。"""
        n = int(self.cfg.get("card", {}).get("sync_recent_max", 10) or 10)
        infos = self._recent_list_infos(n)
        card = build_session_list_card(infos, self.cfg)
        mid = await self.feishu.send_card(card)
        if mid:
            self._last_list_card_id = mid
        logger.info(f"sync_history: 已同步最近 {len(infos)} 个会话，列表卡 {'发送成功' if mid else '发送失败'}")
        return len(infos)

    def _materialize_session(self, session_id: str, mapping: Dict[str, Any]) -> SessionState:
        """把 registry 里的历史会话物化成 SessionState（从 transcript 重建面板），
        供「立即刷新」对不在内存的会话也能禁用旧卡并发送新卡。"""
        cwd = str(mapping.get("cwd") or "")
        project = (str(mapping.get("project_name") or "")
                   or cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
        s = SessionState(session_id=session_id, cwd=cwd, project_name=project,
                         short_id=session_id[:8])
        s.client_source = str(mapping.get("client_source", "cli") or "cli")
        s.claude_code_title = str(mapping.get("claude_code_title", "") or "")
        s.feishu_message_id = str(mapping.get("feishu_message_id") or "").strip() or None
        s.card_seq = int(mapping.get("card_seq", 1) or 1)
        s.status = mapping.get("status") or "ended"
        path = self._transcript_path(session_id)
        if not s.claude_code_title and path:
            # 刷新历史会话时标题来自 transcript（aiTitle/customTitle），与列表卡一致
            s.claude_code_title = self._transcript_title(path)
        if path:
            turns, offset = extract_turns_since(path, 0)
            s.transcript_offset = offset
            # 需求3：物化（刷新）的卡片默认全部折叠，且用 transcript 真实时间戳显示每轮时间。
            # Q2 修复：不再用 registry 的 card_messages（那只是「当前卡」条数，滚动后可能只剩几条），
            # 改为展示最近最多 max_messages 轮对话，让刷新后的卡片有足够历史。
            s.panels = self._panels_from_turns(turns[-self.max_messages:], expanded=False)
        return s

    async def refresh_card(self, session_id: str) -> bool:
        """点击会话列表卡里的「立即刷新」（改进3，问题2）：
        1. 旧卡（若存在）PATCH 成「已归档」，停止更新；
        2. 重新发送一张内容最新的全新卡片，并更新映射。
        对重启后不在内存的历史会话：先从 registry + transcript 物化再刷新。"""
        s = self.sessions.get(session_id)
        if s is None:
            mapping = self.registry.get(session_id)
            if not mapping:
                await self.feishu.send_text(f"⚠️ 找不到会话 {session_id[:8]}（既不在运行也不在记录中）。")
                return False
            s = self._materialize_session(session_id, mapping)
            self.sessions[session_id] = s
            self._persist(s)
        # 1) 禁用旧卡（用旧的 panels 渲染归档说明）
        if s.feishu_message_id:
            await self.feishu.update_card(s.feishu_message_id, build_card(
                s, self.cfg, include_pending=False,
                superseded_note="📦 已归档：点击「立即刷新」已生成新卡片，本卡停止更新",
                expand_latest=False))
        # 2) 重建面板：从 transcript 取最近最多 max_messages 轮（Q2 修复）。
        #    内存路径的 s.panels 可能只保留最近一部分（_add_panel 防御截断），
        #    直接用会把更早历史丢光；统一从 transcript 重建，新卡才有完整历史。
        path = self._transcript_path(session_id)
        if path:
            turns, offset = extract_turns_since(path, 0)
            s.transcript_offset = offset
            s.panels = self._panels_from_turns(turns[-self.max_messages:], expanded=False)
        # 3) 发送全新卡片（需求3：对话默认折叠模式）
        new_mid = await self.feishu.send_card(build_card(s, self.cfg, expand_latest=False))
        if new_mid:
            s.feishu_message_id = new_mid
            s.update_count = 0
            s.card_seq += 1
            self._persist(s)
            logger.info(f"sync_refresh: 已禁用旧卡并发送新卡 {session_id} -> {new_mid}")
            return True
        logger.error(f"sync_refresh: 新卡发送失败 {session_id}")
        return False

    # ---------------- 删除会话（列表卡「删除」，当前需求）----------------
    def _session_alive(self, session_id: str) -> bool:
        """删除守卫：~/.claude/sessions/*.json 活跃注册表里该会话任一 pid 存活即视为活跃。
        活跃会话拒绝删除（Claude Code 无 session 锁，删 transcript 会破坏正在运行的会话且不可逆）。"""
        pid_map = self._active_pid_map()
        return any(self._pid_alive(int(p)) for p in pid_map.get(session_id, []) if p)

    def _session_title(self, session_id: str) -> str:
        """删除确认/已删除卡用的标题：内存实时 > registry > 磁盘 transcript > 兜底。"""
        s = self.sessions.get(session_id)
        if s is not None:
            t = (s.claude_code_title or "").strip() or s.project_name
            if t:
                return t
        reg = self.registry.get(session_id) or {}
        t = (reg.get("claude_code_title") or "").strip() or reg.get("project_name") or ""
        if t:
            return t
        path = self._transcript_path(session_id)
        if path:
            t = self._transcript_title(path)
            if t:
                return t
        return "未命名会话"

    async def request_delete_session(self, session_id: str) -> bool:
        """删除第一步：活跃守卫 + 发二次确认卡（删除不可逆，必须显式确认）。

        活跃（pid 存活）或内存中仍有待确认/提问的会话直接拒绝；
        否则回发「确认删除」卡，点确认后由 delete_session 真正执行。"""
        if not session_id:
            await self.feishu.send_text("⚠️ 缺少 session_id，无法删除。")
            return False
        if self._session_alive(session_id):
            await self.feishu.send_text(f"⚠️ 会话 {session_id[:8]} 正在运行（活跃注册表），已拒绝删除。")
            return False
        s = self.sessions.get(session_id)
        if s is not None and s.pending_decision:
            await self.feishu.send_text(f"⚠️ 会话 {session_id[:8]} 有待确认/提问未处理，已拒绝删除。")
            return False
        card = build_delete_confirm_card(session_id, self._session_title(session_id))
        mid = await self.feishu.send_card(card)
        if mid:
            self._pending_delete_cards[session_id] = mid
        return bool(mid)

    async def delete_session(self, session_id: str) -> bool:
        """删除会话本体（确认后执行）：
        1. 再查一次活跃守卫（确认期间可能被重新激活）；
        2. 把该会话旧卡与确认卡 PATCH 成「已删除」，停止更新；
        3. 从内存移出并取消 flush；
        4. 删除 ~/.claude/projects/*/ 下该 sid 的全部 transcript；
        5. 从持久化 registry 移除；
        6. 原地刷新上一张「最近会话」列表卡，让该行消失。"""
        if self._session_alive(session_id):
            await self.feishu.send_text(f"⚠️ 会话 {session_id[:8]} 正在运行（活跃注册表），已拒绝删除。")
            return False
        title = self._session_title(session_id)
        s = self.sessions.get(session_id)
        # 1) 归档：会话旧卡（若存在）+ 确认卡 → 「已删除」
        old_card_mid = ""
        if s is not None and s.feishu_message_id:
            old_card_mid = s.feishu_message_id
        elif not old_card_mid:
            old_card_mid = (self.registry.get(session_id) or {}).get("feishu_message_id", "") or ""
        confirm_mid = self._pending_delete_cards.pop(session_id, "") or ""
        dead_card = build_deleted_card(session_id, title)
        for mid in (old_card_mid, confirm_mid):
            if mid and str(mid).strip():
                try:
                    await self.feishu.update_card(str(mid).strip(), dead_card)
                except Exception as e:
                    logger.warning(f"归档删除卡失败 {mid}: {e}")
        # 2) 移出内存 + 取消 flush
        if s is not None:
            if s.flush_task and not s.flush_task.done():
                s.flush_task.cancel()
            self.sessions.pop(session_id, None)
        # 3) 删磁盘 transcript
        removed, failed = 0, 0
        for p in self._transcript_paths(session_id):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                failed += 1
                logger.warning(f"删除 transcript 失败 {p}: {e}")
        # 4) 清 registry
        if session_id in self.registry:
            self.registry.pop(session_id, None)
            self._dump_registry()
        await self._refresh_list_card_after_delete()
        logger.info(f"sync_delete: 已删除会话 {session_id}，移除 transcript {removed} 个"
                    f"{f'，{failed} 个失败' if failed else ''}")
        return True

    async def _refresh_list_card_after_delete(self):
        """删除后重算最近会话并原地 PATCH 上一张列表卡（该行消失）。
        失败仅记录，不阻塞删除本身。"""
        if not self._last_list_card_id:
            return
        try:
            n = int(self.cfg.get("card", {}).get("sync_recent_max", 10) or 10)
            infos = self._recent_list_infos(n)
            card = build_session_list_card(infos, self.cfg)
            await self.feishu.update_card(self._last_list_card_id, card)
        except Exception:
            logger.exception("删除后刷新列表卡失败")

    # ---------------- 去抖 + 发卡 ----------------
    def _mark_dirty(self, s: SessionState):
        now = time.time()
        s.dirty = True
        if not s.dirty_since:
            s.dirty_since = now
        s.last_event_at = now
        if s.flush_task is None or s.flush_task.done():
            s.flush_task = asyncio.create_task(self._flush_loop(s))

    async def _flush_loop(self, s: SessionState):
        try:
            while s.dirty:
                now = time.time()
                quiet = now - s.last_event_at
                total = now - s.dirty_since
                wait = min(self.debounce - quiet, self.max_batch_wait - total)
                if wait > 0:
                    await asyncio.sleep(wait)
                if s.last_patch_at:
                    since = time.time() - s.last_patch_at
                    if since < self.min_interval:
                        await asyncio.sleep(self.min_interval - since)
                s.dirty = False
                s.last_patch_at = time.time()
                try:
                    await self._flush(s)
                except Exception as e:
                    logger.error(f"刷新卡片失败 {s.session_id}: {e}")
            s.flush_task = None
        except asyncio.CancelledError:
            s.flush_task = None
            raise

    async def _flush(self, s: SessionState):
        if not self.feishu.target_chat_id:
            logger.warning("未配置 target_chat_id，跳过发卡")
            return
        if not s.feishu_message_id:
            # 首张卡：受消息上限约束（超大补卡时只显示最近 max_messages 条，更早的在 transcript）
            if len(s.panels) > self.max_messages:
                s.panels = s.panels[-(self.max_messages):]
            mid = await self.feishu.send_card(build_card(s, self.cfg))
            if mid:
                s.feishu_message_id = mid
                s.update_count = 0
                logger.info(f"卡片已发送 {s.session_id} -> {mid}")
            else:
                logger.error(f"卡片发送失败 {s.session_id}（send_card 返回空）")
            self._persist(s)
            return
        # 需求（多卡 bug 修复）：不再滚动新卡。一个 session 始终只有一张卡片，
        # 有更新就原地 PATCH 同一张卡；显示内容由 build_card 的 max_panels 压缩
        # 兜底（超出时把最旧步骤合并成"更早 N 步"摘要，完整历史仍在 transcript）。
        # 此前滚动逻辑在 panels 超限后用 s.panels = s.panels[-(overflow):] 截断，
        # 当 overflow 本身 > max_messages 时新卡仍超限 → 每次更新都滚一张新卡，
        # 一个会话在飞书里堆积多张卡片。
        ok = await self.feishu.update_card(s.feishu_message_id, build_card(s, self.cfg))
        if ok:
            s.update_count += 1
            logger.info(f"卡片已更新 {s.session_id}（第 {s.update_count} 次，本卡 {len(s.panels)} 条）")
        else:
            logger.error(f"卡片更新失败 {s.session_id}")
        self._persist(s)
