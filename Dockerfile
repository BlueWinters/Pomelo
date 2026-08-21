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

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

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


CMD ["/entrypoint.sh"]
