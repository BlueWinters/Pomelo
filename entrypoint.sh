#!/bin/bash
set -e

# ==================== 启动编排 ====================
# 启动前先做两处残留清理（容器被强杀/重启后会残留）：
#   - /run/dbus/pid ：/run 不是 tmpfs，dbus 的 pid 文件跨重启残留；
#   - /tmp/chrome-profile/Singleton* ：旧 Chrome 进程已死、锁必为 stale，
#     不清会误判"profile 被占用"而拒绝启动。
# 随后后台依次拉起三个进程（端口均由上方 ENV 统一定义并注入）：
#   - dbus-daemon    : headless 容器无 dbus 时 Chrome 会连刷 ERROR 日志
#     （system_bus_socket / NameHasOwner 等），起一个 system bus 即消除；
#   - rpa_server     : dashboard 会话文件浏览 + 快照，监听 0.0.0.0:${RPA_FILE_PORT}；
#     --cdp-port 指向容器内 Chrome CDP 回环端口（快照在容器内直连 Chrome）；
#   - cdp_relay      : 面向 worker 的 CDP 转发层，0.0.0.0:${CDP_RELAY_PORT}
#     -> 127.0.0.1:${CHROME_CDP_PORT}(Chrome)。swarm 的 service 不支持共享网络栈，
#     worker 连不上 Chrome 回环，故由它转发；compose 下 worker 直连回环可绕开本进程；
#   - google-chrome  : 新版只监听容器回环 127.0.0.1:${CHROME_CDP_PORT}
#     （--remote-debugging-address 被忽略），CDP 天然不出容器。参数说明：
#       --headless=new                     无头模式
#       --no-sandbox                       容器内以 root 运行
#       --disable-dev-shm-usage            避免 /dev/shm 过小导致崩溃
#       --enable-unsafe-swiftshader        消除 WebGL 软件回退弃用警告
#       --log-level=3                      只保留 FATAL —— GCM 注册（DEPRECATED_ENDPOINT）
#                                          等 Google 后台服务噪音无法用 flag 彻底禁用
#                                          （CEF 社区已确认），用 log-level 压制残余杂音
# 最后 sleep 2 等端口就绪，前台 wait 阻塞保持容器存活：
#   任一进程崩溃时容器不退出（由 healthcheck 感知）；仅当全部后台进程退出后
#   wait 才返回、容器才停止（由 restart 策略拉起）。
# 安全：本镜像不发布任何宿主端口，三个进程的暴露范围仅限所属 swarm/compose
# 私有网络 —— CDP 不出宿主、不出公网。


# 清理与准备工作
mkdir -p /run/dbus && rm -f /run/dbus/pid
dbus-daemon --system --fork
rm -f /tmp/chrome-profile/Singleton*

# 后台启动服务
python3 /usr/local/bin/rpa_server.py \
    --port "${RPA_FILE_PORT}" \
    --cdp-port "${CHROME_CDP_PORT}" \
    --root "${RPA_FILE_ROOT:-/tmp/.sessions}" \
    --token "${RPA_FILE_TOKEN:-}" &

python3 /usr/local/bin/cdp_relay.py \
    --listen-port "${CDP_RELAY_PORT}" \
    --target-port "${CHROME_CDP_PORT}" &

google-chrome \
    --headless=new --no-sandbox --disable-dev-shm-usage \
    --disable-background-networking --enable-unsafe-swiftshader --log-level=3 \
    --remote-debugging-port="${CHROME_CDP_PORT}" \
    --user-data-dir=/tmp/chrome-profile &

sleep 2
wait -n  # 建议使用 wait -n，只要有任意后台进程异常退出即可及时暴露