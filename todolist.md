多租户体系 你已经把 tenant_key 打进了主链路，做到租户级会话、状态、记忆、调度隔离，不再是单工作区单用户思维。

为什么这里我没有只靠session，还要有 tenant
tenant 是个人/租户的长期容器，session是这个租户下面一条具体对话线程
tenant管跨会话，跨时间，跨lane都要共享的东西
比如用户长期画像，租户级soul.md/user.md/heartbeat.md
memory
主会话指针
heartbeat状态
最近活跃时间

租户数据从文件迁到 MySQL 会话、租户文档、memory、tenant state、cron 等，已经不再主要依赖本地文件，而是走数据库持久化。

异步后处理链路 你把一些“写动作”从主对话里拆出去了，走 postprocess / background worker，不让主链路阻塞在长耗时写入上。

结构化记忆提取 不只是简单存历史，而是做了 structured memory，把用户画像、偏好、长期目标、周期任务等抽出来，写回长期存储。

服务路线分层 已经不是一个简单 agent 进程，而是分成主对话、cron、heartbeat、postprocess、background worker 这些不同 lane / role。

任务队列与租约机制 你已经引入了 Redis Streams / lease 这一套思路，让异步任务、调度抢占、并发控制更像一个真正 runtime，而不是本地脚本。

Cron / Heartbeat 重构 定时任务和 heartbeat 不再只是附属逻辑，而是独立调度能力，能按租户扫描、排队、执行。

API 服务化 你已经有了 api/server 这条 HTTP/SSE 服务链路，不只是 CLI agent，而是一个可以被前端调用、可流式输出的服务。

Prompt 可观测性 你把 debug_prompt、debug_timing、prompt snapshot、stable/dynamic 分层展示都做出来了，这一点其实很重要，很多项目根本没有。

火山 Responses API 直连 你不是只“换模型供应商”，而是专门做了 VolcengineResponsesProvider，接了火山自己的 Responses API。

Prefix Cache 体系 你不只是“开了缓存”，而是做了显式 prefix cache、stable/dynamic prompt 拆分、tool schema 纳入 cache key、cache observability。

主聊天工具裁剪 你已经开始按 lane 裁剪 tool schema，比如把 message 从 main lane 隐掉，这属于 runtime 优化，不是普通功能开发。

会话与上下文策略升级 session summary、memory compact、history 选择、prompt budget，这些说明你已经在做上下文工程，而不是简单拼历史。

运行时启动与装配重构 bootstrap、agent_factory、runtime services 这一层已经做出来了，说明你在把系统往可部署 runtime 方向推。

配置和运行目录解耦，现在很多运行时目录是跟着 config path 派生的，这已经不是最初那种固定的 ～/.nanobot 的单机模型

迁移兼容思路，现在不是粗暴重写，而是保留了就文件态到 MySQL 的迁移路径和兼容逻辑

Turn 处理链路拆分 原来堆在 loop 里的 turn 逻辑被拆成了四个独立文件：turn_processor（主调度）、turn_commit（写入提交）、turn_effects（副作用执行）、turn_utils（工具函数）。这是一次重要的架构分层，让每个 turn 阶段的职责边界清晰，可以独立测试和替换。

事件总线（bus/） 独立出了 bus/events.py 和 bus/queue.py，作为系统内部的事件传递通道。这和任务队列不是同一个东西，任务队列是跨进程的异步调度，事件总线是进程内各模块之间解耦通信用的。

Sub-agent / Spawn 机制 做了 subagent.py 和 tools/spawn.py，agent 可以在运行时 spawn 子 agent 去处理子任务，这是从单 agent 执行模型向多 agent 协作方向迈出的一步。

Storage 接口抽象层 storage/interfaces.py 定义了存储的抽象接口，storage_factory.py 负责按配置组装具体实现（MySQL / Redis / 文件）。这不只是"用了 MySQL"，而是做了设计上的接口隔离，存储后端可以替换而不影响上层逻辑。

Context Budget 管理 独立出了 context_budget.py 专门管理 token 预算，包括各块内容的 token 上限分配、裁剪策略等。和"上下文工程"有关系，但是一个独立的计算和决策模块，不是散在 loop 里的临时逻辑。

多 Provider 体系重构 不只是"接了火山"，providers/ 下形成了完整的 provider 注册体系：azure_openai / litellm / custom / openai_codex / volcengine_responses，统一走 provider registry 管理，新增模型供应商只需实现接口并注册，不需要改主链路。

Task 三层协议拆分 任务系统被拆成了三层：task_protocol.py（协议定义）、task_serialization.py（序列化/反序列化）、task_runners.py（执行器）。这比"有一个任务队列"要更完整，是一个有协议约束、可扩展的任务执行框架。

Kimi Context Caching 适配 Prefix Cache 体系不只覆盖了火山，还专门为 Kimi 做了 context caching 适配，拼接顺序也做了调整以提高缓存命中概率。多模型的缓存策略是分开维护的，不是一套通用逻辑硬套所有供应商。