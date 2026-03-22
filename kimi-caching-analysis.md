# Kimi Context Caching 调研与实测分析

## 1. 背景

这份文档的目标是把两部分信息放在一起看：

1. 2024 年 7 月前后的公开资料中，Kimi `Context Caching` 的对外表述
2. 我们对 Moonshot/Kimi 官方 API 的直接实测结果

当前关注的问题不是“缓存概念是否存在”，而是：

- Kimi/Moonshot 是否真的有上下文缓存
- 这个缓存是否能从 API 响应中被观测到
- 它对 TTFT / 首 token 延迟是否一定有帮助
- 对我们当前 `nanobot` 项目的 prompt 结构，哪些部分应该稳定、哪些部分不该尝试缓存

## 2. 关于那篇知乎文章

目标文章：

- [知乎专栏文章](https://zhuanlan.zhihu.com/p/707098636)

说明：

- 直接抓取知乎原文时遇到 `403`，无法稳定拿到完整正文
- 因此这里采用了公开搜索摘要和相关新闻交叉确认

根据公开摘要，这篇文章对应的主题与以下结论一致：

- 2024 年 7 月，Kimi 面向开发者推出 `Context Caching`
- 核心宣传点是：
  - 长文本场景下显著降本
  - 首 token 延迟显著下降
- 典型适用场景是：
  - 固定大文档反复问答
  - 长系统提示反复复用
  - 代码库 / 产品说明书 / 知识库类场景

公开摘要中常见的表述包括：

- 成本最高下降约 90%
- 首 token 延迟显著下降
- 适合“稳定大前缀 + 少量变化问题”的调用模式

这和我们当前对项目的判断是吻合的：如果存在自动 prefix cache，那么最适合命中的就是稳定的 system prompt 前缀。

## 3. 对当前项目的结构判断

当前 `nanobot` 里，最适合缓存的稳定前缀是：

- `# 魔镜` 身份设定
- `base/AGENTS.md`
- shared `SOUL.md`
- shared `USER.md`
- shared `TOOLS.md`
- 相对稳定的 skills 摘要

不适合拿来作为缓存核心前缀的内容是：

- tenant `Memory`
- `Session Summary`
- runtime metadata（时间、channel、chat_id）
- 最近历史消息
- 用户本轮消息

我们已经对 `system prompt` 顺序做过一轮实验，把稳定段前置、易变段后置。

当前代码中的 `build_system_prompt()` 顺序已经调整为：

- identity
- bootstrap files
- `# Active Skills`
- `# Skills`
- `# Session Summary`
- `# Memory`
- `extra_sections`

这一步的意义是：

- 它本身不等于“已经命中缓存”
- 但它更符合 prefix cache 的命中条件

## 4. 直连 Moonshot 官方 API 的实测

为了避免被本地项目封装误导，这里直接调用了 Moonshot 官方接口：

- `https://api.moonshot.cn/v1/chat/completions`
- model: `kimi-k2.5`
- `thinking` 显式关闭
- `temperature = 0.6`

测试目标：

1. 看原始 API 返回里有没有 cache 相关字段
2. 看重复相同长稳定前缀时，缓存是否真的命中
3. 看命中缓存后，TTFT 是否一定更快

### 4.1 非流式重复请求测试

测试方法：

- 构造一段约 `10206` 字符的稳定 system prompt
- 连续发两次完全相同请求
- 用户消息固定为 `只回复：OK`

结果：

| Run | status | elapsed_ms | 关键 header | usage 关键信息 |
| --- | --- | ---: | --- | --- |
| 1 | 200 | 2216 | `msh-request-id` | `prompt_tokens=4700` |
| 2 | 200 | 2661 | `msh-context-cache-token-saved=4700` | `cached_tokens=4700`，`prompt_tokens_details.cached_tokens=4700` |

结论：

- 第二次请求明确出现了：
  - `usage.cached_tokens`
  - `usage.prompt_tokens_details.cached_tokens`
  - `msh-context-cache-token-saved`
- 这说明：
  - Kimi/Moonshot 的 context caching 在 API 层确实存在
  - 它不是只在控制台消费里“看起来像有”

### 4.2 流式重复请求测试（已有缓存样本）

测试方法：

- 使用相同长前缀
- 连续 3 次流式请求
- 记录首个内容 token 时间

结果：

| Run | first_content_ms | 关键 header |
| --- | ---: | --- |
| 1 | 779 | `msh-context-cache-token-saved=4700` |
| 2 | 788 | `msh-context-cache-token-saved=4700` |
| 3 | 757 | `msh-context-cache-token-saved=4700` |

结论：

- 这组样本说明缓存处于稳定命中状态
- 但在这个量级下，TTFT 并没有出现“越来越快”的明显趋势

### 4.3 流式“新前缀 miss -> 立即 hit”测试

测试方法：

- 给稳定前缀追加一个新的唯一 `probe_id`
- 第 1 次请求应视为新的前缀
- 第 2 次立即重放，应视为命中缓存

结果：

| Run | first_content_ms | 关键 header |
| --- | ---: | --- |
| 1 | 3119 | 无 cache header |
| 2 | 5963 | `msh-context-cache-token-saved=4724` |

结论：

- 第二次确实命中了缓存
- 但 TTFT 反而更慢
- 这说明“cache hit”不等于“这次一定首 token 更快”

### 4.4 更大前缀样本测试

测试方法：

- 使用更长的稳定前缀
- 再做一次 `miss -> hit`

结果：

| Run | first_content_ms | 关键 header |
| --- | ---: | --- |
| 1 | 2361 | `msh-context-cache-token-saved=4608` |
| 2 | 1929 | `msh-context-cache-token-saved=9401` |

结论：

- 这组里第二次确实更快
- 但提升没有夸张到“prefill 基本消失”

## 5. 综合判断

基于文章摘要和我们的实测，目前最可信的结论是：

### 5.1 已经可以确认的事实

- Kimi/Moonshot 的 `Context Caching` 真实存在
- 命中缓存时，Moonshot 会在 API 响应中暴露字段：
  - `usage.cached_tokens`
  - `usage.prompt_tokens_details.cached_tokens`
- 同时还会在响应头返回：
  - `msh-context-cache-token-saved`

### 5.2 暂时不能下死结论的点

- `cache hit` 并不保证每次 TTFT 都下降
- 至少在我们当前样本里：
  - 有的 hit 更快
  - 有的 hit 更慢

所以更合理的理解是：

- Kimi caching 对“费用”和“重复 prompt token 复用”很可能是明确有效的
- 对“首 token 更快”有帮助的可能
- 但不是稳定、线性的收益

### 5.3 为什么会这样

可能原因包括：

- 平台侧仍然存在调度 / 排队波动
- 不是所有 prefill 都被缓存完全抵消
- 流式首包的返回时机还受服务端实现影响
- 命中缓存节省的是一部分 prefix 处理，但不等于整个前置耗时消失

## 6. 当前项目里还缺什么

虽然 Moonshot 已经返回了缓存信息，但当前 `nanobot` 里并没有把这些信息保留下来。

当前 provider 只保留了基础 usage：

- `prompt_tokens`
- `completion_tokens`
- `total_tokens`

这意味着在当前前端调试页面里，你还看不到：

- 本次是否命中缓存
- 命中了多少 `cached_tokens`
- `prompt_tokens_details`
- `msh-context-cache-token-saved`

## 7. 对我们项目最实际的下一步建议

### 7.1 第一优先级：做 cache observability

建议把以下信息透出到 debug 通道：

- `usage.cached_tokens`
- `usage.prompt_tokens_details`
- 原始 usage 里其他 cache 相关字段
- 如果能从 SDK / provider 层拿到，也加上 `msh-context-cache-token-saved`

这样我们才能在自己的前端里直接看到：

- 哪次 hit 了
- 命中了多少 token
- hit 后 TTFT 是否真的改善

### 7.2 第二优先级：继续增强稳定前缀

保持稳定前缀尽量长：

- `# 魔镜`
- shared bootstrap docs
- skills summary

避免让高波动内容进入系统前缀前半段：

- memory
- session summary
- runtime metadata

### 7.3 第三优先级：做 A/B 观察，而不是先做结论

建议不要先假设：

- “只要 hit 就一定更快”

更好的方式是：

1. 连续多轮相同稳定前缀测试
2. 记录：
   - `cached_tokens`
   - `accepted_to_first_token_ms`
   - `llm_wait_ms`
3. 再看：
   - 命中率
   - 命中后的 TTFT 分布

## 8. 最终结论

一句话总结：

**Kimi/Moonshot 的 context caching 确实存在，而且我们已经通过原始 API 响应验证了这一点；但它当前更像是“真实的 token/cache 复用能力”，不应简单理解为“命中后一定稳定降低首 token 延迟”。**

对当前 `nanobot` 来说，方向是对的：

- 稳定段前置，继续做
- 但下一步最关键的不是继续猜，而是把 `cached_tokens` 等观测先接进系统

只有这样，后面我们才能真正基于自己项目的数据判断：

- Kimi caching 在我们的 prompt 结构下，到底值不值得继续专项优化
