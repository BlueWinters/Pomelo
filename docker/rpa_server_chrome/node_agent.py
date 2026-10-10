#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chrome 算力节点守护 Agent (Chrome Node Agent)。

职责：
1. [Pre-flight] 循环探测本地 Chrome CDP 端口就绪 (127.0.0.1:9229/json/version)；
2. [Register] 指数退避重试向 API 发起自注册，加入算力池（强化错误原因与网络状态诊断）；
3. [Heartbeat] 周期性发送轻量心跳保活（支持 Redis 重启后自动重注册自愈）；
4. [Deregister] 监听 SIGTERM/SIGINT 信号，容器退出时自动从池中注销下线。
"""
import os
import sys
import json
import time
import signal
import logging
import threading
import urllib.request
import urllib.error
import socket
from typing import Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [node-agent] %(message)s"
)
logger = logging.getLogger("node-agent")


class ChromeNodeAgent:
    """Chrome 算力节点注册与保活 Agent。"""

    def __init__(
        self,
        node_id: Optional[str],
        register_url: str,
        host_ip: str,
        novnc_port: str,
        cdp_port: int = 9222,
        relay_port: int = 9222,
        chrome_local_port: int = 9229,
        heartbeat_interval: int = 7200,
    ):
        self.node_id = node_id.strip() if node_id else None
        self.is_auto_node_id = not bool(self.node_id)
        self.host_ip = host_ip.strip() if host_ip else ""
        self.novnc_port = str(novnc_port) if novnc_port else "6080"
        self.cdp_port = int(cdp_port) if cdp_port else 9222
        self.relay_port = int(relay_port) if relay_port else 9222
        self.chrome_local_port = int(chrome_local_port) if chrome_local_port else 9229
        self.cdp_endpoint = f"http://{self.host_ip}:{self.cdp_port}" if (self.node_id and self.host_ip) else ""
        self.heartbeat_interval = heartbeat_interval
        # 停止标志
        self._stop_event = threading.Event()
        self._is_registered = False

        swarm_fallback = "http://api:8000/dashboard/rpa/pool/nodes/register"
        raw_url = register_url.strip() if register_url else swarm_fallback
        self.register_url = raw_url.rstrip("/")
        self.heartbeat_url = self.register_url.replace("/register", "/heartbeat")
        self.deregister_url = self.register_url.replace("/register", "/deregister")
        self.fetch_node_id_url = self.register_url.replace("/register", "/fetch_node_id")

    def fetch_next_node_info(self, timeout: float = 5.0) -> Optional[dict]:
        """向 API 请求预分配下一个空闲可用的节点信息 (包含 node_id, novnc_port, cdp_port, host_ip 等)。"""
        try:
            url = f"{self.fetch_node_id_url}?host_ip={self.host_ip}" if self.host_ip else self.fetch_node_id_url
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    data["node_id"] = str(data.get("node_id")).strip()
                    data['novnc_port'] = str(data.get('novnc_port'))
                    data['cdp_port'] = str(data.get('cdp_port'))
                    data['host_ip'] = str(data.get('host_ip'))
                    return data
                else:
                    logger.warning(f"⚠️ 预申请接口返回非 200 状态码 [{resp.status}] (URL: {url})")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.warning(f"⚠️ 预申请 node_info HTTP 错误 [{e.code} {e.reason}] (URL: {self.fetch_node_id_url}): {err_body}")
        except urllib.error.URLError as e:
            logger.warning(f"⚠️ 预申请 node_info 网络连接失败 (URL: {self.fetch_node_id_url}): {e.reason}")
        except Exception as e:
            logger.warning(f"⚠️ 预申请 node_info 发生未预期异常 [{type(e).__name__}] (URL={self.fetch_node_id_url}): {e}")
        return None

    def check_local_chrome_ready(self, timeout: float = 2.0) -> bool:
        """[前置门禁] 探测本地 Chrome CDP 端口是否已正常输出 /json/version。"""
        url = f"http://127.0.0.1:{self.chrome_local_port}/json/version"
        try:
            req = urllib.request.Request(url, headers={"Host": "localhost"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    return "Browser" in data or "webSocketDebuggerUrl" in data
        except Exception as e:
            logger.debug(f"检查本地 Chrome 端口就绪状态 ({url}): {e}")
            return False
        return False

    def send_register_request(self, timeout: float = 12.0) -> Tuple[bool, bool, str]:
        """向 API 发送自注册请求。
        
        返回: (是否注册成功, 是否发生 409 节点冲突, 详细错误原因说明)
        """
        payload = {
            "node_id": str(self.node_id),
            "host_ip": str(self.host_ip),
            "cdp_port": str(self.cdp_port),
            "cdp_endpoint": str(self.cdp_endpoint),
            "novnc_port": str(self.novnc_port),
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.register_url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 201):
                    return True, False, "注册成功"
                else:
                    return False, False, f"HTTP状态码异常: {resp.status}"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore").strip()
            is_conflict = (e.code == 409)
            err_msg = f"HTTP {e.code} ({e.reason}): {err_body}"
            logger.error(f"❌ [API响应错误] 向 {self.register_url} 发送注册请求失败: {err_msg} (Payload: {payload})")
            return False, is_conflict, err_msg
        except urllib.error.URLError as e:
            reason = str(e.reason)
            err_msg = f"网络不可达 ({reason})"
            logger.error(f"❌ [网络连接失败] 无法连接到注册中心: {self.register_url} | 原因: {reason}")
            return False, False, err_msg
        except socket.timeout:
            err_msg = f"连接超时 (timeout={timeout}s)"
            logger.error(f"❌ [连接超时] 请求 {self.register_url} 超时 (>{timeout}s)")
            return False, False, err_msg
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.error(f"❌ [未预期异常] 注册请求异常: {err_msg}")
            return False, False, err_msg

    def send_heartbeat_request(self, timeout: float = 5.0) -> bool:
        """向 API 发送心跳包（携带完整注册参数供服务端比对）。"""
        payload = {
            "node_id": str(self.node_id),
            "host_ip": str(self.host_ip),
            "cdp_port": str(self.cdp_port),
            "cdp_endpoint": str(self.cdp_endpoint),
            "novnc_port": str(self.novnc_port),
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.heartbeat_url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            logger.error(f"❌️ 心跳发送失败 ({self.heartbeat_url}): {e}")
            return False

    def send_deregister_request(self, timeout: float = 3.0) -> bool:
        """向 API 发送注销请求。"""
        if not self.node_id:
            return False
        payload = {"node_id": self.node_id}
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.deregister_url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.warning(f"⚠️ 注销节点请求异常 ({self.deregister_url}): {e}")
            return False

    def run(self):
        """运行 Agent 主循环（前置探活 -> 预申请与注册自愈 -> 周期心跳）。"""
        logger.info("==============================================================================")
        logger.info(f"🚀 ChromeNodeAgent 启动中...")
        logger.info(f"  📌 节点唯一标识 (node_id) : {self.node_id or '(自动预申请)'}")
        logger.info(f"  🌐 宿主机公开 IP (host_ip) : {self.host_ip}")
        logger.info(f"  🖥️ 外部 VNC 端口 (novnc)   : {self.novnc_port}")
        logger.info(f"  🔗 注册目标 URL (register) : {self.register_url}")
        logger.info("==============================================================================")

        # 1. 前置健康检查：等待本地 Chrome 真正就绪
        while not self._stop_event.is_set():
            if self.check_local_chrome_ready():
                logger.info("✅ 本地 Chrome CDP 已完全就绪，准备注册至算力池...")
                break
            logger.info("⏳ 等待本地 Chrome 渲染引擎启动中 (127.0.0.1:9229/json/version 未就绪)...")
            self._stop_event.wait(1.5)

        # 2. 动态预申请与自注册循环（含 409 冲突重新预申请）
        backoff = 1.0
        while not self._stop_event.is_set():
            # 若未指定 node_id，实时动态预申请
            if not self.node_id:
                logger.info(f"🔍 正在向 API [{self.fetch_node_id_url}] 预申请空闲节点编号...")
                data = self.fetch_next_node_info()
                if data and data.get("node_id"):
                    self.node_id = data["node_id"]
                    self.novnc_port = data.get("novnc_port", self.novnc_port)
                    self.host_ip = data.get("host_ip", self.host_ip)
                    self.cdp_port = int(data.get("cdp_port", self.cdp_port))
                    effective_cdp_port = self.cdp_port if (self.host_ip and self.host_ip not in ("127.0.0.1", "localhost")) else self.relay_port
                    effective_host = self.host_ip if (self.host_ip and self.host_ip not in ("127.0.0.1", "localhost")) else self.node_id
                    self.cdp_endpoint = f"http://{effective_host}:{effective_cdp_port}"
                    logger.info(f"📋 动态预申请成功分配节点: {self.node_id} (novnc_port={self.novnc_port}, host_ip={self.host_ip}, cdp_endpoint={self.cdp_endpoint})")
                else:
                    logger.warning(f"⚠️ 预申请 node_id 失败 (无法连接 {self.fetch_node_id_url})，{backoff:.1f}s 后重试...")
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 1.5, 15.0)
                    continue

            # 发送注册请求
            ok, is_conflict, err_msg = self.send_register_request()
            if ok:
                self._is_registered = True
                logger.info(f"🎉 节点 {self.node_id} 成功注册至 RPA 算力池 (注册端点: {self.register_url})！")
                break

            if is_conflict and self.is_auto_node_id:
                logger.warning(f"⚠️ 节点 ID '{self.node_id}' 产生并发冲突 (已被占用: {err_msg})，重新申请下一个可用 ID...")
                self.node_id = None
                self._stop_event.wait(0.5)
                continue

            # 输出详尽的重试与诊断提示
            logger.warning(
                f"⚠️ [注册重试] 节点 '{self.node_id}' 注册失败: {err_msg} | "
                f"注册目标: {self.register_url} | 将在 {backoff:.1f}s 后重试..."
            )
            self._stop_event.wait(backoff)
            backoff = min(backoff * 1.5, 15.0)

        # 3. 周期性心跳保活（2小时一次，携带注册信息由服务端核对）
        while not self._stop_event.is_set():
            self._stop_event.wait(self.heartbeat_interval)
            if self._stop_event.is_set():
                break

            if self.check_local_chrome_ready():
                ok = self.send_heartbeat_request(timeout=5.0)
                if ok:
                    logger.debug(f"💓 节点 {self.node_id} 心跳同步正常")
                else:
                    logger.warning(f"⚠️ 节点 {self.node_id} 心跳发送失败，等待下一周期自愈重试")

    def stop(self):
        """停止 Agent 并向 API 执行优雅下线注销。"""
        if self._stop_event.is_set():
            return
        logger.info(f"🛑 收到下线信号，正在注销节点: {self.node_id}...")
        self._stop_event.set()
        if self._is_registered:
            self.send_deregister_request()
            logger.info(f"👋 节点 {self.node_id} 已成功注销并下线。")


def main():
    register_url = os.environ.get("RPA_REGISTER_URL")
    if not register_url:
        logger.info("未配置 RPA_REGISTER_URL 环境变量，跳过自动注册流程。")
        return

    node_id = os.environ.get("RPA_NODE_ID")
    host_ip = os.environ.get("RPA_EXTERNAL_HOST", "").strip()
    if not host_ip or host_ip in ("127.0.0.1", "localhost", "0.0.0.0"):
        logger.error(f"❌ [启动失败] 环境变量 RPA_EXTERNAL_HOST 必须显式指定为物理宿主机真实局域网 IP (当前为: '{host_ip}'，禁止为空或 127.0.0.1)！")
        sys.exit(1)

    novnc_port = os.environ.get("RPA_NOVNC_EXTERNAL_PORT", "6080")
    cdp_port = int(os.environ.get("RPA_CDP_EXTERNAL_PORT", os.environ.get("CDP_RELAY_PORT", 9222)))
    relay_port = int(os.environ.get("CDP_RELAY_PORT", 9222))
    chrome_local_port = int(os.environ.get("CHROME_CDP_PORT", 9229))
    heartbeat_interval = int(os.environ.get("RPA_HEARTBEAT_INTERVAL", 7200))

    agent = ChromeNodeAgent(
        node_id=node_id,
        register_url=register_url,
        host_ip=host_ip,
        novnc_port=novnc_port,
        cdp_port=cdp_port,
        relay_port=relay_port,
        chrome_local_port=chrome_local_port,
        heartbeat_interval=heartbeat_interval,
    )

    def _signal_handler(signum, frame):
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    agent.run()


if __name__ == "__main__":
    main()
