# Momentum 代码审查与 OpenAI Agents SDK 兼容性报告

**审查副本：** `/workspace/Momentum-review/repo`  
**远端项目：** <https://github.com/woyaoxingfua/Momentum>  
**状态：** 变更仅保留在本地审查副本；未提交、未推送，GitHub 远端未修改。

## 结论摘要

Momentum 当前使用 Agents SDK 的 **Chat Completions 路径**：通过 `AsyncOpenAI(api_key, base_url)` 构造 client，再显式创建 `OpenAIChatCompletionsModel`。它已使用 Runner、普通本地 function tools、两个专家 Agent 的 handoff、hooks、输入/输出 guardrails、结构化输出以及原生流式运行。OpenAI Chat Completions 的基础文本/工具工作流在接口层匹配；对 OpenAI-compatible endpoint 和 Ollama `/v1` 只能承诺**有条件兼容**，具体能力取决于模型/provider 是否正确支持工具调用、schema、流式 chunk、图片及 provider-specific 参数。

没有发现应当仅为追新而升级依赖的理由。审查环境实际安装 `openai-agents==0.23.1`、`openai==3.23.0`、`mcp==1.30.0`；项目目前只声明 `openai-agents>=0.2.0`，没有上限或 lock 文件，因此其他部署环境解析到的版本可能不同。本轮没有升级 SDK、收紧版本范围或切换 Responses API。

发现并修复一项实际配置缺口：项目此前将 `set_default_openai_client(..., use_for_tracing=False)` 当作关闭 tracing，但该参数只决定该 client 的 API key 是否用于 trace 上传，并不禁用某一次 Agent run。现在统一通过每次调用的 `RunConfig.tracing_disabled=provider.disable_tracing` 执行配置。此字段在官方 v0.2.0 源码中已经存在，因此与项目声明的最低版本相容；本地回归测试验证 OpenAI-compatible provider 禁用、OpenAI provider 保持原设置。完整测试通过。

## 版本与依赖

| 项目 | 审查环境实际版本 | 仓库声明 | 评估 |
|---|---:|---|---|
| OpenAI Agents SDK | `0.23.1` | `openai-agents>=0.2.0` | 无上限、无 lock；实际部署版本不能由仓库声明唯一确定 |
| OpenAI Python SDK | `3.23.0` | 未单独 pin | SDK 传递依赖；升级时需与 Agents SDK 一起验证 |
| MCP Python SDK | `1.30.0` | `mcp>=1.28,<2` | MCP 范围来自此前针对 MCP 低层 API 的兼容性修订，不属于本次 Agents SDK 评估重点 |

Agents SDK 官方说明其 `0.Y.Z` 版本仍会演进，minor 版本可能带来破坏性变更；建议将**经过测试的版本组合**记录到部署 lock，而不是直接把最低版本改成当时最新版本。发布记录中的 HTTPX2 迁移主要影响自定义 `http_client`/transport；Momentum 当前没有传入自定义 HTTP client。发布说明中关于显式 client 与 organization/project 重复参数的限制也未命中当前代码，因为项目未在该处传入 organization/project。见[官方发布说明](https://openai.github.io/openai-agents-python/release/)及[官方 releases](https://github.com/openai/openai-agents-python/releases)。

## 现有 SDK 用法与兼容性

| 领域 | 项目实际用法 | 兼容判断与边界 |
|---|---|---|
| **Runner** | 规划/解析和普通对话调用 `Runner.run`；聊天调用 `Runner.run_streamed` 并消费 `stream_events()`；读取 `final_output`/`final_output_as`，用 `to_input_list()` 保存对话；普通工作流 `max_turns=30`。 | 这些是官方核心接口。SDK 文档说明 streamed run 要消费完事件流后结果才完整。当前代码按 `RunItemStreamEvent` 和 Responses 风格的 `RawResponsesStreamEvent`/`TextDelta` 提取增量；非 OpenAI provider 的实际 chunk 形状仍需实测。流式异常目前会重新执行一次非流式 Runner；若首次流已触发写工具副作用，可能重复执行，当前没有幂等/已执行事件保护。 |
| **Agent 与 handoff** | 构造主 `Momentum`、`InsightAgent`、`WeatherAgent`；主 Agent 通过 `handoffs` 转交统计和天气意图；指令为缓存 Agent 构造时生成的静态字符串；使用生命周期 hooks。 | 对支持 Chat Completions 工具调用的模型，基础 handoff/API 形态适配。动态 instructions、`Agent.as_tool()`、handoff 输入过滤、tool guardrails、人审审批目前没有接入。Guardrails 和 hooks 不等于写操作授权。 |
| **模型与 provider** | 明确使用 `OpenAIChatCompletionsModel`，并将 `AsyncOpenAI` 自定义 `base_url`/API key 注入；Ollama 归一到 `/v1`，配置中关闭 tracing。 | OpenAI Chat Completions 是最直接的兼容面。官方对新 OpenAI 原生应用推荐优先考虑 Responses，但很多兼容服务没有 Responses；不能将 Momentum 当前模型路径描述成 Responses 通用实现。Ollama 文档将其 API 标为 OpenAI-compatible 子集：文本、工具、结构化输出、流式和视觉能力须按 Ollama 版本及具体模型分别验证。 |
| **Tools 与结构化输出** | 大量本地 `@function_tool`；任务、天气、洞察等工具通过本地 Python 执行；规划/解析 Agent 使用 Pydantic `output_type`。 | 不依赖 hosted tools，因此适合 Chat Completions 路径；前提是 provider 支持函数调用及参数 schema。严格 JSON Schema、optional 字段、并行工具、结构化输出质量不能从“OpenAI-compatible”名称推定。当前没有 `ToolSearchTool`、tool namespace 或 Hosted MCP。 |
| **Session/历史** | `_conversation_history` 是进程内按 `user_id` 保存的 list；Agent 执行前拼入 history，完成后写入 `to_input_list()`；历史上限是最近 40 个 item。 | 这是应用手写的易失对话循环，不是 SDK Session。进程重启/多 worker 不共享历史；按 item 截断可能切开 tool-call/tool-output 配对，并发读改写也没有锁或事务。官方 Session 可提供统一 history 接口，但持久化迁移需另行设计数据库、异步驱动、并发与回滚；不能与 Responses 的 `conversation_id`/`previous_response_id` 混为一谈。 |
| **Tracing/观测** | 项目设置 `use_for_tracing`，但之前没有传 `RunConfig.tracing_disabled`；hooks 和本地日志另行记录事件。 | `use_for_tracing=False` 不是全局或 per-run tracing-off。自定义 provider 可能仍产生指向 OpenAI trace exporter 的请求/错误。已修复为 per-run 设置，不修改 SDK 全局状态。对 OpenAI 原生 provider，trace 默认可能包含输入、模型输出及 tool I/O；本地 logger 也会截取部分消息/结果，敏感信息治理仍是待决策略。 |

### Provider 兼容矩阵（证据等级）

| 能力 | OpenAI Chat Completions | 通用 OpenAI-compatible endpoint | Ollama `/v1` |
|---|---|---|---|
| 基础文本与 Runner 循环 | 接口匹配 | 协议条件匹配 | 协议条件匹配，需按模型实测 |
| 本地 function tools / handoff | 模型支持 tool calling 时可用 | 依赖 tool schema/调用实现 | 依赖模型工具调用质量与 schema 支持 |
| Pydantic/严格 structured output | 需模型/API 支持 | 可能降级或拒绝严格 schema | 文档能力是子集，必须实测 |
| 增量 streaming/tool-call delta | 官方适配器支持 | chunk 形状可能不同 | 需按版本/模型实测 |
| Responses-only hosted tools、server conversation state | 当前 Chat Completions model 不适用 | 除非另有 Responses 实现 | 不能从 `/v1` Chat Completions 推定支持 |
| 图片 | 当前代码发 OpenAI `image_url` data URI | 依服务格式 | Ollama 文档称支持 base64 image、不支持 image URL；当前 `image_url` 形态不能据此判定兼容 |
| `thinking`/`reasoning_effort` | 取决于模型/请求接口 | `extra_body` 属 provider-specific 字段 | Ollama 对其文档列出的字段有支持，但其他 provider 未保证 |

以上是代码/API 形态和官方能力文档的比较，不代表本轮对 OpenAI、第三方 endpoint 或 Ollama 做了真实模型端到端调用；没有对应 provider 凭据/实例时，仍须在目标环境补做 smoke test。Ollama 官方兼容说明特别指出其 Chat Completions API 不支持 image URL、`tool_choice` 等若干字段，参见[Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)。

## 新功能评估与本轮改动

### 已实施：按 run 关闭非 OpenAI provider tracing

在 `src/momentum_agent/agent_app.py` 新增 `_build_run_config(...)`，同步用于普通对话、视觉输入、流式对话及流式视觉四条 Runner 路径。它把已有 `ProviderConfig.disable_tracing` 传给 SDK 的 `RunConfig.tracing_disabled`，保留输入/输出 guardrails 和 workflow 名称。使用每个 run 独立的配置，而不是在并发请求中切换 SDK 全局 `set_tracing_disabled()` 状态。

验证依据：审查环境中 `RunConfig` 的运行时签名有 `tracing_disabled`；官方 v0.2.0 源码也已定义该字段并传入 trace context。测试 `tests/test_agents_sdk_review.py` 覆盖 enabled/disabled 两种 provider 策略和 guardrail/workflow 参数透传。相关官方 API：[RunConfig](https://openai.github.io/openai-agents-python/ref/run/)；[v0.2.0 RunConfig 源码](https://raw.githubusercontent.com/openai/openai-agents-python/v0.2.0/src/agents/run.py)；[非 OpenAI provider tracing 建议](https://openai.github.io/openai-agents-python/models/)。

### 未贸然接入的能力

- **Responses API 与服务端对话续接：** `previous_response_id`、`conversation_id`、Responses prompts 及 hosted tools 属 Responses 路径；Chat Completions adapter 会丢弃部分 Responses-only 字段。由于目前仍需兼容 OpenAI-compatible/Ollama，不应整体切换。可在未来单独增加 OpenAI-only Responses agent，并用 feature flag/测试隔离。
- **ToolSearchTool、deferred tools、namespaces、HostedMCPTool：** 这些是 Responses/托管能力，不适用于当前 Chat Completions 模型。若工具规模确实造成上下文/选择问题，再做 Responses 专用分支；跨 provider 的 MCP 更适合考察本地 MCP server 接入。
- **SDK Sessions/SQLAlchemySession：** 当前没有持久会话表和异步 DB driver（项目 MySQL extra 是同步 `pymysql`）。若要多 worker/重启持久化，应先定义完整 turn 截断、会话隔离、并发写入和数据迁移，再考虑自定义 Session 或 SQLAlchemySession；SDK 没有针对当前内存 list 的一键迁移 API。
- **RunState、中断/审批恢复、取消：** SDK 支持流式取消、`interruptions`、`to_state()`/`RunState` 恢复和 human-in-the-loop，但 Momentum 尚无暂停/恢复 UI、状态存储或写工具副作用幂等策略。现阶段不适合只为了新 API 而接入。
- **tool approval：** 当前任务创建/编辑/完成/放弃等工具会直接执行。若产品要求高风险操作确认，应设计显式审批流与可恢复状态；不要把 guardrails 或 hook 日志当授权控制。
- **trace PII 与本地日志：** SDK 提供 `RunConfig.trace_include_sensitive_data`；默认 traces 可包含模型输入/输出和 tool I/O。是否默认关闭会影响排障信息，应先确定生产隐私/观测政策，并同时审计本地 logger；本轮未擅自改变 OpenAI provider 的 trace payload 行为。
- **AnyLLM/LiteLLM：** 官方提供适配器/路由扩展，但会增加依赖和迁移面。当前 `AsyncOpenAI(base_url)` 已满足已声明的兼容端点需求，没有证据证明引入适配器会解决现有问题。

## 本轮代码改动与测试

本轮 Agents SDK 专项仅增改：

- `src/momentum_agent/agent_app.py`：统一 RunConfig 构造，并为四种运行路径按 provider 关闭 tracing。
- `tests/test_agents_sdk_review.py`：新增两条 per-run tracing 回归测试。

全量验证结果：

- Python：**207 passed，10 skipped**（MySQL 集成测试在当前环境未配置数据库）。
- 前端 Node：**6 passed**。
- `pip check`：无 broken requirements。
- `compileall`、`git diff --check`：通过。
- 新增 SDK 专项测试：**2 passed**。

本轮未执行需要凭据的真实 LLM/provider 集成测试；上述 provider 差异仍需在实际 OpenAI-compatible/Ollama 部署做能力矩阵验证。

## 优先级建议

1. **锁定经过验证的 SDK/OpenAI 组合**：在部署环境记录 `agents.__version__`、`openai-agents` 和 `openai` 版本，逐个升级 minor 版本并跑完整回归；当前不要只为追最新版改依赖。
2. **先修流式重放风险**：增加模拟 provider 测试，验证在工具已执行后流失败不造成副作用重放；再设计安全的 SDK cancel/resume 或明确禁止自动二次执行。
3. **建立 provider capability tests**：针对 OpenAI Chat Completions、代表性兼容服务及每个目标 Ollama 模型覆盖文本、只读工具、写工具、handoff、structured output、streaming/tool delta、视觉、reasoning 字段和错误路径。开发期可考虑 `OpenAIProvider(use_responses=False, strict_feature_validation=True)`，但需确认与当前显式 Agent model 的组装方式。
4. **改善会话存储边界**：处理多 worker 持久化、并发、完整 turn 截断与工具 item 配对；之后再迁移到 Session backend。
5. **决定敏感 tracing 政策与写工具审批级别**：根据产品隐私要求配置 SDK trace payload 和本地日志；对删除、放弃等写操作评估显式审批。
6. **Ollama 图片兼容专项**：当前使用 OpenAI image_url data URI，而 Ollama 文档不支持 image URL；需要 provider-gated 的格式转换和真实模型测试后再修改默认路径。

原有非 SDK 审查建议仍适用：UI 主题优先提供少量内置主题；任意 UI 导入若实现，应限制为字段白名单 JSON/CSS 变量，不接收可执行 HTML/JavaScript。MCP 若监听公网，应要求身份验证并配合 HTTPS、限流及访问控制；天气 API 的自动化测试继续依赖 mock，避免 CI 依赖公共网络。

## 关键文件

- 审查报告：`/workspace/Momentum_REVIEW.md`
- 项目副本：`/workspace/Momentum-review/repo`
- Agents SDK 主实现：`/workspace/Momentum-review/repo/src/momentum_agent/agent_app.py`
- provider 配置：`/workspace/Momentum-review/repo/src/momentum_agent/config.py`
- SDK 依赖声明：`/workspace/Momentum-review/repo/pyproject.toml`
- 本轮专项测试：`/workspace/Momentum-review/repo/tests/test_agents_sdk_review.py`
- 本轮官方来源核对记录：`/workspace/Momentum-review/research-notes.md`

## 官方资料

- [Agents SDK Models：Responses/Chat Completions、自定义 base_url、tracing 与兼容性](https://openai.github.io/openai-agents-python/models/)
- [Runner 与 RunConfig API](https://openai.github.io/openai-agents-python/ref/run/)、[Runner 使用指南](https://openai.github.io/openai-agents-python/running_agents/)、[Streaming](https://openai.github.io/openai-agents-python/streaming/)
- [Agents 与动态 instructions](https://openai.github.io/openai-agents-python/agents/)、[Handoffs](https://openai.github.io/openai-agents-python/handoffs/)、[Tools 与 ToolSearch](https://openai.github.io/openai-agents-python/tools/)、[Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- [MCP](https://openai.github.io/openai-agents-python/mcp/)、[Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)、[RunState](https://openai.github.io/openai-agents-python/ref/run_state/)
- [Sessions](https://openai.github.io/openai-agents-python/sessions/)、[SQLAlchemy Session](https://openai.github.io/openai-agents-python/sessions/sqlalchemy_session/)、[Results/history](https://openai.github.io/openai-agents-python/results/)
- [Tracing](https://openai.github.io/openai-agents-python/tracing/)、[Release process/changelog](https://openai.github.io/openai-agents-python/release/)、[GitHub releases](https://github.com/openai/openai-agents-python/releases)
- [Ollama OpenAI API compatibility](https://docs.ollama.com/api/openai-compatibility)
- [OpenAI Function Calling](https://platform.openai.com/docs/guides/function-calling)、[Responses conversation state](https://platform.openai.com/docs/guides/conversation-state?api-mode=responses)
- [Agents SDK AnyLLM adapter](https://openai.github.io/openai-agents-python/ref/extensions/models/any_llm_model/)、[LiteLLM provider](https://openai.github.io/openai-agents-python/ref/extensions/models/litellm_provider/)


## Momentum 的目标与更有说服力的主线

Momentum 不是一次性作品集 demo，也不是本轮要改造成的 SaaS。当前优先级是让你本人日常持续用得顺，同时把架构、安全和工程质量做扎实；如果体验稳定，再留出未来推荐给少数他人的空间。因而多人协作、计费和多租户扩展不是当前验收条件，但按用户隔离保存偏好、明确同步边界、可恢复的数据结构和可复现测试仍值得保持。

最适合展示的主线不是“我接了 LLM/MCP”，而是“把一件模糊的事变成今天能开始并能收尾的承诺”：快速记录 → 澄清或拆出下一步 → 选择今天的重点 → 开始专注 → 完成或调整 → 回看实际进展。Agent、MCP、流式响应、tracing 和本地工具执行可以作为这条体验背后的系统设计证据；如果脱离实际任务闭环展示，它们本身不足以证明消费者产品有独特价值。

这一判断与成熟产品的公开功能相符：[Todoist](https://www.todoist.com/features) 已把快速捕捉、自然语言日期、Today/Upcoming/Filters 和进展反馈做成常见能力；[Sunsama](https://www.sunsama.com/) 强调每日规划、日历 timeboxing、Focus/Pomodoro 和收工习惯；[Motion](https://www.usemotion.com/features/ai-task-manager.html) 则以自动排程、动态重排和逾期风险提醒为卖点。因此不宜把“AI 任务管理”或“有 MCP”当作已证实的市场差异。更可验证的方向是让 Momentum 成为你可信赖的个人推进台：它记住你选了什么，帮助你更快开始，并能展示计划和实际完成之间的差别。可以先以数周自用观察“捕捉后多久开始”“今天承诺的完成率”“计划与实际专注时长差异”，再决定是否值得为其他用户加 onboarding 或扩展。

此前对功能缺口的批评也要结合代码修正：仓库已有 heartbeat 检查和浏览器通知模块，所以“完全没有提醒”不准确；更重要、但本轮没有实测的是浏览器权限关闭、页面休眠或通知投递失败时的可靠性。反过来，Momentum 的任务和主要数据仍由服务器数据库提供服务，本轮新增的只是本地图片；因此不应把整个产品宣传成 local-first。个人单用户阶段不需要预先搭建商业化或复杂多租户，但未来可分享要求保留清晰的账号边界、导出/恢复能力和权限验证。

## 本轮实现与数据边界

背景图现在分为两条明确路径。用户上传的 PNG/JPEG/WebP/GIF（上限 8 MB）以 Blob 存在当前浏览器 IndexedDB，并按浏览器中的 Momentum 用户标识隔离；服务端只收到 `source=local`、透明度等设置元数据，图片 URL 为空，图片内容不进入数据库或服务器文件。界面说明本地图片暂不能跨设备同步。公开 HTTP(S) 图片 URL 则经过协议、凭据和本地/私网主机校验，浏览器直接加载，云端保存 URL 与 `remote_url` 来源标记，可在登录同一账号的设备间恢复。两种方式都能清除，清除时尝试同步移除账户引用。

城市设置通过 Open-Meteo geocoding 搜索结果让用户明确选择（包括地区/国家），并通过天气接口检查位置是否可用；确认后的默认城市、国家/地区和坐标复用现有按用户隔离的 `user_memory` 键值存储，没有另建图片表，也不保存图片字节。外观提供多个差异明显的内置主题（纸页、墨色、苔绿、午夜）；可选 JSON 配色导入只接收白名单颜色字段与十六进制色值，不执行任意 HTML、CSS 或 JavaScript。Service Worker 缓存版本升到 v2，并预缓存新设置模块，避免旧缓存继续展示旧 UI。

界面调整以任务闭环为首屏中心，收敛空列表的块状视觉，改善侧栏标题和模型状态显示；设置仍能切换背景、主题与默认城市。保留此前 Open-Meteo 默认天气、MCP Streamable HTTP 和 Agents SDK per-run tracing 修复，没有删除现有功能、做破坏性迁移或使用生产 API 密钥。

## 实测结果与仍未确认的环节

真实浏览器在独立测试服务和隔离 SQLite 数据库上操作，截图视口为 1274×624。此前已验证切换苔绿主题、搜索 Paris 并保存法国巴黎，隔离库记录为 `巴黎`、`法国`；后续隔离 Chromium 复测补齐了天气按钮的 UI 反馈：实时请求返回 200 后，页面明确显示“天气可用：北京 · 中国，19.3°C，晴朗（Open-Meteo）。现在可以保存为默认城市。”随后只在本地隔离代理把 `/api/weather` 改为 503，页面显示“天气测试失败：隔离测试：天气服务暂不可用”。没有点击“设为默认城市”。这两条结果来自独立测试 origin，不涉及默认试用服务。

还在浏览器真实新增了“E2E验收：完成一个 Momentum 浏览器闭环”任务，再点击可见任务卡片上的“完成”。截图先显示 1 个待办和任务动作，完成后待办计数归零并刷新“下一步”提示；测试数据库中该条记录为 `done`。缓存升级后截图显示更清楚的“本地模式 · 尚未连接模型”状态、较完整的任务首屏和轻量空状态。上述截图用于实际视觉检查，没有作为持久截图附件保存。

本地背景图的最新隔离 Chromium 验收已确认刷新恢复：上传 1,680 字节 PNG 并重新加载 `/app` 后，截图仍显示该背景；只读 IndexedDB 检查在 `momentum-background-local` 的 `images` store 读到 1 个含图片数据的 1,680 字节 PNG Blob。实际 `POST /api/preferences` 请求体仅 57 字节，只含 `background` 下的 `opacity/source/url` 元数据，`source=local`、`url` 为空；观察中没有 data URI 或图片二进制签名。该结果证实本次测试图片由本地 IndexedDB 恢复，且本次偏好请求未携带图片内容，不扩大为跨设备同步保证。

这轮还发现桌面右侧设置栏不可滚动，导致天气控件被固定高度裁切。前端仅调整 `app.css` 和偏好设置 UI 可达性测试；修复后可在真实 Chromium 中滚动并使用天气按钮。Node 前端测试 53/53 通过，`git diff --check` 通过；后端未改。背景验证、天气验证使用独立 origin `127.0.0.1:37619`、`127.0.0.1:58829`，临时后端 `127.0.0.1:35145` 与独立 SQLite；没有访问 `try.db`、8765、历史专注会话或其他已有浏览器 origin。测试后已关闭标签、停止临时服务并删除 SQLite sidecar 与 PNG 夹具；没有提交或推送。[隔离浏览器验证记录](/home/ubuntu/upload/85dd0045662e6e62fbd8e271_report.md)

专注计时、Agent 拆分、复盘以及通知权限/休眠时投递并未纳入上述浏览器 E2E；这轮背景和天气的通过结果也不改变后文专注结算 SDK #1/#11 两条历史 reload 失败仍未解释的结论。

全量 Python 测试 **223 passed、10 skipped**；前端 Node 测试 **12 passed**；`compileall`、修改过的 JS 模块语法检查及 `git diff --check` 均通过。10 条跳过项沿用当前环境未配置 MySQL 集成数据库的条件。所有浏览器 E2E 写入仅发生在本轮独立测试数据库，任务试用服务 `http://100.117.187.0:8765` 的监听仍在且登录页返回 HTTP 200；它没有被重启，`/workspace/Momentum-review/data/try.db` 未被测试或清理。本轮临时 8766 服务已停止。代码仍只在本地审查副本，未提交、未推送。


## 本轮深化：真实专注时长与执行洞察

专注计时现用单调时钟累计实际运行段，暂停时间不计入。计时器开始后获得服务端会话标识；完成或提前停止时提交同一个幂等会话记录，包含实际秒数、计划时长和结束原因。保存失败可以重试；后端验证任务属于当前用户、实际秒数与计划范围及结束原因。专注记录继续写入既有 `task_events` JSON，不加表、不迁移或改写旧数据。旧版只有计划时长的记录继续保留并标记为旧记录，但不再冒充真实投入。

Insights 与仪表盘完成趋势改从任务状态变为 `done` 的事件取完成时间，不再用可被后续编辑影响的 `tasks.updated_at` 推算。任务完成周期来自创建时间到完成事件时间；平均实际专注、计划准确度和专注时段只使用真实 `actual_seconds`。统计侧栏与 14 天图表保留一位小数分钟，因此 6 秒显示为 `0.1m`，而不是被整分钟除法吞成 `0m`。`/api/focus/stats` 的会话时间也转成 ISO 8601 字符串，保证真实 HTTP JSON 响应可读。GitHub Actions 现用 Node 22 的内置测试运行器执行 `tests/frontend/*.test.mjs`，没有增加 npm 安装或构建依赖。

可控时钟测试模拟了 **2.6 秒运行 → 60 秒暂停 → 3.8 秒恢复 → 停止**，结果是 **6 秒实际投入、`stopped`**；另一个用例验证达到计划秒数后为 `completed`。这些是注入时钟的确定性 JS 测试，不是等待真实时间的手测。隔离 SQLite 实例上的认证 HTTP 验收通过 `/api/focus/start`、`/api/focus/finish` 和 `/api/focus/stats` 读回同一条 **6 秒 / stopped** 记录，`/api/stats` 也返回 200。真实 API 首次暴露的 stats JSON datetime 序列化问题已修复，并有 Python 回归测试；增加的 6 秒用例还断言 API 汇总和图表都保留为 0.1 分钟。

浏览器确实打开并截图检查了隔离实例登录入口（1280×624），但按你的要求没有提交密码、接管登录或绕过应用认证。因此开始/暂停/恢复/停止的登录后 UI 流程**尚未在真实浏览器确认**；HTTP API 验收与可控时钟测试不等同于该 UI 手测。若要手工补验，在已登录的工作台选择一个任务，依次开始、暂停一段时间、恢复并停止，确认实际时长不含暂停时间且统计显示相同会话；再让一段较短测试计划自然到期，确认结果为完成。

最终全量结果：Python **232 passed、10 skipped**；前端 Node **17/17 passed**；Python 编译、改动 JS 语法检查和 `git diff --check` 均通过。10 条跳过测试仍是当前环境没有 MySQL 集成服务；SQL 表结构没有更改。全部浏览器/API 写入都在独立 `/tmp` 测试数据库，临时 8766 服务已停止且测试库未删除；只读端口检查确认原 `100.117.187.0:8765` 仍在监听，本轮没有向其进程发信号或操作 `/workspace/Momentum-review/data/try.db`。改动仅在本地副本，未提交、未推送。


## 本轮：流式失败时不再自动重跑 Agent

`run_agent_message_stream` 原先在流式异常或结果未完成时无条件再调用 `Runner.run`。这可能在写工具已经提交副作用后重新运行同一轮。检查到当前安装的 Agents SDK 0.23.1 虽提供 `tool_called` / `tool_output` 事件，但其内部 `stream_step_result_to_queue` 是从已生成的 step result 排队通知；这不是针对业务写入的事务确认，也不能可靠证明“没有收到事件”就等于“没有副作用”。因此这轮采取保守方案：移除该路径的非流式自动 fallback，流式异常和未完成结果都返回明确的 `error` 与结束事件，提示先检查任务状态再由用户决定是否重试。即使异常发生在首个工具之前也会显式失败，不自动降级重跑；失败的流不会保存对话历史。

新增可控 Runner 回归测试：模拟写工具在 `tool_called` 后执行一次，再收到 `tool_output`，随后流中断；断言写副作用仍为一次、`Runner.run` fallback 为零，并向前端发出错误。另测工具尚未执行就发生异常的情况，仍会返回可见错误、不会自动调用 fallback。当前聊天 SSE 处理器会显示此错误并结束加载状态。

本轮全量回归：Python **234 passed、10 skipped**；前端 Node **17/17 passed**；Python 编译、相关 JavaScript 语法检查和 `git diff --check` 通过。10 条跳过仍因没有 MySQL 集成服务。临时 8766 已关闭；没有向 `try.db` 或试用服务发请求、写数据或发信号，改动仅在本地且未提交/推送。

只读监听检查显示 `100.117.187.0:8765` 仍在监听，但 PID 从此前观察到的 31448 变为 75251。新进程由 PID 1 托管，运行时长约 3 分 53 秒（早于本轮第一条工具调用）；旧 PID 已不存在。原因无法从只读信息归因，本轮没有接触该端口或数据库，也没有重启、终止或修复此服务。


## README：背景、城市与主题使用说明

原 README 只有配置向导中的默认城市条目和天气服务概述，没有写出 Web 设置的实际操作、图片本地/云端差别或主题导入格式。这轮在 README 新增工作台设置指南：指出桌面侧栏「偏好设置」和手机底部「设置」入口；说明本地图片仅存浏览器 IndexedDB（PNG/JPEG/WebP/GIF、≤8 MB），同步只含来源标记和透明度，而公开 HTTP(S) 图片 URL 同步来源、URL 与透明度、由各设备浏览器直接加载；也说明清除操作的效果。URL 校验文案收窄到代码当前确实拒绝的 localhost、`.local` 主机名、常见私网 IP 字面值及内嵌账号密码。

指南也写明城市搜索后要先选候选项，天气测试会实际请求 Open-Meteo 并返回结果或错误；确认可用后可保存城市、国家和坐标到账户偏好。主题部分列出四个内置主题，以及自定义配色需为不超过 8 KB、只含完整 12 个白名单字段、颜色为 `#RRGGBB` 或 `#RRGGBBAA` 的 JSON；示例已由项目的 `isSafePalette` 校验器实际验证通过。自定义配色只在此浏览器生效，任意 HTML/CSS/JavaScript 导入没有支持，也不会执行。

检查结果：README JSON/入口/隐私边界静态校验通过；Python **234 passed、10 skipped**，前端 Node **17/17 passed**；背景、城市和主题模块语法检查、Python 编译及 `git diff --check` 通过。文档核对同时发现背景图片检查的 timeout 默认值有异常内容；依据此前授权，在不读取或复现该内容的前提下重置为有限的 **1000 ms**，残留模板标记检查通过。原试用端口 `8765` 仍监听；本轮没有向其发送请求或信号、没有重启服务、没有读写 `try.db`，也未提交或推送。


## MCP 远程监听的 Bearer 密钥门禁

原启动入口允许 Streamable HTTP 或 SSE 绑定非 loopback 地址而不配置密钥。本轮新增 host 分类与启动门禁：stdio 不受影响；HTTP/SSE 绑定 loopback 可继续免密；`0.0.0.0`、`::`、LAN/Tailscale IP 以及除 `localhost` 外的主机名都按远程处理，空白密钥也视为未配置。缺少 `MOMENTUM_MCP_API_KEY` 时，统一入口、直接传输启动函数和两个 CLI 入口都会在打开数据库/启动监听器前拒绝，并明确提示设置该环境变量或改绑 loopback。主机名不做 DNS 解析，除明确的 `localhost` 外一律按远程处理。

配置密钥后，Streamable HTTP 的 `/mcp` 初始化及后续请求、SSE 的 `/sse` 建连及 `/messages/` 后续请求都使用启动时捕获的同一密钥；比较统一使用 `hmac.compare_digest` 对 UTF-8 字节执行，错误的非 ASCII token 也会正常拒绝而非抛出比较异常。README、`.env.example`、console-script 与 PyInstaller/python -m CLI help 已同步说明：stdio/loopback 可免密，其他 HTTP host 必须配置 Bearer 密钥。

MCP 定向测试 **78 passed**；完整 Python 测试 **270 passed、10 skipped**，前端 Node **17/17 passed**。测试覆盖无密钥远程拒绝、loopback 与 stdio 免密、带密钥远程启动、空白密钥、CLI 在打开数据库前拒绝，以及两种协议在初始化/消息阶段对缺失和错误 Bearer 的处理；鉴权路径用 ASGI 测试客户端和模拟传输验证，没有启动真实远程监听器。双 CLI help、Python 编译和 `git diff --check` 均通过。原 Web 试用端口 `8765` 仍监听；本轮只用 `ss` 查看状态，没有请求或重启它，也未触碰 `try.db`、提交或推送。


## 产品改进与最后一轮验收

代码审查后，产品侧的三项优先改进是：做可信的版本化备份恢复；刷新后可恢复专注且不虚构离线时长；把工作台建议直接连到同一任务的开始，并以完成事件和真实专注秒数做当天回顾。备份已有 v1 合并兼容、v2 空数据域原子恢复、凭据 memory 过滤和 16 MiB 备份专用限制；专注快照按用户隔离并记录已确认秒数；建议/日报 API 已实现结构化 task ID、本地时区日界与真实 session 时长口径。

另新增 `tests/test_task_api_contract.py::test_import_eof_truncated_json_returns_400_without_database_writes`：在 fresh SQLite 与随机账号上通过真实 loopback HTTP 发送备份 JSON，声明的 `Content-Length` 比实际数据多 16 字节，半关闭写端后触发 EOF；路由返回 400“请提供 JSON 数据。”，用户所有应用表前后快照不变。新测 **1/1 passed**，目标文件 **7/7 passed**；只改测试文件。该证据覆盖 SQLite 上的 raw HTTP parser/dispatcher 对截断 JSON 的拒绝与零写，不是事务中途进程中断恢复、真实 MySQL 或浏览器 E2E。

本轮已把“今天收尾”入口接入任务完成成功态。先前一条预置已完成任务的重开→再次完成→收尾→刷新路径稳定显示 2 个完成事件、137 秒和 1 个已结束专注 session；随后在全新隔离库完成了同一 `task_id` 的完整真实 Chromium 链路：建议卡直启、专注开始、结束并保存、任务完成、打开收尾及刷新读回均使用 `TASK_1`。专注以 `SESSION_1` 保存 24 秒，结果为 `stopped` 且带 `ended_at`；三次复盘请求均只带一个合法 `timeZone=UTC` 和 `localDate=2026-10-04`，刷新前后稳定为 1 个完成事件、24 秒和 1 次 session，SQLite 事件数维持 3→3，与 API 一致。

该端到端场景使用新建临时 SQLite、loopback 服务和随机测试账号，经隔离 `/api/register` / `/api/login` 建立登录态；没有填写网页密码、改源码或触碰既有数据库/服务，临时服务已关闭。[端到端记录](/home/ubuntu/upload/6f37ba86f9088abd3e90e4fd_report.md) · [脱敏 API/SQLite 对照证据](/home/ubuntu/upload/4b0526fee46d90987bc251c6_api-db-evidence.redacted.json)。这证明了本次同一任务的计划→开始→完成→收尾刷新路径；不代表所有时区边界都已覆盖，也不改变专注结算 SDK #1/#11 历史 reload 失败仍未解释的结论。
随后将复盘指标和列表标题明确为“完成事件 / 完成记录”，并说明同一任务重开后再次完成会另添一条记录；统计规则与 API 不变。隔离 SQLite/Chromium 验收中，单次完成显示 1 条；同一任务 `todo→done→reopen→done` 显示两条同标题记录，时间分别为 14:54:39 与 14:54:41；刷新后仍为 2 条，API 与 SQLite 事件相符且刷新未新增写入。1440px 桌面与 390px 视口均无横向溢出；Node **84/84**、`git diff --check` 通过。本轮只改 `advice-review.mjs` 与 3 个前端测试文件，未改 API/后端；[脱敏实测证据](/home/ubuntu/upload/84db330ccb315352736a23db_evidence.json) · [刷新后 390px 截图](/home/ubuntu/upload/54f7e5b92ab4a562e2d0987e_review-two-events-390px.png)。
随后为 today-close 中没有 `due_at` 的 `todo`/`doing` 任务加入“设置截止时间”，复用现有编辑器和保存路径；已有截止任务仍显示原截止时间与顺延入口，不改 API 或排程。隔离 Chromium 固定 `Asia/Hong_Kong` 输入本地 **2026-10-07 16:45**，仅发 1 次 PUT、仅新增 1 条 `updated`；服务端与 SQLite 的 `due_at` 为 **2026-10-07T08:45:00Z**，复盘卡即时显示 16:45，刷新后仍一致。桌面和 390×844 均无横向溢出；Node **86/86**、`git diff --check` 通过。本轮仅改前端入口与测试，未改 API/后端，也未重跑 Python 全套；[桌面刷新后截图](/home/ubuntu/upload/b1dd732b869320c4b5ab844a_desktop-after-refresh.png) · [390px 刷新后截图](/home/ubuntu/upload/af6f502c1320228ae0aa7b2b_mobile-390x844-after-refresh.png)。
随后在 today-close 的 `todo`/`doing` 任务卡加入“完成”，复用任务列表现有完成处理器及按 task ID 的同步锁；顺延和设置截止时间入口保留。新建临时用户、SQLite 与 Chromium 的隔离验收中，快速重复触发只发 1 次成功 POST、仅写 1 条完成事件；复盘即时和刷新后均为 1，目标任务从未完成区移除，其他任务数据不变。1440px 桌面与 390×844 无横溢；Node **87/87**、`git diff --check` 与 JS 语法检查通过。本轮只改前端静态资源和测试，API/后端未改；Python **395 passed / 11 skipped** 为此前后端回归，本轮未重跑 Python 全套。[脱敏验收证据](/home/ubuntu/upload/f5fad34680154e8bfc8824fb_acceptance-evidence.json) · [桌面截图](/home/ubuntu/upload/8db1dad5d59f07098abe8c4f_today-close-desktop-1440.png) · [390px截图](/home/ubuntu/upload/982ffde4903706e9473cc6aa_today-close-mobile-390x844.png)。
随后在 today-close 未完成任务卡加入“开始专注”，复用现有 focus controller；B 卡启动时仅发 1 次 `/api/focus/start`，UI 当前专注显示 B，结束后临时 SQLite 中仅有 1 条归属 B 的 session event。活动期间入口禁用且不发新请求，快速双击也只启动一场；其他任务状态与截止时间不变。1440px 桌面、390×844 均无横向溢出；Node **90/90**、4 个相关 JS 语法检查与全仓 `git diff --check` 通过。本轮仅改 `static/` 与前端测试，未改 API/后端。按现有 API 契约，session event 只在 `finish` 时落库；跨设备活动状态未验证。Python **395 passed / 11 skipped** 是此前后端全量基线，本轮未重跑。[桌面截图](/home/ubuntu/upload/d175a78566b0cb43c4c8c9fa_today-close-desktop-1440.png) · [390px截图](/home/ubuntu/upload/625cc40373fb0437f106780f_today-close-mobile-390x844.png)。

之后修复首页“下一步”建议与解释错位：`/api/advice` 返回的 `suggestion` 会从 todo+doing 排序，而旧 `advice` 仅由 todo 生成，可能点名另一项任务。前端现在将 explanation 与所选 suggestion task 绑定；若原文未包含该任务标题，则显示该任务的安全兜底说明。仅修改 `advice.js`、`advice-review.mjs`、`tests/frontend/next-step-review.test.mjs` 与 fixture；新增 doing-vs-todo Node 覆盖同时确认开始专注仍调用卡片自身的 task_id。定向 Node 与语法/diff 检查通过；本项没有 API 或浏览器 E2E。

Insights 的准确度卡补上可信度与统计范围说明：保留既有实际专注秒数/估算公式与 API，标签为“已完成任务实际专注与估算的接近度”，始终展示 `estimated_focus_tasks` 样本任务数；n=0/1 显示“样本较少，仅供参考”，n=3 不显示该提示。卡片说明“实际专注按任务累计；同一任务重开后多轮专注仍合并统计，不代表单次完成周期”，并补充“依据近30天专注记录，最多纳入最近完成的100个任务”以及“使用任务当前估时；完成后改估时会重算历史准确度”。Node 用例在 n=0/1/3 逐项断言这些说明、样本数字与低样本提示；定向 **4/4**、冻结树完整 Node **123/123**。估时仍可编辑，未增加历史快照或改公式/API。SQLite 还覆盖 `test_focus_window_uses_event_created_at_and_only_latest_100_done_tasks` 与 `test_accuracy_recalculates_from_current_estimate_after_done_task_update`：前者验证 event.created_at 近30天窗口和最新100个已完成任务，后者确认任务完成后 estimate 从20改30会令历史准确度从0.5重算为1.0，而实际专注秒数仍为30分钟。去重后的 `tests/test_insights.py` 全文件 **19/19 passed**；两项均仅测试，未改 runtime。
只读复核确认 `/api/focus/start` 校验本人任务处于 `todo`/`doing` 后，每次返回新 `session_id`/`started_at`，不持久化活动会话；SQLite session event 在 `finish` 才写入。前端同步 pending 锁与同 origin/user 的 Web Locks 可拦截常见双击和同源多标签重复启动；finish 幂等按同一用户与同一 `session_id` 去重，但不能合并不同 start。API 当前不承诺服务端用户级单活；不同浏览器 profile、设备或直接 API 调用不受这些前端锁保护。同一客户端若 `/api/focus/start` 已在服务端处理、但响应因 timeout/断连丢失，当前没有 request idempotency key 或状态查询端点可确认先前生成的 session ID；用户重试会获得新的 `session_id`，且不能称为原请求的服务端幂等回放。由于 start 不持久化活动会话，现有证据不表明这会重复写入专注事件；首次生成的 ID 对客户端不可恢复这一网络不确定边界尚无真实网络故障注入验证。界面现明确提示“无法确认开始；再次点击将从新时间重新开始”；对应 Node mock 只验证本地反馈与重试控制，不证明服务器是否处理过首个请求，也不恢复原 session。确定性 4xx 可纠正后重试。以上属于当前 API/产品边界，不是已证实的重复计时或重复记账缺陷。

为补齐服务器进程边界，新增独立测试文件 `tests/test_focus_process_race.py::test_concurrent_focus_finish_http_requests_across_processes_write_once`，仅加测试、未改 runtime。两个独立 OS server/PID 与端口共享新建临时 SQLite；跨进程 gate 确认两条相同 user/session_id/payload 的真实 `/api/focus/finish` 请求都到达后再并发放行。两请求均返回 200 且响应相同，session event 与 73 秒实际记录只落一份；从第二进程以同 session_id 重放后，全表快照不变。新用例 **1 passed**，与 `test_focus_actual_time.py` 合计 **18 passed**，另在 **5 个独立 SQLite** 上稳定复跑 **5/5 passed**；新文件 whitespace 检查通过。此处是真实 loopback HTTP + SQLite 多进程证据，不代表真实 MySQL或浏览器 E2E。

随后新增 `tests/test_focus_finish_http_recovery.py::test_focus_finish_http_rolls_back_and_same_session_retry_succeeds`，仅测试文件变更。真实 loopback `/api/focus/finish` 请求在临时 SQLite 中由 event INSERT trigger 触发 500；任务/session 相关及全表快照均无部分写。移除 trigger 后，用同一 session_id 与 payload 重试成功，仅保存一条 event 与 73 秒记录；同请求重放稳定。新用例 **1 passed**，与 `test_focus_actual_time.py` 定向合计 **18 passed**，whitespace clean。此项验证真实 HTTP + SQLite 的写入故障回滚与恢复，不代表真实 MySQL/browser。

另新增独立 `tests/test_focus_finish_conflict_process.py::test_cross_process_focus_finish_conflicting_payloads_have_one_winner`。两个独立 OS server/PID 与端口共用全新临时 SQLite，以同 user/session_id 和相同固定字段、不同 `actual_seconds` 并发提交真实 HTTP 请求；恰一 200、一 409，唯一赢家写入 event/时长。赢家原 payload 重放仍为 200，败方 payload 重试仍为 409，整表快照稳定。该测试只新增文件、未改 runtime；定向 **1 passed**，并在 **5 个独立 SQLite** 上复跑 **5/5 passed**。这是多进程 loopback HTTP + SQLite 的异 payload 冲突证据，不外推至 MySQL/browser。

另新增 `tests/test_focus_finish_response_loss.py::test_focus_finish_recovers_after_committed_response_is_lost`，仅加测试、未改 runtime。第一进程完成 SQLite 事务提交后，在 `send_json` 写出响应头前由 gate 捕获原 status/raw body 并 SIGKILL；客户端只观察到连接失败。第二进程使用同一临时 SQLite 重新登录，以原 session_id/payload 重放，精确恢复捕获的 status 与 raw bytes，event/实际秒数未增加且业务表快照稳定。新用例 **1 passed**，并在 **5 个独立 SQLite** 上复跑 **5/5 passed**。这是 `/focus/finish` 自身的跨进程 post-commit/response-loss 恢复证据，独立于 `/done` 的同类测试；不代表真实 MySQL或浏览器 E2E。
随后完成同源双标签活动专注同步的隔离验收：两个同一用户、同 origin 的 Chromium 标签并发尝试启动时，A 发出 1 次 `/api/focus/start`、B 发 0 次；两页同步显示同一 task/session，B 的 4 个 start 入口及暂停/结束控件均禁用。A 停止后 B 同步回到 idle，刷新后仍 idle；没有重复 finish，临时 SQLite 最终只有 1 条 session event，任务状态和截止时间不变。Node **95/95**、语法检查及 `git diff --check` 通过。本轮只改前端静态资源与测试，Python **395 passed / 11 skipped** 是旧基线、未重跑。验证范围仅同一浏览器/同源；跨设备、API 直调与真实 MySQL 均未覆盖。[脱敏证据](/home/ubuntu/upload/b8aac1f812e1d07e5a5f5e68_evidence-redacted.json) · [1440px截图](/workspace/Momentum-review/repo/tests/frontend/artifacts/focus-multitab-20261004-073727-3604-desktop.png) · [390px截图](/workspace/Momentum-review/repo/tests/frontend/artifacts/focus-multitab-20261004-073727-3604-mobile.png)。

另在 `tests/frontend/focus-tabs.test.mjs` 新增 `focusStart()` 重叠调用的 Node 控制器回归：第一调用处于 pending 时第二次不发 POST；最终仅一条 POST、一份 session/snapshot。此为 Node/mock 级控制器证据，不等同真实浏览器、多用户 API 或数据库 E2E；只改测试文件、runtime 未变。新增用例后 Node 全套 **121/121**。

随后在同一 `tests/frontend/focus-tabs.test.mjs` 新增确定 409 后显式 retry 成功的 Node controller/mock 用例：首次 mock 拒绝后验证错误显示、pending/phase/按钮复位、锁释放且无 owner/snapshot；用户第二次明确调用后请求成功，只保存第二次返回的 session。定向 **7/7**、全 Node **122/122**；只改测试文件、runtime/API 未变。该 409 是测试 mock 输入，不代表真实 API 行为；也不验证 timeout/断连后服务端 session 状态。

另仅改 `focus.js` 与 `tests/frontend/focus-tabs.test.mjs`，为没有 status 的网络/timeout 错误显示“无法确认开始；再次点击将从新时间重新开始”，保留确定 4xx 的原 formatter。timeout mock 断言不自动重发、状态仍 idle、pending/锁复位；只有用户显式再次点击才发起新的 start 并保存新 session。定向 **8/8**，Node 全套 **123/123**；仅为 UI/controller mock 证据，不是实际 API/网络故障验收、原 session 恢复或服务器幂等证明。
幂等补丁前的只读源码核对发现周期任务完成存在代码级幂等缺口：`handle_done_task` 调用 `complete_recurring_task`；其内部 `update_status` 在事务/锁中只对真实状态转移写一条 `status_changed`，但只要返回的源任务带 recurrence，每次调用都会在该事务之外新建下一期；没有按源完成 occurrence 的唯一映射或幂等键。故同一已完成周期任务的顺序重放、响应丢失后重试或并发 `/done` 可能产生多个下一期，而源任务仍只有一条完成事件。现有测试只覆盖单次周期任务完成，缺 HTTP 重放/并发覆盖。证据为源码路径，不是已执行 API 复现；MySQL 行锁也只读到源码机制，真实 MySQL 未测（`handlers.py` 80–92；`sqlite.py` 337–369、653–668；`mysql.py` 440–508、854–869）。
Web `/done` 已按确认契约加入必需 UUIDv4 `Idempotency-Key` 请求账本：账本按认证用户和 key 检索，指纹为 canonical `{method, task_id}`；命中相同指纹原样重放保存的 HTTP 状态/JSON，异指纹返回 `409 idempotency_conflict`。每次真实非 `done→done`（含 reopen 后再次完成）生成一条完成 occurrence；同一事件至多映射一条 recurring 下一期。已 done 时新 key 为无写 `200 already-done`；旧库已有 done、无映射的情况按 no-op，不回填历史。任务列表和 today-close 两个完成入口共用持久 pending UUID；页面加载不自动发请求，结果未确认时显式重试复用原 key。
事务内包含 request ledger、目标状态、完成/级联事件、recurring 下一期及 occurrence mapping；SQLite 使用 `BEGIN IMMEDIATE`，MySQL 路径有行锁代码及 SQL mock，但真实 MySQL仍未测。旧 SQLite/MySQL 仅加 ledger/mapping 表，不猜测性回填。真实 loopback HTTP `/done` 测试加入 `tests/test_done_http_contract.py`；另经只读检查确认 `test_done_rejects_missing_or_invalid_key_over_http_without_any_writes` 用 ThreadingHTTPServer 覆盖缺失、格式错误与非 UUIDv4 key 的 400，且 task/event/occurrence/ledger 快照均无写入；此为既有测试覆盖核对，未重复加测。Web `/done` 阶段定向 **122/122**，当时 Python **429 passed / 11 skipped / 0 failed**、Node **102/102**、compileall 71 个 Python 文件通过；11 项因未配置真实 MySQL URL 跳过。tracked/staged diff check 通过；早先 Rak4 任务遗留的 untracked `tests/frontend/task-card-responsive.test.mjs` EOF 空行告警未改，且不在 tracked diff 检查范围。README 已补上完成接口契约；该文档编辑后未重跑 runtime tests。此处数据是 Web `/done` 阶段基线，不代表后续 Agent/CLI 变更后的最新回归。
随后为专注休息面板 `focusDone()` 补齐失败态反馈，仍在 `/api/focus/finish` 成功后调用共享 `completeTask(taskId)`，并复用原 task-scoped UUIDv4 pending。只有任务完成明确成功才关闭/重置面板；不确定/pending 时保留休息面板与“待确认”反馈，且仅当存在匹配、状态为 uncertain 的 pending intent 才展示显式同 key retry，不会自动重发或重做 `/focus/finish`。带明确 4xx status 的确定错误显示原始错误、标为 alert 且不给 retry；401/timeout 等无 status 错误仍按 uncertain 处理。新增/扩展 `tests/frontend/focus-completion-idempotency.test.mjs` 覆盖以上状态分流；定向 **12/12**、当前前端 Node 全套 **115/115**、相关 JS 语法检查与 `git diff --check` 通过。仅改 `focus.js` 与该 Node 测试；本轮未跑浏览器 E2E或后端测试，也未改 `/focus/finish`。Node mock 结果不等同真实 HTTP/浏览器验收。

随后，Agent `complete_task` 与 CLI `done` 分别按“每次真实非 `done→done`（包括 reopen 后再次完成）恰生成一个 recurring 下一期；已完成状态重复调用不再生成；普通任务不建下一期”的口径完成统一。Agent 路径 SQLite 定向 43 passed、当时 Python **438 passed / 11 skipped**、Node **103/103**、compileall 118 个文件通过；Agent 没有稳定调用 ID，occurrence 去重不承诺请求响应重放，MySQL 仅有 SQL mock、真实 MySQL 未测。CLI 路径真实子进程测试覆盖首次完成、顺序重复、reopen 后再完成及普通任务；定向 **10 passed**，当前串行全量为 Python **439 passed / 11 skipped / 0 failed**、Node **103/103**、compileall 119 个文件及 diff checks 通过。

CLI 完成后另做了只读的真实并发验收：两个独立 CLI 子进程在本轮新建 SQLite 文件的 `BEGIN IMMEDIATE` 入口同时就绪，核实两者均连接同一临时库后放行，实际重叠约 **4.02 ms**。两个并发 `done` 均以退出码 0 结束，一个创建下一期、另一个报告任务已完成；最终恰有一条 done event、一个 occurrence mapping 和一个 next。之后 reopen 再完成，三者各增至 2 且 occurrence 与完成事件对应；仓库状态指纹前后相同，本次并发验收未改代码。该结果只证明隔离 SQLite/CLI，不是 MySQL 证据；本机没有 `mysqld`、`mariadbd`、Docker 或 Podman，未启动或连接任何 MySQL。[并发验收 JSON](/home/ubuntu/upload/79945e28b2c164d1ff607afe_verification-report.json)。

另新增 `tests/test_cli_recurring_completion.py::test_cli_recurring_completion_post_commit_output_loss_is_idempotent`，仅加测试。隔离 hook 调用原完成方法并确认其返回（事务已提交）后，先 fsync 一个 marker、再以退出码 86 结束子进程，因此第一次 CLI stdout 为空；数据库快照确认源任务已完成且只有一条 DONE event、一个 occurrence、一个 next。第二次 CLI 使用受限环境与同一新 SQLite 重试，正常报告“已处于完成状态”，全表业务快照不变。目标文件 **3/3 passed**；新用例另在 **5 个独立 basetemp/SQLite** 上复跑 **5/5 passed**（每次均 1 passed），并纳入冻结树全量。此项只证明 SQLite CLI 状态写入唯一与失败后安全重跑，不是 HTTP 原响应字节回放，也未验证真实 MySQL/browser。

随后把 CLI recurring done 的跨进程 SQLite 竞争固化为 `tests/test_cli_recurring_completion.py::test_cli_done_cross_process_recurring_completion_is_atomic`，仅增自动化测试。两个真实 CLI 子进程使用同一新 SQLite，在 `BEGIN IMMEDIATE` 前经 gate 同步放行；两进程均成功退出，但全库只有一条 DONE event、一个 occurrence 和一个 next task，输出分别表示创建下一期与任务已完成。该用例在 **5 个独立 SQLite/basetemp** 上复跑 **5/5 passed**；子进程环境仅用 allowlist，dotenv fail-fast 禁用。它是隔离 SQLite + 真实 CLI 子进程证据，不是 HTTP、MySQL 或浏览器验证。

此前的只读真实 loopback HTTP + 临时 SQLite 并发验收，随机用户经 `ThreadingHTTPServer` 注册/登录后，两请求以不同 UUIDv4 key 同时经 dispatcher 到达 `/done`；首轮和 HTTP reopen 后第二轮均为双 200，各轮只新增一条 DONE event、一个 occurrence mapping、一个 next。相同 key 的重试精确返回原状态与 body；服务、线程及临时 DB/sidecar 均已清理，报告 JSON 保留。该运行时验收只证明 loopback HTTP + SQLite，不代表浏览器或 MySQL。[运行时验收报告](/home/ubuntu/upload/1b2655d799808fb325bb701d_report.json)。

之后将该路径固化为 `tests/test_done_http_contract.py::test_concurrent_done_dispatch_is_atomic_across_reopen_and_exactly_replayable`，仅新增/修改测试文件、不改业务运行时代码。测试用随机用户和真实认证、随机 loopback `ThreadingHTTPServer`，在两轮中用不同 UUIDv4 key 并发请求同一 recurring 任务，中间经 HTTP reopen；每轮断言双 200、恰一条 done event / occurrence / next，另断言五次同 key 重放的 HTTP status 与 raw body 完全一致且无额外写入。该自动化回归定向 **45 passed**；当时全量 Python **440 passed / 11 skipped**、Node **103/103**、compileall 119 个 Python 文件及 diff checks 通过。测试只覆盖真实 loopback HTTP + SQLite，不代表浏览器 E2E 或真实 MySQL。

另外做了服务重启后的 one-off 持久回放验收（未改仓库代码或测试）：首次真实 `/done` 返回 200 后，第一台 `ThreadingHTTPServer` 已 shutdown/close/join 并清除 `_store_cache`；同一轮新建 SQLite 文件上再启第二台服务并重新登录，用原 UUIDv4 key 重试，得到与首次完全一致的 HTTP status 和原始 body 字节。除重新登录新增的一条 session 行外，tasks/events/occurrences/next/ledger 与全表计数均未改变；临时 DB/sidecar 已清理。此为一次性 loopback HTTP + SQLite 运行时验收，不是自动化回归，也不代表浏览器 E2E 或 MySQL。[脱敏重启验收报告](/home/ubuntu/upload/report.md)。

随后将服务重启后的持久回放从 one-off 结果固化为 `tests/test_done_http_contract.py::test_done_http_replays_persistent_idempotency_after_server_restart` 自动化回归，仅改该测试文件，未改业务运行时代码。真实 loopback HTTP 中，临时 SQLite 上首次完成 recurring 任务后关闭并 join 第一台 server、清 store cache，再用同库启第二台 server、重新登录并带原 UUIDv4 key 重放；HTTP status 与原始 body 字节完全一致，除新增 session 外，task/event/occurrence/next/ledger 等非 session 快照不变；测试同时断言 DB 路径与服务/线程清理。注意这只是在同一 Python 进程里重建 HTTP server/store，不是 OS 进程退出后重启；真正的进程级验证另见下段。该测试 **1 passed**，HTTP contract 文件当时 **7 passed**；当时 Python 全量 **441 passed / 11 skipped**，Node **103/103**，compileall 119 个 pyc，diff checks 通过。

随后新增独立文件 `tests/test_done_process_restart.py::test_done_http_replay_survives_independent_python_process_restart`，仅新增测试、未改 runtime。该测试启动两个不同 OS Python 子进程，各自运行真实 loopback `ThreadingHTTPServer`/`MomentumHandler` 并使用同一个 `tmp_path` SQLite；第一进程经 HTTP 注册、建 recurring task 并完成后以 SIGTERM 优雅退出，确认端口关闭，再启动第二进程、重新登录，用原 UUIDv4 key 重放。状态码与 raw body 完全一致，快照确认原完成 event/occurrence/next/ledger 未重复写入；两个子进程和端口均清理。独立用例 **1 passed**；其后 Python 全量 **444 passed / 11 skipped**，11 项因未配置真实 MySQL URL 跳过；MySQL未测。Node **104/104** 是此前 A→B→A mock 回归结果，本轮 OS/Python 测试后未重跑；diff 与 staged checks 通过。该用例是真实 OS 进程重启 + loopback HTTP + 临时 SQLite 证据，不代表真实 MySQL或浏览器 E2E。

不同于上段的跨进程顺序重启 replay，新增 `tests/test_done_process_restart.py::test_concurrent_done_http_requests_across_independent_processes_write_once`，仅增测试。两个不同 OS Python server 进程与端口共享同一全新临时 SQLite；跨进程 gate 确认两条真实 `/done` 请求同时到达后放行，针对同一 user/task/key 均返回 200 且 raw body 完全一致，最终仅一条 DONE event、occurrence、next 与 ledger。对两个 server 分别用原 key replay 后，状态码/body 均精确一致且数据库快照不变。用例 **1 passed**、目标文件 **2 passed**；随后在 **5 个独立 basetemp/SQLite** 上各复跑一次 **5/5 passed**，未改代码。此证据验证跨 OS 进程的 SQLite/HTTP 竞争，不等同于前述单进程多线程并发、顺序进程重启、真实 MySQL或浏览器 E2E。

另新增跨进程同 task、不同 key 竞争回归 `tests/test_done_process_restart.py::test_concurrent_done_http_requests_with_distinct_keys_across_processes_replay_write_once`，仅增测试。两个独立 OS server/PID 及端口共享全新临时 SQLite；gate 确认两个真实 `/done` 请求同时到达后，以同一 user/task、两个不同 UUIDv4 key 并发提交。两请求均为 200，其中恰一响应创建 recurring next，另一响应为稳定的 already-done；仅一条源 DONE event、occurrence 与 next task，但两个 key 各有一条 ledger。分别在两个 server 上用各自原 key 重放，status/raw body 完全一致且应用表快照不变。定向用例通过，目标文件 **4 passed**；另在 **5 个独立 basetemp/SQLite** 上复跑 **5/5 passed**，并纳入后续唯一统一回归。这是 SQLite 多进程 HTTP 证据，不代表 MySQL或浏览器 E2E。

跨进程并发的另一种竞争是同 user/key 被用于不同 task。新增 `tests/test_done_process_restart.py::test_same_key_different_task_ids_conflicts_across_processes_and_replays_exactly`，仅增测试：两个独立 OS server/PID 与端口共享同一新 SQLite，gate 同步两个请求后同时提交不同 recurring task；结果恰一 200、一 409 `idempotency_conflict`，仅赢家 task 写 DONE event/occurrence/next 与唯一 ledger，输家 task及其完成副作用均不变。赢家原 key replay 精确，输家原 key 重试仍为 409，快照稳定。用例 **1 passed**、目标文件 **3 passed**；随后在 **5 个独立 basetemp/SQLite** 上复跑 **5/5 passed**。它与单进程多线程异 task 冲突测试、跨进程同 task 双 200 write-once、跨进程顺序重启 replay 分别记录；全部只证明真实 HTTP + SQLite，不代表 MySQL/browser。

新增事务提交后丢失 HTTP 响应的跨进程恢复回归 `tests/test_done_process_restart.py::test_done_http_recovers_after_committed_response_is_lost`，仅增测试、未改 runtime。第一 OS server 经真实 `/done` 完成 recurring task；测试在事务已提交、任何 HTTP 响应头写出前暂停并 SIGKILL 该进程，客户端仅观察到连接失败。新 OS server 使用同一隔离 SQLite 重启并重新登录，以原 UUIDv4 key 请求，精确恢复已保存的 HTTP status 与 raw body；业务表快照无变化，session 行单独核对。定向 **1 passed**、目标文件 **5 passed**；另在 **5 个独立 SQLite** 上稳定复跑 **5/5 passed**。这是跨进程真实 HTTP + SQLite 的 post-commit/response-loss 证据，不代表真实 MySQL或浏览器 E2E。

随后新增跨用户同 UUIDv4 隔离回归 `tests/test_done_http_contract.py::test_done_http_idempotency_key_is_isolated_between_users`，仍只改测试文件。两个随机用户经真实 loopback 注册/登录，各自创建 recurring 任务后共用同一 key 分别完成；两人各得独立 200、DONE event、occurrence、next 和 ledger，同用户重放各自原响应，所有用户/全表快照检查均确认另一用户数据不变。该用例 **1 passed**、HTTP contract 文件 **8 passed**，当时全 Python **442 passed / 11 skipped**，diff/staged checks 通过；Node **103/103** 和 compileall 119 pyc 是此前结果，本轮未重跑。这条同 key 正向隔离用例本身没有另发 A 用户 token 访问 B 用户 task 的负向授权请求，覆盖范围限于 `/api/me`、任务列表、DB owner 及各自完成/重放与快照断言；后续补充的负向归属测试见下段。11 项因未配置 MySQL 测试 URL 跳过；真实 MySQL与浏览器 E2E仍未验证。

随后新增 `tests/test_done_http_contract.py::test_done_http_cross_user_task_is_hidden_and_does_not_consume_idempotency_key`，先在真实 loopback HTTP + 临时 SQLite 中发现：A 用户用自己的 token 对 B 的 task 发合法 `/done` 请求虽返回通用 404，却新增 A 的 ledger 行、占用了该 UUIDv4 key。经批准后，后端仅改 SQLite/MySQL storage adapters 与相关测试：对新 key 先按 user+task 查询归属，foreign/missing task 直接返回通用 404，不创建 ledger；已有 key 的 replay/conflict 检查顺序保持不变。修复后的 HTTP 回归确认 foreign 和 missing 两种 404 的响应完全相同，A/B task、event、occurrence、ledger 及全表快照均不变；同一 key 随后可分别由 B、A 在各自任务上使用，独立完成并各自产生一套 event/occurrence/next/ledger 与各自响应。HTTP contract 文件 **9/9**，SQLite 定向与 MySQL 内存 mock 用例各 **1/1**，相关测试集合 **94 passed / 11 skipped**，当时 Python 全量 **446 passed / 11 skipped**，Node **104/104**；`compileall` 检查 `src/tests` 的 75 个 Python 文件成功，`git diff --check` 与 staged diff check 均通过。11 个 MySQL 集成测试仍因未配置 URL 而跳过，MySQL adapter 这里只由内存 mock 覆盖，未连接真实 MySQL。

在该负向归属修复后，又为“同一用户把同一个 UUIDv4 key 并发用于两个不同 task”补上 `tests/test_done_http_contract.py::test_done_http_same_user_same_key_concurrent_different_tasks_conflicts_and_replays_winner`，仅新增测试文件内容、不改运行代码。真实 loopback `ThreadingHTTPServer` + 新临时 SQLite 中，屏障同步两个请求，结果恰一 200、一 409 `idempotency_conflict`；赢家只有一条 DONE event / occurrence / next / ledger，输家保持 todo 且无写入，赢家同键重放原响应，输家重试仍冲突，后续快照不变。用例 **1 passed**、HTTP contract 文件 **10 passed**、当时 Python 全量 **447 passed / 11 skipped**，diff/staged checks 通过；随后将该并发用例在 **5 个独立 basetemp/SQLite** 上各运行一次，**5/5 passed**，未改文件。验证只覆盖 loopback HTTP + SQLite，真实 MySQL与浏览器 E2E仍未验证。

随后把两个剩余组合分别固化为独立 HTTP 回归，均只改 `tests/test_done_http_contract.py`。`test_done_http_same_task_same_key_concurrent_requests_replay_one_result` 通过真实 dispatcher 同步两条同 user/task/key 请求，双双 200 且 raw body 完全一致，账面仅一条 DONE event、occurrence、next 与 ledger；重复回放原响应无额外写入。`test_done_http_same_key_concurrent_users_have_isolated_results_and_replays` 则让两个 user 对各自任务并发使用相同 key，双方独立得到 200、各自 event/occurrence/next/ledger 与不串数据的响应，之后各自精确重放。每个新用例 **1 passed**，HTTP contract 文件 **12/12**，最新全 Python **449 passed / 11 skipped**，diff/staged checks 通过。两项均为真实 loopback HTTP + 临时 SQLite，不代表真实 MySQL或浏览器 E2E。

最后补充 `tests/test_done_http_contract.py::test_done_http_last_recurring_child_completes_parent_without_parent_next`，仅新增 HTTP contract 测试。真实 loopback `/done` 完成最后一个 recurring child 后，父任务按既有语义自动变为 DONE、只写一条 DONE event，不产生 parent occurrence 或 next；明确完成的 child 只写一条 DONE event，并恰生成自己的 occurrence 与下一期，next 保留 child 的 recurring/parent 关系。响应和 ledger 均指向 child 的 next，原 key 重放 status/raw body 一致且快照不变。该用例 **1 passed**、HTTP contract 文件 **13/13**，最新 Python 全量 **450 passed / 11 skipped**，diff checks 通过；Node **104/104** 是此前前端回归结果，本轮未重跑。范围为 loopback + 隔离 SQLite，真实 MySQL与浏览器 E2E仍未验证。

随后新增 `tests/test_done_http_contract.py::test_done_http_rolls_back_ledger_insert_failure_and_retries_same_key`，只追加测试文件内容。临时 SQLite trigger 故意让真实 loopback `/done` 在写 request ledger 时失败并返回 500；任务、event、occurrence、next 与 ledger 的全表快照均无部分写入。移除隔离故障后，以相同 task/key 重试返回 200，只留一套完成结果，原始响应可精确重放且快照不再变化。该用例 **1 passed**、HTTP contract 文件 **14 passed**、当时 Python 全量 **451 passed / 11 skipped**，diff check 通过；仅为 SQLite + loopback 故障注入，不代表真实 MySQL 或浏览器 E2E。

再新增 `tests/test_done_http_contract.py::test_done_http_rolls_back_next_task_insert_failure_and_retries_same_key`，仅追加 route 测试。临时 SQLite trigger 针对 recurring next-task INSERT 注入失败，真实 loopback `/done` 返回 500，完整快照无任何部分写；移除 trigger 后同 task/key 重试返回 200，只生成一套完成结果，raw response 精确 replay 且快照保持不变。该用例 **1 passed**、HTTP contract 文件 **15/15**、当前 Python 全量 **452 passed / 11 skipped**，diff checks 通过。此为隔离 SQLite + loopback 故障注入，不代表真实 MySQL 或浏览器 E2E。

再补充 `tests/test_done_http_contract.py::test_done_http_rolls_back_occurrence_mapping_insert_failure_and_retries_same_key`，仅新增测试。临时 SQLite trigger 使 occurrence mapping INSERT 在真实 loopback `/done` 中失败并返回 500；source task/status、DONE event、next、mapping 与 ledger 均完整回滚。移除 trigger 后同 task/key 重试成功，occurrence 指向实际 done event 与 next，原始响应精确 replay 且快照不变。该用例 **1 passed**、HTTP contract 文件 **16/16**、最新 Python 全量 **453 passed / 11 skipped**，diff checks 通过；证据仅为隔离 SQLite + loopback，不代表真实 MySQL或浏览器 E2E。

最后为 source DONE event INSERT 失败补上 `tests/test_done_http_contract.py::test_done_http_rolls_back_source_done_event_insert_failure_and_retries_same_key`，仅新增 route 测试。临时 SQLite trigger 使真实 loopback `/done` 返回 500，完整用户快照无部分写；移除 trigger 后同 task/key 请求成功，并能精确 replay 原响应。该用例定向 **1 passed**；随后在 **5 个独立 basetemp/SQLite** 上各运行一次，**5/5 passed**，无代码变更；它已纳入下述当前树全量回归。此处只验证隔离 SQLite/HTTP，不代表真实 MySQL或浏览器 E2E。

随后扩展 `tests/test_done_idempotency.py::test_sqlite_migration_preserves_legacy_completed_recurring_without_backfill`，仅改测试文件。它在临时旧 schema 中预置含 `recurrence`/`user_id` 的已完成 recurring task 和既有 DONE event，但不建新的 ledger/occurrence 表；当前 store 初始化后两表自动 additive-create，既有 task/event 原值不变、task 数不增且没有 next/occurrence。之后以合法 user/key 调用和重放已完成任务，均稳定返回既定 `200 already-done`；只创建一条 request ledger，不新增 DONE event、occurrence 或 next。新增用例 **1 passed**、目标文件 **30 passed**，全 Python **451 passed / 11 skipped**，diff/staged checks 通过。该迁移验证只用 tmp SQLite；真实 MySQL migration与浏览器 E2E未验证。

Agent 路径另补 `tests/test_agent_task_completion.py::test_agent_tool_concurrent_recurring_completion_creates_one_next_task`，仅新增测试。两个线程用屏障并发调用实际公开 `complete_task` tool wrapper，经 `_invoke_function_tool` 执行，临时 SQLite 上结果分别为唯一一次创建 next 与另一调用观察到已完成；恰有 1 条 DONE event、1 个 occurrence、1 个 next，Agent 无稳定 request ID，因此 ledger 为 0。定向 **1 passed**、该测试文件 **10 passed**；当时 Python 全量 **443 passed / 11 skipped**。这验证的是 Agent 工具 wrapper 并发状态转移，不代表真实 MySQL或请求响应重放；Node/compileall 没有在这轮 Python 测试后重跑。

前端另补 `tests/frontend/task-completion-user-isolation.test.mjs`，Node 定向 **1/1**；当前工作树重跑 `node --test tests/frontend/*.test.mjs` 为 **104/104**，Python `compileall` 与相关 whitespace/staged checks 通过。该用例仅新增 Node 测试、未改 runtime；用隔离 localStorage/fetch mock 覆盖 A 发起后响应丢失、切到 B 看不到/不能重试 A 的 pending、B 生成自己的 key、切回 A 不自动 POST 且显式 retry 复用原 user/url/method/body/key。响应/服务端完成效应由 mock 模拟；Node 自动化结果不等同真实 HTTP/SQLite 或浏览器 E2E。

浏览器端端到端仍未执行，不能记作通过或产品功能失败：首次隔离尝试在注册时遇 `users.id` 唯一约束 `409`，未发 `/done`；另一次服务启动后六个 `.mjs` 返回 `500`，原因未确认；随后用当前 repo 的显式 `src` 启动并确认六个 `.mjs` 均为 `200`，但获准的 in-app browser `openTab` 返回 `PERMISSION_DENIED`，context 未创建，未注册/登录或发 `/done`。服务、临时库与脚本已清理。后来在用户明确要求打开页面后，通过获准的 In-App Browser 打开隔离新库的公开首页；这不是绕过先前 `PERMISSION_DENIED` 的路径替代，也没有登录或执行功能操作，因此不构成浏览器 E2E。SDK #1/#11 历史 reload 失败仍未解释，真实 MySQL未验证，改动未提交/未推送。

SHA-256 fingerprint 与严格 `days` 后端改动后的串行全量回归为 Python **406 collected、395 passed、11 skipped、0 failed**；当时 Node **83/83**，review contract **19/19**（含于 Python 全套），顺延幂等定向测试 **40/40**（SQLite 26、MySQL SQL mock 14），compileall 与 diff check 均通过。11 项因未配置 `MOMENTUM_TEST_MYSQL_URL` 跳过，未连接真实 MySQL。此 Python 全量结果早于下述前端文案/展示改动；前端改动后没有重跑 Python 全套。


随后补做了纯隔离香港本地日边界验收：真实 Chromium 151 使用 Playwright `BrowserContext.clock` 固定浏览器时钟，服务器仍走独立 UTC 时钟；新建临时 SQLite、随机测试账号和 loopback 服务，浏览器外网请求受阻，本轮未改源码或测试。香港 2026-10-04 23:59（15:59Z）请求 `Asia/Hong_Kong` / `2026-10-04`，得到 1 个完成事件、601 秒 / 1 场；跨到 2026-10-05 00:01（16:01Z）后请求日正确切换，边界完成事件归新日，120 秒跨午夜 session 整段加上恰在边界结束的 30 秒 session，共 150 秒 / 2 场。相同绝对时刻的 UTC 对照均请求 `2026-10-04`。四种时区/时刻组合各经页面实际操作并刷新复查（8 次 `/api/review` 均为 200）；SQLite 的 5 条事件及签名摘要前后不变，无新增或重复。前端 Node **55/55**、review 契约 **19/19**；本轮没有改代码。

此复盘按 `localDate` 查询完整自然日，并不按浏览器“当前时刻”截断；因此 UTC 15:59 对照会包含测试 fixture 中 16:00Z 的事件，这不代表该事件在 15:59 已发生。[香港边界验收报告](/home/ubuntu/upload/c2bf01812791046983f384f3_report.md) · [脱敏请求与 SQLite 证据](/home/ubuntu/upload/b467f13fdbf7d8e6ca2012ee_sanitized-evidence.json) · [香港 23:59 页面](/home/ubuntu/upload/fca93abc56dee258b945c0f8_Asia_Hong_Kong_1559.png) · [香港 00:01 页面](/home/ubuntu/upload/2d38ad8af0ce872c481216e2_Asia_Hong_Kong_1601.png)

首版 today-close 前端把待处理项限定为 `todo`/`doing`：已有 `due_at` 才可操作，文案为“顺延现有截止日 1 天”；无截止时间会解释不可用，请求中锁按钮并对 404/409 给反馈。首版 Node mock 为 **68/68**；后续双入口共享控制器与幂等键实现，见本节最新 E2E **82/82**。

后端随后完成数据隔离补丁：owner 条件更新零行时不写 `updated` 事件、不做无 owner 的全局读取，普通任务 PUT 对不存在/非本人统一 404；推迟仅接受有截止日且状态为 `todo`/`doing` 的本人任务，缺失/非本人返回 404，无截止日或非活动状态返回 409，失败不写事件。SQLite/API 定向测试 **74 项通过**；MySQL SQL 模拟 **19 项通过**、另有 **11 项**因未配置真实测试连接而跳过；真实 MySQL 未验证，代码未提交。

随后在新临时 SQLite 和真实 Chromium 完成顺延主路径联验：单次 UI 操作发出一个 `{days:1}` POST（200），目标 `due_at` 精确增加一天，仅新增一条 `updated` 事件；刷新后截止日保持，复盘的完成数/专注秒数/场次不变。未认证、无效路径 ID、确认不存在的 ID、有截止日但无 `due_at`、done、dropped 分别返回 401/400/404/409/409/409，均未写事件。主路径单次成功和这些边界状态已由隔离 SQLite 直接核对；[脱敏浏览器/HTTP/SQLite 证据](/home/ubuntu/upload/60ca04db6d24d578be88cb52_report.json) · [桌面成功截图](/home/ubuntu/upload/b10f1d6e1893383bb19f4ed2_desktop_post_success.png)。

独立临时用户 A/B 的 API 隔离回归随后覆盖非本人 PUT/postpone：B 对 A 的任务执行两类请求时，都与不存在的 ID 返回相同通用 404；响应不含 A 的任务标题/截止日，任务快照未变、事件数和 `updated` 数均无增长。该定向测试 **11/11 通过**、`git diff --check` clean；这一轮只改测试文件，不是 Chromium UI 实测，也未验证真实 MySQL。

服务端为顺延 API 实现必填 UUIDv4 `Idempotency-Key`：按认证用户与 key 保存规范请求指纹及原响应；同 key、同请求安全重放，不再顺延，同 key 不同请求冲突；成功更新、单条 `updated` 事件与幂等记录按事务提交，旧 SQLite 启动时自动建表。严格 `days` 校验保留省略值默认 3，仅接受显式 JSON 正整数；无效值及日期运算越界在写入前拒绝。规范请求指纹在保留 JSON 类型差异后存为固定 64 字符 SHA-256 十六进制摘要，不受长正整数撑大 `VARCHAR(255)` 字段的影响；新增摘要实现与长值测试不改变外部 API。
锁语义只读审查显示：SQLite `BEGIN IMMEDIATE` 会串行化同一数据库的写事务；等待超时约 5 秒，异常会回滚，已提交者可被重放、回滚者可重试。MySQL 同一实例/库路径按 user+key 获取 10 秒命名锁；锁超时发生在事务和写入前并返回 500，事务写失败会 rollback，`finally` 释放命名锁，连接关闭也会释放。唯一约束是兜底而非冲突后的重读恢复：若发生唯一键插入冲突，当前任务/事件/幂等事务回滚并返回 500。命名锁不含 database/schema 名，同一 MySQL 实例不同库中的相同 user/key 可能不必要串行或超时，但不会混用库内幂等数据。以上是源码路径和 SQL mock 可见的语义；真实 MySQL 锁、释放与提交失败路径尚未实测。
随后新增 `tests/test_mysql_focus_lock_cleanup.py` 的 **2 项 SQL mock** 回归：模拟 `/focus/finish` INSERT 与 commit 异常，核对 rollback/connection close、命名锁释放及同 session 重试成功。该测试证明 mock 观察到的调用顺序与清理分支，不是 MySQL server 的锁/事务集成结果；真实 MySQL仍未验证。
只读复核 `/postpone` 的 foreign/missing task 行为：SQLite/MySQL 都先按调用者 `user_id+key` 查既有账本；新 key 的 owner-scoped task 查询若未命中，返回通用 404 后会把该确定响应记入调用者自己的账本。已有测试明确要求该 404 可同键重放；因此同一调用者随后把该 key 用于另一个 task/fingerprint 会得到 409，而其他用户可独立使用同一 key。此行为符合已确认的“确定响应同键缓存”契约，不会修改或泄露另一用户任务，也未发现新的跨用户数据缺陷；本轮只是源码/测试审查，没有运行时复现或改代码。与 `/done` 不同，不应在未重新确认契约前移除该缓存行为。
README 随后补充 Web 顺延 API 契约：保留原 CLI 示例，说明 `Idempotency-Key` UUIDv4、默认 `days=3`、新意图用新 key、未决重试复用原 key/body；新增 `days_invalid` 与 `days_out_of_range` 均为 400 且在任务、事件、幂等记录写入前拒绝，并列出 400/404/409 错误边界与 curl 占位示例。仅文档修改，`git diff --check -- README.md` 通过，没有重跑 runtime tests。[当前 README](/workspace/Momentum-review/repo/README.md)。

前端两入口共用 controller，并按用户/任务持久化 pending UUID：请求先保存；重载不自动 POST；结果不确定时 GET 只更新显示，不清 pending；显式重试复用原 key/body；确定结算后才清除，新的明确点击才生成新 UUID。

前一轮真实隔离 E2E 使用当前共享树静态模块、真实 Momentum HTTP handler、全新 SQLite、随机 API 用户与独立 Chromium。共 7 个 page POST 均带 UUIDv4 与精确 `{"days":1}`，每次只转发一次且均为 200。旧任务列表与 today-close 的快速双击分别只产生 1 个 POST/1 条 `updated`；两种入口间的响应丢失场景均在 reload 后通过另一入口显式复用原 key/body，服务器返回原响应，不再增加截止日或事件；随后新 key 的明确点击才再次顺延一天。无截止日期两入口禁用且零请求，复盘指标前后均为 0 项/0 秒/0 次；390×844 页面无横向溢出。[本轮摘要](/home/ubuntu/upload/summary.md) · [脱敏 E2E 结果](/home/ubuntu/upload/e2e-results.json)

随后真实隔离 pending UUID 的 A→B→A E2E 通过。A 的第一次请求由服务端成功处理，但页面响应被丢弃；隔离 SQLite 只顺延一天、写 1 条 `updated` 和 1 条幂等记录。切换到 B 并刷新后看不到 A 的任务/pending/retry，也没有 POST；切回 A 后从另一入口显式以原 key/body 重试，原始响应哈希相同，due/event/幂等计数不再增加并清除 pending。只记录 key 哈希；新建临时用户、SQLite、浏览器 context 与随机端口均清理。[pending A→B→A 脱敏证据](/home/ubuntu/upload/ac4bf70bb79f1d81a5238e39_momentum-postpone-e2e-evidence-170cb895a3.json)。这证明的是新 pending UUID 的用户隔离，不外推真实 MySQL、旧 SDK context 或历史双 POST 根因。

该轮还发现旧任务列表在无 pending 的初始态下，retry 控件虽然有 `hidden` 属性，Chromium 仍判为可见；此前未点击，因此是否能触发写请求未知。随后只修正 CSS 对 `hidden` 的覆盖，并在新隔离 Chromium 验证两个入口均为 `hidden`、计算样式 `display:none`、locator 不可见；retry 控件未点击，POST 数为 0。临时 SQLite 和服务已清理。[无 pending 控件验证 JSON](/tmp/momentum-postpone-hidden-qa-823fc75e1d55.json)

此前另一种模拟中一次 UI 操作后 7 微秒出现第二个相同 POST、导致截止日 +2 天并写两条 `updated`；触发来源仍未知。本轮真实幂等键 E2E 证明同 key 重放不会重复写，但不能回溯证明旧观测的触发根因已消除。


**专注成功后的本地快照清理在 SDK 浏览器回归中仍失败，但另一条真实浏览器路径通过。** 两种结果分开记录如下，不能把局部通过外推为整体通过。

旧 owner 的历史 task #1 冻结载荷重试返回 200，隔离库只有一条 18 秒事件，页面即时回到 idle；但两次 SDK reload 后都重新显示 18 秒恢复卡。未清站点存储、未关标签、未重复该历史请求。

另一个新 origin 上的 task #11 首次 finish 返回 503、同载荷重试返回 200，隔离库只有一条 21 秒事件；两次 SDK reload 后也重新显示 21 秒恢复卡。#11 是新 origin 首装路径，不是同源 Service Worker 升级实验。随后独立 Chromium/Playwright 新上下文用真实 localStorage 执行注入 503→相同冻结请求重试成功：观测到 `snapshotSettled=true`，marker 与 user/session/payload 一致，v1/v2 pending 快照已删除，SQLite 仅一条事件；v6 Service Worker active 并控制页面；两次 reload 后无恢复卡、无第三次请求。该 fresh-context 路径通过，但不覆盖旧 owner 的历史 #1 或已安装 worker 的同源升级过程。

此前 SDK 路径的 #1 与 #11 两条 reload 复现仍是已观察到的失败证据：#1 是旧上下文中 finish 成功后两次 reload 仍出现恢复卡；#11 是新 origin 首装后的相同现象，并非同源升级测试。最近一次 SDK 对照未完成注册/登录，finish 请求数为 0；该浏览器工具不支持 API 登录态或 localStorage/storageState 注入，也不能直接读取页面快照或 Service Worker 状态，因此没有本次现场状态可比。这是“对照未完成”，不是新增失败。Playwright fresh-context 的真实 localStorage 首装路径通过，但不能回溯旧上下文状态。

新 SQLite、同一 origin/Chromium context 的 A→B→A 双向隔离检查已通过。A 的首次 finish 被浏览器侧注入 503 且未转发，刷新后保留 retry-only 快照；切换到 B 并刷新时看不到 A 的恢复卡，B 自己没有 v2 快照/marker，而 A 的原快照保持不变；切回 A 并刷新后原卡恢复，任务标题与已确认秒数匹配。全程没有成功 finish，SQLite 中 focus event 为 0；没有改应用源码、重放历史 #1 或触碰旧数据。该结果验证的是用户快照隔离，不代表已验证成功结算后的快照清理。

截至目前，Playwright fresh-context 的失败→同载荷成功→两次 reload 路径和 A→B→A 用户隔离均通过；SDK 的 #1/#11 两条既有 reload 复现仍是未解释的失败证据，现有证据不足以判定唯一根因。SDK 新对照未完成且 finish 请求数为 0，不构成新增失败；v4→v6 同源 Service Worker 升级路径因缺可信工件仍无法复现。当前 Node 中 RecoveryStore 新实例读回与 finish retry helper 测试不等同于重建完整 `focus.js` module/controller；不能据此解释或排除 #1/#11。整体快照清理不能标为已修复。

只读代码审查显示，FocusRecoveryStore 严格校验 v1/v2 快照；合法 v1 会归一化迁移到 v2，并保留用户、session、任务、已确认秒数及有效的 finish payload，随后才尝试删除旧键。强校验失败会让整份快照无效；其中秒数是本地最近成功持久化值，不等同于服务端确认。

Service Worker 的 `clients.claim()` 不会自动重载已经打开的旧文档，静态资源的 `caches.match()` 又没有限定 cache name，安装时也没有清掉 v6 缓存。因此旧页面继续运行旧 JS 或命中同名旧缓存是源码级可能性，但尚未证实为 SDK #1/#11 的根因。源码与本地 `build/lib` 静态资源哈希差异是版本排查线索；SDK 实际取到的资源哈希与 `src` 一致，不能据此认定旧包是根因。安全诊断顺序是先查已有历史响应/资源记录；若记录不存在，只在全新 origin/context/session 做受控复现，并记录 marker、v1/v2 快照 schema、恢复的 `session_id`、Service Worker 状态和资源哈希，不触碰历史存储。历史失败保持未解释。

本地浅克隆只有 1 个可达提交、没有 tags 或不可达对象；当前源码 Service Worker 是 v6，忽略的 build 产物是 v1。独立检查公开仓库 [woyaoxingfua/Momentum](https://github.com/woyaoxingfua/Momentum) 的 refs 时，`ls-remote` 的 HEAD/master 为 `b8cfcfa5e9dd995ff1cf8507d695bba189f7eba0`；9 个公开分支 refs 与 4 个 PR refs 的可达闭包共 108 个提交，范围从 `a0525ba6f5a5d8ce328a204572e18deada5d29fa`（2026-05-28）到 `73f19683c1122fa96b91cf42ed713f788b07a3e7`（2026-08-19）。当时 tags/releases API 没有条目。由于当前浅克隆无法提供可信 v4 基线，v4→v6 只能标为本地无法复现；这不证明远端历史从未存在 v4，也没有把 v1 假作 v4 或触碰旧存储。

遍历上述可达历史树，`src/momentum_agent/static/sw.js` 只有两个不同 blob，且二者的缓存名均为 `momentum-v1`：blob `1683921aae72a683970d6099f8a0433cc164b1f8`（SHA-256 `ccd56132b09ffa9302477b1ecd3c0bd23e9f49b71bef743be1c82d792b636011`，23 个快照，代表提交从 `712903dff7d8dbc672f3041dedb82d572111f02a` 到 `73f19683c1122fa96b91cf42ed713f788b07a3e7`）；blob `32e0bbc8c7908cd4618fab0cdb531c97e540cd65`（SHA-256 `87795a54e3ae88880a71d12092b099231ce9b131376684a86c1c7f21963cd308`，9 个快照，代表提交从 `478633b6e6666d8ab3f0b2b156ab037c0a1d4b83` 到 `89c80c17562d18a92d85cccdcb3135cd0199e3d0`）。两个可达的历史 `focus.js` blob（`031a66fdf5f3ea986992b2ad13d17014f1d492ca`、`8ec8ac7b9b9be7b12ec590a66afb609e1bd4dc80`）均无 recovery 标记，历史树也没有 focus-recovery 路径。

因此目前没有可验证的 v4 工件来做可信的 v4→v6 升级实验；这只覆盖当时广告 refs 可达的提交闭包，不能证明被删除、未广告或不可达的远端对象从未含 v4。测试前需提供可追溯来源的 v4 release/archive 或确切含 v4 SW 的 commit/tag，并包含 `sw.js`、其预缓存静态资源及源码/提交哈希；还需 release manifest、CI artifact provenance 或部署记录证明该版本与归档的对应关系。只有口述版本、截图或 v1 文件不足以建立可信升级路径。

可复核材料：[Playwright 回归脚本](/home/ubuntu/upload/72c1b76f2778e2a9c10c52d6_focus_recovery_browser.py)、[fresh-context 结算结果](/home/ubuntu/upload/f773e3abd753ee7b72fcd49a_report.json)、[SDK 对照边界记录](/home/ubuntu/upload/02d4088224bfc26980ee5c5c_sdk-comparison-observation.json)、[A→B→A 脱敏证据](/home/ubuntu/upload/efa98fc62badd39a2436a03a_evidence.json)、[A 待重试截图](/home/ubuntu/upload/47390a09864eeec8e14d49e6_A_pending_after_reload.png)、[切到 B 后看不到 A 快照的截图](/home/ubuntu/upload/0646256daa6c63db8c192747_B_no_A_recovery_after_reload.png)、[切回 A 后恢复的截图](/home/ubuntu/upload/b41233b36abc3e5507f25104_A_restored_after_return_reload.png)、[首次 503 截图](/home/ubuntu/upload/9258efbe1af0e7e5de66be62_first-503-retry-visible.png)和[两次 reload 后结算截图](/home/ubuntu/upload/078868c11594338d05b249a9_settled-after-two-reloads.png)。

本轮 E2E 和最终回归使用新 `/tmp` 隔离 SQLite、临时账号或 SQL mock；最终测试没有读取或检查数据库文件，`git diff --check` 明确排除 `data/try.db`。另有一项较早的旧诊断任务启动过 51473，启动参数指向 `/tmp/momentum-review-e2e/isolated-clean.sqlite3`（不是 `/workspace/Momentum-review/data/try.db`）；只检查了该路径是否存在，但 SQLite store 被打开两次。是否执行 schema/init SQL、是否读取页或记录以及文件大小均未知；没有手动 SQL/API 请求。服务已 Ctrl-C 停止并退出，之后未再访问。整个任务没有向试用服务 `8765` 发送请求或信号。另一次基线扫描从仓库根运行 `find`，可能读取过 `try.db` 的文件元数据，但数据库内容未打开或读取、未哈希、未连接；后续扫描限定在 `src/tests/build.py`。真实 MySQL 未验证；代码改动仍未提交、未推送。

在建议卡新增了估时编辑入口：仅对当前绑定的 todo/doing 任务，允许直接调整预计时长（1–120 分钟），复用现有任务更新接口；只有保存成功才刷新卡片和任务对象，失败保留旧值并提示错误。随后从卡片启动专注仍传同一任务 ID，并使用更新后的估时。定向 Node 测试 **38/38**；未做浏览器验收。

这项功能合入后的冻结共享树唯一串行全量为 Python **470 passed / 11 skipped**、Node **126/126**；23 个 JS/MJS 语法检查、Python `compileall`、tracked/staged diff 与指定 untracked 文件 whitespace 检查均通过。测试使用新隔离 `/tmp` 数据并已清理；因未设置 `MOMENTUM_TEST_MYSQL_URL`，11 项 MySQL 集成测试跳过。

备份证据现包含三条分开的 HTTP 路径：既有 v2 空数据域恢复成功（含大于 2 MiB 的 round-trip）；新增 `tests/test_backup_handlers.py::test_http_v1_import_merges_into_bearer_user_and_preserves_other_user` 通过真实 loopback `/api/import` 和 recipient Bearer token 验证 v1 merge，保留 recipient 原任务与 memory、导入新任务且 victim 账户快照不变；raw EOF 截断 JSON 用例只证明 parser/dispatcher 返回 400 且数据库零写，不代表事务中断恢复。storage 事务故障回滚仍由各自回归单独证明。新增 v1 用例仅是 loopback+SQLite 路由与认证隔离证据；SQL mock 不是真实 MySQL，真实 MySQL 与浏览器 E2E 均未测。CLI stdout-loss 只验证 SQLite CLI 状态安全重试，不等于 HTTP 原始响应重放；focusStart 的 409/timeout 是 Node controller/mock 证据，不证明实际网络恢复或服务端幂等。改动仍未提交或推送。

建议卡还新增了“完成此任务”动作，仅完成卡片当前绑定的 `suggestion.task_id`，复用现有完成 helper 与幂等键；确定错误不会伪装为完成，pending/结果不确定时不自动重发，只允许显式复用原 key 重试。成功后刷新任务、今日状态和下一条建议。该功能合入后的冻结树全量为 Python **470 passed / 11 skipped**、Node **128/128**；23 个 JS/MJS 语法检查、`compileall`、diff/whitespace 检查均通过，隔离资源已清理。未做浏览器 E2E；真实 MySQL 仍未验证，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过。

随后建议卡新增“顺延一天”：仅当当前建议绑定的是有截止日期的待办/进行中任务时显示，固定顺延 1 天并复用现有幂等顺延 helper；成功后刷新任务、今日状态和建议，无截止日时隐藏。确定失败不改变卡片状态，网络结果不确定时不自动重发，只允许显式复用原 key 重试。该功能合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **132/132**；23 个 JS/MJS 语法检查、`compileall`、diff/whitespace 均通过，隔离资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过。

Stats 仪表盘新增“今日负载”卡：从现有 `/api/config` 读取每日容量，并读取 todo/doing 任务，只统计本地日期的今日到期及逾期任务；显示有正估时任务的计划分钟合计、相对容量的余量或超额，以及未估时任务数。说明明确这是计划估时而非实际专注；缺少容量设置时沿用 45 分钟默认，显式 0 不计算比例，接口失败显示错误而非零负载。该功能只改 Stats 页面和 Node 测试，不改 API 或 schema；定向 Node **7/7**。合入后的唯一全套为 Python **470 passed / 11 skipped**、Node **139/139**，23 个 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过。

任务列表新增本地“逾期 / 今天到期”筛选，仅在 todo/doing 状态可用，并与现有搜索结果取交集；切换到 done/dropped 会清除筛选并隐藏或禁用控件。日期-only 截止值按日历日比较，带时区时间戳按本地日期归类；筛选只作用于已加载任务，不改服务器数据或 API。该项加入后的唯一全量为 Python **470 passed / 11 skipped**、Node **140/140**；46 个 JS/MJS 语法检查、`compileall`、tracked/staged diff 与具名 untracked whitespace 检查均通过，隔离 SQLite/basetemp/pycache 已清理。11 项因 `MOMENTUM_TEST_MYSQL_URL` 未设置而跳过；真实 MySQL、浏览器 E2E 未测，改动未提交或推送。

Stats 增加未来 7 个完整本地自然日的截止任务分布，从明天开始按日显示 todo/doing 任务的正估时分钟合计与未估时任务数；不把每日容量外推成周容量，也不宣称该图是自动排程或实际专注。该项复用现有配置/任务请求，不改 API 或 schema；定向 Node **10/10**。合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **143/143**；47 项 JS/MJS 语法、`compileall`、diff/whitespace 检查均通过，隔离资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因 `MOMENTUM_TEST_MYSQL_URL` 未设置而跳过。

未来7天分布现可继续点入行动：日期桶链接带 `due_on=YYYY-MM-DD` 返回首页，恢复精确本地日期筛选并显示可清除的筛选状态；在 todo/doing 间切换保留日期，切至 done/dropped 时清除参数；它与“逾期/今天到期”筛选互斥。日期参数只驱动前端本地过滤，不改 API 或数据库。该项定向 Node **10/10**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **149/149**，47 项 JS/MJS 语法、`compileall` 与 diff/whitespace 检查通过，隔离资源已清理。真实 MySQL、浏览器 E2E 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

今日负载卡也可点击进入本地今天的 todo/doing 清单，使用 `due_on=本地今天` 精确筛选；跳转不包含逾期任务，而负载汇总本身仍包括逾期，界面对此有说明。该导航只使用前端日期筛选，不改 API/schema。定向 Node **17/17**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **149/149**，47 项 JS/MJS 语法、`compileall` 和 diff/whitespace 检查均通过，隔离资源已清理。真实 MySQL 和浏览器 E2E 未测；11 项因未设 `MOMENTUM_TEST_MYSQL_URL` 跳过。

今日负载卡中的“未估时”数字现可打开精确任务清单：显示逾期及今天到期、仍开放且缺少有效正估时的 todo/doing 任务；卡片计数与清单共用同一资格/估时判断。清单过滤可与搜索相交、可显式清除，并和指定日期及“逾期/今天”过滤保持明确优先级；不写数据、不加 API/schema。该功能的定向 Node 测试 **17/17**。全量中首轮曾有 3 项 Node 失败，定位为 future-distribution 测试 VM harness 未注入两个 helper 依赖；只修 harness、未改运行时代码，相关 Stats 测试 **11/11** 通过后冻结树唯一全量重跑通过：Python **470 passed / 11 skipped**、Node **152/152**，48 项 JS/MJS 语法、`compileall`、diff/whitespace 全通过，临时资源已清理。真实 MySQL 与浏览器 E2E 未测，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过；未提交或推送。

未估时清单新增快捷补估：仅对当前清单中的任务提供 15/25/45/60 分钟选择和显式保存，PUT 请求只带 `estimated_minutes`，不会覆盖标题、截止日、优先级、备注或标签。保存成功后重新读取任务并更新跨标签 Stats 计数，任务按相同口径离开未估时清单；明确的 4xx 保留任务并显示错误，断连或 timeout 显示结果未确认、不自动重发，先由用户显式刷新核对，再决定是否用同值重试。定向 Node **24/24**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **158/158**、49 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离资源已清理。该项未改 API/schema，也未做浏览器 E2E；真实 MySQL 未测，11 项因 `MOMENTUM_TEST_MYSQL_URL` 未设置跳过，未提交或推送。

任务列表在原有默认与智能排序间增加第三种“短任务优先”：只对当前已加载的搜索/筛选结果本地重排，估时为正的任务从短到长，缺估时排后；并列顺序与父子任务组保持稳定。切换到 done/dropped 会退出短任务模式，不向服务端传入新排序参数。定向 Node **13/13**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **162/162**、50 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因 `MOMENTUM_TEST_MYSQL_URL` 未设置跳过，未提交或推送。

任务行新增独立“专注”动作，仅供 todo/doing 使用，复用既有 `startSuggestedFocus`/共享 `focusStart`，向当前任务传入正确 `task_id` 和估时；原 todo“开始”仍只改变任务状态。测试覆盖任务绑定、已活动/请求中专注拦截，以及 4xx/timeout 错误反馈。定向 Node **11/11**；该项合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **164/164**、51 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离资源已清理。未改专注 API/schema，未做浏览器 E2E；真实 MySQL 未测，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，未提交或推送。

专注休息面板增加“再专注一轮”：显示当前任务和 N 分钟；只有显式点击才清除休息倒计时，并调用共享 start 流程创建新 session，不复用旧 session ID，也不自动启动。新 session 若遇到 4xx 或网络结果不确定，旧 session/休息面板状态保留且不自动重发。定向 Node **13/13**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **168/168**、51 项 JS/MJS 语法检查、`compileall` 与 diff/whitespace 全通过，隔离资源已清理。未改 API/schema、未做浏览器 E2E；真实 MySQL 未测，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，未提交或推送。

专注任务选择器增加显式“按任务估时开始”入口：仅所选任务的 `estimated_minutes` 为 1–120 安全整数时启用，并使用该任务估时启动；缺失、非正、非整数或大于 120 时禁用并解释，不会静默回退到当前手动时长。原有 25/45/60 分钟手动选择及普通启动流程保持不变。定向 Node **16/16**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **171/171**、51 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离资源已清理。未改 API/schema、未做浏览器 E2E；真实 MySQL 未测，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，未提交或推送。

全局专注任务选择器修复了只随任务页 `currentStatus` 读取、初始化时可能漏掉 doing 任务的问题：现在并行读取 todo 与 doing，只有双请求完整成功才合并去重、保留仍开放的当前选择；任务不再开放时清空，任一请求失败则清空/禁用并显示专属错误，不提供误导性的半份列表，也不调用会重绘任务页的 `loadTasks()`。仅前端与 Node 测试变更，无 API/schema 改动。定向 Node **19/19**；期间发现一条旧静态断言仍要求 focus.js 导入已移除的 `loadTasks`，只修正测试契约、未改运行时代码后，相关 Node 文件 **31/31**。冻结树唯一全量最终通过：Python **470 passed / 11 skipped**、Node **174/174**、51 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离临时资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因 `MOMENTUM_TEST_MYSQL_URL` 未设而跳过，未提交或推送。


专注任务选择器新增可访问的标题搜索框，仅对已加载的 todo/doing 选项做大小写不敏感子串过滤；搜索仍匹配当前选择时保留，不匹配则清空，清除搜索恢复完整选项，输入过程不发额外请求。按任务估时启动及原有 25/45/60 分钟手动专注语义不变。定向 Node **20/20**；合入后的唯一全量为 Python **470 passed / 11 skipped**、Node **175/175**、51 项 JS/MJS 语法检查、`compileall` 和 diff/whitespace 全通过，隔离资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

专注任务选择器的选项现在附带待办/进行中状态、短 ID 和估时/未估时标识，以区分标题相同的条目；搜索仍只匹配原标题。普通启动和按估时启动都将原始 `task.title` 写入 session/state/recovery snapshot，不会把展示标签当作任务名。定向 Node **21/21**。首轮完整行为回归 Python **470 passed / 11 skipped**、Node **176/176**，51 项 JS/MJS 语法、`compileall` 与 tracked diff 均通过；当时唯一终检问题是具名 `focus-tabs.test.mjs` 有一条多余 EOF 空行。随后只删除该空行，最终文件的 focus-tabs Node 定向复验 **21/21**、具名 untracked whitespace 和 EOF 单换行检查通过；未重跑 Python或全量，因为测试/运行时代码行为未变。真实 MySQL、浏览器 E2E 未测，11 项因未设 `MOMENTUM_TEST_MYSQL_URL` 跳过；隔离资源已清理，改动未提交或推送。

Focus 汇总区新增“最近专注记录”，直接使用既有 `/api/focus/stats` 的 `sessions` 展示最近 3 条，不新增 API、schema 或任务查询；按结束时间（缺失时用开始时间）排序，显示任务编号、本地时间、完成/停止结果，以及实际时长或明确标注为“实际时长未知”的旧记录计划时长。`actual_seconds = 0` 仍作为有效实测；记录使用安全文本渲染，不展示完整 session ID。空列表与请求失败分开呈现，刷新失败会清掉上一轮旧列表，避免错误提示旁残留过期数据。定向 `focus-tabs` Node **24/24**；冻结树唯一全量为 Python **470 passed / 11 skipped**、Node **179/179**，51 项 JS/MJS 语法检查、`compileall`、diff/whitespace 检查均通过，隔离临时资源已清理。真实 MySQL 与浏览器 E2E 未测；11 项 MySQL 测试因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

任务页头部工具栏针对窄屏增加布局修正：在不超过 600px 的断点内允许操作区换行，搜索框独占一行并可收缩，筛选芯片也可换行；原有控件均保留，桌面端基准规则不变。仅改 `app.css` 并新增独立 `mobile-toolbar.test.mjs`；定向 Node **3/3**、syntax 与 whitespace 检查通过。冻结树唯一全量为 Python **470 passed / 11 skipped**、Node **182/182**、52 项 JS/MJS 语法检查、`compileall`、diff/whitespace 全通过，隔离临时资源已清理。Node/静态断言不等同于真实窄屏浏览器验收；浏览器 E2E 与真实 MySQL 未测，11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

Focus 汇总区将表示滚动统计窗口的“本周”更正为“近7天”，与后端从当前时刻向前七天的查询口径一致，避免被理解为自然周；仅改标签与既有 Node harness 文案断言，统计数值、sessions 请求/过滤及 legacy 处理均未改变。定向 Node **24/24**、syntax/whitespace 通过；合入后的冻结树唯一全量为 Python **470 passed / 11 skipped**、Node **182/182**、52 项 JS/MJS 语法检查、`compileall` 与 diff/whitespace 全通过，隔离资源已清理。未做浏览器 E2E，真实 MySQL 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

Focus 汇总的“次数”改称“近7天实际次数”，明确这是滚动近7天内有 `actual_seconds` 的 session 数，而不是包含 legacy 记录的总条数；既有统计值未变，Node fixture 确认 7 条 actual 计入、另 1 条 legacy 仅有计划时长且不计入。仅改标签与既有 `focus-tabs` harness 断言，不改请求、统计逻辑或 API。定向 Node **24/24**、syntax/whitespace 通过；冻结树唯一全量为 Python **470 passed / 11 skipped**、Node **182/182**、52 项 JS/MJS 语法检查、`compileall`、diff/whitespace 均通过，隔离资源已清理。未做浏览器 E2E，真实 MySQL 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。

Focus 汇总增加日期归属说明：“Focus今日时长按开始时的本地日期归属；每日复盘按结束时的本地日期归属。”这解释了跨夜 session 在两个页面可能落入不同日期的原因；仅增加说明和既有 Node harness 断言，不改统计值、请求或 API。定向 Node **24/24**、syntax/whitespace 通过；冻结树唯一全量为 Python **470 passed / 11 skipped**、Node **182/182**、52 项 JS/MJS 语法检查、`compileall` 与 diff/whitespace 全通过，隔离临时资源已清理。未做浏览器 E2E，真实 MySQL 未测；11 项因未设置 `MOMENTUM_TEST_MYSQL_URL` 跳过，改动未提交或推送。
