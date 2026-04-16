# SimpleClaw 项目带读

## 0) 文件夹结构图与职责

```text
SimpleClaw/
├── simpleclaw/
│   ├── api/            # HTTP API + SSE 入口
│   ├── agent/          # Agent 核心（以下展开）
│   │   ├── loop.py                 # Agent 总控入口：接入消息、注册工具、调度执行
│   │   ├── scheduler.py            # 会话 mailbox 调度：同 tenant:session 串行
│   │   ├── turn_processor.py       # 单轮主流程：构建消息、LLM 调用、工具迭代
│   │   ├── tool_execution_guard.py # 工具调用守卫：校验、超时、错误兜底、defer
│   │   ├── context.py              # Prompt 装配：系统提示、历史、记忆、skills
│   │   ├── context_budget.py       # 上下文预算切分策略（history/summary/memory）
│   │   ├── session_summary.py      # 会话摘要去噪与滚动合并
│   │   ├── memory.py               # 记忆整合策略（consolidation）
│   │   ├── memory_store.py         # 长期记忆与历史归档存储抽象
│   │   ├── structured_memory.py    # 结构化记忆提取并写入 USER/SOUL/HEARTBEAT
│   │   ├── postprocess.py          # 延迟写后处理（异步执行 deferred actions）
│   │   ├── runtime.py              # TenantRuntime 管理与租户隔离
│   │   ├── subagent.py             # 子代理任务管理与并发控制
│   │   ├── turn_commit.py          # 回合结果落库与裁剪规则
│   │   ├── turn_effects.py         # 回合后副作用调度（postprocess/structured memory）
│   │   ├── turn_utils.py           # 回合无状态辅助函数
│   │   ├── skills.py               # Skills 扫描与加载
│   │   ├── __init__.py             # 对外导出 Agent 公共接口
│   │   └── tools/                  # 可被模型调用的工具集合
│   │       ├── base.py             # 工具基类
│   │       ├── registry.py         # 工具注册与 schema 暴露
│   │       ├── filesystem.py       # 文件读写改查
│   │       ├── shell.py            # Shell 执行
│   │       ├── web.py              # 搜索与抓取
│   │       ├── message.py          # 主动发消息
│   │       ├── cron.py             # 定时任务工具
│   │       ├── spawn.py            # 子代理 spawn
│   │       ├── mcp.py              # MCP 工具桥接
│   │       └── __init__.py         # tools 模块导出
│   ├── runtime/        # 多角色装配（chat-api / scheduler / workers）
│   ├── cron/           # 定时任务
│   ├── heartbeat/      # 周期触发
│   ├── storage/        # MySQL/Redis 存储实现
│   ├── session/        # 会话管理
│   ├── channels/       # Telegram/Slack/Feishu 等渠道适配
│   └── config/         # 配置模型与加载
├── tests/              # 回归与行为测试
├── bridge/             # Node 侧 WhatsApp 桥接
├── api-test.html       # API/SSE 可视化调试页
├── README.md           # 架构与启动说明
└── pyproject.toml      # 依赖与 CLI 入口（sclaw）
```

关键入口：
- API 入口：`simpleclaw/api/server.py:330`
- Agent 直调：`simpleclaw/agent/loop.py:573`
- 单轮处理：`simpleclaw/agent/turn_processor.py:468`
- 会话调度：`simpleclaw/agent/scheduler.py:28`
- 运行时装配：`simpleclaw/runtime/bootstrap.py:326`

## 1) 核心流程带读（示例 A：API 流式对话）

真实请求样例（仓库已有）：

```json
{
  "message": "你好",
  "tenant_key": "tenant-demo",
  "session_key": "main:tenant-demo",
  "conversation_id": "main:tenant-demo",
  "message_id": "msg-001",
  "images": ["https://example.com/demo.jpg"],
  "debug_trace": true
}
```

数据流向：
1. `/turn/stream` 入口解析并补默认 tenant/session/chat（`simpleclaw/api/server.py:449`）。
2. API 层调用 `agent_loop.process_direct(...)`（`simpleclaw/api/server.py:518`）。
3. `process_direct` 构造 `InboundMessage`，写入 `_stream_text/_debug_trace` 等元数据（`simpleclaw/agent/loop.py:595`）。
4. 进入按 `tenant:session` 串行的 mailbox 调度，保证同会话有序（`simpleclaw/agent/scheduler.py:57`）。
5. `TurnProcessor.process_message` 组装 prompt/history、跑工具迭代、得到最终回复（`simpleclaw/agent/turn_processor.py:610`）。
6. SSE 按事件发出 `accepted/text_delta/final`，调试时附加 `debug_prompt/debug_timing`（`simpleclaw/api/server.py:563`）。

输出落点：
- 当前请求最终回复由 `/turn/stream` 返回。
- 异步后台消息可通过 `/events/stream` 推送（`simpleclaw/api/server.py:643`）。

人工总结:
一个信息进来, 先补齐metadata, 然后就构造为统一处理的 InboundMessage 格式,
封装为 mailbox 串行调度, 以 tenant 区分会话, 然后走 Prompt 拼接, 工具调用流程


## 2) 核心流程带读（示例 B：结构化记忆分支）

真实对话样例（测试里）：
- 用户输入：`以后你叫小美，我叫老大，每两小时提醒我喝水。`（`tests/test_structured_memory.py:116`）
- 期望提取：`assistant_alias / preferred_address / recurring_reminder`（`tests/test_structured_memory.py:121`）

数据流向：
1. 主回复完成后，调度结构化记忆任务（`simpleclaw/agent/turn_processor.py:681`）。
2. `StructuredMemoryManager.schedule` 异步入队（`simpleclaw/agent/structured_memory.py:156`）。
3. 提取器用严格 JSON 规则从本轮对话抽取稳定信息（`simpleclaw/agent/structured_memory.py:265`）。
4. 应用到 `MEMORY.md` + 租户 `USER.md/SOUL.md/HEARTBEAT.md`（`simpleclaw/agent/structured_memory.py:391`）。
5. 若识别到周期任务，会同步 heartbeat 间隔（`simpleclaw/agent/structured_memory.py:527`）。

输出落点：
- 长期记忆与租户文档更新。
- heartbeat interval 可能从默认值被更新到更快节奏（测试示例为 7200 秒，见 `tests/test_structured_memory.py:167`）。

人为解读: memory 是整体调度层, memory_store 是存储MEMORY.md+HISTORY.md
而 structured_memory, 会存储稳定事实, 存储 MEMORY.md、USER.md、SOUL.md、HEARTBEAT.md,
有 heartbeat 事件的话, 也会在这里处理
## 3) 核心 API 请求文档

接口：
- `POST /turn`：同步返回最终回复（`simpleclaw/api/server.py:614`）
- `POST /turn/stream`：SSE 流式返回（`simpleclaw/api/server.py:626`）
- `GET /events/stream`：后台消息流（cron/heartbeat/debug_trace）（`simpleclaw/api/server.py:643`）

常用字段：
- 必填：`message`（兼容 `content/text`，见 `simpleclaw/api/server.py:408`）
- 可选：`tenant_key`、`session_key`、`conversation_id`、`message_id`、`images`、`debug_trace`

`curl` 示例（流式）：

```bash
curl -N -X POST http://127.0.0.1:18790/turn/stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": "你好",
    "tenant_key": "tenant-a",
    "session_key": "main:tenant-a",
    "conversation_id": "main:tenant-a",
    "debug_trace": true
  }'
```

## 4) 最简配置与启动命令

最小配置（先跑通）：

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "moonshot/kimi-k2.5"
    }
  },
  "runtime": {
    "mysql": { "enabled": false },
    "redis": { "enabled": false }
  }
}
```

最少命令：
1. `pip install -e .`
2. `sclaw onboard`
3. 配置 `~/.simpleclaw/config.json`
4. 启动 API：`sclaw api --host 127.0.0.1 --port 18791`

说明：
- `sclaw api` 默认端口是 `18791`（`simpleclaw/cli/commands.py:534`）。
- `api-test.html` 默认连的是 `18790`（`api-test.html:349`），需要改页面 base URL 或用 `sclaw serve chat-api --port 18790`。
