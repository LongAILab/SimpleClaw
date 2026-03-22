# 当前改动记录

## 1. 这次做了什么

### 1.1 调度模型升级
- 已将原来的 `per-session lock` 过渡实现，升级为显式的 `session mailbox / worker` 模型。
- 现在消息处理路径变成：
  - `MessageBus inbound`
  - 按 `routing_key = tenant + session` 路由
  - 进入对应 session 的 `mailbox`
  - 由该 session 的 `worker` 串行消费

### 1.2 统一 direct / API / 后台入口的调度语义
- `process_direct()` 已接入同一套 mailbox 调度。
- 这意味着 `CLI direct / API / cron / heartbeat` 这类 direct turn，不再绕开主调度层。

### 1.3 停止逻辑更清晰
- `/stop` 现在会同时处理：
  - 当前 session 正在执行的任务
  - 当前 session mailbox 中尚未消费的排队消息
  - 当前 session 下挂的 subagent

## 2. 代码拆分成了什么样

### 2.1 `nanobot/agent/loop.py`
职责：
- 作为总入口和装配层
- 初始化 tools
- 初始化 `TenantRuntimeManager`
- 初始化 `SessionScheduler`
- 初始化 `TurnProcessor`
- 处理 `run / stop / restart / process_direct`

当前原则：
- `loop.py` 尽量不承载具体业务细节
- 更多是“接线”和“调用”

### 2.2 `nanobot/agent/runtime.py`
职责：
- 定义 `TenantRuntime`
- 管理 tenant 级 runtime 的创建与缓存
- 把以下 tenant-scoped 组件收拢到一起：
  - `workspace`
  - `context`
  - `sessions`
  - `subagents`
  - `memory_consolidator`

这样后面看多租户隔离相关逻辑时，不需要回到 `loop.py` 里找。

### 2.3 `nanobot/agent/scheduler.py`
职责：
- 定义 `SessionEnvelope`
- 管理 `session_mailboxes`
- 管理 `session_workers`
- 管理 active tasks
- 管理 session 级取消

这里就是纯调度层，后面如果继续做：
- queue length
- priority
- backlog policy
- stuck turn detect
- fairness

优先应该继续在这里扩展，而不是再往 `loop.py` 塞。

### 2.4 `nanobot/agent/turn_processor.py`
职责：
- 单轮 turn 的实际处理
- tool context 注入
- LLM/tool loop
- slash command
- session 保存
- progress 回传

这里就是“处理一轮消息”的核心逻辑，后续如果做：
- follow-up candidate
- side effects emit
- foreground / background split

优先应该继续在这里演进。

## 3. 为什么这样拆

### 3.1 之前的问题
之前的问题不是功能不能用，而是职责混在一起：
- 调度逻辑在 `loop.py`
- runtime 构建在 `loop.py`
- 单轮处理在 `loop.py`
- stop/cancel 在 `loop.py`
- direct turn 入口也在 `loop.py`

时间一长会出现几个问题：
- 文件越来越大
- 很难快速定位问题属于“调度层”还是“turn 处理层”
- 后续改 heartbeat / follow-up / event orchestration 时容易继续堆逻辑
- 多人维护时更容易互相影响

### 3.2 现在的目标
现在的拆分目标是：
- `loop.py` 负责装配
- `scheduler.py` 负责调度
- `runtime.py` 负责 tenant runtime
- `turn_processor.py` 负责单轮处理

这样以后扩展时，改动边界会更清楚。

## 4. 这样拆会不会额外浪费运行时间

### 4.1 结论
- 会有极小的额外 Python 调用开销
- 但这个开销相对整个 agent turn 来说，基本可以忽略

### 4.2 为什么基本可以忽略
一次 agent turn 的主要耗时通常在：
- LLM 请求
- 工具执行
- 文件 IO
- 网络 IO
- shell 执行

而这次拆分新增的成本主要只是：
- 多几层 Python 函数调用
- 多几个对象之间的方法转发

这类开销通常是微秒到毫秒级，和大模型调用、工具调用相比非常小。

### 4.3 反而更可能带来的收益
虽然有极小的函数调用开销，但拆分后的收益通常更大：
- 更容易定位瓶颈
- 更容易单独优化调度层
- 更容易给 scheduler 增加指标
- 更容易避免后续因为结构混乱带来的性能回归

也就是说：
- 从“CPU 指令数”看，理论上略多
- 从“真实系统维护与演进成本”看，整体更优

## 5. 当前状态一句话总结

- 调度模型已经从 `per-session lock` 升级为显式 `session mailbox / worker`
- 代码结构已经从“单文件混合实现”拆成了“装配层 / 调度层 / runtime 层 / turn 处理层”
- 当前没有明显性能损失，后续维护性明显更好

## 6. cron 当前状态

### 6.1 当前结论
- cron 主体能力已经基本完成。
- 现在它已经不是“单实例本地闹钟 + 直接跑 job”的临时实现，而是更接近：
  - job 仓储
  - job 调度器
  - 单次执行器
  - 可选通知用户

### 6.2 cron 现在的工作方式
- 用户在主会话里创建 cron job。
- 创建时会记录：
  - `tenant_key`
  - 投递目标 `channel/chat_id`
  - `origin_session_key`
  - `deliver`
  - `execution_policy`
- 真正触发执行时，仍然默认进入隔离执行会话：
  - `cron:{job.id}`

这意味着：
- cron 来源于主会话
- 但默认不会执行在主会话里
- 不会污染用户主聊天上下文

### 6.3 这轮新增的关键实现
- `nanobot/cron/executor.py`
  - 继续负责单次 cron turn 的统一执行
  - 统一处理 execution session key 和是否通知用户

- `nanobot/cron/repository.py`
  - 新增 cron 仓储层
  - 把 `jobs.json` 的读写、下次触发时间计算、状态回写抽离出来

- `nanobot/cron/scheduler.py`
  - 新增 cron 调度层
  - 负责轮询 due jobs
  - 负责 claim/lease
  - 避免多实例下同一 job 被重复执行

- `nanobot/cron/service.py`
  - 现在更像 facade
  - 对外保留旧 API
  - 内部委托给 `repository + scheduler`

### 6.4 当前关键语义
- `deliver` 已经是显式参数，不再硬编码。
- `execution_policy` 已经成为显式字段，当前支持：
  - `isolated-per-job`
  - `isolated-per-run`
  - `reuse-origin-session`
- `agents.cron` 已经进入配置层，可在 `config.json` 中单独配置：
  - `model`
  - `provider`
  - `temperature`
  - `maxToolIterations`
  - `executionPolicy`
  - `deliverDefault`

### 6.5 当前还没有完成的部分
- 还没有更友好的管理界面
- 还没有更完整的 cron 运营指标和可观测信息
- 当前 claim/lease 还是文件版仓储，后面更适合切到数据库

### 6.6 当前对 cron 的判断
- cron 的核心目标已经完成：
  - 隔离主会话
  - 支持多租户
  - 支持多实例下的去重执行基础语义
- 所以后面 cron 更多是增强项，而不是主体能力缺失

## 7. heartbeat 当前状态

### 7.1 当前结论
- heartbeat 主体能力也已经基本完成。
- 它现在不再是“单个 heartbeat 服务 + 配置一个 `targetTenantKey` 去唤醒某个租户”的模式。
- 现在已经升级成：
  - tenant state 仓储
  - heartbeat 调度器
  - 按租户扫描 due heartbeat
  - 对每个租户单独做 `skip / defer / run`

### 7.2 heartbeat 和 cron 的定位差异
- `cron` 更像：
  - 按 job 调度
  - 后台任务执行器
  - 独立执行链路

- `heartbeat` 更像：
  - 按 tenant 调度
  - 主会话轻量推进器
  - 周期性唤醒主会话
  - 决定当前是 `skip / defer / run`

### 7.3 这轮新增的关键实现
- `nanobot/tenant/state.py`
  - 新增 `TenantStateRepository`
  - 统一维护：
    - `tenant -> primary_session`
    - `last_user_activity_at_ms`
    - heartbeat 调度状态
    - `next_run_at_ms`
    - `last_run_at_ms`
    - `last_status`

- `nanobot/runtime/leases.py`
  - 新增统一 lease 仓储
  - heartbeat 和 cron 都复用这一层做跨进程 claim

- `nanobot/heartbeat/scheduler.py`
  - 新增多租户 heartbeat 调度器
  - 会扫描 due tenants
  - 逐个租户检查：
    - 有没有主会话
    - 最近是否刚活跃
    - 主 session 是否 busy
  - 然后决定：
    - `skip`
    - `defer`
    - `run`

- `nanobot/agent/loop.py`
  - 真实用户消息进来时会刷新租户主会话
  - turn 执行时会写入 `session-busy` lease
  - 给 heartbeat 提供跨实例可见的 busy 信息

- `nanobot/cli/commands.py`
  - gateway 已不再只做“选一个目标 heartbeat session”
  - 现在是装配：
    - `TenantStateRepository`
    - `LeaseRepository`
    - `HeartbeatScheduler`
    - heartbeat decider agent

### 7.4 heartbeat 现在的工作方式
- gateway 启动 heartbeat scheduler
- scheduler 定期扫描租户状态，找出到期的 tenant
- 对每个 tenant：
  - 读取该租户的 `primary_session`
  - 检查最近用户活跃时间
  - 检查 `session-busy` lease
  - 如果不适合推进，则 `defer`
  - 如果适合推进，则在主会话上下文里做 `skip/run` 判断
  - 如果判断为 `run`，再把 heartbeat context 注入 prompt，推进主会话一轮

### 7.5 当前 heartbeat 已完成的能力
- `skip/run` 判断复用主会话上下文
- `HEARTBEAT.md` 通过 system prompt section 注入
- 主 session 忙时不会硬插队，而是走 `defer`
- heartbeat 已经具备多租户调度能力，不再是单租户目标模式
- heartbeat 已经具备多实例下的基础去重语义

### 7.6 当前还没有完成的部分
- 当前租户状态和 lease 仍然是文件仓储，稳态上更适合数据库化
- heartbeat 规则内容 `HEARTBEAT.md` 目前仍是实例级共享文件，不是 tenant 级配置
- heartbeat 还没有和 future `follow-up / background event` 抽成统一后台编排框架

### 7.7 当前对 heartbeat 的判断
- heartbeat 主体已经基本完成。
- 后续重点不再是“它能不能跑”，而是：
  - 数据库化
  - 可观测性
  - 与 follow-up 的统一调度

## 8. subagent 当前状态

### 8.1 当前已经完成的部分
- subagent 已经跟随 tenant/session 作用域运行。
- 当前已经补了：
  - `tenant_key`
  - `session_key`
  - session 级取消
  - 并发上限控制

### 8.2 当前对 subagent 的判断
- subagent 现在已经脱离“几乎等于没有”的状态。
- 但它仍然更偏：
  - session 内的后台协作单元
  - 而不是完整独立的长期后台工作流框架

### 8.3 当前还缺什么
- 缺少更清晰的 subagent 生命周期状态展示
- 缺少更完整的结果汇总 / 中间态观测
- 还没有和未来 `follow-up / event orchestration` 统一成一套后台框架

## 9. follow-up 当前状态

### 9.1 当前结论
- follow-up 还没有真正开始做。
- 目前更多只是方向明确，还没有正式实现。

### 9.2 当前更合理的定位
- follow-up 不应该只是一个普通 tool。
- 更适合是：
  - 租户级后台编排能力
  - 可以被 heartbeat / 后台事件 / 会后处理共同驱动

### 9.3 现在还缺的核心部分
- follow-up 数据结构
- follow-up 仓储
- due follow-up 调度器
- 执行后的 ack / retry / cancel 语义
- 与主会话、heartbeat、memory、notification 的协作边界

## 10. 当前总判断

### 10.1 基本完成
- cron：基本完成
- heartbeat：基本完成

### 10.2 已有基础，但还可继续增强
- subagent：已有基础，但还不是完整后台框架

### 10.3 还未正式开始
- follow-up：基本还没做

### 10.4 下一步更值得做什么
- 如果继续补稳态能力，优先做：
  - 把 tenant state / lease / cron repository 切到数据库后端
  - 给 cron / heartbeat / subagent 增加更明确的可观测性
  - 开始设计并落地 follow-up 的仓储和调度器

## 11. 并发与容量控制补充判断

### 11.1 当前需要明确区分的两件事
- `tenant/session` 隔离解决的是：
  - 上下文正确性
  - 顺序一致性
  - 不同租户之间不串数据
- 但它不自动解决：
  - 大量并发请求时的资源保护
  - LLM/tool/IO 的全局限流
  - 瞬时高峰的容量控制

所以：
- **租户隔离 != 系统能扛住高并发**

### 11.2 cron 并发的当前判断
- 当前 cron 已经完成：
  - 多租户隔离
  - 主会话隔离
  - 多实例 claim/lease 去重
- 这轮又补了：
  - `CronScheduler` 现在会把 due jobs 并发提交执行
  - 通过 `max_concurrency` 控制 cron lane 的并发上限
  - 不再是单实例里逐个 job 顺序拉起

也就是说：
- 10 点所有 cron 可以“同时进入待执行状态”
- 真正执行由 cron lane 的 worker pool 控制并发上限

### 11.3 heartbeat 并发的当前判断
- 当前 heartbeat 已经比 cron 更接近 worker-pool 思路：
  - `HeartbeatScheduler` 会扫描 due tenants
  - 然后按 `maxConcurrency` 并发处理多个 tenant
- 这轮又补了：
  - heartbeat 初始调度已经支持稳定 `stagger/jitter`
  - 不同 tenant 不再更容易整齐对齐到同一时刻
- 所以 heartbeat 现在已经具备：
  - `tenant state`
  - `due tenant scan`
  - `worker pool`
  - `stagger/jitter`
  - `multi-instance`

- 后续真正还缺的，不再是 heartbeat 基础并发，而是：
  - 更强的 observability
  - 数据库化状态仓储
  - 和 follow-up 的统一后台编排

### 11.4 前台用户消息其实也有同样的问题
- 这点必须明确：
  - **不只是 cron / heartbeat 需要并发治理**
  - **前台用户同时发消息也一样需要**

- 当前前台消息已经有：
  - `tenant + session mailbox`
  - 每个 session 串行 worker
  - `main lane` 并发上限
  - `main lane backlog` 上限
  - `main lane per-tenant` 并发配额
- 这解决了：
  - 同一会话内顺序执行
  - 不同会话互不串上下文
  - 前台消息不会再无限制并发把进程直接打满
  - 热点 tenant 不会轻易长期占满全部 main lane

- 但当前还没有真正补上的，是：
  - 前台高峰时的 backpressure
  - LLM/tool 的全局容量保护

也就是说，如果很多用户像 cron 一样在同一时刻都发消息：
- 当前语义上可以并发
- 但缺少全局容量治理
- 理论上仍然可能把模型、工具、进程资源打满

### 11.5 前台消息是否也需要 worker pool
- 结论：
  - **已经开始做了**

- 当前已经具备：
  - `ingress queue`
  - `per-session mailbox`
  - `main lane` worker pool
  - backlog 上限触发的最小 busy backpressure
  - `per-tenant` 公平并发配额

这个模型的目标是：
- 同一 session 继续保持串行
- 不同 session 可以并发
- 但并发总量受控
- 突发高峰时可以限流、排队、降级

### 11.6 前台和后台是否适用同一种 worker pool
- 不建议完全共用一个无差别 worker pool。
- 更合理的是：
  - `main lane`
    - 服务用户实时消息
  - `cron lane`
    - 服务 cron 执行
  - `heartbeat lane`
    - 服务 heartbeat 推进
  - `subagent lane`
    - 服务 subagent 并发

原因是：
- 如果完全共用一个池，后台任务很容易挤占前台实时对话
- 如果按 lane 拆开，又能更清楚地做：
  - 优先级
  - 并发配额
  - 限流
  - 降级

### 11.7 当前最值得补的并发治理方向
- `foreground messages`
  - 在现有 `session mailbox + main lane` 基础上继续补更强的 backpressure / fairness 策略
- `shared limits`
  - 给 LLM / tools / outbound delivery 增加统一限流和配额控制

### 11.8 当前总判断
- 多租户正确性的第一步已经完成：
  - tenant/session 隔离
  - cron 隔离主会话
  - heartbeat 按 tenant 调度
- 多租户容量治理的第二步也已经开始：
  - `main / cron / heartbeat / subagent` lane 并发上限
  - heartbeat `stagger/jitter`
  - main lane 最小 backpressure / per-tenant fairness
- 但多租户容量治理还没有完全做完。
- 后续要真正扛住大规模用户，必须补：
  - 更完整的 backpressure
  - 更完整的 fairness
  - 数据库化状态仓储
  - 更完整的限流和可观测性
