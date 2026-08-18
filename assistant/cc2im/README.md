# Claude Code → 飞书 实时监控

VSCode 扩展 / CLI 里的 Claude Code 运行时，把会话状态、最新输出、以及「需要人工确认」的信息，通过飞书机器人实时推送到你的飞书终端。

- 以 **session 为单位**：一个 Claude Code 会话 = 一张飞书卡片，输出以**可折叠面板**累积，卡片原地更新（PATCH），不刷屏。
- 需要人工确认时：卡片切到橙色「⏳ 等待确认」状态，展示 **✅ 允许 / ❌ 拒绝** 按钮；在飞书里点击即可远程决策。
- **AskUserQuestion（多选项 + 其他输入）**：Claude 调 AskUserQuestion 时，卡片直接渲染每个选项为可点按钮，底部附带「✍️ 其他」输入框（`form` 表单）——点选项或输入文本提交后，答案经 hook 的 `updatedInput.answers` 回填给 Claude Code。
- 远程批准默认**有界等待**：决策窗口（默认 60s）内飞书无响应 → **自动允许**，绝不阻塞 Claude 挂死。

所有代码在 `assistant/cc2im/` 目录下。

---

## 架构

```
Claude Code (VSCode / CLI)
   │  hooks：SessionStart / UserPromptSubmit / Notification / Stop / PermissionRequest / SessionEnd
   ▼  （每个事件 = 一个独立快进程）
cc2im/cc_hooks.py
   │  - 转发事件 → POST http://127.0.0.1:8790/hook
   │  - Stop 秒回 approve；PermissionRequest 长轮询 /decision/{id}
   ▼
cc2im/cc_service.py ─── 统一常驻进程（FastAPI，仅监听 127.0.0.1:8790）
   ├─ /hook         聚合事件，按 session 分组，去抖/节流、滑动窗口后攒批 PATCH 卡片
   ├─ /decision/{id} 长轮询：进程内 asyncio.Future，按钮点击即时 resolve，超时自动 allow
   ├─ /health
   └─ lifespan 并行挂载 Lark WebSocket（cc2im/feishu_ws.py）
         └─ CC 卡片按钮(带 cc 标记) → 进程内 resolve 决策（零跨进程通信）
   ▼
cc2im/cc_card.py + cc_feishu.py → 飞书交互卡片 → 你的飞书终端
```

## 文件清单

| 文件 | 职责 |
|---|---|
| `cc_service.py` | 统一常驻进程：FastAPI(127.0.0.1:8790) + lifespan 启动飞书 WS |
| `feishu_ws.py` | Lark WebSocket 网关（独立后台线程），卡片按钮回调桥回主循环 |
| `cc_hooks.py` | Hook 入口：转发事件、Stop 秒回 approve、PermissionRequest 长轮询 |
| `cc_state.py` | Session 状态机：面板/状态、去抖节流、滑动窗口、决策 broker（超时自动允许） |
| `cc_card.py` | 飞书卡片 v2 构建：header 状态色 + 折叠面板 + 按钮区 |
| `cc_transcript.py` | 读 `transcript_path`（JSONL），提取最新 assistant 文本 + 工具调用 |
| `cc_feishu.py` | tenant_access_token 缓存、发卡(interactive)、PATCH 原地更新 |
| `cc_config.py` | 配置加载（config.json，可被 `CC_CONFIG` 环境变量覆盖） |
| `cc_install_hooks.py` | 安装/卸载 hooks 到 `~/.claude/settings.json`（用当前 venv python 绝对路径） |
| `config.json` | 端口、飞书 app_id/secret、target_open_id、决策窗口、卡片参数 |
| `CC_MONITOR_PLAN.md` | 设计 Plan（v2，已评审） |

## 快速开始

### 1. 配置

编辑 `config.json`（真实凭据请放本地 `config.local.json`，用 `CC_CONFIG` 环境变量指定，不提交到 git）：

```jsonc
{
  "enabled": true,
  "listen_host": "127.0.0.1",
  "listen_port": 8790,
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "target_chat_id": "oc_xxx",     // 机器人所在群的 chat_id
    "target_open_id": "",            // 预留：按人推送时填 open_id
    "domain": "feishu.cn"            // 国际版飞书填 "larksuite.com"
  },
  "card": {
    "max_panels": 12,                // 单卡最多面板数，超出压缩成摘要
    "window_lines": 30,              // 每个面板只保留最新 30 行
    "debounce_sec": 2.5,             // 事件去抖：静默 2.5s 后批量 PATCH
    "max_batch_wait_sec": 5.0,       // 最长攒批等待
    "min_patch_interval_sec": 2.0,   // PATCH 最小间隔，避免打爆频控
    "data_dir": "~/.agent-assist"    // 会话↔卡片映射文件目录（持久化续接）
  },
  "decision": {
    "timeout_sec": 60                // 远程批准等待窗口，超时自动允许
  }
}
```

### 2. 启动常驻进程（先启动）

双击 `assistant/cc2im/start_cc_monitor.bat`（自动检测端口占用、从 `assistant/` 目录启动并写日志），或在 `assistant/cc2im/` 下带子命令运行：

```bat
start_cc_monitor.bat            运行在前台（Ctrl+C 停止，日志同时写 local\_cc_service.log）
start_cc_monitor.bat bg         后台运行（最小化窗口，日志写 local\_cc_service.log）
start_cc_monitor.bat status     查看状态 + /health 健康检查
start_cc_monitor.bat stop       停止服务（结束占用 8790 端口的进程）
start_cc_monitor.bat restart    重启（先 stop 再前台运行）
```

手动启动：

```bash
cd assistant
D:/anaconda3/envs/main/python.exe -m cc2im.cc_service
```

> 必须先有飞书凭据且应用已配置「以应用身份发消息 / 更新消息」+ 卡片回调订阅（`card.action.trigger`）。现有机器人一般已具备。
> 报 `[Errno 10048] ... bind ... 8790` 表示服务已在运行（端口被占用），不要再起第二个实例；重启前先结束占用 8790 的 python 进程。

### 3. 安装 hooks

```bash
cd assistant
D:/anaconda3/envs/main/python.exe -m cc2im.cc_install_hooks install
```

写入 `~/.claude/settings.json`（保留你已有的 permissions/theme/其他 hook），命令使用**当前 venv 的 python 绝对路径**。

> 如果 Claude Code 已在运行，装完 hooks 后需**重启**（CLI 重启 / VSCode 重启扩展）。

### 4. 验证

正常跑一个 Claude Code 任务 → 飞书机器人单聊/群里收到一张「🤖 Claude Code · <项目名>」卡片，随会话推进原地累积更新；遇到确认时卡片变橙色带 ✅/❌ 按钮。

## 卸载

```bash
cd assistant
D:/anaconda3/envs/main/python.exe -m cc2im.cc_install_hooks uninstall
```

## 配置项说明

- 卡片频率防护：`debounce_sec`（攒批静默）、`min_patch_interval_sec`（节流）、`max_messages_per_card`（单卡消息条数上限）、`window_lines`/`max_panels`（滑动窗口/压缩）。
- 决策窗口 `decision.timeout_sec`：PermissionRequest 的 hook 超时会自动设为 窗口+30s，长轮询不会被 SIGTERM。

## 排障

| 现象 | 检查 |
|---|---|
| 没收到卡片 | `cc_service` 日志；`curl http://127.0.0.1:8790/health`；config 里 `target_chat_id` |
| hook 不触发 | `~/.claude/settings.json` 里 hooks 必须是**扁平格式** `hooks.{事件}`（`{matcher, hooks:[...]}` 包裹）；旧版 `hooks.commands.{事件}` 会被引擎静默忽略（debug 显示 `Found 0 total hooks in registry`）；改完需重启 Claude Code / VSCode |
| 卡片按钮点了没反应 | 飞书应用是否订阅 `card.action.trigger`；`feishu_ws.py` 日志 |
| AskUserQuestion 卡发不出去 | 表单容器 tag 必须是 `form`（`form_container` 会报 `not support tag`）；按钮用 `form_action_type: "submit"` |
| 选了选项但 Claude 没收到答案 | PermissionRequest 的 `updatedInput.answers` 需要 questions **原样透传**、键为问题全文；若该版本 PermissionRequest 不透传，改为 PreToolUse hook（matcher `tool_name == "AskUserQuestion"`） |
| 超时自动允许未生效 | `decision.timeout_sec` 是否被改小；hook 的 `timeout` 是否 > 窗口 |
| VSCode 里 Stop/Notification 偶发不触发 | 已知扩展 bug，先 CLI 验证；必要时给 `cc_service` 加 transcript 轮询兜底 |
| 飞书 WS 连不上 | 网络/SSL 问题查 `cc_service` 日志；国际版改 `domain: "larksuite.com"` |

## 里程碑

- **M1 链路跑通**：hooks 装好 → 常驻进程起 → CLI 跑真实任务 → 飞书收卡片并累积 → 确认时橙色 + ✅/❌ → 点击生效 / 不点超时自动允许。
- **M2 VSCode 验证**：VSCode 里各事件触发；如 Stop 偶发不触发，加 transcript 轮询兜底。
- **M3 打磨**：滑动窗口/压缩/滚动调优、多权限聚合、节流参数调优。
