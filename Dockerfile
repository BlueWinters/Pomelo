ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim-bookworm

LABEL maintainer="xiaoye"
LABEL description="Google Chrome 的远程 Playwright 服务"

# 1. 设置环境变量：指定 npmmirror 为 Playwright 浏览器下载镜像源
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# 2. 将本地下载好的 Chrome 安装包复制到容器镜像中
COPY google-chrome-stable_current_amd64.deb /tmp/google-chrome-stable.deb

RUN set -x \
    # 3. 替换 Debian 镜像源为清华源
    && sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    # 4. 安装本地 Chrome 包及中文字体（apt 会自动补齐 Chrome 所需的 Linux 依赖）
    #    dbus：headless Chrome 启动时若连不上 /run/dbus/system_bus_socket 会刷大量 ERROR 日志
    && apt-get update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
        /tmp/google-chrome-stable.deb \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        dbus \
    # 5. 安装 Python 依赖（含 playwright 及 playwright-stealth 反爬加固库）
    && pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple/ \
        playwright==1.61.0 \
        playwright-stealth \
    # 6. 补齐其他可能缺失的底层系统依赖
    && playwright install-deps \
    # 7. 清理安装包和缓存减小镜像体积
    && rm -f /tmp/google-chrome-stable.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache/pip

# 8. RPA 定制服务：
#    rpa_server.py —— dashboard 运维用（会话文件浏览 + 浏览器快照，暴露会话目录 /tmp/.sessions）
#    cdp_relay.py  —— worker 直连 Chrome CDP 的低层网络转发
COPY rpa_server.py /usr/local/bin/rpa_server.py
COPY cdp_relay.py /usr/local/bin/cdp_relay.py

# 端口统一在此定义，CMD 启动时作为参数注入脚本与 Chrome：
#   RPA_FILE_PORT   —— rpa_server 文件+快照 HTTP 端口
#   CDP_RELAY_PORT  —— cdp_relay 对外 CDP 转发端口（worker 经网络连这里）
#   CHROME_CDP_PORT —— Chrome CDP 回环端口（仅容器内，快照与 cdp_relay 的目标）
ENV RPA_FILE_PORT=9224 \
    CDP_RELAY_PORT=9222 \
    CHROME_CDP_PORT=9229

# EXPOSE 仅为镜像文档声明（Dockerfile 语法不能引用 ENV 变量），实际端口以 ENV + CMD 传参为准
EXPOSE 9224 9222
WORKDIR /app

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
CMD mkdir -p /run/dbus && rm -f /run/dbus/pid \
    && dbus-daemon --system --fork ; \
    rm -f /tmp/chrome-profile/Singleton* ; \
    python3 /usr/local/bin/rpa_server.py \
        --port "${RPA_FILE_PORT}" \
        --cdp-port "${CHROME_CDP_PORT}" \
        --root "${RPA_FILE_ROOT:-/tmp/.sessions}" \
        --token "${RPA_FILE_TOKEN:-}" & \
    python3 /usr/local/bin/cdp_relay.py \
        --listen-port "${CDP_RELAY_PORT}" \
        --target-port "${CHROME_CDP_PORT}" & \
    google-chrome \
        --headless=new --no-sandbox --disable-dev-shm-usage \
        --disable-background-networking --enable-unsafe-swiftshader --log-level=3 \
        --remote-debugging-port="${CHROME_CDP_PORT}" \
        --user-data-dir=/tmp/chrome-profile & \
    sleep 2 && wait
