# -*- coding: utf-8 -*-
"""飞书 API 客户端：tenant_access_token 缓存、发文本/卡片、PATCH 原地更新卡片。

传输层使用 asyncio 原生流（asyncio.open_connection）而非 httpx/httpcore。
原因：本机 Clash TUN 网络环境下 httpx/httpcore 的 TLS 连接被拦截，稳定返回
Go 的 "404 page not found"（curl.exe 与 asyncio 原生流均正常），故放弃 httpx。

所有 API 路径均需带 /open-apis 前缀（飞书开放平台要求）。
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Dict, Optional

from loguru import logger

BASE_DOMAINS = {
    "feishu.cn": ("open.feishu.cn", 443),
    "larksuite.com": ("open.larksuite.com", 443),
}


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, target_chat_id: str = "",
                 target_open_id: str = "", domain: str = "feishu.cn",
                 timeout: float = 10.0):
        self.app_id = app_id
        self.app_secret = app_secret
        self.target_chat_id = target_chat_id
        self.target_open_id = target_open_id
        host, port = BASE_DOMAINS.get(domain, BASE_DOMAINS["feishu.cn"])
        self._host = host
        self._port = port
        self.base = f"https://{host}"
        self.timeout = timeout
        self._token: Optional[str] = None
        self._expire_at: float = 0.0

    # ------------------------------------------------------------------
    # 底层 HTTP（asyncio 原生流，单请求/响应，无需连接池）
    # ------------------------------------------------------------------
    async def _http(self, method: str, path: str, json_body: Any = None,
                    headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发一次请求，返回 {status, json, text}。失败时返回 {"code":-1,"msg":...}。"""
        body = b""
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self._host}",
            "Accept: application/json",
            "Content-Type: application/json",
            "Connection: close",
        ]
        if headers:
            for k, v in headers.items():
                lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(body)}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

        async def _do():
            reader, writer = await asyncio.open_connection(
                self._host, self._port, ssl=_ssl_context(), server_hostname=self._host,
            )
            try:
                writer.write(request)
                await writer.drain()
                data = bytearray()
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            raw = bytes(data)
            head, _sep, resp = raw.partition(b"\r\n\r\n")
            status = 0
            try:
                status = int(head.split(b" ", 2)[1])
            except Exception:
                pass
            return status, resp

        try:
            status, resp = await asyncio.wait_for(_do(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return {"code": -1, "msg": f"请求超时({self.timeout}s)"}
        except Exception as e:
            return {"code": -1, "msg": f"网络异常: {type(e).__name__}: {e}"}

        text = resp.decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {}
        return {"status": status, "json": parsed, "text": text}

    # ------------------------------------------------------------------
    # token
    # ------------------------------------------------------------------
    async def _tenant_token(self) -> str:
        if self._token and time.time() < self._expire_at - 60:
            return self._token
        path = "/open-apis/auth/v3/tenant_access_token/internal"
        last_err = "未知错误"
        for attempt in range(3):
            d = await self._http(
                "POST", path,
                json_body={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            j = d.get("json", {})
            if d.get("status") == 200 and j.get("code") == 0 and j.get("tenant_access_token"):
                self._token = j["tenant_access_token"]
                self._expire_at = time.time() + j.get("expire", 7200)
                return self._token
            last_err = d.get("msg") or f"HTTP {d.get('status')} {d.get('text', '')[:120]}"
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
        logger.error(f"获取 tenant_access_token 失败: {last_err}")
        raise RuntimeError(f"获取 tenant_access_token 失败: {last_err}")

    async def _request(self, method: str, path: str, *, json_body: Any = None,
                       retries: int = 2) -> Dict[str, Any]:
        """发起带鉴权的 API 请求，网络闪断/非 0 code 时重试 retries 次。"""
        token = await self._tenant_token()
        headers = {"Authorization": f"Bearer {token}"}
        last: Dict[str, Any] = {"code": -1, "msg": "unknown"}
        for attempt in range(retries + 1):
            d = await self._http(method, path, json_body=json_body, headers=headers)
            j = d.get("json", {})
            if d.get("status") == 200 and j.get("code") in (0, None):
                return j
            last = {"code": j.get("code", -1), "msg": j.get("msg") or d.get("msg")
                    or f"HTTP {d.get('status')} {d.get('text', '')[:200]}"}
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
        return last

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def send_message(self, receive_id: str, msg_type: str, content: Any,
                           receive_id_type: str = "chat_id") -> Optional[str]:
        d = await self._request(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            json_body={"receive_id": receive_id, "msg_type": msg_type,
                       "content": json.dumps(content, ensure_ascii=False)},
        )
        if d.get("code") != 0:
            logger.error(f"发送消息失败({msg_type}): {d.get('msg')} log={d.get('log_id')}")
            return None
        mid = (d.get("data") or {}).get("message_id")
        if not mid:
            logger.error(f"发送成功但未返回 message_id: {d}")
        return mid

    async def send_text(self, text: str, chat_id: str | None = None) -> Optional[str]:
        cid = chat_id or self.target_chat_id
        return await self.send_message(cid, "text", {"text": text})

    async def send_card(self, card: Dict[str, Any], chat_id: str | None = None) -> Optional[str]:
        cid = chat_id or self.target_chat_id
        return await self.send_message(cid, "interactive", card)

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    async def update_card(self, message_id: str, card: Dict[str, Any]) -> bool:
        d = await self._request(
            "PATCH", f"/open-apis/im/v1/messages/{message_id}",
            json_body={"content": json.dumps(card, ensure_ascii=False)},
        )
        if d.get("code") != 0:
            logger.error(f"更新卡片失败({message_id}): {d.get('msg')} log={d.get('log_id')}")
            return False
        return True
