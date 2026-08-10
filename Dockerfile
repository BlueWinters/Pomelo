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

# 2. 构建时从 Google 官方源下载 Chrome 安装包（海外构建节点可直连 dl.google.com）
RUN set -x \
    # 3. 替换 Debian 镜像源为清华源
    && sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    # 4. 安装 wget/ca-certificates，再下载 Chrome deb，最后安装 Chrome 包及中文字体
    #    （apt 会自动补齐 Chrome 所需的 Linux 依赖；dbus：headless Chrome 启动时若连不上
    #    /run/dbus/system_bus_socket 会刷大量 ERROR 日志）
    && apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/google-chrome-stable.deb \
    && apt-get install -y --no-install-recommends \
        /tmp/google-chrome-stable.deb \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        socat \
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

EXPOSE 9223
WORKDIR /app

# 启动远程服务
# Chrome 的 --remote-debugging 只能绑定容器内 127.0.0.1（新版 Chrome 忽略 --remote-debugging-address），
# 因此用 socat 在 0.0.0.0:9223 上做 TCP 转发（compose 里映射宿主端口 → 9223）。
# 注意：容器被强杀/重启后，profile 卷里可能残留 Singleton* 锁文件（旧 Chrome 进程已死、
# 锁必为 stale），不清理会导致新 Chrome 误判"profile 被占用"而拒绝启动，因此启动前先清锁。
# dbus-daemon：headless 容器无 dbus 时 Chrome 启动会连刷 ERROR 日志（system_bus_socket /
# NameHasOwner 等），起一个 system bus 即可消除。/run 不是 tmpfs，restart 会残留 pid 文件，
# 故启动前先删 /run/dbus/pid。
# Chrome 参数：--enable-unsafe-swiftshader 消除 WebGL 软件回退弃用警告；
# --log-level=3 只保留 FATAL 日志 —— GCM 注册（DEPRECATED_ENDPOINT）等后台 Google 服务
# 噪音无法通过 flag 彻底禁用（CEF 社区已确认），dbus 修复后用 log-level 压制残余杂音。
CMD ["sh", "-c", "mkdir -p /run/dbus && rm -f /run/dbus/pid && dbus-daemon --system --fork ; rm -f /tmp/chrome-profile/Singleton* ; google-chrome --headless=new --no-sandbox --disable-dev-shm-usage --disable-background-networking --enable-unsafe-swiftshader --log-level=3 --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile & sleep 2 && exec socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222,retry=10,interval=1"]
