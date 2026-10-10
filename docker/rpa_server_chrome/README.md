# 财喵 RPA Chrome 弹性算力集群模块

本目录包含了财喵系统（CaiMiao Backend）中 **RPA 浏览器弹性算力节点** 的完整容器化镜像构建、服务编排、自注册守护 Agent、声明式拓扑配置与解析工具及单节点/集群调度脚本。

---

## 目录文件清单与组件职责

| 文件 / 脚本 | 类型 | 职责说明 |
| :--- | :--- | :--- |
| **`rpa_nodes.yml`** | 配置文件 | **算力集群声明式拓扑配置**。分为 `base`（全局基础缺省配置）与 `nodes`（节点独立配置列表）。 |
| **`parse_config.py`** | 辅助工具 | **拓扑配置独立解析脚本**。负责读取 `rpa_nodes.yml`、完成默认值继承与校验，并输出标准 CLI 参数行供 `command.sh` 调用。 |
| **`launch_node.sh`** | 调度驱动 | **单节点启动与热扩容工具**。支持 7 个标准长选项（`--vncpwd`、`--node-id`、`--host-ip`、`--vnc-port`、`--cdp-port`、`--image`、`--help`）及自适应申请。 |
| **`entrypoint.sh`** | 容器入口 | **容器内部进程初始化总控**。负责拉起并守护 Xvfb 虚拟屏幕、Fluxbox 窗口管理器、x11vnc 抓屏、noVNC 网页代理、Google Chrome、CDP 代理及 NodeAgent。 |
| **`node_agent.py`** | 守护进程 | **算力池自注册与心跳守护 Agent**。负责本地 Chrome 探活、自动向后端 API 注册入池、定时发送心跳保活，以及监听退出信号注销下线。 |
| **`cdp_relay.py`** | 网络中间件 | **CDP 远程调试反向代理**。运行于容器 9222 端口，提供高效、双向透明的 WebSocket / HTTP CDP 调试流量中继。 |
| **`rpa_server.py`** | 运维服务 | **节点运维与快照管理服务**。运行于容器 9224 端口，提供会话隔离目录浏览、会话文件下载及实时截图接口。 |
| **`Dockerfile`** | 镜像构建 | 构建 `rpa-server-chrome:v0.2.4` 基础镜像，集成 X11 图形环境、Chrome 浏览器及 Python 运行依赖。 |
| **`google-chrome-stable_current_amd64.deb`** | 离线安装包 | Google Chrome 官方 Linux 64 位离线安装包，保证容器构建过程零外网依赖。 |

---

## 核心架构与调度联动机制

```text
       ┌──────────────────────────────────────────────────────────┐
       │                rpa_nodes.yml (声明式拓扑)                 │
       │                                                          │
       │  ┌──────────────────────┐      ┌──────────────────────┐  │
       │  │  base (基础缺省配置)  │      │  nodes (节点列表)    │  │
       │  │  - host_ip           │      │  - node_id           │  │
       │  │  - image             │      │  - host_ip           │  │
       │  │  - vncpwd            │      │  - vnc_port          │  │
       │  │  - resolution        │      │  - cdp_port          │  │
       │  │  - base_vnc/cdp_port │      │  - vncpwd / image    │  │
       │  └──────────┬───────────┘      └──────────┬───────────┘  │
       └─────────────┼─────────────────────────────┼──────────────┘
                     │                             │
                     └──────────────┬──────────────┘
                                    ▼
       ┌──────────────────────────────────────────────────────────┐
       │           parse_config.py (拓扑独立解析器)               │
       │  1. 合并 base 缺省值与各 node 独立配置                    │
       │  2. 过滤 enabled: true 节点并输出标准 CLI 长参数行        │
       └────────────────────────────┬─────────────────────────────┘
                                    │ 解析结果 (逐行标准命令行参数)
                                    ▼
       ┌──────────────────────────────────────────────────────────┐
       │                 command.sh (集群总控)                     │
       │  - start : 启动 Compose 主干服务 -> 批量驱动 launch_node  │
       │  - stop  : 一键清理全量算力节点 -> 关闭 Compose 主干服务   │
       └────────────────────────────┬─────────────────────────────┘
                                    │ 组装参数依次调用 (--node-id, --host-ip, --vncpwd ...)
                                    ▼
       ┌──────────────────────────────────────────────────────────┐
       │             launch_node.sh (单节点驱动/热扩容)            │
       │  1. 端口与 ID 防碰撞检测 (缺省则自动向 API 预申请)        │
       │  2. docker run 启动 Chrome 算力容器并自注册入池          │
       └────────────────────────────┬─────────────────────────────┘
                                    │ 启动并注册
                                    ▼
       ┌──────────────────────────────────────────────────────────┐
       │           caimiao-chrome-1 / caimiao-chrome-2...         │
       │   Xvfb + Fluxbox + x11vnc + noVNC + Chrome CDP + Agent   │
       └──────────────────────────────────────────────────────────┘
```

---

## 配置文件说明 (`rpa_nodes.yml`)

### 1. 配置项映射关系

| 配置层级 | 字段名 | 对应控制层 | 说明与继承逻辑 |
| :--- | :--- | :--- | :--- |
| **`base`** | `host_ip` | `parse_config.py` | 默认宿主机公开 IP，供 Dashboard 与节点访问 |
| | `image` | `parse_config.py` | 默认 Chrome 节点 Docker 镜像版本（如 `rpa-server-chrome:v0.2.4`） |
| | `vncpwd` | `parse_config.py` | 全局默认 VNC 访问密码（留空为免密；子节点未指定时继承） |
| | `resolution` | `parse_config.py` | 默认虚拟桌面分辨率（如 `1920x1080x24`） |
| | `base_vnc_port` | `parse_config.py` | 默认 VNC 动态申请起始端口（`6080`） |
| | `base_cdp_port` | `parse_config.py` | 默认 CDP 调试动态申请起始端口（`9222`） |
| **`nodes`** | `node_id` | `launch_node.sh --node-id` | 节点唯一标识（留空则由节点自动向 API 预申请空闲 ID） |
| | `host_ip` | `launch_node.sh --host-ip` | 该节点宿主机 IP（缺省自动继承 `base.host_ip`） |
| | `vnc_port` | `launch_node.sh --vnc-port` | 该节点 noVNC 宿主端口（缺省自适应分配 `6080+`） |
| | `cdp_port` | `launch_node.sh --cdp-port` | 该节点 CDP 调试宿主端口（缺省自适应分配 `9222+`） |
| | `vncpwd` | `launch_node.sh --vncpwd` | 该节点专属 VNC 访问密码（缺省继承 `base.vncpwd`） |
| | `image` | `launch_node.sh --image` | 该节点专属镜像版本（缺省继承 `base.image`） |
| | `enabled` | `parse_config.py` | 是否拉起（`true`: 立即拉起; `false`: 声明保留暂不拉起） |

### 2. 完整配置示例

```yaml
base:
  host_ip: "192.168.1.207"
  image: "rpa-server-chrome:v0.2.4"
  vncpwd: ""
  resolution: "1920x1080x24"
  base_vnc_port: 6080
  base_cdp_port: 9222

nodes:
  - node_id: "chrome-1"
    host_ip: "192.168.1.207"
    vnc_port: 6080
    cdp_port: 9222
    vncpwd: ""                      # 免密访问
    image: "rpa-server-chrome:v0.2.4"
    enabled: true

  - node_id: "chrome-2"
    host_ip: "192.168.1.207"
    vnc_port: 6081
    cdp_port: 9223
    vncpwd: ""                      # 免密访问
    image: "rpa-server-chrome:v0.2.4"
    enabled: true

  # 远端分布式节点（支持不同 IP、独立端口与专属密码）
  - node_id: "chrome-3"
    host_ip: "192.168.1.208"
    vnc_port: 6080
    cdp_port: 9222
    vncpwd: "SecurePassNode3"       # 独立配置访问密码
    image: "rpa-server-chrome:v0.2.4"
    enabled: false                  # 预留节点，暂不拉起
```

---

## 使用指南

### 1. 集群生命周期总控 (`./command.sh`)

`command.sh` 位于项目根目录，专注于整个后端集群的一键启停，支持传入自定义 YAML 配置文件：

```bash
# 1. 默认启动：拉起 Compose 服务，并使用默认的 docker/chrome/rpa_nodes.yml 批量启动算力节点
./command.sh start

# 2. 自定义配置启动：指定特定的 YAML 拓扑配置文件
./command.sh start my_nodes.yml
./command.sh start /path/to/custom_nodes.yml

# 3. 停止系统：一键安全下线所有 Chrome 算力容器，并关闭 Compose 基础设施服务
./command.sh stop
```

---

### 2. 单节点启动与热扩容 (`./docker/chrome/launch_node.sh`)

用于单独拉起、动态扩容或故障单点恢复：

```bash
# 示例 1: 自动向后端 API 预申请下一个空闲 ID 与端口，免密拉起（最常用热扩容方式）
./docker/chrome/launch_node.sh

# 示例 2: 启动一个带密码保护的自适应节点
./docker/chrome/launch_node.sh --vncpwd "MySecurePassword"

# 示例 3: 完全指定参数启动 (指定节点 ID、宿主 IP、端口与密码)
./docker/chrome/launch_node.sh --node-id chrome-3 --host-ip 192.168.1.208 --vnc-port 6080 --cdp-port 9222 --vncpwd "Node3Pass"
```

---

### 3. 拓扑解析工具使用 (`./docker/chrome/parse_config.py`)

```bash
# 读取默认配置文件，输出所有启用的节点启动参数行
python3 docker/chrome/parse_config.py

# 指定配置文件路径
python3 docker/chrome/parse_config.py --config docker/chrome/rpa_nodes.yml
```

### 4. git bash 远程启动
```bash
cat docker/chrome/launch_node.sh | ssh ranger@192.168.1.207 "tr -d '\r' | RPA_REGISTER_URL='http://192.168.1.207:8000/dashboard/rpa/pool/nodes/register' bash -s -- --host-ip 192.168.1.207 --image rpa-server-chrome:v0.2.5"
```


---

## 运维管理入口

- **算力池运维大盘**：`http://<宿主机IP>:8000/dashboard/rpa_pool.html`
- **远程人工接管工作台**：`http://<宿主机IP>:8000/dashboard/rpa_takeover.html`
- **noVNC 远程桌面直连**：`http://<宿主机IP>:6080/vnc.html`（免输密直连：`http://<宿主机IP>:6080/vnc.html?password=<密码>`）
- **CDP 调试探活**：`http://<宿主机IP>:9222/json/version`
- **Dozzle 容器实时日志**：`http://<宿主机IP>:8080`
- **OpenAPI 接口文档**：`http://<宿主机IP>:8000/docs`
