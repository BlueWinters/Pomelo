# -*- coding: utf-8 -*-
"""飞书 WebSocket 网关。

lark_oapi 的 ws.Client.start() 是阻塞的，且自带独立 asyncio 事件循环，
因此必须在独立后台线程里运行。卡片按钮回调在 WS 线程触发，
通过 run_coroutine_threadsafe 桥回主事件循环（FastAPI loop）解决决策。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, Optional

from loguru import logger

# lark_oapi ws.Client 的 domain 参数是"完整 base URL"（带 scheme），不是裸域名
_DOMAIN_URLS = {
    "feishu.cn": "https://open.feishu.cn",
    "larksuite.com": "https://open.larksuite.com",
}


def _ensure_ws_card_patch() -> bool:
    """自愈 fork 版 lark_oapi 的卡片回调 bug。

    本环境安装的 lark_oapi 是 fork：ws.Client._handle_message 对
    MessageType.CARD 直接 return——既不派发卡片回调 handler，也不回写响应帧，
    导致飞书按钮回调收不到响应 → 点击报 200671。把 CARD 分支改为走
    _do_without_validation（与官方 SDK 一致），幂等，每次启动自愈。
    """
    try:
        import lark_oapi.ws.client as mod  # noqa: PLC0415
        p = mod.__file__
        src = open(p, encoding="utf-8").read()
        old = "elif message_type == MessageType.CARD:\n                return"
        new = ("elif message_type == MessageType.CARD:\n"
               "                result = self._event_handler._do_without_validation(pl)")
        if new in src:
            return True
        if old in src:
            open(p, "w", encoding="utf-8").write(src.replace(old, new))
            logger.info("已为 fork 版 lark_oapi 补丁 ws.Client 的 CARD 分支（卡片回调可用）")
            return True
        logger.warning("lark_oapi ws/client.py 未见预期 CARD 分支，跳过补丁")
        return False
    except Exception:
        logger.exception("补丁 lark_oapi CARD 分支失败")
        return False


class FeishuWsGateway:
    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self.client = None
        self._lark = None
        self._resp_cls = None  # P2CardActionTriggerResponse，在 WS 线程内导入
        self._thread: Optional[threading.Thread] = None
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        # 卡片按钮回调：async (value: dict) -> None，在主循环执行
        self.on_card_action: Optional[Callable[[Dict[str, Any]], Any]] = None
        # 机器人菜单回调：async (event_key: str) -> None，在主循环执行（如 sync_history）
        self.on_menu: Optional[Callable[[str], Any]] = None
        self._running = False

    def start(self, main_loop: asyncio.AbstractEventLoop) -> bool:
        if self._running:
            return True
        self.main_loop = main_loop
        self._running = True
        self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        # 必须在 WS 线程内 import lark_oapi：lark_oapi 的 ws.Client 使用模块级
        # loop = asyncio.get_event_loop()，在主线程 import 会把该 loop 绑到 FastAPI 主循环，
        # 导致 run_until_complete 报 "This event loop is already running"。
        try:
            import lark_oapi as lark  # noqa: PLC0415
        except ImportError:
            self._running = False
            logger.warning("未安装 lark-oapi，飞书 WebSocket 网关不可用（pip install lark-oapi）")
            return
        self._lark = lark
        # 卡片回调响应类（旧路径 lark_oapi.ws.model.event 在 fork 版不存在）
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse  # noqa: PLC0415
            self._resp_cls = P2CardActionTriggerResponse
        except Exception:
            self._resp_cls = None
            logger.warning("无法导入 P2CardActionTriggerResponse，卡片回调可能不可用")
        _ensure_ws_card_patch()
        try:
            # 事件注册在 EventDispatcherHandler.builder 上，ws.Client 接收 event_handler
            builder = lark.EventDispatcherHandler.builder("", "", lark.LogLevel.INFO)
            builder.register_p2_im_message_receive_v1(self._on_message)
            builder.register_p2_card_action_trigger(self._on_card_action)
            builder.register_p2_im_chat_member_bot_added_v1(self._on_bot_added)
            builder.register_p2_application_bot_menu_v6(self._on_menu)
            event_handler = builder.build()

            kwargs: Dict[str, Any] = dict(
                app_id=self.app_id,
                app_secret=self.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
            if self.domain:
                kwargs["domain"] = _DOMAIN_URLS.get(self.domain, self.domain)
            client = lark.ws.Client(**kwargs)
            self.client = client
            logger.info("飞书 WebSocket 网关已启动")
            client.start()
        except Exception:
            logger.exception("飞书 WebSocket 网关异常退出")
        finally:
            self._running = False

    def stop(self):
        self._running = False
        try:
            if self.client is not None:
                self.client.stop()
        except Exception:
            pass

    # ---------------- 回调（在 WS 线程执行）----------------
    # 注意：lark 以单参数 f(data) 调用 handler，签名必须是 (self, data)，
    # 写成 (ctx, data) 会在每次回调抛 TypeError → 飞书收到 500 → 200671。
    def _on_message(self, data):
        try:
            logger.info(f"[ws] 收到消息: {getattr(data, 'message_id', '')}")
        except Exception:
            pass
        return None

    def _on_bot_added(self, data):
        logger.info("[ws] 机器人被拉入群")
        return None

    def _on_menu(self, data):
        """机器人菜单回调（application.bot_menu.v6）：取 event_key 分发到主循环。

        菜单「同步历史」的事件 ID 是 sync_history；事件模型为
        P2ApplicationBotMenuV6.event.event_key（Lark 通过 init() 动态赋值，
        因此用 getattr 逐层取，避免强依赖具体类）。"""
        try:
            d = getattr(data, "event", None) or getattr(data, "data", None) or data
            event_key = str(getattr(d, "event_key", "") or "")
            logger.info(f"[ws] 收到机器人菜单回调: {event_key}")
            if event_key and self.on_menu is not None and self.main_loop is not None:
                # 与卡片回调一致：把 coroutine 调度到 FastAPI 主事件循环
                asyncio.run_coroutine_threadsafe(self.on_menu(event_key), self.main_loop)
        except Exception:
            logger.exception("处理菜单回调异常")
        return None

    def _on_card_action(self, data):
        # 必须返回 P2CardActionTriggerResponse({})，否则飞书会当作回调失败 → 200671。
        try:
            # 卡片回调模型：P2CardActionTrigger.event.action.value
            d = getattr(data, "event", None) or getattr(data, "data", None)
            action = getattr(d, "action", None)
            value = dict(getattr(action, "value", None) or {})
            # 按钮 value 里的 cc 标记（兼容 int/str 两种形态）
            if isinstance(value, dict) and str(value.get("cc", "")) == "1":
                # 表单提交：输入文本在 form_value（键=组件 name）；独立输入在 input_value。
                # 合并进 value，交给 handle_card_value 解析。
                fv = getattr(action, "form_value", None)
                if isinstance(fv, dict):
                    value["form_value"] = fv
                iv = getattr(action, "input_value", None)
                if iv is not None:
                    value["input_value"] = iv
                cb = self.on_card_action
                if cb is not None and self.main_loop is not None:
                    # 把 coroutine 调度到主事件循环
                    asyncio.run_coroutine_threadsafe(cb(value), self.main_loop)
        except Exception:
            logger.exception("处理卡片回调异常")

        if self._resp_cls is not None:
            try:
                return self._resp_cls({})
            except Exception:
                pass
        return None
