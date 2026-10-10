ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim-bookworm

LABEL maintainer="xiaoye"
LABEL description="Google Chrome 远程 Playwright + noVNC Web 远程桌面接管服务"

# 1. 设置环境变量
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    RESOLUTION=1920x1080x24 \
    NOVNC_PORT=6080 \
    VNC_PORT=5900 \
    RPA_FILE_PORT=9224 \
    CDP_RELAY_PORT=9222 \
    CHROME_CDP_PORT=9229

# 2. 将本地下载好的 Chrome 安装包复制到容器镜像中
#COPY google-chrome-stable_current_amd64.deb /tmp/google-chrome-stable.deb

RUN set -x \
    # 3. 替换 Debian 镜像源
    && sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update  \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/google-chrome-stable.deb \
    # 4. 安装 Chrome、中文字体、X11 虚拟屏幕 (Xvfb)、VNC 服务 (x11vnc)、窗口管理器 (fluxbox) 与 noVNC/websockify
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
        /tmp/google-chrome-stable.deb \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        fonts-noto-cjk \
        fontconfig \
        dbus \
        xvfb \
        x11vnc \
        fluxbox \
        novnc \
        websockify \
        procps \
        net-tools \
    # 5. 配置 noVNC 默认访问页 (访问 http://ip:6080/ 直接打开 vnc.html 画面)
    && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && fc-cache -f -v \
    # 6. 安装 Python 依赖（含 playwright 及 playwright-stealth 反爬加固库）
    && pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple/ \
        playwright==1.61.0 \
        playwright-stealth \
    # 7. 补齐其他可能缺失的底层系统依赖
    && playwright install-deps \
    # 8. 清理安装包和缓存减小镜像体积
    && rm -f /tmp/google-chrome-stable.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache/pip

# 9. 部署 RPA 定制服务脚本
COPY rpa_server.py /usr/local/bin/rpa_server.py
COPY cdp_relay.py /usr/local/bin/cdp_relay.py
COPY node_agent.py /usr/local/bin/node_agent.py
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# 暴露端口：
# 6080: noVNC Web 远程桌面直接访问端口
# 5900: x11vnc 原生 VNC 端口
# 9222: 面向 Worker 的 CDP 调试代理端口
# 9224: 面向 Dashboard 的运维文件/快照 HTTP 端口
EXPOSE 6080 5900 9222 9224
WORKDIR /app

CMD ["/entrypoint.sh"]
