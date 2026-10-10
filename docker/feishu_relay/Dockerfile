# ==========================================
# 飞书 Relay 中继镜像（轻量级纯转发）
# ==========================================
# 只安装 lark_oapi + httpx + loguru，不包含任何业务依赖。
# 构建：docker build -f docker/feishu_relay/Dockerfile -t caimiao-feishu-relay .
# 国内镜像：docker build --build-arg USE_CHINA_MIRROR=true -f docker/feishu_relay/Dockerfile -t caimiao-feishu-relay .
ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

ARG USE_CHINA_MIRROR=false

# 换源（国内环境切换阿里云镜像）
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources ; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 只安装转发所需的三个包（国内环境走阿里云 pip 镜像）
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/" ; \
    else \
        PIP_INDEX="https://pypi.org/simple/" ; \
    fi ; \
    pip install --index-url "$PIP_INDEX" --upgrade pip && \
    pip install --index-url "$PIP_INDEX" lark_oapi httpx loguru

# 复制 relay 脚本和管理页面
COPY feishu_relay.py /app/feishu_relay.py
COPY static/ /app/static/

# 创建低权限用户
RUN useradd --create-home --uid 10001 relayuser \
    && chown -R relayuser:relayuser /app

USER relayuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, os; port = os.getenv('FEISHU_RELAY_HEALTH_PORT', '8070'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "/app/feishu_relay.py"]