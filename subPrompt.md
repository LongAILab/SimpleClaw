# 魔镜多时期 Prompt 分层设计

## 目标

不要为新手期、探索期、成熟期分别维护三整份独立 prompt。

推荐方案：

- 保留当前主 prompt 作为长期稳定的 `base prompt`
- 针对不同阶段，只额外注入一小段 `stage overlay`
- 再叠加少量 `milestone overlay`
- 阶段切换由业务代码决定，不由大模型自行决定

这样做的好处：

- 不会让用户感觉“像换了一个人”
- 不会把 prompt 维护拆成三套
- 更适合现在已有的 stable prefix / dynamic tail 结构
- 更容易做缓存命中

---

## 当前 Base Prompt 的定义

当前可作为三时期共同底座的内容：

1. `_get_identity()`
2. `workspace/base` 下的共享文档
3. shared skills
4. tenant overrides / tenant skills
5. 其他已有动态上下文（session summary、memory、extra_sections）

注意：

- `TOOLS.md` 属于工具使用规则
- 真正的 tool schema 是单独传给模型的，不在 `_get_identity()` 里

换句话说，之后做时期切换时，不是替换整份 prompt，而是：

- `base prompt` 保持不变
- 仅改变注入到 `extra_sections` 里的阶段策略片段

---

## 总体结构

每轮实际给模型的系统 prompt 可以理解为：

```text
Base Prompt
+ Journey Stage Overlay
+ Milestone Overlay
+ Session Summary
+ Memory
+ Other Dynamic Context
```

其中：

- `Base Prompt`：长期稳定，不轻易动
- `Journey Stage Overlay`：当前阶段默认策略
- `Milestone Overlay`：用户已解锁的能力提示

---

## 阶段核心原则

### 新手期

特点：

- 用户还不熟悉产品能力
- 需要更明确的下一步引导
- 需要更强的拍照与报告理解辅助

策略：

- 默认帮助用户收窄问题
- 优先指导拍照与基础分析
- 每次只给 1 到 2 个可执行动作
- 深度分析后优先解释“怎么看”

### 探索期

特点：

- 用户已经能独立完成基础拍照和分析
- 开始对报告细节、变化原因、方案选择产生兴趣

策略：

- 减少教学密度
- 提升开放度和承接能力
- 支持围绕分析结果的连续追问
- 更自然地引出比较、追踪、方案建议

### 成熟期

特点：

- 用户已经把魔镜当作长期陪伴工具
- 重点不再是学会使用，而是长期协同

策略：

- 更少“产品教学”
- 更强历史连续性
- 更自然地使用长期记忆、目标、节律、提醒
- 回答更像长期搭子，而不是功能说明器

---

## 为什么不用三整份 Prompt

如果直接维护：

- `novice_prompt`
- `explore_prompt`
- `mature_prompt`

问题会很明显：

- 三套规则容易漂移
- 公共人格会重复维护
- 某些边界规则容易三边不同步
- 升级时像“切人格”

因此更好的方式是：

- 一份 `base prompt`
- 三份轻量 `stage overlay`
- 若干 `milestone overlay`

---

## 状态持久化设计

阶段切换不由大模型决定，而是由后端业务状态决定。

建议放到 tenant state 中：

```json
{
  "journey": {
    "stage": "novice",
    "score": 0,
    "milestones": {
      "first_photo_uploaded": false,
      "first_deep_analysis_completed": false,
      "report_followup_done": false,
      "long_term_goal_stated": false
    }
  }
}
```

建议说明：

- `stage`：当前时期，`novice | explore | mature`
- `score`：阶段积分，用来判断整体熟练度
- `milestones`：关键行为开关，比纯积分更可靠

---

## 推荐升级逻辑

### novice -> explore

建议同时满足：

- `score >= 8`
- `first_deep_analysis_completed = true`
- `report_followup_done = true`

### explore -> mature

建议同时满足：

- `score >= 18`
- `long_term_goal_stated = true`

原则：

- 升级尽量单向，不要频繁降级
- 分数看趋势，里程碑看真实性
- 里程碑优先于分数

---

## 推荐打分规则

可以先做一版最小可用：

- 首次上传有效照片：`+2`
- 首次完成深度分析：`+3`
- 首次对分析报告进行追问：`+3`
- 连续两次围绕分析结果追问：`+2`
- 主动表达长期目标或偏好：`+4`

注意：

- 积分只是辅助，不单独决定升级
- 必须结合 milestone 判断

---

## Stage Overlay 设计

### Novice Overlay

```md
# Journey Policy

Current stage: novice

Default strategy:
- Assume the user may still be learning how to use 魔镜 well.
- Prefer one clear next step over multiple open-ended choices.
- Be more proactive in guiding photo capture and basic report reading.
- After analysis, explain the most important finding first, then briefly say how to interpret it.
- Keep exploration options limited unless the user explicitly asks for more.
```

### Explore Overlay

```md
# Journey Policy

Current stage: explore

Default strategy:
- Assume the user already understands the basic flow of photo capture and deep analysis.
- Reduce tutorial density and answer exploration questions more directly.
- After analysis, it is appropriate to offer two meaningful directions for deeper follow-up.
- Prefer comparison, explanation, reasoning, and next-step options over basic usage teaching.
- Fall back to tutorial-style guidance only when the user appears confused or blocked.
```

### Mature Overlay

```md
# Journey Policy

Current stage: mature

Default strategy:
- Assume the user sees 魔镜 as a long-term beauty companion rather than a guided onboarding product.
- Minimize product teaching unless explicitly requested.
- Use remembered preferences, history, and long-term goals for continuity when helpful.
- Prefer collaborative planning, trend tracking, and sustained guidance.
- Respond like a trusted long-term companion with practical beauty judgment.
```

---

## Milestone Overlay 设计

milestone 不一定改变整个阶段，但可以局部增强策略。

例如：

### 深度分析已解锁

```md
# Journey Unlocks

- The user has already completed at least one deep analysis successfully.
- It is appropriate to assume the user can understand a concise analytical summary.
- You may suggest deeper comparison or focused follow-up questions when relevant.
```

### 长期目标已明确

```md
# Journey Unlocks

- The user has expressed at least one long-term beauty goal.
- When helpful, connect immediate advice to that longer-term goal.
```

---

## 注入机制说明

关键点：

- 不是由大模型自己决定切换阶段
- 不是在已有 prompt 上做字符串替换
- 而是每一轮请求到来时，后端重新组装系统 prompt

伪代码：

```python
state = tenant_state_repo.get_or_create(tenant_key)

stage_overlay = build_stage_overlay(state.journey.stage)
milestone_overlays = build_milestone_overlays(state.journey.milestones)

extra_sections = [
    stage_overlay,
    *milestone_overlays,
]

messages = context_builder.build_messages(
    history=history,
    extra_sections=extra_sections,
    session_metadata=session_metadata,
)
```

这就意味着：

- 当前是 `novice`，就注入 `novice overlay`
- 以后升级为 `explore`，下轮自动改注入 `explore overlay`

不是替换整份 prompt，只是换一小段动态策略。

---

## 升级事件的触发方式

不建议每轮都重算复杂逻辑。

推荐做法：

- 正常轮次：只读取 `journey.stage`
- 关键事件发生时：更新 `score` 与 `milestones`
- 如果达到升级条件：更新 `journey.stage`

例如：

```python
state = tenant_state_repo.get_or_create(tenant_key)
state.journey.score += 3
state.journey.milestones["first_deep_analysis_completed"] = True

if (
    state.journey.stage == "novice"
    and state.journey.score >= 8
    and state.journey.milestones["first_deep_analysis_completed"]
    and state.journey.milestones["report_followup_done"]
):
    state.journey.stage = "explore"

tenant_state_repo.save(state)
```

---

## 适合当前项目的接入点

可以按以下思路接入：

1. `tenant/state.py`
   扩展 `TenantState`，加入 `journey`

2. 单独新增一个小模块，例如：
   - `nanobot/journey/policy.py`
   - `nanobot/journey/scoring.py`

3. 在进入 `ContextBuilder.build_messages(...)` 之前
   根据 tenant state 生成 `stage overlay` 与 `milestone overlays`

4. 通过 `extra_sections` 注入动态策略

这样改动最小，也最符合当前 prompt 结构。

---

## 产品层面的设计原则

真正要做的不是“切 prompt”，而是“调整默认策略”。

因此用户感受到的变化应该是：

- 新手期：更会带我走
- 探索期：更懂得接住我的问题
- 成熟期：更像长期搭子

而不是：

- 新手期像一个人
- 探索期像另一个人
- 成熟期又像第三个人

这就是 `base prompt + stage overlay + milestone overlay` 的核心意义。

---

## 当前建议

先做第一版最小闭环：

1. 增加 `journey.stage`
2. 增加 `journey.score`
3. 增加 3 到 4 个 milestone
4. 只写 `novice / explore / mature` 三段 overlay
5. 通过 `extra_sections` 注入

先跑通，再慢慢细化打分和里程碑。
