#!/usr/bin/env python3
"""CDP 转发层（运行在 rpa-chrome-server 容器内）：把 Chrome 的 CDP 端口暴露到私有网络。

Chrome 新版强制把 CDP 绑定在容器回环 127.0.0.1（--remote-debugging-address 被忽略），
CDP 端口天然不出容器。compose 下 worker 经 network_mode:service:rpa-N 共享网络栈，
连 127.0.0.1 即可直达 Chrome；但 docker-stack(swarm) 的 service 不支持共享网络栈，
worker 与 rpa 是 overlay 网络上两个独立容器，只能连 rpa 容器的 overlay 网卡地址，
而 Chrome 不肯在 overlay 网卡上监听。

因此这里加一个纯 TCP 双向转发：监听 listen_host:listen_port（默认 0.0.0.0:9222），
把每个连接透传到容器内 Chrome 回环 target_host:target_port（默认 127.0.0.1:9229）。
CDP 的 HTTP 握手与 WebSocket 长连接都是 TCP 之上的协议，纯透传即可，无需解析协议。

与 rpa_server.py 的分工（二者职责分离，各占一个端口）：
- 本脚本：面向 worker 的低层网络代理（无鉴权、纯字节流透传），端口 9222。
- rpa_server.py：面向 dashboard 的运维 HTTP API（会话文件浏览 + 快照，带 token），
  端口 9224。
两者都是 rpa 容器内独立进程，暴露范围 = 所属 swarm/compose 私有网络，不出宿主、
不出公网。Chrome 回环端口因此用 9229（避让本脚本占用的 9222）。
"""
import argparse
import socket
import threading
import re


def _pipe(src: socket.socket, dst: socket.socket, is_client: bool = False) -> None:
    """单向搬运：把 src 的字节流写入 dst（支持 Host 头适配与半关闭），直至 EOF 或出错。"""
    first_chunk = is_client
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            if first_chunk:
                # 兼容性重写：将外部请求的 Host 头重写为 Chrome 认可的 Host: localhost:9229
                data = re.sub(rb'(?i)Host:\s*[^\r\n]+', b'Host: localhost:9229', data, count=1)
                first_chunk = False
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_conn(client: socket.socket, target: tuple) -> None:
    """为单个入站连接建立到 Chrome 的上游连接并双向透传。"""
    try:
        upstream = socket.create_connection(target, timeout=5)
    except OSError:
        client.close()
        return
    a = threading.Thread(target=_pipe, args=(client, upstream, True), daemon=True)
    b = threading.Thread(target=_pipe, args=(upstream, client, False), daemon=True)
    a.start()
    b.start()
    a.join()
    b.join()
    for s in (client, upstream):
        try:
            s.close()
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="CDP 转发层（默认 0.0.0.0:9222 -> 127.0.0.1:9229）")
    ap.add_argument("--listen-host", default="0.0.0.0", help="对外监听地址")
    ap.add_argument("--listen-port", type=int, default=9222, help="对外监听端口（worker 经网络连这里）")
    ap.add_argument("--target-host", default="127.0.0.1", help="Chrome CDP 回环地址")
    ap.add_argument("--target-port", type=int, default=9229, help="Chrome CDP 回环端口")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen_host, args.listen_port))
    srv.listen(128)
    print(f"[cdp-relay] {args.listen_host}:{args.listen_port} -> {args.target_host}:{args.target_port}", flush=True)

    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        threading.Thread(
            target=_handle_conn,
            args=(client, (args.target_host, args.target_port)),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
