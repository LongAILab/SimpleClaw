# 多租户与并发当前进度

## 1. 当前已经做到的事情

### 1.1 运行与接入
- 已经完成本地运行环境初始化，项目内实例使用 `nanobot/.nanobot/`。
- 已经补充一个最小 HTTP API 入口，可以通过 `nanobot api -c ".../.nanobot/config.json"` 启动。
- 这个 API 目前支持最小测试能力：
  - `GET /`
  - `GET /health`
  - `GET /turn`
  - `POST /turn`

### 1.2 多租户基础边界
- 已经给 `InboundMessage` 增加 `tenant_key`。
- 已经引入 `effective_tenant_key` 和 `routing_key`，使调度不再只依赖 `session_key`。
- 已经开始支持 tenant workspace，目标路径为：
  - `workspace/tenants/<tenant>/`
- 已经让 `SessionManager` 支持 tenant-aware 的会话读写与枚举。
- 已经让 `cron/subagent` 开始保留 `tenant_key/session_key`。

### 1.3 并发基础改造
- 主消息处理已经不再是“全局收消息后直接 create_task + per-session lock”。
- 现在已经改成：
  - 全局 `MessageBus inbound`
  - 按 `routing_key = tenant + session` 路由
  - 每个 session 一个显式 `mailbox`
  - 每个 session 一个显式 `worker`
  - worker 串行消费本 session 的消息
- `process_direct()` 也已经接入同一套 mailbox 模型，所以 `API / cron / heartbeat / direct turn` 不再绕开主调度语义。
- `/stop` 现在会同时处理：
  - 当前正在执行的 session turn
  - 该 session mailbox 中尚未消费的排队消息
  - 该 session 下挂的 subagent
- session worker 已经具备基础空闲回收能力，空闲一段时间后会自动退出。
- 出站发送已经从单一全局发送器改成：
  - 全局分发
  - 每个 channel 一个 sender worker
- `message/spawn/cron/filesystem/exec` 工具上下文已经从共享实例字段，调整为更偏 task-local 的上下文使用方式。
- `subagent` 已经增加基础并发上限 `Semaphore(4)`，避免无限并发扩张。

## 2. 现在的调度模型是什么

### 2.1 当前实现
当前实现现在更准确地说是：

- `MessageBus` 仍然是一个全局 `asyncio.Queue`
- 消息先进入全局 `inbound`
- `AgentLoop.run()` 从全局队列消费消息
- `AgentLoop` 按 `routing_key` 把消息路由到 session mailbox
- `dict[routing_key, asyncio.Queue]` 承担 session mailbox
- `dict[routing_key, worker task]` 承担 session worker
- 同一 session 的消息由同一个 worker 顺序消费
- 不同 session 的 worker 可以并行推进
- worker 空闲后会回收，不会永远挂着

也就是说，当前模型是：

**全局总线做 ingress，session mailbox / worker 做真正的顺序执行**

### 2.2 和完整 actor/mailbox 还差什么
一个更完整的 session actor/mailbox 架构，通常会是这样：

1. 先按 `tenant + session` 做路由
2. 每个 session 有自己的 mailbox（队列）
3. 每个 session 有自己的 worker / actor
4. 新消息只进入这个 session 的 mailbox
5. 这个 worker 串行消费自己的消息
6. 不同 session 之间天然并行

它更像：

- Session A -> Queue A -> Worker A
- Session B -> Queue B -> Worker B
- Session C -> Queue C -> Worker C

而我们现在已经做到其中一半以上，但还没走到“生产级 actor/mailbox”。

### 2.3 当前 mailbox 模型和完整 actor/mailbox 的区别

#### 当前 mailbox 模型
- queue 是显式存在的
- worker 是显式存在的
- 消息先入 queue，再由 worker 消费
- 同一 session 真正拥有独立排队语义
- 但 queue 仍然是进程内内存对象
- 还没有独立的 mailbox 指标、优先级、持久化、恢复机制

#### 更完整的 actor/mailbox
- session 是一等调度单元
- mailbox 不只是内存结构，通常还会配套更完整的生命周期和观测能力
- 可以做积压监控、优先级、限流、丢弃策略、重试策略
- 甚至可以扩展到跨进程 / 多实例调度

一句话总结：

**现在已经不是“用锁模拟串行”了，而是“已经做成了 session mailbox / worker”，只是还不是生产级 actor system。**

## 3. 当前实现的价值

虽然现在还不是完整 actor/mailbox，但这一步依然非常有价值，因为它已经把最核心的顺序执行模型做实了：

### 3.1 比原来更好了
- 不再是所有用户共享一把全局处理锁
- 不再是“每条消息独立起 task 再抢锁”
- 不同 session 已经可以并行推进
- 同一 session 已经由 mailbox + worker 保证顺序
- direct / API / cron / heartbeat 的调度语义开始统一
- `/stop` 有了更准确的 session 级取消边界
- 多租户的基础字段已经进入消息模型
- 工具上下文不再那么容易在并发时串路由

### 3.2 为什么这一步合理
如果一开始就直接上“持久化消息层 + 多实例 actor system”，会连带改很多层：
- bus
- agent loop
- stop/restart
- subagent 回流
- cron/heartbeat 调度
- 生命周期管理
- 监控指标

所以目前这一步可以理解为：

**先把单进程内的 session mailbox / worker 跑稳，再进一步把它演化成更强的 actor/mailbox。**

## 4. 当前还欠缺什么

### 4.1 `MessageBus` 仍然是内存全局队列
目前还是：
- 进程内
- 无持久化
- 无背压
- 无幂等
- 无重试恢复

所以距离真正生产级还差一层更强的消息基础设施。

### 4.2 mailbox 还缺少观测与治理
现在缺的是：
- queue length
- oldest message age
- average turn latency
- worker idle / busy 状态
- mailbox backlog 告警

### 4.3 worker 生命周期仍然比较轻量
当前虽然已经有：
- worker 创建
- worker 空闲销毁

但还没有：
- worker 数量控制
- 更完整的回收策略
- 异常 worker 诊断
- stuck turn 检测

### 4.4 还没有调度优先级系统
未来需要区分不同类型任务优先级：
- interactive user turn
- subagent summary
- cron
- heartbeat
- follow-up event

当前还没有做成统一优先级调度。

### 4.5 还没有可靠消息层语义
现在还没有：
- ack
- retry
- dead-letter
- exactly-once / at-least-once 这类投递语义

所以当前更适合“单进程服务化 + 逐步增强”，还不适合直接当成强可靠消息系统。

## 5. 当前对“session actor/mailbox”最准确的理解

可以这样理解：

### 已完成
- `tenant_key` 已经引入
- `session_key` 已经保留
- `routing_key = tenant + session` 已经具备
- 显式 mailbox 已经存在
- 显式 session worker 已经存在
- per-session 串行已经由 worker 保证
- direct / bus 都已经进入同一调度模型

### 未完成
- queue 仍然主要是进程内对象，不是可靠消息层
- 还没有完善的指标、优先级、重试与恢复
- 还没有跨进程 / 多实例的一致调度能力

所以当前状态不是：

**完整 session actor/mailbox**

而是：

**向生产级 session actor/mailbox 演进过程中的中间态：in-memory session mailbox / worker + tenant-scoped runtime**

## 6. 下一阶段最值得做的事情

如果“多租户能力”是当前最高优先级，那后面最值得继续补的就是下面这些。

### P0：统一后台任务调度入口
- `cron`
- `heartbeat`
- `subagent result`
- `follow-up event`

这些都应该进入同一调度体系，而不是不同入口各自直接推进。

### P1：补 session / tenant 级指标
- queue length
- oldest message age
- average turn latency
- active workers
- running subagents

### P2：补可靠消息与治理能力
- stop timeout
- stuck turn detect
- mailbox backlog policy
- priority / fairness

### P3：再往更生产级方向推进
- 持久化消息队列
- 幂等键
- 背压
- 优先级
- 重试

## 7. 当前阶段总结

如果一句话总结我们当前的位置：

**我们已经从“per-session lock 的过渡态”推进到了“单进程内显式 session mailbox / worker”的阶段，但离生产级 actor/mailbox 还有可靠消息层、指标与治理能力的差距。**

这个阶段的意义是：

- 架构方向已经对了
- 基础边界已经开始建立
- 调度模型的核心骨架已经成型
- 但消息层和治理层还不够强

后面最关键的一步，不是继续加更多业务能力，而是：

**把 mailbox 之上的后台事件流、指标和可靠性补起来。**

## 8. 关于“workspace 是否一定要放本地文件系统”

### 8.1 当前新的思考
经过讨论，一个新的方向是：

- 不一定要把所有 workspace 状态继续放在本地磁盘目录中
- 很多状态本质上只是字段、结构化数据、文本片段
- 这些内容完全可以放进 MySQL
- 然后通过数据库的增删改查能力去管理它们

这个方向我**部分认可，而且我认为是有价值的**，但需要把概念拆清楚。

### 8.2 我认可的部分
如果你的目标是多租户、后端服务化、未来可能有很多用户，那么把下面这些内容迁到数据库，是很合理的：

- tenant / user profile
- conversation metadata
- session metadata
- long-term memory
- short-term task state
- follow-up events
- cron / reminder / event jobs
- user state summary
- 心跳和主动触达策略相关状态

这些东西本质上都更像：

- 结构化状态
- 可查询状态
- 需要事务与索引
- 需要按 tenant/user/session 维度检索

它们天然比本地 `md/jsonl` 更适合放数据库。

### 8.3 我不完全认可的部分
我不建议把“workspace”这个词直接等同于“MySQL 里的某些字段”。

原因是现在 `nanobot` 里的 workspace 不只是“存东西的地方”，它同时承担了：

- bootstrap 文件
- memory 文件
- sessions 文件
- skills 目录
- file tools 的相对路径基准
- shell tools 的工作目录
- 子代理可见的工作空间

所以如果一句话说“workspace 全部存进 MySQL”，这里会有一个语义问题：

**数据库可以很好地承载“状态”，但未必天然承载“文件工作区语义”。**

### 8.4 更准确的理解
我更推荐把这件事拆成两层：

#### A. Agent State Store（数据库）
把真正的业务状态放进去：

- tenant
- user
- session metadata
- memory
- follow-up events
- jobs
- user state

#### B. Execution Workspace（文件或虚拟工作区）
只保留那些真的需要“文件系统语义”的内容：

- tool 读写文件
- shell 执行脚本
- 技能脚本工作目录
- 代码生成 / 文件编辑类任务

如果未来你的 agent 大多数时候并不需要真实文件操作，那么这个 execution workspace 可以非常薄，甚至按需创建。

### 8.5 因此我的判断
我的判断不是：

**“workspace 不能进数据库”**

而是：

**“不要把所有状态都继续绑在文件 workspace 上；应该把大部分状态迁到数据库，只把真正需要文件语义的部分保留为 execution workspace。”**

也就是说，后续更合理的模型是：

- `tenant_id` 决定数据归属
- 数据库存主状态
- 文件工作区只在必要时存在

这对多租户系统更自然，也更方便后端服务化。

## 9. 关于“主链路和后处理链路分离”

### 9.1 当前新的思考
一个比较优的策略是把系统拆成两段：

#### 主链路
- 用户和 agent 聊天
- 快速产出主回复
- 必要时输出一些参数/事件

#### 后处理链路
- 异步处理 event follow-up
- memory 存储
- 用户状态更新
- reminder / cron / 后台任务

这个思路我**整体认可，而且我认为这是正确方向。**

### 9.2 为什么这个思路是对的
因为陪伴型 agent 不应该把所有事情都塞进“回复用户这一次 turn”里。

有些事情是同步必须完成的：
- 理解用户问题
- 给出回复
- 决定是否需要触发后续动作

有些事情天然更适合异步：
- 写记忆
- 更新用户状态
- 创建 follow-up event
- 后台总结
- 推送提醒

如果把这些后处理都放进主链路，会带来几个问题：

- 响应变慢
- 工具链变长
- 用户体感差
- 并发压力更大
- 主对话和后台逻辑耦合过重

### 9.3 我对这个模型的建议
我建议把它建模成：

#### 主链路（foreground turn）
输入：
- tenant_id
- user_id
- conversation_id
- message

输出：
- assistant response
- structured side effects / emitted events

这里的“structured side effects / emitted events”可以包括：
- memory candidate
- follow-up candidate
- state update candidate
- reminder request

#### 后处理链路（background processors）
消费主链路产出的事件，异步完成：
- memory persist
- follow-up create
- user state update
- schedule job
- analytics / logs

### 9.4 这意味着什么
这意味着以后 `AgentLoop` 的职责应该逐渐更偏向：

- 产出主回复
- 产出副作用意图

而不是：

- 同步把所有副作用都执行完

这会让系统更像：

- 一个 conversation engine
- 加一个 event orchestration layer

### 9.5 这个思路和当前改造的关系
这个思路和我们前面做的多租户 / 并发改造是匹配的。

因为如果主链路和后处理链路要分开，后面就更需要：

- tenant-aware routing
- session mailbox / worker
- background event scheduler
- job / follow-up store

也就是说：

**主后分离不是替代并发改造，而是要求并发与调度层更清晰。**

## 10. 当前对这两个新思考的综合判断

### 10.1 关于 workspace / 数据库
我的建议是：

- 不要继续把所有状态都设计成磁盘 workspace
- 把大部分业务状态迁到 MySQL
- 但保留一个轻量 execution workspace，用于真正需要文件语义的工具

换句话说：

**建议“状态数据库化”，而不是“把所有 workspace 语义强行数据库化”。**

### 10.2 关于主链路 / 后处理
我的建议是：

- 这是正确方向
- 主链路负责和用户完成本轮对话
- 后处理链路负责 memory / follow-up / state update / reminders
- 未来应该用事件化方式连接这两条链路

## 11. 下一步如果继续推进，最值得做什么

基于这两个新判断，后续最值得推进的方向会是：

### P0
- 定义数据库里的核心模型：
  - tenant
  - user
  - conversation
  - message
  - memory
  - event
  - job
  - user_state

### P1
- 明确哪些信息继续留在 execution workspace
- 明确哪些信息从 workspace 迁到 MySQL

### P2
- 让主链路输出结构化 side effects
- 让后处理链路异步消费这些 side effects

### P3
- 把这些 side effects 统一接入当前已经完成的 `session mailbox / worker`
- 再决定是否需要把 mailbox 从“进程内”升级为“持久化消息层”

## 12. 一句话总结

对这两个思考，我的总结是：

**你的方向是对的。**

- `workspace` 不应该继续承担所有状态存储责任，数据库会更适合成为多租户系统的主状态存储
- 主链路和后处理链路应该分离，主负责聊天，后负责 memory / follow-up / event orchestration

但在实现上要注意：

**不要把“状态数据库化”和“文件工作区彻底消失”混成一件事。**

更合理的终局应该是：

- 数据库存状态
- 轻量 workspace 负责执行
- 主链路产出回复与事件
- 后处理链路异步落地这些事件
