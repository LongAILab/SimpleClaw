<div align="center">
  <h1>🐈 simpleclaw</h1>
  <p>面向长期陪伴场景的多租户 AI 助手内核</p>
</div>

`simpleclaw` 是一个面向多租户陪伴式 Agent 的框架：

- 以 `SOUL / USER / HEARTBEAT / MEMORY` 为核心上下文层
- 以 MySQL 为中心存储租户动态数据
- 同时支持主会话、定时任务、heartbeat、postprocess、structured memory 等后台链路
- 提供可观测的 prompt/debug/timing 调试能力

当前项目的产品人格默认是美容陪伴助手 `魔镜`，但底层架构并不绑定单一人设，可以继续扩展到更多陪伴型或任务型 Agent 场景。

## 核心能力
- 多租户上下文装配：共享 `base` 层 + 租户覆盖层 + Session Summary + Memory。
- MySQL 持久化：租户文档、会话、记忆、租户状态、cron 等统一入库。
- 定时任务体系：精确定时用 `cron`，周期性陪伴/追踪用 `heartbeat`。
- 异步后台写入：支持 deferred postprocess 与 structured memory。
- Prompt 可观测：可以查看真实 system prompt、history 选择结果、budget、timing。
- 多运行时角色：`chat-api`、`scheduler-service`、`postprocess-worker`、`background-worker`。
- CLI / HTTP API / SSE 调试页面三种使用方式。

## 架构概览
当前代码的主要分层如下：

1. 入口层  
   `simpleclaw/cli`、`simpleclaw/api` 负责本地命令、HTTP API 与 SSE 调试。
2. Agent 层  
   `simpleclaw/agent` 负责 prompt 构建、turn processing、tool 编排、memory consolidation、structured memory。
3. Runtime 层  
   `simpleclaw/runtime` 负责多服务装配、任务队列、worker 执行、租户运行时。
4. 业务调度层  
   `simpleclaw/cron`、`simpleclaw/heartbeat` 负责任务触发与调度。
5. 存储层  
   `simpleclaw/storage`、`simpleclaw/session`、`simpleclaw/tenant` 负责 MySQL / Redis / 会话 / 状态持久化。

## 提示词层次
主会话 prompt 当前大致按以下顺序装配：

1. 共享基础提示  
   `AGENTS.md`、`SOUL.md`、`USER.md`、`TOOLS.md`
2. 租户覆盖层  
   `SOUL.md` / `USER.md` / `HEARTBEAT.md` 的租户版本
3. Session Summary  
   用于稳定压缩较长对话
4. Memory  
   持久化的长期用户信息、偏好、任务状态
5. Skills 摘要
6. 最近历史窗口  
   按 budget 控制注入长度

## 存储模型
当前仓库已经将租户动态数据从磁盘工作区迁移到数据库侧。核心数据包括：

- 租户文档：`SOUL.md`、`USER.md`、`TOOLS.md`、`HEARTBEAT.md`
- 会话与消息
- 长期记忆快照与记忆事件
- 租户状态
- cron 任务

目标是把"用户相关的持续状态"放进统一数据层，而不是散落在本地文件夹里。

## 本地开发环境
推荐环境：

- Python `>= 3.11`
- MySQL `>= 8`
- Redis `>= 7`

安装依赖：

```bash
git clone <repo-url>
cd simpleclaw
pip install -e .[dev]
```

如果你只想先跑最小环境，也可以：

```bash
pip install -e .
```

## 快速开始
### 1. 初始化配置
```bash
sclaw onboard
```

默认会生成用户级配置和工作区。  
如果你想用自定义配置文件，也可以后续通过 `-c /path/to/config.json` 指定。

### 2. 配置模型与后端
最少需要配置模型提供方；如果要使用多租户能力，建议同时配置 MySQL 与 Redis。

示例：

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
    "mysql": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 3306,
      "user": "root",
      "password": "",
      "database": "simpleclaw"
    },
    "redis": {
      "enabled": true,
      "url": "redis://127.0.0.1:6379/0"
    }
  }
}
```

### 3. 启动方式
#### 方式 A：直接本地聊天
```bash
sclaw agent
```

#### 方式 B：启动 HTTP API
```bash
sclaw api --host 127.0.0.1 --port 18790
```

#### 方式 C：按多服务角色启动
```bash
sclaw serve chat-api
sclaw serve scheduler-service
sclaw serve postprocess-worker
sclaw serve background-worker
```

#### 方式 D：开发态一键编排
```bash
sclaw dev-up
sclaw dev-status
sclaw dev-down
```

## 调试与可观测
仓库自带一个前端调试页：

- `api-test.html`

配合 SSE 调试事件，可以看到：

- `debug_prompt`：真实拼接后的 prompt
- `debug_trace`：异步写入、结构化记忆、后台任务日志
- `debug_timing`：首 token、总耗时、模型等待耗时拆分

如果你在排查"为什么回复慢"，当前推荐优先看：

- `Prompt Observability`
- `debug_timing`

它们可以把"prompt 过大"与"模型上游波动"区分开。

## 常用命令
```bash
sclaw onboard
sclaw agent
sclaw api
sclaw serve chat-api
sclaw serve scheduler-service
sclaw serve postprocess-worker
sclaw serve background-worker
sclaw dev-up
sclaw dev-down
sclaw dev-status
sclaw migrate-runtime-state
```

## 目录说明
```text
simpleclaw/
├── agent/        # prompt、turn、tools、memory、subagent
├── api/          # HTTP API 与 SSE 调试输出
├── bus/          # 消息总线
├── cli/          # CLI 命令入口
├── config/       # 配置模型与路径
├── cron/         # 精确定时任务
├── heartbeat/    # 周期性陪伴调度
├── runtime/      # 多服务装配、任务队列、worker
├── session/      # 会话与主会话路由
├── storage/      # MySQL / Redis 存储实现
└── tenant/       # 租户状态
tests/            # 测试
bridge/           # 渠道桥接相关资源
```

## License
MIT
