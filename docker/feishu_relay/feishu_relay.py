"""飞书事件中继（Relay）：连接飞书 WebSocket，将事件纯转发到内部 API 端点。

纯透传：不注入 app_id / app_secret，全部配置走环境变量（Docker 友好）。
内置管理页面（浏览器访问 :8080），可动态修改转发目标、手动探测目标健康。

环境变量：
  FEISHU_APP_ID              飞书应用 ID（必填）
  FEISHU_APP_SECRET          飞书应用密钥（必填）
  FEISHU_RELAY_TARGET_URL    转发目标 URL（默认 http://127.0.0.1:8000/api/im/feishu/websocket）
  FEISHU_RELAY_HEALTH_PORT   健康检查 / 管理页面端口（默认 8080）
  LOGURU_LEVEL               日志级别（默认 INFO）
"""

from __future__ import annotations

import asyncio
import json
import os
import pprint
import signal
import sys
import threading
import time
import httpx
import lark_oapi as lark
from loguru import logger
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


# 从环境变量读取的静态配置（兼容 FEISHU_APP_ID 与 IM_FEISHU_APP_ID 命名，并过滤异常字符）。
app_id: str = (os.getenv("FEISHU_APP_ID") or os.getenv("IM_FEISHU_APP_ID") or "").strip()
app_secret: str = (os.getenv("FEISHU_APP_SECRET") or os.getenv("IM_FEISHU_APP_SECRET") or "").strip()
health_port: int = int(os.getenv("FEISHU_RELAY_HEALTH_PORT", "8070"))  # 8080被dozzle占用了

# 转发目标 URL。运行期间可通过管理 API 动态修改。
target_url: str = os.getenv(
    "FEISHU_RELAY_TARGET_URL",
    "http://127.0.0.1:8000/api/im/feishu/websocket",
)

# 转发目标 URL 的锁。
target_lock: threading.Lock = threading.Lock()

# 进程运行状态。
websocket_connected: bool = False
shutdown_event: threading.Event = threading.Event()
main_loop: asyncio.AbstractEventLoop | None = None
start_time: float = 0.0

# 静态页面目录，位于本文件所在目录的 static/ 子目录。
static_dir: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 转发请求的默认超时。
http_timeout = httpx.Timeout(30.0, connect=10.0)


def get_target_url() -> str:
    """返回当前转发目标 URL。"""
    with target_lock:
        return target_url


def set_target_url(url: str) -> None:
    """更新转发目标 URL。"""
    global target_url
    with target_lock:
        target_url = url


def derive_health_url() -> str:
    """从当前转发目标推导出 API 的健康检查地址。

    例如 http://api:8000/api/im/feishu/websocket 对应
    http://api:8000/api/health。
    """
    parsed = urlparse(get_target_url())
    return f"{parsed.scheme}://{parsed.netloc}/api/health"


class AdminHandler(BaseHTTPRequestHandler):
    """管理 HTTP 请求处理器。

    提供静态页面、健康检查和转发目标管理 API：

      GET  /              返回管理页面（static/index.html）
      GET  /health        健康检查（200/503）
      GET  /admin/status  完整状态（WS 连接、目标 URL、运行时长）
      GET  /admin/target  返回当前转发目标
      POST /admin/target  更新转发目标
      POST /admin/test    向当前目标发送 mock 飞书事件探测连通性
    """

    def do_GET(self) -> None:
        """分发 GET 请求。"""
        routes = {
            "/": self.serve_index,
            "/index.html": self.serve_index,
            "/health": self.handle_health,
            "/admin/status": self.handle_status,
            "/admin/target": lambda: self.send_json(
                200, {"url": get_target_url()}
            ),
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """分发 POST 请求。"""
        routes = {
            "/admin/target": self.handle_update_target,
            "/admin/test": self.handle_test_target,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_json(404, {"error": "not found"})

    def serve_index(self) -> None:
        """返回管理页面 HTML。"""
        filepath = os.path.join(static_dir, "index.html")
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except OSError:
            self.send_json(500, {"error": "static file missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_health(self) -> None:
        """健康检查：WS 已连接且未关闭时返回 200，否则返回 503。"""
        ok = websocket_connected and not shutdown_event.is_set()
        self.send_json(
            200 if ok else 503, {"status": "ok" if ok else "error"}
        )

    def handle_status(self) -> None:
        """返回完整运行状态。"""
        uptime = int(time.monotonic() - start_time) if start_time else 0
        self.send_json(200, {
            "ws_connected": websocket_connected and not shutdown_event.is_set(),
            "target": get_target_url(),
            "uptime": uptime,
        })

    def handle_update_target(self) -> None:
        """读取 JSON body，校验并更新转发目标 URL。"""
        try:
            body = self.read_json()
            url = (body.get("url") or "").strip()
            error = self.validate_url(url)
            if error:
                self.send_json(400, {"error": error})
                return
            set_target_url(url)
            logger.info(f"转发目标已更新，new_target={url}")
            self.send_json(200, {"ok": True, "url": url})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.error(f"更新转发目标失败，exc={exc}")
            self.send_json(500, {"error": "内部错误"})

    def handle_test_target(self) -> None:
        """向当前转发目标 POST 一条 mock 飞书事件，手动探测连通性。"""
        logger.info(f"手动探测转发目标，target={get_target_url()}")
        payload = build_mock_event()
        pprint.pprint(payload)

        result = {
            "ok": False,
            "target": get_target_url(),
            "status": None,
            "detail": "",
        }
        try:
            timeout = httpx.Timeout(10.0, connect=5.0)
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    get_target_url(),
                    content=json.dumps(payload, ensure_ascii=False),
                    headers={"Content-Type": "application/json"},
                )
            result["status"] = resp.status_code
            if resp.status_code < 300:
                result["ok"] = True
                result["detail"] = f"目标可达，HTTP {resp.status_code}"
                logger.info(
                    f"探测通过，target={result['target']} "
                    f"status={resp.status_code}"
                )
            elif resp.status_code == 503:
                result["detail"] = "目标可达，但 Feishu 适配器未配置（HTTP 503）"
                logger.warning("探测：目标可达但 Feishu 适配器未配置，status=503")
            else:
                result["detail"] = (
                    f"目标返回 HTTP {resp.status_code}："
                    f"{(resp.text or '')[:120]}"
                )
                logger.warning(f"探测：目标返回异常状态码，status={resp.status_code}")
        except httpx.ConnectError:
            result["detail"] = "无法连接目标主机"
            logger.warning(f"探测失败：无法连接，target={result['target']}")
        except httpx.TimeoutException:
            result["detail"] = "连接目标超时"
            logger.warning(f"探测失败：超时，target={result['target']}")
        except Exception as exc:
            result["detail"] = f"探测异常：{exc}"
            logger.warning(f"探测失败：异常，exc={exc}")

        self.send_json(200, result)

    @staticmethod
    def validate_url(url: str) -> str | None:
        """校验转发目标 URL。

        返回错误描述；URL 合法时返回 None。
        """
        if not url:
            return "url 不能为空"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "url 必须以 http:// 或 https:// 开头"
        if not parsed.netloc:
            return "url 格式无效，缺少主机地址"
        return None

    def read_json(self) -> dict:
        """读取请求体并解析为 dict。"""
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            raise ValueError("请求体为空")
        if length > 2048:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("请求体不是有效的 JSON")

    def send_json(self, status: int, data: dict) -> None:
        """以 JSON 形式返回响应。"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        """屏蔽 HTTP 服务器自带的访问日志，避免污染主日志。"""
        pass


def start_admin_server() -> HTTPServer:
    """在独立 daemon 线程中启动管理 HTTP Server。"""
    server = HTTPServer(("0.0.0.0", health_port), AdminHandler)
    threading.Thread(
        target=server.serve_forever, daemon=True, name="admin"
    ).start()
    return server


def run_on_main(coro) -> None:
    """把协程安全地调度到主事件循环执行。

    lark_oapi 的 ws.Client 在独立线程触发事件回调，不能直接在该线程运行
    asyncio 协程，须经 run_coroutine_threadsafe 桥接到主循环。
    """
    if main_loop is not None and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, main_loop)
    else:
        logger.warning("主事件循环不可用，事件转发被跳过")


async def forward_event(
    http_client: httpx.AsyncClient, raw_json_str: str
) -> None:
    """向当前转发目标 POST 一条事件。

    飞书 WS 事件不支持 ack/nack，失败不重试；传输失败时额外通知用户离线。
    """
    if shutdown_event.is_set():
        return

    target = get_target_url()
    try:
        resp = await http_client.post(
            target,
            content=raw_json_str,
            headers={"Content-Type": "application/json"},
        )
    except httpx.TimeoutException:
        logger.warning(f"转发失败：连接超时，target={target}")
        notify_offline(raw_json_str)
        return
    except httpx.ConnectError:
        logger.warning(f"转发失败：无法连接，target={target}")
        notify_offline(raw_json_str)
        return
    except Exception as exc:
        logger.warning(f"转发失败：异常，target={target} exc={exc}")
        notify_offline(raw_json_str)
        return

    if resp.status_code >= 400:
        logger.warning(
            f"转发失败：目标返回 HTTP {resp.status_code} "
            f"body={(resp.text or '')[:200]}，target={target}"
        )
    else:
        logger.info(f"转发成功，target={target} status={resp.status_code}")


def notify_offline(raw_json_str: str) -> None:
    """转发失败时通知用户机器人已下线。

    best-effort 通知：任何步骤失败均静默忽略。
    """
    try:
        data = json.loads(raw_json_str)
        open_id = (
            data.get("event", {})
            .get("operator", {})
            .get("operator_id", {})
            .get("open_id", "")
        )
        if not open_id:
            return

        with httpx.Client(timeout=5.0) as client:
            token = client.post(
                "https://open.feishu.cn/open-apis/auth/v3/"
                "tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            ).json().get("tenant_access_token")
            if not token:
                return
            client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages"
                "?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": json.dumps(
                        {"text": "当前飞书机器人已经下线或正在重启"}
                    ),
                },
            )
    except Exception as e:
        logger.warning(f"通知用户机器人下线失败：exception={e}")


def marshal_payload(data) -> str:
    """把 lark_oapi 事件序列化为 JSON 字符串，并打印完整内容。"""
    raw = lark.JSON.marshal(data)
    text = raw if isinstance(raw, str) else str(raw)
    logger.info("收到飞书消息，内容如下：")
    pprint.pprint(json.loads(text))
    return text


def on_event(http_client: httpx.AsyncClient, data) -> None:
    """通用事件回调：打印消息并转发到目标 API。"""
    if shutdown_event.is_set():
        return
    raw = marshal_payload(data)
    run_on_main(forward_event(http_client, raw))


def on_card_action(http_client: httpx.AsyncClient, data):
    """卡片动作回调：转发，并返回空响应防止飞书重置卡片。

    lark_oapi 收到 None 返回值时会回传空响应给飞书，导致卡片 UI 被重置；
    必须返回 ``P2CardActionTriggerResponse({})`` 告知飞书卡片内容不变。
    """
    if shutdown_event.is_set():
        return None
    raw = marshal_payload(data)
    run_on_main(forward_event(http_client, raw))

    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    assert isinstance(data, P2CardActionTrigger)
    return P2CardActionTriggerResponse({})


def build_event_handler(http_client: httpx.AsyncClient):
    """构造 lark_oapi 事件分发器，注册需要转发的事件类型。"""
    builder = lark.EventDispatcherHandler.builder("", "", lark.LogLevel.INFO)

    # 用户消息、机器人加群、进入单聊都走 on_event。
    builder = builder.register_p2_im_message_receive_v1(
        lambda d: on_event(http_client, d)
    )
    builder = builder.register_p2_im_chat_member_bot_added_v1(
        lambda d: on_event(http_client, d)
    )
    builder = builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
        lambda d: on_event(http_client, d)
    )

    # 卡片按钮回调（返回空响应防止飞书重置卡片）。
    if hasattr(builder, "register_p2_card_action_trigger"):
        builder = builder.register_p2_card_action_trigger(
            lambda d: on_card_action(http_client, d)
        )
    else:
        builder = builder.register_p2_customized_event(
            "card.action.trigger",
            lambda d: on_card_action(http_client, d),
        )

    # 自定义菜单事件（旧版 SDK 无专属方法时用 customized 兜底）。
    if hasattr(builder, "register_p2_application_bot_menu_v6"):
        builder = builder.register_p2_application_bot_menu_v6(
            lambda d: on_event(http_client, d)
        )
    else:
        builder = builder.register_p2_customized_event(
            "application.bot.menu_v6",
            lambda d: on_event(http_client, d),
        )

    return builder.build()


async def wait_for_target(
    http_client: httpx.AsyncClient, max_wait: int = 120
) -> bool:
    """轮询等待目标 API 就绪，超时返回 False。"""
    health_url = derive_health_url()
    logger.info(f"等待目标 API 就绪，url={health_url} max_wait={max_wait}s")

    deadline = time.monotonic() + max_wait
    while not shutdown_event.is_set():
        try:
            resp = await http_client.get(health_url, timeout=httpx.Timeout(3.0))
            if resp.status_code == 200:
                logger.info("目标 API 已就绪")
                return True
        except Exception as e:
            logger.warning(f"等待目标 API 尝试，url={health_url}，error={e}")

        if time.monotonic() >= deadline:
            logger.error(f"等待目标 API 超时，url={health_url}")
            return False
        await asyncio.sleep(1)

    return False


def build_mock_event() -> dict:
    """构造一条 mock 飞书事件，用于手动探测转发目标。

    使用未知 event_type：目标 API 会解析为 event，但 build_im_message
    返回 None，最终仅返回 ``{"code": 0}``，不会触发任何真实业务处理。
    """
    now_ms = int(time.time() * 1000)
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"relay_health_{now_ms}",
            "event_type": "relay.health_check",
            "create_time": str(now_ms),
            "token": "",
            "app_id": app_id,
            "tenant_key": "",
        },
        "event": {},
    }


def setup_logging() -> None:
    """配置 loguru 日志输出到 stderr。"""
    logger.remove()
    logger.add(
        sys.stdout,
        level=os.getenv("LOGURU_LEVEL", "INFO"),
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        enqueue=False,
    )


def main() -> None:
    """飞书 Relay 主入口。"""
    global start_time
    start_time = time.monotonic()
    setup_logging()

    if not app_id or not app_secret:
        logger.error("FEISHU_APP_ID 或 FEISHU_APP_SECRET 未设置，无法启动")
        sys.exit(1)

    logger.info(
        f"飞书 Relay 启动，app_id={app_id}，target={get_target_url()} "
        f"health_port={health_port}"
    )

    admin_server = start_admin_server()
    logger.info(f"管理服务已启动，http://<host>:{health_port}")

    async def run() -> None:
        global main_loop, websocket_connected
        main_loop = asyncio.get_running_loop()

        async with httpx.AsyncClient(timeout=http_timeout) as http_client:
            if not await wait_for_target(http_client):
                logger.error("目标 API 不可达，Relay 退出")
                return

            ws_client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=build_event_handler(http_client),
                log_level=lark.LogLevel.INFO,
            )

            websocket_connected = True
            logger.info("飞书 Relay 已就绪，正在监听事件...")
            try:
                # ws_client.start() 内部阻塞，放到线程池中执行。
                await asyncio.get_running_loop().run_in_executor(
                    None, ws_client.start
                )
            except Exception as e:
                logger.exception(f"飞书 WS 客户端异常退出，exception={e}")
            finally:
                websocket_connected = False
                logger.info("飞书 WS 客户端已断开")

    def handle_signal(signum, frame) -> None:
        logger.info(f"收到信号 {signum}，正在关闭...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        admin_server.shutdown()
        logger.info("飞书 Relay 已退出")


if __name__ == "__main__":
    main()
