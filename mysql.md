# Nanobot MySQL 数据库设计方案

---

## 一、设计思路总览

### 现状分析

当前系统数据分两层存储：

**已入 MySQL（运行态）**
- `nb_tenant_state`：租户 heartbeat 状态、主 session 指针
- `nb_cron_jobs`：定时任务
- `nb_session_meta` + `nb_session_messages`：会话历史
- `nb_runtime_tasks`：异步任务队列记录

**还在文件系统（workspace 文档层）**
- `workspace/overrides/SOUL.md`：角色/人格设定
- `workspace/overrides/USER.md`：用户信息设定
- `workspace/overrides/TOOLS.md`：工具使用说明
- `workspace/overrides/HEARTBEAT.md`：心跳触发指令
- `workspace/memory/MEMORY.md`：长期记忆快照
- `workspace/memory/HISTORY.md`：历史归档日志
- `workspace/skills/*/SKILL.md`：技能文件（暂不迁移）

### 核心设计原则

1. **workspace 文档层支持完整 CRUD**  
   `SOUL.md`、`USER.md`、`HEARTBEAT.md` 等本质是"可修改的租户配置文档"，必须支持增删改查，不能只是 insert。

2. **session 按 session_type 分类，不只有主会话**  
   系统中存在多种 session 类型：主会话（main）、cron 执行会话、heartbeat 会话、subagent 会话、未来的 event follow-up 会话。这些都应该有明确的 `session_type` 标记，并通过 `origin_session_key` 绑定回主会话。

3. **profile 不单独放 tenant 表**  
   用户的个人资料（称呼、偏好、约束等）本质上是 memory 和 USER.md 的内容，不应该在 `nb_tenants` 里硬编码字段。`nb_tenants` 只存身份标识和轻量元数据。

4. **memory 分快照和事件两层**  
   - 快照（`nb_tenant_memory`）：当前 MEMORY.md 的内容，prompt 热路径直接读取
   - 事件（`nb_tenant_memory_events`）：每次 consolidation 产生的历史记录，对应 HISTORY.md

5. **文档版本可追溯**  
   workspace 文档（SOUL/USER/HEARTBEAT 等）支持版本历史，每次修改留一条版本记录，方便回滚和审计。

---

## 二、表结构设计

### 2.1 租户主表 `nb_tenants`

**职责**：租户身份注册，只存稳定身份字段，不承接高频资料更新，也不直接记录 session。

```sql
CREATE TABLE IF NOT EXISTS nb_tenants (
    tenant_id           BIGINT          NOT NULL    AUTO_INCREMENT,
    tenant_key          VARCHAR(255)    NOT NULL    COMMENT '业务租户标识，对外暴露，例如 tenant-a',
    status              VARCHAR(32)     NOT NULL    DEFAULT 'active' COMMENT 'active / suspended / deleted',
    created_at_ms       BIGINT          NOT NULL,
    updated_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (tenant_id),
    UNIQUE KEY uq_tenant_key (tenant_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**说明**：
- `tenant_id` 是内部主键，供数据库关联和索引使用
- `tenant_key` 是业务键，供代码和外部接口使用
- `display_name`、称呼、偏好、标签这类字段虽然合理，但属于高频变化的资料层，不建议放进这张主身份表
- `extra_json` 虽然灵活，但容易退化成杂项字段收容器，当前阶段不建议放在 `nb_tenants`
- session 不直接记录在这张表里；一个 tenant 会有多个 session，应该拆到 `nb_sessions`，而当前指针类信息放到 `nb_tenant_state`
- `status` 支持软删除和暂停

**补充理解**：
- `tenant_id` 回答的是“数据库里这一行是谁”
- `tenant_key` 回答的是“业务系统里这个租户叫什么”
- 如果后续要做大量表关联，优先用 `tenant_id` 做外键；如果是接口入参、日志、路由上下文，继续使用 `tenant_key`
- 本文后续章节里仍会暂时沿用 `tenant_key` 表示租户范围；等看到 `nb_sessions`、`nb_tenant_state` 时，再统一收口是否全部切换为 `tenant_id`

---

### 2.2 租户 Session 表 `nb_sessions`

**职责**：记录所有类型的 session，包括主会话、cron 执行会话、heartbeat 会话、subagent 会话等。

```sql
CREATE TABLE IF NOT EXISTS nb_sessions (
    tenant_key              VARCHAR(255)    NOT NULL,
    session_key             VARCHAR(255)    NOT NULL,
    session_type            VARCHAR(32)     NOT NULL    DEFAULT 'main'
                            COMMENT 'main / cron / heartbeat / subagent / event_followup',
    origin_session_key      VARCHAR(255)    NULL        COMMENT '关联的主 session，cron/heartbeat/subagent 填此字段',
    channel                 VARCHAR(64)     NULL        COMMENT '来源渠道，如 api / telegram',
    chat_id                 VARCHAR(255)    NULL        COMMENT '渠道内的 chat_id',
    title                   VARCHAR(255)    NULL        COMMENT '会话标题，可选',
    is_primary              TINYINT(1)      NOT NULL    DEFAULT 0 COMMENT '是否为租户主会话',
    last_consolidated       INT             NOT NULL    DEFAULT 0 COMMENT '已归档到 memory 的消息数量偏移',
    metadata_json           JSON            NOT NULL    DEFAULT (JSON_OBJECT()),
    created_at              DATETIME        NOT NULL,
    updated_at              DATETIME        NOT NULL,
    PRIMARY KEY (tenant_key, session_key),
    INDEX idx_sessions_tenant_type (tenant_key, session_type),
    INDEX idx_sessions_origin (tenant_key, origin_session_key),
    INDEX idx_sessions_updated (tenant_key, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**session_type 枚举说明**：

| session_type | 说明 | origin_session_key |
|---|---|---|
| `main` | 用户主会话 | null |
| `cron` | cron 任务执行会话，格式 `cron:{job_id}` | 主 session_key |
| `heartbeat` | heartbeat 触发会话 | 主 session_key |
| `subagent` | subagent 后台执行会话 | 触发它的 session_key |
| `postprocess` | postprocess 异步写会话 | 原始 session_key |
| `event_followup` | 未来 event 跟进会话（预留） | 主 session_key |

**说明**：
- 这张表替代并升级现有的 `nb_session_meta`
- `origin_session_key` 是关键字段，让所有衍生 session 都能追溯回主会话
- `is_primary` 标记当前租户的主活跃会话（与 `nb_tenant_state.primary_session_key` 保持同步）

---

### 2.3 Session 消息表 `nb_session_messages`

**职责**：append-only 的消息存储，保持现有结构，补充独立字段方便查询。

```sql
CREATE TABLE IF NOT EXISTS nb_session_messages (
    tenant_key          VARCHAR(255)    NOT NULL,
    session_key         VARCHAR(255)    NOT NULL,
    seq                 INT             NOT NULL    COMMENT '消息序号，从 0 开始，append-only',
    role                VARCHAR(32)     NOT NULL    COMMENT 'user / assistant / tool / system',
    content_json        JSON            NOT NULL    COMMENT '完整消息体，兼容 OpenAI 格式',
    tool_name           VARCHAR(128)    NULL        COMMENT 'role=tool 时的工具名',
    tool_call_id        VARCHAR(128)    NULL        COMMENT 'role=tool 时的 tool_call_id',
    tokens_estimate     INT             NULL        COMMENT 'token 估算，可选',
    created_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (tenant_key, session_key, seq),
    INDEX idx_messages_role (tenant_key, session_key, role),
    INDEX idx_messages_time (tenant_key, session_key, created_at_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**说明**：
- 消息体完整存在 `content_json`，兼容现有代码
- `role`、`tool_name`、`created_at_ms` 单独列出，方便统计和过滤
- 保持 append-only 原则，不做 UPDATE

---

### 2.4 租户文档表 `nb_tenant_documents`

**职责**：存储 workspace 中所有可修改的文档型配置，支持完整 CRUD + 版本历史。  
替代文件：`SOUL.md`、`USER.md`、`TOOLS.md`、`HEARTBEAT.md`。

```sql
CREATE TABLE IF NOT EXISTS nb_tenant_documents (
    doc_id              BIGINT          NOT NULL    AUTO_INCREMENT,
    tenant_key          VARCHAR(255)    NOT NULL,
    doc_type            VARCHAR(64)     NOT NULL
                        COMMENT 'soul / user / tools / heartbeat / custom',
    doc_name            VARCHAR(255)    NOT NULL    COMMENT '文档名称，custom 类型可自定义',
    content             LONGTEXT        NOT NULL    COMMENT '文档正文，Markdown 格式',
    content_hash        VARCHAR(64)     NOT NULL    COMMENT 'SHA256，用于快速判断是否变更',
    format              VARCHAR(16)     NOT NULL    DEFAULT 'markdown',
    version_no          INT             NOT NULL    DEFAULT 1 COMMENT '当前版本号',
    is_active           TINYINT(1)      NOT NULL    DEFAULT 1 COMMENT '是否生效，0 表示已禁用',
    created_by          VARCHAR(64)     NULL        COMMENT '创建来源，如 user / agent / system',
    updated_by          VARCHAR(64)     NULL        COMMENT '最后修改来源',
    created_at_ms       BIGINT          NOT NULL,
    updated_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (doc_id),
    UNIQUE KEY uq_tenant_doc_type_name (tenant_key, doc_type, doc_name),
    INDEX idx_docs_tenant_type (tenant_key, doc_type),
    INDEX idx_docs_active (tenant_key, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**doc_type 枚举说明**：

| doc_type | 对应文件 | 说明 |
|---|---|---|
| `soul` | `overrides/SOUL.md` | 角色人格设定 |
| `user` | `overrides/USER.md` | 用户信息设定 |
| `tools` | `overrides/TOOLS.md` | 工具使用说明 |
| `heartbeat` | `overrides/HEARTBEAT.md` | 心跳触发指令 |
| `custom` | 自定义文档 | 未来扩展用 |

**说明**：
- 这张表支持完整 CRUD，不是只 insert
- `content_hash` 用于判断是否真的发生了变更，避免无效版本写入
- `is_active` 支持软禁用某个文档（不删除，只是不注入 prompt）
- `created_by` / `updated_by` 区分是用户手动改的还是 agent 自动改的

---

### 2.5 文档版本历史表 `nb_tenant_document_versions`

**职责**：记录每次文档变更的历史快照，支持回滚和审计。

```sql
CREATE TABLE IF NOT EXISTS nb_tenant_document_versions (
    version_id          BIGINT          NOT NULL    AUTO_INCREMENT,
    doc_id              BIGINT          NOT NULL    COMMENT '关联 nb_tenant_documents.doc_id',
    tenant_key          VARCHAR(255)    NOT NULL,
    doc_type            VARCHAR(64)     NOT NULL,
    doc_name            VARCHAR(255)    NOT NULL,
    version_no          INT             NOT NULL,
    content             LONGTEXT        NOT NULL    COMMENT '该版本的完整内容快照',
    content_hash        VARCHAR(64)     NOT NULL,
    change_summary      VARCHAR(512)    NULL        COMMENT '变更摘要，可选',
    change_source       VARCHAR(64)     NULL        COMMENT '变更来源：user / agent / postprocess / api',
    operator_id         VARCHAR(255)    NULL        COMMENT '操作者标识',
    created_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (version_id),
    INDEX idx_doc_versions (doc_id, version_no),
    INDEX idx_doc_versions_tenant (tenant_key, doc_type, doc_name, version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**说明**：
- 每次 `nb_tenant_documents` 的 content 发生变化时，自动写一条版本记录
- `change_source` 区分是用户主动修改、agent 自动更新还是 postprocess 写入
- 可以基于 `version_no` 做回滚

---

### 2.6 租户记忆快照表 `nb_tenant_memory`

**职责**：存储当前 MEMORY.md 的快照，供 prompt 热路径直接读取。

```sql
CREATE TABLE IF NOT EXISTS nb_tenant_memory (
    tenant_key              VARCHAR(255)    NOT NULL,
    long_term_markdown      LONGTEXT        NOT NULL    COMMENT '当前 MEMORY.md 全文',
    structured_facts_json   JSON            NULL
                            COMMENT '结构化 facts，由 structured_memory 提取，格式见下方说明',
    last_updated_session    VARCHAR(255)    NULL        COMMENT '最后触发更新的 session_key',
    last_consolidation_ms   BIGINT          NULL        COMMENT '最后一次 consolidation 时间',
    updated_at_ms           BIGINT          NOT NULL,
    PRIMARY KEY (tenant_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**`structured_facts_json` 格式示例**：

```json
{
  "User Information": {
    "姓名": "小美",
    "称呼": "老大",
    "关系设定": "闺蜜"
  },
  "Preferences": {
    "长期偏好": "偏爱清透底妆，不喜欢厚重感"
  },
  "Important Notes": {
    "长期约束": "对酒精成分过敏"
  },
  "Project Context": {
    "长期目标": "改善毛孔粗大问题"
  }
}
```

**说明**：
- `long_term_markdown` 是 prompt 注入时直接使用的内容，对应现在的 `MEMORY.md`
- `structured_facts_json` 是 `StructuredMemoryManager` 提取的结构化版本，方便程序读取和展示
- 这张表是 1:1 per tenant，每次 consolidation 后 upsert
- 用户的 profile（称呼、偏好、约束等）就存在这里的 `structured_facts_json` 里，不放 `nb_tenants`

---

### 2.7 记忆事件历史表 `nb_tenant_memory_events`

**职责**：记录长期记忆的事件流水，对应 `HISTORY.md`，默认不拼接到 prompt，只在需要回忆旧信息时按需检索。

```sql
CREATE TABLE IF NOT EXISTS nb_tenant_memory_events (
    event_id            BIGINT          NOT NULL    AUTO_INCREMENT,
    tenant_key          VARCHAR(255)    NOT NULL,
    session_key         VARCHAR(255)    NULL        COMMENT '触发本次记忆写入的 session',
    source_type         VARCHAR(32)     NOT NULL
                        COMMENT 'consolidation / structured_memory / raw_archive / manual',
    event_type          VARCHAR(64)     NOT NULL
                        COMMENT 'profile_fact / preference / constraint / project_context / conversation_recap / raw_archive / manual_edit',
    topic               VARCHAR(255)    NULL        COMMENT '事件主题，如 称呼偏好 / 广州出差 / 护肤计划',
    keywords_text       VARCHAR(1000)   NULL        COMMENT '当前 MySQL 检索阶段使用的关键词串，供 LIKE / FULLTEXT / 应用层切词召回',
    importance          TINYINT         NOT NULL    DEFAULT 3 COMMENT '事件重要度，1-5，越高越值得优先召回',
    history_entry       TEXT            NOT NULL    COMMENT '归档后的摘要文本，对应 HISTORY.md 的一条记录',
    memory_patch        TEXT            NULL        COMMENT '本次对 long_term_markdown 的增量改动说明，可选',
    items_json          JSON            NULL        COMMENT '结构化提取结果，可选，例如 preferred_address=老大',
    start_seq           INT             NULL        COMMENT '本次事件覆盖的起始消息序号，可选',
    end_seq             INT             NULL        COMMENT '本次事件覆盖的结束消息序号，可选',
    created_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (event_id),
    INDEX idx_memory_events_tenant (tenant_key, created_at_ms),
    INDEX idx_memory_events_session (tenant_key, session_key),
    INDEX idx_memory_events_type (tenant_key, event_type, created_at_ms),
    INDEX idx_memory_events_topic (tenant_key, topic),
    INDEX idx_memory_events_importance (tenant_key, importance, created_at_ms),
    FULLTEXT KEY ft_memory_events_recall (topic, keywords_text, history_entry)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**说明**：
- 这张表不是默认 prompt 输入源；默认进入 prompt 的是 `nb_tenant_memory.long_term_markdown`
- 这张表更像数据库版 `HISTORY.md`，用于“你还记得我上次说过什么吗”这类按需回忆
- 每次 `MemoryStore.consolidate()`、`StructuredMemoryManager.execute()`、手动修改 memory 成功后，都可以写一条
- `history_entry` 对应现在追加到 `HISTORY.md` 的那段文字
- `event_type + topic + keywords_text + importance` 是当前 MySQL 阶段的检索骨架，不依赖向量库也能先做一层召回
- 典型检索流程是：先按 `tenant_key` 过滤，再按 `event_type` / `topic` / 时间范围缩小范围，然后结合 `LIKE` 或 `FULLTEXT` 查询 `keywords_text`、`history_entry`
- `items_json` 存本次 structured memory 提取到的结构化结果，方便追溯“这条记忆从哪来”
- 未来如果引入向量数据库，这张表仍然可以保留为 source of truth；向量索引只作为额外召回层，而不是替代它

---

### 2.8 租户运行态表 `nb_tenant_state`（现有，建议扩展）

**职责**：租户级别的运行时状态，包括主 session 指针和 heartbeat 调度状态。

```sql
-- 现有表，建议在现有基础上补充以下字段
ALTER TABLE nb_tenant_state
    ADD COLUMN IF NOT EXISTS last_cron_session_key    VARCHAR(255) NULL COMMENT '最近一次 cron 执行的 session_key',
    ADD COLUMN IF NOT EXISTS last_heartbeat_session_key VARCHAR(255) NULL COMMENT '最近一次 heartbeat 执行的 session_key';
```

**说明**：
- 现有字段已经足够，只需补充 cron/heartbeat 最近执行的 session 指针，方便调试和追溯

---

### 2.9 媒体资产表 `nb_media_assets`（预留）

**职责**：记录租户上传的图片、音频等媒体文件的元数据，文件本体存文件系统或对象存储。

```sql
CREATE TABLE IF NOT EXISTS nb_media_assets (
    asset_id            BIGINT          NOT NULL    AUTO_INCREMENT,
    tenant_key          VARCHAR(255)    NOT NULL,
    session_key         VARCHAR(255)    NULL        COMMENT '关联的 session，可选',
    media_type          VARCHAR(32)     NOT NULL    COMMENT 'image / audio / document / video',
    storage_path        TEXT            NOT NULL    COMMENT '文件存储路径或 URL',
    mime_type           VARCHAR(128)    NULL,
    size_bytes          BIGINT          NULL,
    sha256              VARCHAR(64)     NULL        COMMENT '文件指纹，用于去重',
    original_filename   VARCHAR(512)    NULL,
    created_at_ms       BIGINT          NOT NULL,
    PRIMARY KEY (asset_id),
    INDEX idx_assets_tenant (tenant_key, created_at_ms),
    INDEX idx_assets_session (tenant_key, session_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**说明**：
- 大文件不进 MySQL，只存 metadata 和路径
- `sha256` 用于去重，避免同一张图重复存储

---

## 三、表关系总览

```
nb_tenants (1)
    ├── nb_tenant_state (1:1)
    ├── nb_tenant_documents (1:N) ──► nb_tenant_document_versions (1:N)
    ├── nb_tenant_memory (1:1)
    ├── nb_tenant_memory_events (1:N)
    ├── nb_sessions (1:N)
    │       └── nb_session_messages (1:N)
    ├── nb_cron_jobs (1:N)
    ├── nb_runtime_tasks (1:N)
    └── nb_media_assets (1:N)
```

---

## 四、Prompt 拼装读取路径

当用户发一条消息时，prompt 拼装的数据来源如下：

```
1. nb_tenant_documents WHERE doc_type='soul' AND is_active=1
   → 注入 SOUL.md（角色设定）

2. nb_tenant_documents WHERE doc_type='user' AND is_active=1
   → 注入 USER.md（用户信息）

3. nb_tenant_documents WHERE doc_type='tools' AND is_active=1
   → 注入 TOOLS.md（工具说明）
![1774071393854](image/mysql/1774071393854.png)![1774071395726](image/mysql/1774071395726.png)![1774071413702](image/mysql/1774071413702.png)![1774071420930](image/mysql/1774071420930.png)![1774071425043](image/mysql/1774071425043.png)![1774071438502](image/mysql/1774071438502.png)
4. nb_tenant_memory WHERE tenant_key=?
   → 注入 long_term_markdown（长期记忆）

5. nb_sessions + nb_session_messages WHERE session_key=? AND seq >= last_consolidated
   → 注入近程历史上下文

6. 当前用户消息
   → 直接注入

7. [heartbeat lane 专用]
   nb_tenant_documents WHERE doc_type='heartbeat' AND is_active=1
   → 注入 HEARTBEAT.md

8. nb_tenant_memory_events
   → 默认不注入 prompt；仅在用户追问历史事实、旧项目、旧偏好时按需检索并压缩后再注入
```

---

## 五、Session 类型与 origin_session_key 关系

```
主会话 (session_type=main)
    session_key = "main:tenant-a"
    origin_session_key = null

    ├── cron 执行会话 (session_type=cron)
    │       session_key = "cron:35114a94"
    │       origin_session_key = "main:tenant-a"
    │
    ├── heartbeat 会话 (session_type=heartbeat)
    │       session_key = "heartbeat:tenant-a:..."
    │       origin_session_key = "main:tenant-a"
    │
    ├── subagent 会话 (session_type=subagent)
    │       session_key = "subagent:abc123"
    │       origin_session_key = "main:tenant-a"
    │
    └── postprocess 会话 (session_type=postprocess)
            session_key = "postprocess:main:tenant-a"
            origin_session_key = "main:tenant-a"
```

---

## 六、现有表迁移说明

| 现有表 | 迁移方向 |
|---|---|
| `nb_tenant_state` | 保留，补充 `last_cron_session_key` / `last_heartbeat_session_key` 字段 |
| `nb_session_meta` | 迁移到 `nb_sessions`，补充 `session_type` / `origin_session_key` 字段 |
| `nb_session_messages` | 保留，补充 `role` / `tool_name` / `created_at_ms` 独立列 |
| `nb_cron_jobs` | 保留不变 |
| `nb_runtime_tasks` | 保留不变 |

---

## 七、新增表清单

| 表名 | 职责 |
|---|---|
| `nb_tenants` | 租户身份注册 |
| `nb_tenant_documents` | workspace 文档（SOUL/USER/TOOLS/HEARTBEAT），支持 CRUD |
| `nb_tenant_document_versions` | 文档版本历史 |
| `nb_tenant_memory` | 当前记忆快照（MEMORY.md + structured facts） |
| `nb_tenant_memory_events` | 记忆事件历史（HISTORY.md，默认不进 prompt，按需检索） |
| `nb_media_assets` | 媒体文件元数据（预留） |

---

## 八、关键设计决策说明

### Q1：为什么 profile 不放 nb_tenants？

用户的个人资料（称呼、偏好、肤质、约束等）本质上是从对话中动态提取的，会随时间演化。  
这类内容放在 `nb_tenant_memory.structured_facts_json` 更合理，因为：
- 它由 `StructuredMemoryManager` 自动提取和更新
- 它需要随时被 `MEMORY.md` 的内容同步
- 它不是用户注册时填写的静态字段

`nb_tenants` 只存身份识别用的轻量字段（渠道来源、状态、外部 ID 等）。

---

### Q2：为什么 workspace 文档要支持 CRUD 而不是只 insert？

`SOUL.md`、`USER.md`、`HEARTBEAT.md` 这些文件的本质是"可配置的 prompt 模板"，用户或 agent 都可能去修改它们。  
如果只支持 insert，每次修改都是一条新记录，会造成：
- 查询"当前生效版本"需要额外逻辑
- 无法直接 update 一条记录

正确的做法是：
- `nb_tenant_documents` 存当前生效版本，支持 update
- `nb_tenant_document_versions` 存每次变更的历史快照
- 通过 `content_hash` 判断是否真的变化，避免无意义的版本写入

---

### Q3：session 为什么要有 session_type？

现在代码里 session_key 的命名已经隐含了类型：
- `main:tenant-a`
- `cron:35114a94`
- `postprocess:main:tenant-a`

但这是约定俗成的，没有显式字段。加上 `session_type` 之后：
- 可以直接查"这个租户有哪些 cron 执行历史"
- 可以直接查"这个 cron job 对应的 session 消息"
- 可以让 heartbeat/subagent 的 session 明确绑定回主会话

---

*文档生成时间：2026-03-20*
