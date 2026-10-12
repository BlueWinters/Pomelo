#!/bin/bash
set -e

# ==============================================================================
# 启动编排：Xvfb + Fluxbox + x11vnc + noVNC/websockify + Chrome + CDP + rpa_server
# ==============================================================================

# 1. 残留与锁文件清理 (防止容器异常退出或重启后留下 stale lock)
mkdir -p /run/dbus && rm -f /run/dbus/pid
dbus-daemon --system --fork
rm -f /tmp/chrome-profile/Singleton*
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
# 预创建 Chrome 默认下载目录及共享临时下载目录并赋权，防止无头下载因路径不存在而丢弃
mkdir -p /root/Downloads /tmp/downloads
chmod 777 /root/Downloads /tmp/downloads

# 2. 启动 Xvfb 虚拟显示屏幕 (默认 1920x1080 24位真彩色)
echo "[entrypoint] 🖥️ Starting Xvfb on display ${DISPLAY:-:99} (${RESOLUTION:-1920x1080x24})..."
Xvfb "${DISPLAY:-:99}" -screen 0 "${RESOLUTION:-1920x1080x24}" -ac +extension GLX +render -noreset &
sleep 1

# 3. 启动轻量级窗口管理器 Fluxbox (保证 Chrome 弹窗、窗口聚焦与最大化正常)
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

# 5. 启动 websockify (noVNC HTML5 Web 界面代理)
echo "[entrypoint] 🌐 Starting noVNC websockify on port ${NOVNC_PORT:-6080}..."
websockify --web /usr/share/novnc "${NOVNC_PORT:-6080}" "localhost:${VNC_PORT:-5900}" &

# 6. 启动 Dashboard 运维服务 (会话文件浏览 + 快照接口，9224 端口)
python3 /usr/local/bin/rpa_server.py \
    --port "${RPA_FILE_PORT:-9224}" \
    --cdp-port "${CHROME_CDP_PORT:-9229}" \
    --root "${RPA_FILE_ROOT:-/tmp/.sessions}" \
    --token "${RPA_FILE_TOKEN:-}" &

# 7. 启动 CDP 转发层 (面向 Worker 的 9222 端口双向透传)
python3 /usr/local/bin/cdp_relay.py \
    --listen-port "${CDP_RELAY_PORT:-9222}" \
    --target-port "${CHROME_CDP_PORT:-9229}" &

# 8. 启动 Google Chrome (在 X11 虚拟桌面上有头运行，完整呈现 GUI 界面与弹窗，同时开启 CDP 调试端口)
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

# 9. 启动 RPA 算力池自注册守护 Agent (前置探活 + 注册 + 心跳 + 优雅下线)
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
