# Claude Code → 飞书 实时监控 Plan (v2)

> 目标：VSCode 扩展 / CLI 里的 Claude Code 在运行时，把任务状态、最新输出、以及「需要人工确认」的信息，通过飞书机器人实时推到自己的飞书终端。
> 要求：所有代码在 `assistant/cc2im/` 下；以 session 为单位，一张卡片一个 session，卡片原地更新，输出以可折叠面板累积；需要确认时展示并更新按钮。

---

## 1. 总体架构（统一常驻进程，v2 修订）

```
Claude Code (VSCode 扩展 / CLI)
   │  hooks：SessionStart / UserPromptSubmit / Notification / Stop / PermissionRequest / SessionEnd
   ▼  (每个事件 = 一个独立快进程)
cc2im/cc_hooks.py
   │  - 读 stdin JSON → POST http://127.0.0.1:8790/hook → 秒回 approve（不阻塞 Claude）
   │  - PermissionRequest：POST 拿 decision_id → GET /decision/{id} 长轮询等待决策
   ▼
cc2im/cc_service.py —— 统一常驻进程（FastAPI，仅监听 127.0.0.1:8790）
   ├─ /hook        聚合事件，按 session_id 分组，去抖/节流、滑动窗口后攒批 PATCH
   ├─ /decision/{id}  长轮询：进程内等 asyncio.Future，按钮点击即时 resolve，超时自动 allow
   ├─ /card_action  (可选，供调试) 卡片按钮回调
   ├─ /health
   └─ lifespan 启动时并行挂载 Lark WebSocket（cc2im/feishu_ws.py，内聚原 feishu_robot.py 能力）
         ├─ CC 卡片按钮(带 cc 标记) → 进程内 resolve 对应 Future（零跨进程通信）
         └─ 业务事件 → 原样转发 http://127.0.0.1:8000（保持现有业务流程不变）
   ▼
cc2im/cc_card.py + cc_feishu.py → 飞书交互卡片（发首卡 + PATCH 原地更新）→ 用户飞书终端
```

**常驻进程只有一个**：`cc_service.py`。原 `feishu_robot.py` 的 WebSocket 能力并入其中（长连接只能单实例，避免双进程抢连），业务事件转发逻辑原样保留。

---

## 2. 事件 → 行为映射

| Hook 事件 | 触发时机 | 行为 |
|---|---|---|
| `SessionStart` | 会话开始/恢复/`/clear` | 建 session 状态，发首张卡片「🟢 任务开始」 |
| `UserPromptSubmit` | 用户每次回车 | 追加折叠面板「👤 用户请求 <prompt>」 |
| `Stop` | Claude 完成一轮回答 | 读 transcript 尾部 → 追加折叠面板「🤖 回复摘要 + 🔧 工具调用列表」→ 攒批 PATCH 卡片 |
| `PermissionRequest` | 权限弹窗"将要弹出"时 | **远程批准**：发卡片 ✅允许/❌拒绝 → hook 长轮询等待 → 按钮点击进程内 resolve → 返回 allow/deny；**超时（默认 60s）未收到 → 自动 allow** |
| `Notification`(matcher=`permission_prompt`) | Claude 要人工确认 | 卡片状态改「⏳ 等待人工确认」+ 按钮（PermissionRequest 的冗余信号，防漏） |
| `Notification`(matcher=`idle_prompt`) | 本轮结束，等用户输入 | 状态改「✅ 已空闲」 |
| `SessionEnd` | 会话结束 | 状态改「🏁 已结束」，面板全部收起 |

- `Stop` hook **阻塞回合结束** → `cc_hooks.py` 立即输出 `{"decision":"approve"}`（`stop_hook_active=true` 时同样 approve，绝不二次 block）。
- `PermissionRequest` 的 hook 命令超时必须 **> 决策窗口**（installer 自动把 timeout 设为 窗口+余量，默认 60s 窗口 → timeout 90s），避免长轮询被 hook 超时 SIGTERM。
- 冗余：`Notification permission_prompt` 已知 bug 可能缺 `notification_type` → 聚合服务 fallback 检查 message 内容含 "permission"。
- 任何异常都不得影响 Claude Code 本身（hook 静默失败、快速退出）。

---

## 3. 文件清单（全部在 `assistant/cc2im/` 下）

| 文件 | 职责 |
|---|---|
| `cc_service.py` | **统一常驻进程**：FastAPI(127.0.0.1:8790)，lifespan 并行启动 Lark WS；`/hook` `/decision/{id}` `/health` |
| `feishu_ws.py` | Lark 长连接客户端 + 事件分发（内聚原 feishu_robot.py）：CC 卡片按钮进程内 resolve Future；业务事件转发 8000 |
| `cc_hooks.py` | Hook 入口：转发事件、Stop 秒回 approve、PermissionRequest 长轮询决策；异常静默 |
| `cc_state.py` | Session 状态机：面板/状态、**去抖节流**、**滑动窗口**、卡片滚动/压缩策略 |
| `cc_card.py` | 飞书卡片 v2 构建：header 状态色 + 折叠面板 + 按钮区 |
| `cc_transcript.py` | 读 `transcript_path`（JSONL），提取最新 assistant 文本 + tool_use 名称 |
| `cc_feishu.py` | tenant_access_token 缓存、发卡(interactive)、PATCH 更新卡片 |
| `config.json` | 端口、飞书 app_id/secret、target_open_id、决策窗口、卡片窗口参数 |
| `cc_install_hooks.py` | 写/卸载 hooks 到 `~/.claude/settings.json`；**command 用 `sys.executable` 绝对路径**（当前 venv 的 python）；保留已有配置合并 |
| `feishu_robot.py` | **已移除**：功能并入 `feishu_ws.py`，历史文件不再保留 |
| `README.md` | 启动步骤、排障 |

> 飞书凭据：默认复用原 `feishu_robot.py` 的 `APP_ID / APP_SECRET`，放入 `config.json`（可覆盖），避免 argparse import 副作用。

---

## 4. 卡片设计（一张 session 一张卡片，原地更新）

```
┌──────────────────────────────────────────┐
│ 🤖 Claude Code · caimiao_backend     [状态色] │
│   ⏳ 等待人工确认 · session xxxx · 步骤 3/…   │
├──────────────────────────────────────────┤
│ 🗂 cwd · 开始 14:30:05 · 已更新 14:35:12     │
│ ─────────────────────────────────────    │
│ ▾ 🕐14:30 · 👤 用户请求                     │  ← collapsible_panel
│ ▸ 🕐14:31 · 🤖 Claude 回复 + 🔧 Bash/Write  │  ← collapsible_panel
│ ▸ 🕐14:35 · ⏳ 请求允许执行 <tool>           │  ← 待确认 panel（最新默认展开）
│ ─────────────────────────────────────    │
│ [ ✅ 允许 ]  [ ❌ 拒绝 ]   [ ⏸ 去终端处理 ]     │  ← 仅待确认时出现
└──────────────────────────────────────────┘
```

<a href="./card.html" target="_blank" style="display: inline-block; padding: 8px 16px; background-color: #0969da; color: #ffffff; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: bold;">
  🚀 点击在新标签页预览 HTML 页面
</a>

- 状态色：运行=blue、待确认=orange、空闲/结束=green、错误=red。
- 每个 `Stop` 追加一个 `collapsible_panel`（默认折叠，最新一条展开）：时间戳 + 回复摘要 + 工具调用列表。
- **滑动窗口**：每面板内容仅保留最新 N 行关键输出/操作摘要（默认 30，可配）；面板总数上限 `max_panels=12`，超出把最旧面板压缩成「📦 更早 N 步」摘要。
- 待确认 panel 附上 `tool_name + tool_input` 摘要，让用户知道要批什么。
- 按钮 `value` 带标记 `{cc:1, session_id, decision_id, action}`，`feishu_ws.py` 据此进程内识别并 resolve。

---

## 5. 关键技术约束与对策（含 review 修订）

| 约束 / 问题 | 对策 |
|---|---|
| `Stop` hook 阻塞回合，默认超时 600s | `cc_hooks.py` 先转发再秒回 `{"decision":"approve"}` |
| **收到 update 就 PATCH 会打爆频控** | **攒批节流**：更新先入待刷新池，2~3s 静默后批量 PATCH 一次；同时 PATCH 最小间隔 ≥2s，最长攒批等待 5s 兜底 |
| **卡片无上限追加全量日志会超 30KB / 编辑次数上限** | **滑动窗口**：面板内容只留最新 N 行（默认 30）；面板数 ≤12；旧面板压缩为摘要；PATCH 次数超限 → 旧卡标记结束、同 session 滚动新卡 |
| 双进程抢连 Lark 长连接 / 卡片回调跨进程转发复杂 | **合并为一个统一常驻进程** `cc_service.py`；卡片按钮在进程内用 `asyncio.Future` resolve，hook 长轮询零延迟解阻塞 |
| hook 长轮询被 hook 命令超时 SIGTERM | installer 把 PermissionRequest hook `timeout` 设为 决策窗口+余量（默认 60s 窗口 → 90s 超时） |
| **VSCode 扩展触发 hooks 有已知 bug**（Stop/Notification 偶发不触发） | 先 CLI 测通再测 VSCode；兜底：`cc_service.py` 定时轮询 transcript 文件增量 |
| 远程批准 `permissionDecision:"allow"` 在部分版本不抑制原生弹窗（bug #52822） | 需实测；失败则降级为「去终端处理」提示 |
| 超时自动 allow = 终端权限超时后放行 | 决策窗口可配；对敏感操作生效，介意可后续按工具类型调整（matcher 区分） |
| 一个 session 连续多个权限请求 | 决策 token 逐一处理，待批准区展示「最近 1 条 + 计数」，避免刷屏 |
| **Hook 调用的 Python 环境硬编码风险** | `cc_install_hooks.py` 用 `sys.executable`（当前 venv 的 python 绝对路径，Windows 下为 `venv\Scripts\python.exe`）写入 command |
| 需要用户 open_id 才能发消息 | config.json 手动填（已确认） |

---

## 6. 已确认决策

- **A. 服务形态（v2 修订）**：**统一常驻进程** `python -m cc2im.cc_service`，内聚 Lark WebSocket（合并原 `cc_aggregator.py` + `feishu_robot.py`）；业务事件仍转发主 FastAPI:8000，不影响现有业务。
- **B. 远程批准**：启用；`PermissionRequest` hook 长轮询决策（默认 60s），**超时未收到则自动 allow**；卡片按钮回调在进程内 `asyncio.Future` 零延迟解阻塞。
- **C. 接收人**：config.json 手动填 `target_open_id`（先飞书单聊机器人拿 open_id）。
- **D. Hook 安装范围**：默认用户级 `~/.claude/settings.json`（覆盖 CLI + VSCode + 所有项目）。
- **E. 卡片频率/容量（v2 新增）**：攒批节流 2~3s + 滑动窗口（每面板最新 30 行、面板 ≤12）。

---

## 7. 回环测试所需环境

1. **飞书客户端**（手机/桌面/Web 任选）：登录自己账号，**单聊给机器人发条消息** → 拿 `open_id`（出现在 `feishu_ws.py` 的日志/print 里），并测试按钮点击回环。
2. **统一常驻进程**：`python -m cc2im.cc_service`（代替 feishu_robot.py；旧 `feishu_robot.py` 已移除，勿再运行，避免双实例抢连）。
3. **主业务 FastAPI**：`uvicorn app.main:app`（端口 8000）保持运行，`cc_service.py` 会把业务事件转发过去。
4. **Claude Code**：CLI 先测，再测 VSCode 扩展；装完 hooks 后**重启 VSCode**。
5. **飞书应用权限**：确认已开「以应用身份发消息 / 更新消息」+ `card.action.trigger` 事件订阅（现有机器人在用，大概率已具备）。
6. **依赖**：`fastapi / uvicorn / httpx / loguru / lark-oapi` 均在现有依赖或 `[feishu]` 可选依赖，无新增。

---

## 8. 里程碑

- **M1 跑通链路**：hooks 装好（venv python 绝对路径）→ 统一进程起 → CLI 跑一个真实任务 → 飞书收卡片并原地累积（节流后 PATCH）→ 触发确认时卡片橙色 + ✅/❌ 按钮 → 飞书点击生效；不点则超时自动 allow。
- **M2 VSCode 验证 + 兜底**：VSCode 里验证各事件触发；如 Stop 偶发不触发，启用 transcript 轮询兜底。
- **M3 打磨**：滑动窗口/压缩/滚动调优、多权限聚合、节流参数调优。
