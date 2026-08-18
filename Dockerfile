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

# 2. 换源与依赖安装
RUN set -x \
    # 3. 换 Debian 源为阿里云镜像（ACR 构建环境外网受限：清华源返回 403，dl.google.com 不通）
    && sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    # 4. 安装 wget/ca-certificates，从 Google 官方源下载 Chrome deb
    #    （本机可直连 dl.google.com；若日后改走 ACR 云端构建，此步需换成国内可达源）
    && apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/google-chrome-stable.deb \
    # 5. 安装 Chrome 包及中文字体（apt 自动补齐 Chrome 所需 Linux 依赖；
    #    dbus：headless Chrome 启动时若连不上 /run/dbus/system_bus_socket 会刷大量 ERROR 日志）
    && apt-get install -y --no-install-recommends \
        /tmp/google-chrome-stable.deb \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        socat \
        dbus \
    # 6. 安装 Python 依赖（走阿里云 pypi 镜像）
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
        playwright==1.61.0 \
        playwright-stealth \
    # 7. 补齐其他可能缺失的底层系统依赖
    && playwright install-deps \
    # 8. 清理安装包和缓存减小镜像体积
    && rm -f /tmp/google-chrome-stable.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache/pip

# 8. RPA 定制服务（dashboard 运维用：会话文件浏览 + 浏览器快照，暴露 RPA 会话目录 /tmp/.sessions）
COPY rpa_server.py /usr/local/bin/rpa_server.py

# 只暴露文件服务端口；CDP(9222) 绑定容器回环，不出容器（worker 经共享网络栈访问）
EXPOSE 9224
WORKDIR /app

# 启动远程服务
# Chrome 的 --remote-debugging 只能绑定容器内 127.0.0.1（新版 Chrome 忽略 --remote-debugging-address），
# 因此 CDP 只对容器回环可见：worker 经共享网络栈直连 127.0.0.1:9222，
# 会话文件与浏览器快照由容器内 file_server(9224) 提供，宿主/局域网永远接触不到 CDP。
# 不再安装 socat、不做任何对外 TCP 转发 —— RPA 容器无需发布宿主端口。
# 注意：容器被强杀/重启后，profile 卷里可能残留 Singleton* 锁文件（旧 Chrome 进程已死、
# 锁必为 stale），不清理会导致新 Chrome 误判"profile 被占用"而拒绝启动，因此启动前先清锁。
# dbus-daemon：headless 容器无 dbus 时 Chrome 启动会连刷 ERROR 日志（system_bus_socket /
# NameHasOwner 等），起一个 system bus 即可消除。/run 不是 tmpfs，restart 会残留 pid 文件，
# 故启动前先删 /run/dbus/pid。
# Chrome 参数：--enable-unsafe-swiftshader 消除 WebGL 软件回退弃用警告；
# --log-level=3 只保留 FATAL 日志 —— GCM 注册（DEPRECATED_ENDPOINT）等后台 Google 服务
# 噪音无法通过 flag 彻底禁用（CEF 社区已确认），dbus 修复后用 log-level 压制残余杂音。
# file_server 与 chrome 均后台运行，前台 wait 阻塞保持容器存活（任一进程崩溃时
# 容器不会退出，由 healthcheck 感知；两者都退出容器才停止，由 restart 策略拉起）。
CMD ["sh", "-c", "mkdir -p /run/dbus && rm -f /run/dbus/pid && dbus-daemon --system --fork ; rm -f /tmp/chrome-profile/Singleton* ; python3 /usr/local/bin/rpa_server.py --port 9224 --root \"${RPA_FILE_ROOT:-/tmp/.sessions}\" --token \"${RPA_FILE_TOKEN:-}\" & google-chrome --headless=new --no-sandbox --disable-dev-shm-usage --disable-background-networking --enable-unsafe-swiftshader --log-level=3 --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile & sleep 2 && wait"]
