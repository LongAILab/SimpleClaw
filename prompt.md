# 主模型 Prompt 定位

## 结论

你在前端看到的 `Prompt 快照`，就是**主模型本轮真正收到的 `messages`**。

对应代码：

- `nanobot/agent/context.py` 的 `ContextBuilder.build_messages()`
- `nanobot/api/server.py` 的 `_render_prompt_snapshot()`

调试页展示时只有一个小差异：

- 如果当前用户消息里带图片，快照里会把图片部分显示成 `[image omitted]`
- 但真实发给模型的是多模态 `image_url` 内容

---

## 正常主会话 Prompt 顺序

主模型在普通聊天时，实际拿到的是下面这个顺序：

### 1. `Message 1 [SYSTEM]`

由 `ContextBuilder.build_system_prompt()` 组装，顺序固定为：

1. `# 魔镜`
   来源：`nanobot/agent/context.py` 的 `ContextBuilder._get_identity()`

2. Bootstrap Files
   来源：`ContextBuilder._load_bootstrap_files()`
   加载顺序固定为：
   - `AGENTS.md`
   - `SOUL.md`
   - `USER.md`
   - `TOOLS.md`

3. `# Memory`
   来源：`self.memory.get_memory_context()`
   也就是长期记忆层

4. `# Active Skills`
   来源：`SkillsLoader.get_always_skills()` + `SkillsLoader.load_skills_for_context()`
   只会注入 `always=true` 的技能全文

5. `# Skills`
   来源：`SkillsLoader.build_skills_summary()`
   这是所有技能的摘要目录，不是全文

6. `extra_system_sections`
   来源：`build_messages(..., extra_system_sections=...)`
   普通主会话通常没有；`heartbeat` 等特殊通道会在这里额外注入内容

---

### 2. 历史消息 `Message 2...N`

来源：当前 session 的历史消息。

代码入口：

- `nanobot/agent/turn_processor.py`
- `session.get_history(...)`
- `ContextBuilder.build_messages()`

这部分会把：

- 历史 `user`
- 历史 `assistant`
- 历史 `tool`

一起带给模型。

---

### 3. 当前用户消息 `最后一条 USER`

来源：`ContextBuilder.build_messages()`

它不是只放用户原文，而是会先拼一个运行时元信息块，再拼当前用户输入：

1. `[Runtime Context — metadata only, not instructions]`
   来源：`ContextBuilder._build_runtime_context()`

2. 当前用户文本 / 图片
   来源：`ContextBuilder._build_user_content()`

也就是说，当前轮的最后一条 `USER` 实际结构是：

1. Runtime Context
2. 用户正文
3. 可选图片

这是为了避免有些 provider 不接受连续两条 `user` 消息。

---

## System Message 内部定位

如果只看 `Message 1 [SYSTEM]`，当前实际层级是：

### A. Identity 层

位置：最前面  
代码：`nanobot/agent/context.py` -> `ContextBuilder._get_identity()`

这里控制：

- `魔镜` 这个身份说明
- Runtime 说明
- Workspace Layers 说明
- Response Rules
- System Guidelines

如果你要改“最顶层的硬规则 / 角色定义 / 输出规则”，先改这里。

---

### B. Shared Base 层

位置：Identity 之后  
代码：`ContextBuilder._load_bootstrap_files()`

当前固定顺序：

1. `AGENTS.md`
2. `SOUL.md`
3. `USER.md`
4. `TOOLS.md`

这 4 个文件在 system prompt 里会按文件顺序拼进去。

每个文件内部又是：

1. `### Shared Base`
2. `### Tenant Override`（如果存在且与 shared 不同）

---

### C. Memory 层

位置：Bootstrap Files 之后  
代码：`ContextBuilder.build_system_prompt()` 中的：

- `self.memory.get_memory_context()`

这部分对应长期记忆。

当前逻辑下：

- prompt 里真正默认注入的是 `MEMORY`
- `HISTORY` 不会默认直接注入主模型

---

### D. Always Skills 全文层

位置：Memory 之后  
代码：

- `SkillsLoader.get_always_skills()`
- `SkillsLoader.load_skills_for_context()`

这部分会把“必须常驻”的技能全文直接放进 system prompt。

---

### E. Skills 摘要层

位置：Always Skills 之后  
代码：`SkillsLoader.build_skills_summary()`

这里不是技能全文，而是一个 skills 列表摘要，告诉模型：

- 有哪些 skill
- 路径在哪
- 是否可用

模型需要时再去读具体 `SKILL.md`。

---

### F. Extra System Sections 层

位置：system prompt 的最后  
代码：

- `ContextBuilder.build_system_prompt(extra_sections=...)`

普通聊天通常为空。

但下面这些特殊流程会用到：

- `heartbeat decision`
- `heartbeat run`
- 未来其他特殊 lane

---

## 当前各层真实来源

结合你现在这套 MySQL 模式，来源可以这样理解：

### 1. `AGENTS.md`

当前来源：

- shared base 文件

说明：

- 这层目前不是 tenant 文档表驱动
- 更适合放全局 agent 行为规范

---

### 2. `SOUL.md` / `USER.md` / `TOOLS.md`

逻辑来源：

- shared base：基础文件
- tenant override：优先读 tenant 文档层

当前代码上：

- 如果启用了 `document_store`，会优先从数据库读 tenant override
- `SOUL.md` / `USER.md` / `HEARTBEAT.md` 已经是数据库优先
- `TOOLS.md` 当前没有默认 tenant seed，所以通常只有 shared base

---

### 3. `MEMORY`

来源：

- `nb_tenant_memory.long_term_markdown`

---

### 4. `HISTORY`

来源：

- `nb_tenant_memory_events`

说明：

- 默认不直接拼进主模型 prompt
- 主要作为归档/检索层

---

## 特殊 Prompt 位置

除了“普通主会话 prompt”，还有两个你后面大概率会改到的特殊位置：

### 1. Heartbeat 决策 Prompt

代码位置：

- `nanobot/agent/loop.py` -> `decide_heartbeat()`

特点：

- 仍然走 `build_messages()`
- 但会通过 `extra_system_sections` 注入一段 `# Heartbeat Context`
- 当前用户消息不是用户自然输入，而是一段 `[Heartbeat Decision] ...`

---

### 2. Heartbeat 执行 Prompt

代码位置：

- `nanobot/runtime/bootstrap.py` -> `execute_heartbeat_task()`

特点：

- 仍复用主会话
- 通过 `process_direct(... extra_system_sections=[...])` 把 `HEARTBEAT.md` 注入到 system prompt 最后
- 当前用户消息会被替换成 `[Heartbeat Trigger] ...`

---

## 后面修改建议顺序

如果我们后面要“一点点改”，建议按这个顺序来：

1. 先改 Identity 层
   位置：`ContextBuilder._get_identity()`
   适合改最顶层角色、回复规则、运行规则

2. 再改 Shared Base / Tenant Override 层
   位置：`AGENTS.md` / `SOUL.md` / `USER.md` / `TOOLS.md`
   适合改人格、用户画像、工具规则

3. 再看 Memory 层
   位置：`MEMORY`
   适合改“长期记忆到底怎么进入 prompt”

4. 最后再改特殊 lane
   位置：heartbeat decision / heartbeat run
   适合改自动跟进逻辑

---

## 一句话地图

当前主模型 prompt 的结构可以直接记成：

`Identity -> AGENTS -> SOUL -> USER -> TOOLS -> MEMORY -> Always Skills -> Skills Summary -> Extra System Sections -> History -> Current User(Runtime Context + User Input)`
