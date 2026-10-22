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
  --register-url <URL>      指定算力池自注册目标 URL (缺省读取 RPA_REGISTER_URL 或配置文件)
  --image <镜像名称>        指定 Chrome 节点 Docker 镜像 (默认 rpa-server-chrome:v0.2.7)
  --help                    显示本帮助信息并退出

环境变量与配置文件取值优先级:
  1. RPA_REGISTER_URL (算力池注册地址):
     命令行 (--register-url) > 环境变量 (RPA_REGISTER_URL) > 配置文件 (.env.multipass) > 默认值 (http://<HOST_IP>:8000/...)
  2. NGINX_GATEWAY_ENABLED (网关模式开关):
     环境变量 (NGINX_GATEWAY_ENABLED) > 配置文件 (.env.multipass) > 默认值 (true)

==============================================================================
📖 示例说明
==============================================================================
1. 【基础推荐】指定宿主机 IP，自适应分配节点 ID 与端口，免密直接启动:
   ./launch_node.sh --host-ip 192.168.1.207

2. 【密码保护】自适应分配 ID 与端口，仅通过 --vncpwd 指定密码:
   ./launch_node.sh --host-ip 192.168.1.207 --vncpwd "123456"

3. 【指定节点扩容】指定节点 ID 为 chrome-3，并指定端口与访问密码:
   ./launch_node.sh --node-id chrome-3 --host-ip 192.168.1.207 --vnc-port 6082 --cdp-port 9224 --vncpwd "AdminPass"

4. 【指定镜像启动】指定特定版本镜像启动:
   ./launch_node.sh --host-ip 192.168.1.207 --image rpa-server-chrome:v0.2.7 --vncpwd "123456"

5. 【传统位置参数兼容模式】:
   ./launch_node.sh chrome-3 192.168.1.207 6082 9224 rpa-server-chrome:v0.2.7 "123456"
   ./launch_node.sh "" 192.168.1.207 "" "" "" "123456"
==============================================================================
EOF
}

set -e

# 0. 全局唯一环境配置文件名（可根据部署环境修改，如 .env.multipass 或 .env）
ENV_FILENAME=".env.multipass"

# 从唯一配置文件 (${ENV_FILENAME}) 中读取指定键的值
get_env_config() {
    local key="$1"
    local script_dir
    script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo ".")"
    local env_path=""
    if [ -f "${script_dir}/../../${ENV_FILENAME}" ]; then
        env_path="${script_dir}/../../${ENV_FILENAME}"
    elif [ -f "${ENV_FILENAME}" ]; then
        env_path="${ENV_FILENAME}"
    fi
    if [ -n "${env_path}" ]; then
        grep -E "^\s*${key}=" "${env_path}" 2>/dev/null | tail -n 1 | cut -d '=' -f2- | tr -d ' "\r\n' || echo ""
    fi
}

# 1. 检查 Docker 环境
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 错误: 未检测到 docker 命令，请先安装 Docker！"
    exit 1
fi

# 2. 参数解析 (严格统一变量名: NODE_ID, HOST_IP, VNC_PORT, CDP_PORT, RPA_REGISTER_URL, IMAGE, VNC_PASSWORD)
NODE_ID=""
HOST_IP=""
VNC_PORT=""
CDP_PORT=""
IMAGE=""
VNC_PASSWORD=""
ENV_RPA_REGISTER_URL="${RPA_REGISTER_URL:-}"
CLI_REGISTER_URL=""

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
                NODE_ID="$2"; shift 2 ;;
            --host-ip)
                HOST_IP="$2"; shift 2 ;;
            --vnc-port)
                VNC_PORT="$2"; shift 2 ;;
            --cdp-port)
                CDP_PORT="$2"; shift 2 ;;
            --register-url)
                CLI_REGISTER_URL="$2"; shift 2 ;;
            --image)
                IMAGE="$2"; shift 2 ;;
            --help)
                show_help
                exit 0 ;;
            *)
                echo "❌ 错误: 未知参数 '$1'，请执行 '$0 --help' 查看使用说明。"
                exit 1 ;;
        esac
    done
else
    # 传统位置参数模式兼容 (密码仅从第 6 个参数获取)
    NODE_ID="${1:-}"
    HOST_IP="${2:-}"
    VNC_PORT="${3:-}"
    CDP_PORT="${4:-}"
    IMAGE="${5:-}"
    VNC_PASSWORD="${6:-}"
fi

# 宿主机 IP 校验：必须通过命令行参数 (--host-ip) 显式指定，严禁缺省或填 127.0.0.1 / localhost
if [ -z "${HOST_IP}" ] || [ "${HOST_IP}" = "127.0.0.1" ] || [ "${HOST_IP}" = "localhost" ] || [ "${HOST_IP}" = "0.0.0.0" ]; then
    echo "❌ 错误: 必须通过 --host-ip 参数显式指定宿主机真实物理 IP (当前为: '${HOST_IP}'，禁止为空或 127.0.0.1)！"
    echo "💡 示例: ./launch_node.sh --host-ip 192.168.1.207"
    exit 1
fi

# 端口绑定推导：NGINX_GATEWAY_ENABLED 取值优先级：环境变量 (NGINX_GATEWAY_ENABLED) > 配置文件 (${ENV_FILENAME}) > 默认值 (true)
if [ -n "${NGINX_GATEWAY_ENABLED:-}" ]; then
    GW_SOURCE="环境变量"
else
    NGINX_GATEWAY_ENABLED="$(get_env_config NGINX_GATEWAY_ENABLED)"
    if [ -n "${NGINX_GATEWAY_ENABLED}" ]; then
        GW_SOURCE="配置文件 (${ENV_FILENAME})"
    else
        NGINX_GATEWAY_ENABLED="true"
        GW_SOURCE="默认值"
    fi
fi
NGINX_GATEWAY_ENABLED=$(echo "${NGINX_GATEWAY_ENABLED}" | tr '[:upper:]' '[:lower:]')

if [ "${NGINX_GATEWAY_ENABLED}" = "false" ] || [ "${NGINX_GATEWAY_ENABLED}" = "0" ]; then
    HOST_BIND="0.0.0.0"
else
    # 若指定了非 127.0.0.1 的外部 IP (如作为分布式 Worker 算力节点)，绑定 0.0.0.0 以支持跨机与宿主通信
    if [ "${HOST_IP}" != "127.0.0.1" ] && [ "${HOST_IP}" != "localhost" ]; then
        HOST_BIND="0.0.0.0"
    else
        HOST_BIND="127.0.0.1"
    fi
fi

# 算力池注册 API 地址：RPA_REGISTER_URL 取值优先级：命令行 (--register-url) > 环境变量 (RPA_REGISTER_URL) > 配置文件 (${ENV_FILENAME}) > 默认值 (http://${HOST_IP}:8000/...)
if [ -n "${CLI_REGISTER_URL}" ]; then
    RPA_REGISTER_URL="${CLI_REGISTER_URL}"
    REG_URL_SOURCE="命令行 (--register-url)"
elif [ -n "${ENV_RPA_REGISTER_URL}" ]; then
    RPA_REGISTER_URL="${ENV_RPA_REGISTER_URL}"
    REG_URL_SOURCE="环境变量 (RPA_REGISTER_URL)"
else
    RPA_REGISTER_URL="$(get_env_config RPA_REGISTER_URL)"
    if [ -n "${RPA_REGISTER_URL}" ]; then
        REG_URL_SOURCE="配置文件 (${ENV_FILENAME})"
    else
        RPA_REGISTER_URL="http://${HOST_IP}:8000/dashboard/rpa/pool/nodes/register"
        REG_URL_SOURCE="默认推导 (HOST_IP:8000)"
    fi
fi
# 若注册目标中带有内网域名 api:8000，则在独立网桥容器模式下自动静默修正为宿主直连 IP (通过 Swarm Routing Mesh 转发)
if echo "${RPA_REGISTER_URL}" | grep -q "://api:"; then
    RPA_REGISTER_URL=$(echo "${RPA_REGISTER_URL}" | sed "s|://api:|://${HOST_IP}:|")
fi

# 3. 确定 NODE_ID 及配套端口（方案 A：启动前预申请）
if [ -n "${NODE_ID}" ]; then
    # 模式 1: 命令行显式指定了 NODE_ID
    NODE_NUM=$(echo "${NODE_ID}" | grep -o -E '[0-9]+' | head -n 1 || echo "1")
    NODE_NUM=${NODE_NUM:-1}
    VNC_PORT="${VNC_PORT:-$((6080 + NODE_NUM - 1))}"
    CDP_PORT="${CDP_PORT:-$((9222 + NODE_NUM - 1))}"
    ALLOC_MODE_TEXT="手动指定"
else
    # 模式 2: 命令行未指定 NODE_ID，向后端 API 预申请下一个空闲 ID 及标准端口 (format=env)
    USER_VNC_PORT="${VNC_PORT}"
    USER_CDP_PORT="${CDP_PORT}"
    USER_HOST_IP="${HOST_IP}"
    FETCH_ENV_URL="${RPA_REGISTER_URL%/register}/fetch_node_id?format=env&host_ip=${HOST_IP}"

    FETCH_RESP=""
    if command -v curl >/dev/null 2>&1; then
        FETCH_RESP=$(curl -s -f -m 5 "${FETCH_ENV_URL}" 2>/dev/null || curl -s -f -m 5 "http://${HOST_IP}:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || curl -s -f -m 5 "http://127.0.0.1:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || true)
    elif command -v wget >/dev/null 2>&1; then
        FETCH_RESP=$(wget -qO- -T 5 "${FETCH_ENV_URL}" 2>/dev/null || wget -qO- -T 5 "http://${HOST_IP}:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || wget -qO- -T 5 "http://127.0.0.1:8000/dashboard/rpa/pool/nodes/fetch_node_id?format=env&host_ip=${HOST_IP}" 2>/dev/null || true)
    fi

    if [ -n "${FETCH_RESP}" ] && echo "${FETCH_RESP}" | grep -q "NODE_ID="; then
        # 一键载入 API 返回的 Shell 变量 (NODE_ID, VNC_PORT, CDP_PORT, HOST_IP)
        eval "${FETCH_RESP}"
        [ -n "${USER_VNC_PORT}" ] && VNC_PORT="${USER_VNC_PORT}"
        [ -n "${USER_CDP_PORT}" ] && CDP_PORT="${USER_CDP_PORT}"
        HOST_IP="${USER_HOST_IP}"
    else
        NODE_ID="chrome-1"
        VNC_PORT="${USER_VNC_PORT:-6080}"
        CDP_PORT="${USER_CDP_PORT:-9222}"
    fi

    # 提取推荐编号中的数字，与本地现有容器进行防冲突二次比对
    NODE_NUM=$(echo "${NODE_ID}" | grep -o -E '[0-9]+' | head -n 1 || echo "1")
    NODE_NUM=${NODE_NUM:-1}

    # 循环检查本地 Docker 容器，若同名容器已存在则自增递推下一个空闲节点号，避免并发启动相互覆盖
    while docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq "^caimiao-chrome-${NODE_NUM}\$|^chrome-${NODE_NUM}\$"; do
        NODE_NUM=$((NODE_NUM + 1))
        NODE_ID="chrome-${NODE_NUM}"
        VNC_PORT="${USER_VNC_PORT:-$((6080 + NODE_NUM - 1))}"
        CDP_PORT="${USER_CDP_PORT:-$((9222 + NODE_NUM - 1))}"
    done

    ALLOC_MODE_TEXT="自动分配"
fi

IMAGE="${IMAGE:-rpa-server-chrome:v0.2.7}"
CONTAINER_NAME="${NODE_ID}"
VOLUME_NAME="rpa_chrome_data_${NODE_ID}"

echo "[1/3] 📋 节点参数就绪: ID=${NODE_ID} (${ALLOC_MODE_TEXT}), IP=${HOST_IP}, VNC=${VNC_PORT}, CDP=${CDP_PORT}, 镜像=${IMAGE}"

# 4. 前置检查目标镜像是否存在于本地
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "缺少镜像: '${IMAGE}'，请先在宿主机导入 (docker load)" >&2
    exit 1
fi

# 5. 检查是否有同名旧容器在运行
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq "^${CONTAINER_NAME}\$|^${NODE_ID}\$"; then
    docker rm -f "${CONTAINER_NAME}" "${NODE_ID}" >/dev/null 2>&1 || true
fi

# 6. 执行 Docker Run 启动容器
CONTAINER_ID=$(docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --shm-size=2gb \
  --label com.docker.stack.namespace=caimiao \
  --label com.docker.swarm.service.name="caimiao_rpa_${NODE_ID}" \
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
  -e RPA_REGISTER_URL="${RPA_REGISTER_URL}" \
  -e RPA_HEARTBEAT_INTERVAL=7200 \
  -v "${VOLUME_NAME}:/tmp/chrome-profile" \
  -v /tmp:/tmp \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  --health-cmd="python3 -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:9222/json/version\", timeout=3)' || exit 1" \
  --health-interval=10s \
  --health-timeout=5s \
  --health-retries=3 \
  --health-start-period=15s \
  "${IMAGE}")

SHORT_ID=$(echo "${CONTAINER_ID}" | cut -c 1-12)
echo "[2/3] 🐳 容器已拉起: ${CONTAINER_NAME} (${SHORT_ID})"

# 6. 监听与捕获容器注册结果
TIMEOUT=30
echo "[3/3] ⏳ 等待算力池自注册确认 (最多 ${TIMEOUT}s) ..."

REGISTERED=0
CONFLICT=0
START_TIME=$(date +%s)

while [ $(( $(date +%s) - START_TIME )) -lt $TIMEOUT ]; do
    # 检查容器是否存活
    if ! docker ps -q --no-trunc 2>/dev/null | grep -q "^${CONTAINER_ID}"; then
        echo "❌ [ERROR] 容器异常退出！最新日志如下："
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

# 7. 结果判定与日志输出
if [ $CONFLICT -eq 1 ]; then
    docker rm -f "${CONTAINER_ID}" >/dev/null 2>&1 || true
    echo ""
    echo "================================================================================================================="
    echo "❌ [ERROR] 注册失败: 节点 '${NODE_ID}' 已存在于算力池，已自动停止并清理冲突容器。"
    echo "💡 建议: 请更换其他节点 ID，或不带 --node-id 参数运行以自动分配可用 ID。"
    echo "================================================================================================================="
    exit 1
fi

if [ $REGISTERED -eq 1 ]; then
    echo ""
    echo "================================================================================================================="
    echo "🎉 RPA Chrome 算力节点启动并注册成功！"
    echo "================================================================================================================="
    echo "  📌 节点标识 (NODE_ID)               : ${NODE_ID} (${ALLOC_MODE_TEXT}, 容器 ID: ${SHORT_ID})"
    echo "  📦 镜像版本 (IMAGE)                 : ${IMAGE}"
    echo "  🌐 宿主地址 (HOST_IP)               : ${HOST_IP} (端口绑定: ${HOST_BIND})"
    echo "  🛡️ 网关开关 (NGINX_GATEWAY_ENABLED) : ${NGINX_GATEWAY_ENABLED} [来源: ${GW_SOURCE}]"
    echo "  🔗 CDP 调试端点                     : http://${HOST_IP}:${CDP_PORT}"
    if [ -n "${VNC_PASSWORD}" ]; then
        echo "  🖥️ noVNC 远程桌面                   : http://${HOST_IP}:${VNC_PORT}/vnc.html (密码保护: ${VNC_PASSWORD})"
        echo "  🔑 noVNC 免密直达链接               : http://${HOST_IP}:${VNC_PORT}/vnc.html?password=${VNC_PASSWORD}"
    else
        echo "  🖥️ noVNC 远程桌面                   : http://${HOST_IP}:${VNC_PORT}/vnc.html (免密访问)"
    fi
    echo "  🤖 注册地址 (RPA_REGISTER_URL)      : ${RPA_REGISTER_URL} [来源: ${REG_URL_SOURCE}]"
    echo "================================================================================================================="
    echo "💡 实时日志查看: docker logs -f ${CONTAINER_NAME}"
    echo "================================================================================================================="
    exit 0
else
    echo ""
    echo "================================================================================================================="
    echo "❌ [ERROR] 注册超时: 容器已拉起，但在 ${TIMEOUT}s 内未检测到算力池注册成功确认。"
    echo "-----------------------------------------------------------------------------------------------------------------"
    echo "  最新容器日志如下:"
    docker logs --tail 25 "${CONTAINER_ID}" 2>&1 || true
    echo "-----------------------------------------------------------------------------------------------------------------"
    echo "💡 请执行 'docker logs -f ${CONTAINER_NAME}' 检查内部状态或网络。"
    echo "================================================================================================================="
    exit 1
fi
