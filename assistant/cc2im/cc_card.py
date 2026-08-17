# -*- coding: utf-8 -*-
"""飞书卡片 v2 构建：一张 session 一张卡片，折叠面板累积输出、滑动窗口、待确认按钮。"""
from __future__ import annotations

from typing import Any, Dict, List

STATUS_TEMPLATE = {
    "running": "blue",
    "idle": "grey",
    "ended": "green",
}
STATUS_LABEL = {
    "running": "🟢 运行中",    # 有任务在跑（不停有输出/transcript 持续写入）
    "idle": "🟡 空闲中",      # 当前任务已完成 / 正等待人工确认 / 等待新指令
    "ended": "🏁 已结束",
}
# 列表卡每行用的极简状态标识（单独成列，与标题/提问数/刷新按钮按列对齐）
STATUS_EMOJI = {
    "running": "🟢",
    "idle": "🟡",
    "ended": "🏁",
}


def display_status(status: str) -> str:
    """把内部状态归一到对外显示的 3 态：awaiting（等待人工确认）归入 idle，
    error 等未知值兜底归 idle。内部状态机仍可产生 awaiting，展示层统一收敛。"""
    return status if status in ("running", "idle", "ended") else "idle"


def _fmt_ts(ts) -> str:
    return ts.strftime("%H:%M:%S") if ts else "--:--:--"


def _wrap(text: str, window_lines: int) -> str:
    """滑动窗口：只保留最新 N 行关键输出，超出加截断说明。"""
    if not text:
        return "（无输出）"
    lines = [ln.rstrip() for ln in text.split("\n")]
    if len(lines) > window_lines:
        lines = lines[-window_lines:]
        lines.insert(0, f"… 仅显示最近 {window_lines} 行，更早内容已省略")
    return "\n".join(lines)


def _more_old_panel(count: int) -> Dict[str, Any]:
    return {
        "title": f"📦 更早 {count} 步",
        "body": f"共 {count} 步较旧的步骤已压缩为摘要，详情在更早的卡片/历史记录中。",
        "expanded": False,
    }


def _append_question(elements: List[Dict[str, Any]], pending: Dict[str, Any]):
    """AskUserQuestion：单选=选项按钮（点击即提交）+ 其他输入；
    多选=multi_select_static（v2 无 checkbox 组件）+ 其他输入 + 提交按钮。
    按钮 value 一律字符串（cc:"1"），避免飞书回调把非字符串 value 当错误。"""
    q = pending.get("question") or {}
    did = pending["decision_id"]
    options = q.get("options") or []
    multi = bool(q.get("multiSelect"))

    content = f"❓ **{q.get('text') or '请选择'}**"
    if q.get("header"):
        content = f"❓ **{q['header']}**\n{q.get('text') or ''}"
    if multi:
        content += "\n（🔁 可多选）"
    elements.append({"tag": "markdown", "content": content, "text_align": "left"})

    # 选项说明（紧凑列表，便于理解）
    if options:
        lines = [f"{i + 1}. **{o['label']}**" + (f" · {o.get('description', '')}" if o.get("description") else "")
                 for i, o in enumerate(options)]
        elements.append({"tag": "markdown", "content": "\n".join(lines), "text_align": "left"})

    form_items: List[Dict[str, Any]] = []
    if multi:
        # 多选：下拉多选组件，值类型为数组（form_value[sel_*]）
        form_items.append({
            "tag": "multi_select_static",
            "name": f"sel_{did}",
            "placeholder": {"tag": "plain_text", "content": "选择多个选项…"},
            "options": [
                {"text": {"tag": "plain_text", "content": o["label"]}, "value": o["label"]}
                for o in options
            ],
        })
    else:
        # 单选：每个选项一个可点按钮（两列一行），点击即提交
        for i in range(0, len(options), 2):
            chunk = options[i:i + 2]
            cols = []
            for o in chunk:
                cols.append({
                    "tag": "column", "width": "weighted", "weight": 1,
                    "elements": [{
                        "tag": "button", "type": "primary",
                        "text": {"tag": "plain_text", "content": o["label"]},
                        "value": {"cc": "1", "decision_id": did, "action": "answer", "answer": o["label"]},
                    }],
                })
            elements.append({"tag": "column_set", "columns": cols})

    # 其他（自定义输入）+ 提交按钮；v2 表单容器 tag 必须是 "form"（带唯一 name）。
    # 提交回调在 action.form_value[custom_*] / [sel_*] 携带数据。
    input_name = f"custom_{did}"
    form_items += [
        {
            "tag": "input",
            "name": input_name,
            "label": {"tag": "plain_text", "content": "✍️ 其他（自定义输入）"},
            "placeholder": {"tag": "plain_text", "content": "输入你的答案后点提交…"},
        },
        {
            "tag": "button", "type": "default",
            "name": f"submit_{did}",
            "form_action_type": "submit",
            "text": {"tag": "plain_text", "content": "提交"},
            "value": {"cc": "1", "decision_id": did,
                      "action": "answer_multi" if multi else "answer_custom"},
        },
    ]
    elements.append({"tag": "hr"})
    elements.append({"tag": "form", "name": f"form_{did}", "elements": form_items})


def build_card(session, cfg: Dict[str, Any], *,
               include_pending: bool = True, superseded_note: str = "",
               expand_latest: bool = True) -> Dict[str, Any]:
    """把 SessionState 渲染成飞书卡片 v2。

    include_pending=False 时不渲染待确认/提问区块（归档旧卡时用，避免旧卡遗留
    早已失效的选项按钮，用户点击无响应——"僵尸卡"问题）。
    superseded_note 非空时替换状态行文本（提示该卡已归档/停止更新）。
    expand_latest=False 时所有对话面板默认折叠（需求3：刷新/归档卡不展开最新一条）。
    卡片不再渲染底部文本输入框与发送按钮（需求：卡片只更新历史，不用于持续回话）。
    """
    card_cfg = cfg.get("card", {})
    max_panels = int(card_cfg.get("max_panels", 12))
    window_lines = int(card_cfg.get("window_lines", 30))

    # ---- 面板上限压缩：最旧的合并成摘要 ----
    panels: List[Dict[str, Any]] = list(session.panels or [])
    dropped = 0
    if len(panels) > max_panels:
        dropped = len(panels) - (max_panels - 1)
        old = panels[:dropped]
        panels = [_more_old_panel(len(old))] + panels[dropped:]
        if expand_latest:
            panels[-1]["expanded"] = True  # 最新一条展开
    if not expand_latest:
        # 需求3：刷新/归档卡默认全部折叠
        for p in panels:
            p["expanded"] = False

    status = display_status(session.status or "running")
    status_text = STATUS_LABEL.get(status, status)
    if superseded_note:
        status_text = superseded_note
    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**{status_text}** · 步骤 {session.step_count}\n"
                f"🗂 {session.cwd} · 开始 {_fmt_ts(session.created_at)} · "
                f"更新 {_fmt_ts(session.last_activity_at)}"
            ),
            "text_align": "left",
        },
        {"tag": "hr"},
    ]

    # ---- 折叠面板 ----
    for p in panels:
        elements.append({
            "tag": "collapsible_panel",
            "expanded": bool(p.get("expanded")),
            "header": {"title": {"tag": "plain_text", "content": p["title"]}},
            "elements": [{"tag": "markdown", "content": _wrap(p.get("body", ""), window_lines)}],
        })

    # ---- 待确认区 ----
    pending = session.pending_decision
    # 三态化后 awaiting 已归入 idle，是否渲染取决于有没有待确认/提问，而不是状态名
    if pending and include_pending:
        elements.append({"tag": "hr"})
        if pending.get("kind") == "question":
            # AskUserQuestion：选项按钮 + 自定义输入
            _append_question(elements, pending)
        else:
            elements.append({
                "tag": "markdown",
                "content": f"⏳ **等待批准**：{pending.get('tool', '?')}\n{pending.get('reason', '')}",
                "text_align": "left",
            })
            if pending.get("decision_id"):
                # 可远程批准
                elements.append({
                    "tag": "column_set",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 1,
                         "elements": [{"tag": "button", "text": {"tag": "plain_text", "content": "✅ 允许"},
                                       "type": "primary",
                                       "value": {"cc": "1", "decision_id": pending["decision_id"], "action": "allow"}}]},
                        {"tag": "column", "width": "weighted", "weight": 1,
                         "elements": [{"tag": "button", "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                                       "type": "danger",
                                       "value": {"cc": "1", "decision_id": pending["decision_id"], "action": "deny"}}]},
                    ],
                })
            else:
                # 仅通知（PermissionRequest 未生效的场景），无法远程批准
                elements.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "⏸ 去终端处理"},
                    "type": "default",
                    "value": {"cc": "1", "action": "ack", "session_id": session.session_id},
                })

    # ---- 卡片主标题：客户端来源 [VSCode]/[CLI]，副标题 = claude-code 标题 + 完整 session-id ----
    client_label = "VSCode" if (session.client_source or "").lower() == "vscode" else "CLI"
    cc_title = (session.claude_code_title or "").strip() or session.project_name or "Claude Code"
    header_title = f"🤖 Claude Code [{client_label}]"
    header_subtitle = f"{cc_title} (session - {session.session_id})"

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "subtitle": {"tag": "plain_text", "content": header_subtitle},
            "template": STATUS_TEMPLATE.get(status, "blue"),
        },
        "body": {"elements": elements},
    }


def build_session_list_card(infos: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """飞书菜单「同步历史」的会话列表卡（改进3 / 需求1、2、当前需求）。

    infos：manager 传最近有更新的 N 个 session 基本信息（按最后活动倒序）。
    每行一个 session，五列用 column_set 加权列对齐，尽量一行显示完整信息：
      状态(emoji) | 标题 | 用户提问条数 | 「刷新」按钮 | 「删除」按钮
    点击刷新经 card.action.trigger 回调（action="sync_refresh"）禁用旧卡并发送全新卡片；
    点击删除（action="sync_delete"）先做活跃守卫再发二次确认卡（删除不可逆）。
    列表卡受 sync_list_max 上限约束（超出只取最近 N 内最新）。
    """
    card_cfg = cfg.get("card", {})
    max_items = int(card_cfg.get("sync_list_max", 30))
    total = len(infos)
    shown = infos[:max_items] if total > max_items else infos
    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**📋 最近会话**（最近有更新的 {total} 个，按最后活动排序）",
            "text_align": "left",
        },
        {"tag": "hr"},
    ]
    if total > max_items:
        elements.append({
            "tag": "markdown",
            "content": f"💡 仅显示最近 {max_items} 个。",
            "text_size": "text_size_note",
            "text_align": "left",
        })
    for info in shown:
        title = (info.get("title") or "").strip() or info.get("project") or "未命名会话"
        sid = info.get("session_id", "")
        status = str(info.get("status") or "ended")
        ucount = max(0, int(info.get("user_count") or 0))
        emoji = STATUS_EMOJI.get(status, "❔")
        # 五列加权对齐：状态 | 标题 | 提问条数 | 刷新 | 删除（每行一行，权重一致形成表格感）
        elements.append({
            "tag": "column_set",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [{"tag": "markdown", "content": emoji, "text_align": "left"}]},
                {"tag": "column", "width": "weighted", "weight": 4,
                 "elements": [{"tag": "markdown", "content": f"**{title}**", "text_align": "left"}]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [{"tag": "markdown", "content": f"**{ucount} 问**", "text_align": "left"}]},
                {"tag": "column", "width": "weighted", "weight": 2,
                 "elements": [{"tag": "button", "type": "default",
                               "text": {"tag": "plain_text", "content": "🔄 刷新"},
                               "value": {"cc": "1", "action": "sync_refresh", "session_id": sid}}]},
                {"tag": "column", "width": "weighted", "weight": 2,
                 "elements": [{"tag": "button", "type": "danger",
                               "text": {"tag": "plain_text", "content": "🗑 删除"},
                               "value": {"cc": "1", "action": "sync_delete", "session_id": sid}}]},
            ],
        })
    elements.append({
        "tag": "markdown",
        "content": "🟢运行中 🟡空闲/待确认 🏁已结束 · 每行：状态｜标题｜提问条数｜刷新｜删除。"
                  "刷新：旧卡归档并重发含最近消息的新卡；删除：活跃会话拒绝，二次确认后移除 registry 与磁盘 transcript。",
        "text_size": "text_size_note",
        "text_align": "left",
    })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 最近会话"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def build_delete_confirm_card(session_id: str, title: str) -> Dict[str, Any]:
    """删除会话的二次确认卡：删除不可逆（registry + 磁盘 transcript），
    必须显式确认；按钮 value 一律字符串（cc:"1"），回调 action=sync_delete_confirm。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🗑 删除会话"},
            "template": "red",
        },
        "body": {"elements": [
            {
                "tag": "markdown",
                "content": f"确认删除会话 **{title}** ？\n将同时移除 registry 记录与磁盘 transcript，**不可恢复**。",
                "text_align": "left",
            },
            {"tag": "hr"},
            {
                "tag": "column_set",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "button", "type": "danger",
                                   "text": {"tag": "plain_text", "content": "✅ 确认删除"},
                                   "value": {"cc": "1", "action": "sync_delete_confirm",
                                             "session_id": session_id}}]},
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "button", "type": "default",
                                   "text": {"tag": "plain_text", "content": "取消"},
                                   "value": {"cc": "1", "action": "sync_delete_cancel",
                                             "session_id": session_id}}]},
                ],
            },
        ]},
    }


def build_deleted_card(session_id: str, title: str) -> Dict[str, Any]:
    """删除完成后替换原卡/确认卡的内容：提示已删除，停止更新。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🗑 已删除会话"},
            "template": "grey",
        },
        "body": {"elements": [
            {
                "tag": "markdown",
                "content": f"会话 **{title}**（`{session_id[:8]}…`）已删除：registry 记录与磁盘 transcript 均已移除。",
                "text_align": "left",
            },
        ]},
    }
