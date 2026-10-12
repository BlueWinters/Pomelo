#!/usr/bin/env bash
# ==============================================================================
# 财喵 RPA Chrome 算力节点手工启动与自注册脚本 (launch_node.sh)
#
# 核心职责：
# 1. 支持指定 node_id 启动（若已被注册则输出报错日志并清理退出）；
# 2. 支持缺省 node_id 启动（启动前先调用后端 API 预分配空闲 node_id 及配套端口）；
# 3. 标准化拉起 Xvfb + Fluxbox + x11vnc + noVNC + Chrome CDP + NodeAgent 容器；
# 4. 监听注册结果并输出详细的成功/失败信息与访问地址。
#
# 帮助信息与示例函数
show_help() {
    cat << 'EOF'
==============================================================================
💡 财喵 RPA Chrome 算力节点启动脚本 (launch_node.sh) 使用说明
==============================================================================
用法:
  ./launch_node.sh [选项...]
  或 (传统位置参数模式)
  ./launch_node.sh [节点ID] [宿主机IP] [noVNC端口] [CDP端口] [镜像版本] [VNC密码]

标准命名选项 (每个选项仅支持唯一标准全称):
  --vncpwd <密码>           设置 VNC 远程桌面访问密码 (默认无密码/免密访问)
  --node-id <节点ID>        指定节点唯一标识 (如 chrome-1；缺省则向 API 自动预申请)
  --host-ip <宿主IP>        指定宿主机公开 IP (默认自动探测本机内网 IP)
  --vnc-port <端口>         指定 noVNC 远程桌面外部映射端口 (默认 6080 起递增)
  --cdp-port <端口>         指定 Chrome CDP 远程调试外部映射端口 (默认 9222 起递增)
  --network <网络名>        指定加入的 Docker 集群网络 (如 caimiao-net；缺省则自动探测)
  --register-url <URL>      指定算力池自注册目标 URL (缺省读取 RPA_REGISTER_URL 或 .env)
  --image <镜像名称>        指定 Chrome 节点 Docker 镜像 (默认 rpa-server-chrome:v0.2.5)
  --help                    显示本帮助信息并退出

==============================================================================
📖 示例说明
==============================================================================
1. 【基础推荐】自适应自动分配 ID 与端口，免密直接启动:
   ./launch_node.sh

2. 【密码保护】自适应分配 ID 与端口，仅通过 --vncpwd 指定密码:
   ./launch_node.sh --vncpwd "123456"

3. 【指定网络与宿主IP】加入 Swarm overlay 网络并指定宿主 IP:
   ./launch_node.sh --host-ip 192.168.1.207 --network caimiao-net

4. 【指定节点扩容】指定节点 ID 为 chrome-3，并指定端口与访问密码:
   ./launch_node.sh --node-id chrome-3 --host-ip 192.168.1.207 --vnc-port 6082 --cdp-port 9224 --vncpwd "AdminPass"

5. 【指定镜像启动】指定特定版本镜像启动:
   ./launch_node.sh --image rpa-server-chrome:v0.2.5 --vncpwd "123456"

6. 【传统位置参数兼容模式】:
   ./launch_node.sh chrome-3 192.168.1.207 6082 9224 rpa-server-chrome:v0.2.5 "123456"
   ./launch_node.sh "" 192.168.1.207 "" "" "" "123456"
==============================================================================
EOF
}

set -e

# 1. 检查 Docker 环境
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 错误: 未检测到 docker 命令，请先安装 Docker！"
    exit 1
fi

# 2. 参数解析 (严格仅支持: --vncpwd, --node-id, --host-ip, --vnc-port, --cdp-port, --network, --image, --help 与 传统位置参数)
INPUT_NODE_ID=""
HOST_IP_PARAM=""
CUSTOM_VNC_PORT=""
CUSTOM_CDP_PORT=""
CUSTOM_NETWORK=""
CUSTOM_IMAGE=""
VNC_PASSWORD=""

# 检查是否传入了以 '-' 开头的命名选项
HAS_FLAGS=0
for arg in "$@"; do
    case "$arg" in
        -* ) HAS_FLAGS=1; break ;;
    esac
done

if [ $HAS_FLAGS -eq 1 ]; then
    # 命名选项模式解析
    while [ $# -gt 0 ]; do
        case "$1" in
            --vncpwd)
                VNC_PASSWORD="$2"; shift 2 ;;
            --node-id)
                INPUT_NODE_ID="$2"; shift 2 ;;
            --host-ip)
                HOST_IP_PARAM="$2"; shift 2 ;;
            --vnc-port)
                CUSTOM_VNC_PORT="$2"; shift 2 ;;
            --cdp-port)
                CUSTOM_CDP_PORT="$2"; shift 2 ;;
            --network)
                CUSTOM_NETWORK="$2"; shift 2 ;;
            --register-url)
                CUSTOM_REGISTER_URL="$2"; shift 2 ;;
            --image)
                CUSTOM_IMAGE="$2"; shift 2 ;;
            --help)
                show_help
                exit 0 ;;
            *)
                echo "❌ 错误: 未知参数 '$1'，请执行 '$0 --help' 查看使用说明。"
                exit 1 ;;
        esac
    done
else
    # 传统位置参数模式兼容 (密码仅从第 6 个参数获取，不从环境变量读取)
    INPUT_NODE_ID="${1:-}"
    HOST_IP_PARAM="${2:-}"
    CUSTOM_VNC_PORT="${3:-}"
    CUSTOM_CDP_PORT="${4:-}"
    CUSTOM_IMAGE="${5:-}"
    VNC_PASSWORD="${6:-}"
fi

# 宿主机 IP 校验：必须通过命令行参数 (--host-ip) 或环境变量 (RPA_EXTERNAL_HOST) 显式指定，严禁缺省或填 127.0.0.1 / localhost
HOST_IP="${HOST_IP_PARAM:-${RPA_EXTERNAL_HOST:-}}"
if [ -z "${HOST_IP}" ] || [ "${HOST_IP}" = "127.0.0.1" ] || [ "${HOST_IP}" = "localhost" ] || [ "${HOST_IP}" = "0.0.0.0" ]; then
    echo "❌ 错误: 必须通过 --host-ip 参数或 RPA_EXTERNAL_HOST 环境变量显式指定宿主机真实物理 IP (当前为: '${HOST_IP}'，禁止为空或 127.0.0.1)！"
    echo "💡 示例: ./launch_node.sh --host-ip 192.168.1.6"
    exit 1
fi

# 端口绑定推导：完全依赖全局单一真实源 NGINX_GATEWAY_ENABLED (true -> 127.0.0.1; false -> 0.0.0.0)
GW_ENABLED="${NGINX_GATEWAY_ENABLED:-}"
if [ -z "${GW_ENABLED}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo ".")"
    if [ -f "${SCRIPT_DIR}/../../.env" ]; then
        GW_ENABLED=$(grep -E '^\s*NGINX_GATEWAY_ENABLED=' "${SCRIPT_DIR}/../../.env" 2>/dev/null | cut -d '=' -f2- | tr -d ' "\r\n' | tr '[:upper:]' '[:lower:]' || echo "")
    elif [ -f ".env" ]; then
        GW_ENABLED=$(grep -E '^\s*NGINX_GATEWAY_ENABLED=' ".env" 2>/dev/null | cut -d '=' -f2- | tr -d ' "\r\n' | tr '[:upper:]' '[:lower:]' || echo "")
    fi
fi

if [ "${GW_ENABLED}" = "false" ] || [ "${GW_ENABLED}" = "0" ]; then
    HOST_BIND="0.0.0.0"
else
    # 若指定了非 127.0.0.1 的外部 IP (如作为分布式 Worker 算力节点)，绑定 0.0.0.0 以支持跨机与宿主通信
    if [ "${HOST_IP}" != "127.0.0.1" ] && [ "${HOST_IP}" != "localhost" ]; then
        HOST_BIND="0.0.0.0"
    else
        HOST_BIND="127.0.0.1"
    fi
fi

# 算力池注册 API 地址 (优先命令行参数 --register-url -> 环境变量 RPA_REGISTER_URL -> .env 文件配置 -> 容器内网默认 http://api:8000/dashboard/rpa/pool/nodes/register)
REGISTER_URL="${CUSTOM_REGISTER_URL:-${RPA_REGISTER_URL:-}}"
if [ -z "${REGISTER_URL}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo ".")"
    if [ -f "${SCRIPT_DIR}/../../.env" ]; then
        ENV_REG_URL=$(grep -E '^\s*RPA_REGISTER_URL=' "${SCRIPT_DIR}/../../.env" 2>/dev/null | cut -d '=' -f2- | tr -d ' "\r\n' || echo "")
        [ -n "${ENV_REG_URL}" ] && REGISTER_URL="${ENV_REG_URL}"
    elif [ -f ".env" ]; then
        ENV_REG_URL=$(grep -E '^\s*RPA_REGISTER_URL=' ".env" 2>/dev/null | cut -d '=' -f2- | tr -d ' "\r\n' || echo "")
        [ -n "${ENV_REG_URL}" ] && REGISTER_URL="${ENV_REG_URL}"
    fi
fi
REGISTER_URL="${REGISTER_URL:-http://api:8000/dashboard/rpa/pool/nodes/register}"
FETCH_ID_URL="${REGISTER_URL%/register}/fetch_node_id"

# 3. 确定 node_id 及配套端口（方案 A：启动前预申请）
if [ -n "${INPUT_NODE_ID}" ]; then
    # 模式 1: 命令行显式指定了 node_id
    NODE_ID="${INPUT_NODE_ID}"
    NODE_NUM=$(echo "${NODE_ID}" | grep -o -E '[0-9]+' | head -n 1 || echo "1")
    NODE_NUM=${NODE_NUM:-1}
    DEFAULT_VNC_PORT=$((6080 + NODE_NUM - 1))
    DEFAULT_CDP_PORT=$((9222 + NODE_NUM - 1))
    VNC_PORT="${CUSTOM_VNC_PORT:-${DEFAULT_VNC_PORT}}"
    CDP_PORT="${CUSTOM_CDP_PORT:-${DEFAULT_CDP_PORT}}"
    IS_AUTO_ALLOCATED=0
else
    # 模式 2: 命令行未指定 node_id，向后端 API 预申请下一个空闲 ID 及标准端口 (format=env)
    FETCH_ENV_URL="${REGISTER_URL%/register}/fetch_node_id?format=env&host_ip=${HOST_IP}"
    echo "🔍 正在向后端 API 预申请下一个可用算力节点: ${FETCH_ENV_URL} ..."

    FETCH_RESP=""
    if command -v curl >/dev/null 2>&1; then
        FETCH_RESP=$(curl -s -f -m 5 "${FETCH_ENV_URL}" 2>/dev/null || curl -s -f -m 5 "http://api:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || curl -s -f -m 5 "http://127.0.0.1/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || true)
    elif command -v wget >/dev/null 2>&1; then
        FETCH_RESP=$(wget -qO- -T 5 "${FETCH_ENV_URL}" 2>/dev/null || wget -qO- -T 5 "http://api:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || wget -qO- -T 5 "http://127.0.0.1/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || true)
    fi

    if [ -n "${FETCH_RESP}" ] && echo "${FETCH_RESP}" | grep -q "NODE_ID="; then
        # 一键载入 API 返回的 Shell 变量 (NODE_ID, VNC_PORT, CDP_PORT, HOST_IP)
        eval "${FETCH_RESP}"
        [ -n "${CUSTOM_VNC_PORT}" ] && VNC_PORT="${CUSTOM_VNC_PORT}"
        [ -n "${CUSTOM_CDP_PORT}" ] && CDP_PORT="${CUSTOM_CDP_PORT}"
        [ -n "${HOST_IP_PARAM}" ] && HOST_IP="${HOST_IP_PARAM}"
    else
        echo "⚠️ 无法连接 API 预申请节点 ID，回退到默认编号: chrome-1"
        NODE_ID="chrome-1"
        VNC_PORT="${CUSTOM_VNC_PORT:-6080}"
        CDP_PORT="${CUSTOM_CDP_PORT:-9222}"
    fi

    # 提取推荐编号中的数字，与本地现有容器进行防冲突二次比对
    NODE_NUM=$(echo "${NODE_ID}" | grep -o -E '[0-9]+' | head -n 1 || echo "1")
    NODE_NUM=${NODE_NUM:-1}

    # 循环检查本地 Docker 容器，若同名容器已存在则自增递推下一个空闲节点号，避免并发启动相互覆盖
    while docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq "^caimiao-chrome-${NODE_NUM}\$|^chrome-${NODE_NUM}\$"; do
        NODE_NUM=$((NODE_NUM + 1))
        NODE_ID="chrome-${NODE_NUM}"
        VNC_PORT="${CUSTOM_VNC_PORT:-$((6080 + NODE_NUM - 1))}"
        CDP_PORT="${CUSTOM_CDP_PORT:-$((9222 + NODE_NUM - 1))}"
    done

    echo "📋 成功确定分配节点 ID: ${NODE_ID} (配套端口: VNC=${VNC_PORT}, CDP=${CDP_PORT}, 宿主IP=${HOST_IP}, 绑定=${HOST_BIND})"
    IS_AUTO_ALLOCATED=1
fi

IMAGE="${CUSTOM_IMAGE:-rpa-server-chrome:v0.2.5}"
CONTAINER_NAME="${NODE_ID}"
VOLUME_NAME="rpa_chrome_data_${NODE_ID}"

# 4. 检查是否有同名旧容器在运行
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq "^${CONTAINER_NAME}\$|^${NODE_ID}\$"; then
    echo "⚠️ 发现已存在同名旧容器: ${CONTAINER_NAME}，正在先清理..."
    docker rm -f "${CONTAINER_NAME}" "${NODE_ID}" >/dev/null 2>&1 || true
fi

# 5. 网络决策逻辑：显式指定参数 (--network) > 环境变量 (RPA_DOCKER_NETWORK) > 自动探测当前容器网络
NET_ARG=""
TARGET_NET="${CUSTOM_NETWORK:-${RPA_DOCKER_NETWORK:-}}"
CONTAINER_REGISTER_URL="${REGISTER_URL}"

if [ -z "${TARGET_NET}" ]; then
    # 优先精确探测当前运行中的 API 或 Redis 容器实际挂载的集群网络（100% 同网段对齐）
    RUNNING_API_CID=$(docker ps --filter "name=api" --filter "name=redis" --format "{{.ID}}" 2>/dev/null | head -n 1)
    if [ -n "${RUNNING_API_CID}" ]; then
        DETECTED_NET=$(docker inspect "${RUNNING_API_CID}" --format '{{range $k, $v := .NetworkSettings.Networks}}{{if and (ne $k "ingress") (ne $k "bridge") (ne $k "host")}}{{$k}} {{end}}{{end}}' 2>/dev/null | awk '{print $1}')
        if [ -n "${DETECTED_NET}" ]; then
            TARGET_NET="${DETECTED_NET}"
        fi
    fi

    # 兜底按优先级探测 Swarm 栈网络与 Compose 网络
    if [ -z "${TARGET_NET}" ]; then
        if docker network ls --format '{{.Name}}' 2>/dev/null | grep -Eq "^caimiao_caimiao-net\$"; then
            TARGET_NET="caimiao_caimiao-net"
        elif docker network ls --format '{{.Name}}' 2>/dev/null | grep -Eq "^caimiao-net\$"; then
            TARGET_NET="caimiao-net"
        elif docker network ls --format '{{.Name}}' 2>/dev/null | grep -Eq "^caimiao_backend_default\$|^caimiao_default\$"; then
            TARGET_NET=$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -E "^caimiao_backend_default\$|^caimiao_default\$" | head -n 1)
        fi
    fi
fi

# 检查目标网络驱动类型：如果是 overlay 网络，standalone 容器使用 -p 暴露宿主端口会导致 docker_gwbridge 丢包/ARP 失败
if [ -n "${TARGET_NET}" ]; then
    NET_DRIVER=$(docker network inspect "${TARGET_NET}" --format '{{.Driver}}' 2>/dev/null || echo "")
    if [ "${NET_DRIVER}" = "overlay" ]; then
        echo "ℹ️ 检测到网络 '${TARGET_NET}' 为 Swarm Overlay 网络，独立算力容器使用物理端口映射(-p)，跳过挂载 Overlay 网络以避免网桥流量黑洞。"
        TARGET_NET=""
    fi
fi

if [ -n "${TARGET_NET}" ]; then
    echo "🌐 挂载 Docker 集群网络: ${TARGET_NET}"
    NET_ARG="--network ${TARGET_NET} --network-alias ${NODE_ID} --network-alias caimiao-${NODE_ID}"
    # 容器与 API 处于同一 Docker 网络时，容器内自注册优先走 Docker 内网域名直连 API（高效安全）
    if [ -z "${RPA_REGISTER_URL}" ] || echo "${RPA_REGISTER_URL}" | grep -q "${HOST_IP}"; then
        CONTAINER_REGISTER_URL="http://api:8000/dashboard/rpa/pool/nodes/register"
    fi
else
    # 未加入集群网络时，若发现注册目标为 Docker 内网域名 api:8000，则自动探测修正为宿主物理 IP / 网关 IP
    if echo "${CONTAINER_REGISTER_URL}" | grep -q "://api:"; then
        echo "⚠️ 未连接 Docker 集群网络，自动将内网域名 'api' 修正为宿主直连 IP (${HOST_IP})..."
        CONTAINER_REGISTER_URL=$(echo "${CONTAINER_REGISTER_URL}" | sed "s|://api:|://${HOST_IP}:|")
    fi
fi

echo "=============================================================================="
if [ ${IS_AUTO_ALLOCATED} -eq 0 ]; then
    echo "🚀 准备启动 RPA Chrome 算力节点容器 (指定节点: ${NODE_ID})"
else
    echo "🚀 准备启动 RPA Chrome 算力节点容器 (自动预分配节点: ${NODE_ID})"
fi
echo "=============================================================================="
echo "  📌 节点标识 (NODE_ID)        : ${NODE_ID}"
echo "  🌐 宿主机公开 IP (HOST_IP)   : ${HOST_IP}"
echo "  🔒 端口绑定地址 (HOST_BIND) : ${HOST_BIND}"
echo "  🖥️ noVNC 远程桌面端口 (VNC) : ${HOST_BIND}:${VNC_PORT} (宿主) -> 6080 (容器)"
echo "  🔗 CDP 调试代理端口 (CDP)   : ${HOST_BIND}:${CDP_PORT} (宿主) -> 9222 (容器)"
echo "  🌐 集群网络 (NETWORK)       : ${TARGET_NET:-[未加入/独立网桥模式]}"
if [ -n "${VNC_PASSWORD}" ]; then
echo "  🔐 VNC 访问密码保护 (AUTH) : [已启用] (访问需输入密码或携带 password 参数)"
else
echo "  🔓 VNC 访问密码保护 (AUTH) : [未启用] (免密直接访问)"
fi
echo "  📦 Chrome 镜像版本 (IMAGE)  : ${IMAGE}"
echo "  🤖 算力池自注册 URL         : ${CONTAINER_REGISTER_URL}"
echo "=============================================================================="

# 6. 生成防弹窗优化的 entrypoint 补丁脚本 (确保 Chrome 启动不受首次引导阻塞，CDP 快速就绪)
ENTRYPOINT_PATCH="/tmp/rpa_entrypoint_${NODE_ID}.sh"
cat << 'EOF_ENTRY' > "${ENTRYPOINT_PATCH}"
#!/bin/bash
set -e

# 1. 残留与锁文件清理
mkdir -p /run/dbus && rm -f /run/dbus/pid
dbus-daemon --system --fork 2>/dev/null || true
rm -f /tmp/chrome-profile/Singleton* 2>/dev/null || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
# 预创建 Chrome 默认下载目录及共享临时下载目录并赋权，防止无头下载因路径不存在而丢弃
mkdir -p /root/Downloads /tmp/downloads
chmod 777 /root/Downloads /tmp/downloads

# 2. 启动 Xvfb 虚拟显示屏幕
echo "[entrypoint] 🖥️ Starting Xvfb on display ${DISPLAY:-:99} (${RESOLUTION:-1920x1080x24})..."
Xvfb "${DISPLAY:-:99}" -screen 0 "${RESOLUTION:-1920x1080x24}" -ac +extension GLX +render -noreset &
sleep 1

# 3. 启动轻量级窗口管理器 Fluxbox
echo "[entrypoint] 🪟 Starting Fluxbox window manager..."
DISPLAY="${DISPLAY:-:99}" fluxbox &

# 4. 启动 x11vnc 抓屏服务
echo "[entrypoint] 📡 Starting x11vnc on port ${VNC_PORT:-5900}..."
if [ -n "$VNC_PASSWORD" ]; then
    mkdir -p ~/.vnc
    x11vnc -storepasswd "$VNC_PASSWORD" ~/.vnc/passwd
    x11vnc -display "${DISPLAY:-:99}" -forever -shared -rfbport "${VNC_PORT:-5900}" -rfbauth ~/.vnc/passwd -bg -o /tmp/x11vnc.log
else
    x11vnc -display "${DISPLAY:-:99}" -forever -shared -rfbport "${VNC_PORT:-5900}" -nopw -bg -o /tmp/x11vnc.log
fi

# 5. 启动 websockify
echo "[entrypoint] 🌐 Starting noVNC websockify on port ${NOVNC_PORT:-6080}..."
websockify --web /usr/share/novnc "${NOVNC_PORT:-6080}" "localhost:${VNC_PORT:-5900}" &

# 6. 启动 Dashboard 运维服务
python3 /usr/local/bin/rpa_server.py \
    --port "${RPA_FILE_PORT:-9224}" \
    --cdp-port "${CHROME_CDP_PORT:-9229}" \
    --root "${RPA_FILE_ROOT:-/tmp/.sessions}" \
    --token "${RPA_FILE_TOKEN:-}" &

# 7. 启动 CDP 转发层
python3 /usr/local/bin/cdp_relay.py \
    --listen-port "${CDP_RELAY_PORT:-9222}" \
    --target-port "${CHROME_CDP_PORT:-9229}" &

# 8. 启动 Google Chrome (优化参数: 禁用首次引导和搜索引擎选择窗口，允许文件下载，快速开启 CDP)
echo "[entrypoint] 🚀 Starting Google Chrome with CDP on port ${CHROME_CDP_PORT:-9229}..."
DISPLAY="${DISPLAY:-:99}" google-chrome \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-background-networking \
    --no-first-run \
    --no-default-browser-check \
    --disable-search-engine-choice-screen \
    --disable-features=Translate,InsecureDownloadWarnings \
    --safebrowsing-disable-download-protection \
    --disable-gpu \
    --remote-allow-origins=* \
    --log-level=3 \
    --window-size=1920,1080 \
    --window-position=0,0 \
    --start-maximized \
    --remote-debugging-port="${CHROME_CDP_PORT:-9229}" \
    --user-data-dir=/tmp/chrome-profile \
    about:blank &

# 9. 启动 RPA 算力池自注册守护 Agent
echo "[entrypoint] 🤖 Starting Chrome Node Agent for self-registration..."
python3 /usr/local/bin/node_agent.py

sleep 2
echo "=========================================================================="
echo "  🎉 noVNC 远程桌面接管就绪: http://<宿主机IP>:${NOVNC_PORT:-6080}/vnc.html"
echo "  🔗 Chrome CDP 调试代理就绪: http://<容器IP>:${CDP_RELAY_PORT:-9222}"
echo "  📂 会话文件与快照服务就绪: http://<容器IP>:${RPA_FILE_PORT:-9224}"
echo "  🤖 算力池自注册 Agent 运行中: ${RPA_REGISTER_URL}"
echo "=========================================================================="

wait -n
EOF_ENTRY
chmod +x "${ENTRYPOINT_PATCH}"

# 7. 执行 Docker Run 启动容器
echo "🐳 正在拉起 Chrome 节点容器 (${CONTAINER_NAME})..."
CONTAINER_ID=$(docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --shm-size=2gb \
  --add-host api:10.14.210.39 \
  --add-host caimiao-master:10.14.210.39 \
  --label com.docker.stack.namespace=caimiao \
  --label com.docker.swarm.service.name="caimiao_rpa_${NODE_ID}" \
  --label com.docker.compose.project=caimiao \
  --label com.docker.compose.service="${NODE_ID}" \
  -p "${HOST_BIND}:${VNC_PORT}:6080" \
  -p "${HOST_BIND}:${CDP_PORT}:9222" \
  -e TZ=Asia/Shanghai \
  -e DISPLAY=:99 \
  -e RESOLUTION=1920x1080x24 \
  -e NOVNC_PORT=6080 \
  -e VNC_PORT=5900 \
  -e VNC_PASSWORD="${VNC_PASSWORD:-}" \
  -e RPA_FILE_PORT=9224 \
  -e CDP_RELAY_PORT=9222 \
  -e CHROME_CDP_PORT=9229 \
  -e RPA_NODE_ID="${NODE_ID}" \
  -e RPA_EXTERNAL_HOST="${HOST_IP}" \
  -e RPA_NOVNC_EXTERNAL_PORT="${VNC_PORT}" \
  -e RPA_CDP_EXTERNAL_PORT="${CDP_PORT}" \
  -e RPA_REGISTER_URL="${CONTAINER_REGISTER_URL}" \
  -e RPA_HEARTBEAT_INTERVAL=7200 \
  -v "${VOLUME_NAME}:/tmp/chrome-profile" \
  -v "${ENTRYPOINT_PATCH}:/entrypoint.sh:ro" \
  -v "${CDP_RELAY_SRC:-/home/ubuntu/caimiao_backend/docker/chrome/cdp_relay.py}:/usr/local/bin/cdp_relay.py:ro" \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  --health-cmd="python3 -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:9222/json/version\", timeout=3)' || exit 1" \
  --health-interval=10s \
  --health-timeout=5s \
  --health-retries=3 \
  --health-start-period=15s \
  "${IMAGE}")

SHORT_ID=$(echo "${CONTAINER_ID}" | cut -c 1-12)

# 7. 监听与捕获容器注册结果
echo "⏳ 容器已拉起 (${SHORT_ID})，正在等待内部 Chrome 初始化与算力池注册 (最多 30s)..."

REGISTERED=0
CONFLICT=0
TIMEOUT=30
START_TIME=$(date +%s)

while [ $(( $(date +%s) - START_TIME )) -lt $TIMEOUT ]; do
    # 检查容器是否存活
    if ! docker ps -q --no-trunc 2>/dev/null | grep -q "^${CONTAINER_ID}"; then
        echo "❌ 容器异常退出！最新日志如下："
        docker logs --tail 30 "${CONTAINER_ID}" 2>&1 || true
        exit 1
    fi

    LOGS=$(docker logs "${CONTAINER_ID}" 2>&1 || true)

    # 1. 检查注册成功标记
    if echo "$LOGS" | grep -q "成功注册至 RPA 算力池"; then
        REGISTERED=1
        break
    fi

    # 2. 检查是否发生 409 重复注册冲突
    if echo "$LOGS" | grep -Eq "HTTP 409|已存在，不可重复注册"; then
        CONFLICT=1
        break
    fi

    sleep 1
done

# 8. 结果判定与日志输出
if [ $CONFLICT -eq 1 ]; then
    echo ""
    echo "=============================================================================="
    echo "❌ [ERROR] 注册失败: 节点 ID '${NODE_ID}' 已经被注册至算力池，不可重复注册！"
    echo "=============================================================================="
    echo "⚠️ 正在停止并清理冲突容器 (${CONTAINER_NAME})..."
    docker rm -f "${CONTAINER_ID}" >/dev/null 2>&1 || true
    echo "💡 建议: 请更换其他未被占用的节点 ID，或者直接不指定 node_id 运行本脚本以自动分配可用 ID。"
    echo "=============================================================================="
    exit 1
fi

if [ $REGISTERED -eq 1 ]; then
    echo ""
    echo "=============================================================================="
    if [ ${IS_AUTO_ALLOCATED} -eq 0 ]; then
        echo "🎉 节点 '${NODE_ID}' 已成功注册至 RPA 算力池！"
    else
        echo "🎉 节点已通过 API 自动预分配并成功注册至 RPA 算力池 (节点: ${NODE_ID})！"
    fi
    echo "=============================================================================="
    echo "  📌 节点标识 (NODE_ID)        : ${NODE_ID}"
    echo "  🌐 宿主机公开 IP (HOST_IP)   : ${HOST_IP}"
    if [ "${HOST_BIND}" = "127.0.0.1" ]; then
        echo "  🔒 端口物理隔离 (BIND)       : 127.0.0.1 (拒绝外部直连，仅通过 Dashboard 3FA/Nginx 网关访问)"
    else
        echo "  🌐 端口对外开放 (BIND)       : ${HOST_BIND} (支持局域网直接访问)"
    fi
    if [ -n "${VNC_PASSWORD}" ]; then
        echo "  🖥️ noVNC 远程桌面地址       : http://${HOST_IP}:${VNC_PORT}/vnc.html (密码保护: ${VNC_PASSWORD})"
        echo "  🔑 noVNC 免密直达链接       : http://${HOST_IP}:${VNC_PORT}/vnc.html?password=${VNC_PASSWORD}"
    else
        echo "  🖥️ noVNC 远程桌面地址       : http://${HOST_IP}:${VNC_PORT}/vnc.html (免密访问)"
    fi
    echo "  🔗 CDP 远程调试地址         : http://${HOST_IP}:${CDP_PORT}"
    echo "  🐳 运行中容器名称           : ${CONTAINER_NAME}"
    echo "=============================================================================="
    echo "💡 提示: 容器运行正常，如需查看实时运行日志可执行: docker logs -f ${CONTAINER_NAME}"
    echo "=============================================================================="
    exit 0
else
    echo ""
    echo "❌ [ERROR] 注册超时: 容器已拉起，但在 ${TIMEOUT}s 内未检测到算力池注册成功确认。"
    echo "------------------------------------------------------------------------------"
    echo "  最新容器日志如下:"
    docker logs --tail 25 "${CONTAINER_ID}" 2>&1 || true
    echo "------------------------------------------------------------------------------"
    echo "💡 请执行 'docker logs -f ${CONTAINER_NAME}' 检查内部状态或网络。"
    echo "=============================================================================="
    exit 1
fi
