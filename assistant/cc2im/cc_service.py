# -*- coding: utf-8 -*-
"""统一常驻进程：FastAPI（仅监听 127.0.0.1:8790）+ 飞书 WebSocket。

- POST /hook            Claude Code hooks 上报事件（会话状态更新 / 权限请求）
- GET  /decision/{id}   cc_hooks 长轮询等待决策结果（按钮点击 / 超时自动允许）
- GET  /health          健康检查

lifespan 启动时并行挂载 Lark WebSocket 网关（独立后台线程），
卡片按钮回调经 run_coroutine_threadsafe 桥回主循环，零延迟解阻塞决策。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from loguru import logger

from .cc_config import load_config
from .cc_feishu import FeishuClient
from .cc_state import SessionManager
from .feishu_ws import FeishuWsGateway


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.cfg = cfg

    loop = asyncio.get_running_loop()

    feishu = FeishuClient(
        app_id=cfg["feishu"]["app_id"],
        app_secret=cfg["feishu"]["app_secret"],
        target_chat_id=cfg["feishu"]["target_chat_id"],
        target_open_id=cfg["feishu"]["target_open_id"],
        domain=cfg["feishu"].get("domain", "feishu.cn"),
    )
    app.state.feishu = feishu
    manager = SessionManager(cfg, feishu, loop)
    app.state.manager = manager

    # 卡片按钮 value → 决策解析（WS 回调与 /card_action 调试端点共用）
    async def handle_card_value(value: Dict[str, Any]):
        if not isinstance(value, dict):
            return
        action = value.get("action")
        if action == "ack":
            s = manager.sessions.get(value.get("session_id", ""))
            if s:
                s.last_result_note = "👀 已提醒，请到终端处理该确认"
                manager._mark_dirty(s)
            return
        if action == "sync_refresh":
            # 会话列表卡：「立即刷新」按钮 → 立刻重推该会话卡片
            await manager.refresh_card(str(value.get("session_id", "")))
            return
        if action == "sync_delete":
            # 会话列表卡：「删除」按钮 → 活跃守卫 + 发二次确认卡（删除不可逆）
            await manager.request_delete_session(str(value.get("session_id", "")))
            return
        if action == "sync_delete_confirm":
            # 确认卡：「确认删除」→ 真正删除（registry + 磁盘 transcript + 归档卡 + 刷新列表）
            await manager.delete_session(str(value.get("session_id", "")))
            return
        if action == "sync_delete_cancel":
            # 确认卡：「取消」→ 仅回一条文本，不动任何数据
            await manager.feishu.send_text("已取消删除，数据未动。")
            return
        did = value.get("decision_id")
        if not did or not action:
            return
        if action == "answer":
            # 选项按钮：value.answer 直接携带所选文本
            answer = value.get("answer")
            if answer:
                await manager.resolve_decision(did, "answer", answer=answer)
            return
        if action in ("answer_custom", "answer_multi"):
            # 表单提交：文本在 form_value，由 WS 层合并进 value["form_value"]
            fv = value.get("form_value") or {}
            text = ""
            for k, v in fv.items():
                if str(k).startswith("custom_"):
                    text = str(v or "").strip()
                    break
            if not text and action == "answer_multi":
                # 多选：form_value[sel_*] 是选中值数组，按引擎契约用 ", " 拼接
                for k, v in fv.items():
                    if str(k).startswith("sel_"):
                        vals = v if isinstance(v, list) else (v or [])
                        text = ", ".join(str(x) for x in vals)
                        break
            if text:
                await manager.resolve_decision(did, "answer", answer=text)
            return
        await manager.resolve_decision(did, action)

    app.state.handle_card_value = handle_card_value

    gw = FeishuWsGateway(
        cfg["feishu"]["app_id"],
        cfg["feishu"]["app_secret"],
        cfg["feishu"].get("domain", "feishu.cn"),
    )
    app.state.gw = gw
    gw.on_card_action = handle_card_value

    # 机器人菜单回调（改进3 / 需求1）：菜单「同步历史」合并回单个「Claude Code」，
    # 事件 ID 统一为 sync_history@claude_code（所有 session 不再区分 CLI/VSCode）。
    # 点击后只同步最近有更新的 N 个会话（sync_recent_max，默认 10）并回发列表卡。
    async def handle_menu(event_key: str):
        if event_key.startswith("sync_history"):
            try:
                n = await manager.sync_all_sessions()
                logger.info(f"菜单 {event_key}：已同步最近 {n} 个会话并回发列表")
            except Exception:
                logger.exception(f"{event_key} 处理失败")
    app.state.handle_menu = handle_menu
    gw.on_menu = handle_menu
    gw.start(loop)

    host, port = cfg.get("listen_host", "127.0.0.1"), int(cfg.get("listen_port", 8790))
    logger.info(f"cc-monitor 已启动: http://{host}:{port}")
    yield
    gw.stop()


app = FastAPI(lifespan=lifespan, title="cc-monitor")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.post("/hook")
async def hook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        res = await app.state.manager.on_event(body)
        return res
    except Exception as e:
        logger.exception("hook 处理异常")
        return {"ok": False, "msg": str(e)}


@app.get("/decision/{decision_id}")
async def decision(decision_id: str):
    action, answer = await app.state.manager.broker.wait(decision_id)
    allow = action in ("allow", "timeout", "answer")
    return {
        "decision": "allow" if allow else "deny",
        "action": action,
        "answer": answer,
    }


@app.post("/card_action")
async def card_action(request: Request):
    """(调试/兜底) 模拟卡片按钮回调：{decision_id, action} 或 {value: {...}}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    value = body.get("value") if isinstance(body.get("value"), dict) else body
    handler = getattr(app.state, "handle_card_value", None)
    if handler:
        await handler(value)
    return {"ok": True, "processed": bool(handler)}


@app.get("/health")
async def health():
    manager = getattr(app.state, "manager", None)
    return {
        "ok": True,
        "sessions": len(manager.sessions) if manager else 0,
        "ws": getattr(app.state.gw, "_running", False),
    }


@app.get("/debug/dns")
async def debug_dns():
    """调试：服务进程内解析 open.feishu.cn 的 IP。"""
    import socket
    out = {}
    for host in ("open.feishu.cn", "open.larksuite.com"):
        try:
            addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
            out[host] = addrs[:20]
        except Exception as e:
            out[host] = f"ERR {e}"
    return out


@app.get("/debug/token")
async def debug_token():
    """调试：用服务实际的客户端走一遍真实 token 获取，报告结果。"""
    feishu = getattr(app.state, "feishu", None)
    if feishu is None:
        return {"ok": False, "msg": "feishu client 未就绪"}
    try:
        token = await feishu._tenant_token()
        return {"ok": True, "token_prefix": token[:12], "base": feishu.base}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/debug/menu")
async def debug_menu(event_key: str = "sync_history@claude_code"):
    """回环测试：模拟飞书菜单点击（等价于 application.bot_menu.v6 回调）。"""
    handler = getattr(app.state, "handle_menu", None)
    if handler:
        await handler(event_key)
        return {"ok": True, "event_key": event_key}
    return {"ok": False, "msg": "handle_menu 未注册"}


@app.post("/debug/command")
async def debug_command(request: Request):
    """回环测试：模拟卡片底部指令表单提交（action=command）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    manager = getattr(app.state, "manager", None)
    ok = await manager.send_command_to_session(
        str(body.get("session_id", "")), str(body.get("text", "")))
    return {"ok": ok}


@app.get("/debug/sessions")
async def debug_sessions(env: str = ""):
    """回环测试：查看所有会话的汇总信息（同步历史数据源）。env 可选 vscode/cli 过滤。"""
    manager = getattr(app.state, "manager", None)
    env_opt = env or None
    return {
        "ok": True,
        "live": len(manager.sessions),
        "registry": len(manager.registry),
        "env": env_opt,
        "sessions": manager._all_session_infos(env_opt),
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        app,
        host=cfg.get("listen_host", "127.0.0.1"),
        port=int(cfg.get("listen_port", 8790)),
        log_level="info",
    )
