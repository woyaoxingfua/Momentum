# Momentum 技术证据附录

本附录保留读者版主报告删去的实现背景与关键验证边界。原始逐轮叙述另存于[历史归档](Momentum_REVIEW_archive_20261007.md)（该归档文件未包含在当前工作树中）。不同时点的测试数字属于各自冻结轮次，不能拼成一次测试；本文标出的 470/11 与 182/182 才是最近一次唯一全量基线。

## SDK 与运行路径

仓库在本次审查时使用 Agents SDK Chat Completions 路径：`AsyncOpenAI(api_key, base_url)` 配合 `OpenAIChatCompletionsModel`，在此之上使用 Runner、function tools、handoff、guardrails、结构化输出和原生 streaming。OpenAI-compatible endpoint 与 Ollama `/v1` 的兼容性依赖各 provider 对工具调用、schema、streaming chunk、图片及 provider-specific 参数的实现；本轮未对每种 provider 进行真实模型 E2E。仓库声明 `openai-agents>=0.2.0`，但没有锁定部署版本组合；本地审查环境为 0.23.1，OpenAI Python SDK 为 3.23.0。

已修复的一项 tracing 配置误用：`use_for_tracing=False` 仅控制 client 的 API key 是否用于上传 trace，不会关闭单次 Agent run 的 tracing。现通过每次调用的 `RunConfig.tracing_disabled` 传递 provider 策略；对应 Python 回归曾验证启用/禁用两种设置及 guardrail/workflow 参数。另移除流式失败后自动回退到非流式 `Runner.run` 的行为，以免写工具已执行后重放；失败会显示错误且不保存该轮历史。仍待产品决定 tracing 中敏感数据与本地日志的策略；SDK Sessions、Responses-only hosted tools、审批恢复等并未仅为追新而接入。原 SDK 评估中的接口细节与来源保存在历史归档第 1–118 行。

## 任务、完成事件与 Focus 数据语义

任务完成趋势与完成事件依据真实状态转移，而不再从易变的 `tasks.updated_at` 推断完成时间。一次真实非 done→done 转移新增一条完成事件；同一任务 reopen 后再次完成会产生另一条事件。后端已使用事件计算洞察，但尚无按历史完成频次聚合、供再次新建任务使用的独立视图。

Focus 用单调时钟累计真实运行秒数，暂停不计；finish 保存实际秒数、计划时长和结束原因到现有 `task_events` JSON，不新增表。旧版只有计划时长的记录仍标记为 legacy，不冒充实际时间。Focus 汇总按开始时的本地日归属，复盘按结束时的本地日归属。Insights 的估时准确度使用任务当前估时，依据近 30 天专注记录且最多纳入最近 100 个已完成任务；同一 task ID reopen 后的多轮专注仍合并在任务层级，并不表示单次完成周期。

`/api/focus/start` 每次创建新 session，活动会话不会在服务端持久化；同 origin/user 的前端 pending 状态和 Web Locks 只能挡住相应客户端的重复入口，不等于跨设备、不同浏览器 profile 或直接 API 请求的用户级单活。无状态网络失败时，前端明确提示再次点击会从新时间开始；这不是服务端幂等回放。相对地，`/api/focus/finish` 使用 session ID 幂等，隔离 SQLite loopback 测试验证了重复请求、跨进程竞争、写入回滚与响应丢失后的同 session 重试。

## Focus 成功后恢复快照：SDK #1 与 #11 仍未解释

两项失败来自历史 SDK 浏览器观察，均是在 finish 成功后 reload 仍出现旧恢复卡；不是最近的 Node mock 失败，也不能与后来 fresh-context 通过路径合并成“已修复”。

- **#1：**旧 owner 上下文使用冻结载荷重试，HTTP 200；隔离库只见一条 18 秒事件，页面即时回到 idle。但随后两次 SDK reload 均重新出现 18 秒恢复卡。未清站点存储、未关闭标签，也未重放该历史请求。
- **#11：**新 origin 首装路径首次 finish 为 503，使用同一载荷重试后 HTTP 200；隔离库只见一条 21 秒事件，但两次 SDK reload 后均重新出现 21 秒恢复卡。它不是同源 Service Worker 升级实验。
- **不同路径的通过结果：**独立 Playwright fresh context 通过真实 localStorage 执行 503→同载荷重试成功，观察到 `snapshotSettled=true`，marker 与用户/session/payload 相符，v1/v2 pending 快照删除；v6 Service Worker 控制页面，两次 reload 无恢复卡且无第三次请求。它证明 fresh-context 首装场景可通过，不能解释旧 owner 的 #1 或 #11。
- **对照未完成：**最近一次 SDK 对照未完成注册/登录，finish 请求数为 0；当时浏览器不支持 API 登录态或 localStorage/storageState 注入，也不能读取页面快照或 Service Worker 状态。因此它不是新增失败，也没有提供可比状态。
- **根因未知：**Node RecoveryStore 新实例读回或 finish helper 单测没有重建完整 `focus.js` module/controller。`clients.claim()` 不会重载已打开文档、同名缓存查找未限定 cache name 等是源码级可能性，但未证实为 #1/#11 根因。v4→v6 升级缺少有可信来源的 v4 工件而不可复现，不能把 v1 当作 v4。

可复核证据：[Playwright 回归脚本](/home/ubuntu/upload/72c1b76f2778e2a9c10c52d6_focus_recovery_browser.py)、[fresh-context 结果](/home/ubuntu/upload/f773e3abd753ee7b72fcd49a_report.json)、[SDK 对照边界记录](/home/ubuntu/upload/02d4088224bfc26980ee5c5c_sdk-comparison-observation.json)、[A→B→A 用户隔离证据](/home/ubuntu/upload/efa98fc62badd39a2436a03a_evidence.json)。其余逐张截图与历史资源索引见历史归档第 316–338 行。

## 最近前端迭代：闭环、任务列表与 Focus

- 建议卡可调整当前建议任务估时、完成当前建议任务、顺延其现有截止日期一天；只使用当前绑定 task ID 与既有 UI 控制器/幂等键。today-close 可设置截止日、完成任务、开始专注。
- Stats 展示逾期/今日到期任务的今日负载，以及未来 7 个完整本地日的截止任务估时分布；日期桶、今日负载和未估时计数可跳转至任务清单。任务列表支持逾期/今日和指定日期过滤、快捷补估及短任务优先排序；未估时、日期、状态和搜索按明确定义取交集/互斥。
- 任务行可直接专注；Focus 休息卡可显式为同一任务启动新 session；全局 selector 并行加载 todo+doing，支持标题搜索和状态/ID/估时标签，保存到 Focus state 的标题仍是原始任务标题。
- Focus 汇总显示近 7 天而非自然周，标明近 7 天实际次数，解释跨夜日期归属，并列出最多 3 条近期记录。近期列表只用现有 stats sessions，显示任务 ID，不额外请求私有标题、不暴露 session ID。
- 任务工具栏在 ≤600px 视口换行且保留控件，桌面规则不变；该项明确**未做浏览器 E2E**。

前端所有权范围限静态资源及 `tests/frontend/`。最近功能均由定向 Node/静态测试及 Gk 负责的唯一全量收敛；若某一具体轮次没有浏览器验证，不能从旧的浏览器通过结果外推。最近唯一全量数字见主报告。

## 隔离 E2E 与数据库边界

历史上部分关键流程在全新临时 SQLite、随机用户、loopback HTTP 和独立 Chromium context 验收，包括 today-close → Focus → 完成 → review 刷新、完成事件重复与刷新、香港本地日界、`/postpone` 重试幂等、同源双标签 Focus，以及本地背景图片刷新恢复。它们是特定隔离场景的证据，不代表所有页面改动都做过浏览器测试，也不等于真实 MySQL。

真实 MySQL 的 11 项集成测试在最近唯一全量中因未配置 `MOMENTUM_TEST_MYSQL_URL` 而跳过。SQL mock 验证的是 mock 观察到的查询/调用路径，不是 MySQL 服务端锁、事务或崩溃恢复。Python 测试与前端 Node/mock、SQLite loopback、真实 MySQL、Chromium UI 各有不同覆盖，报告不得合并或夸大。

报告历史中有一项较早的旧诊断任务启动过 51473，启动参数指向 `/tmp/momentum-review-e2e/isolated-clean.sqlite3`，不是 `/workspace/Momentum-review/data/try.db`；只确认该路径存在，SQLite store 被打开两次。schema/init SQL 是否执行、是否读取页或记录、文件大小均未知。曾有一次仓库根 `find` 可能读取 `try.db` 文件元数据；未打开数据库内容、未读取、未哈希、未连接。当前整理未访问该数据库、端口 8765 或任何未知 `.tmp` 文件；后续也应继续按此边界处理。

## 归档说明

原始逐轮文档体量大且有重复验证文本，已保留为[历史归档](Momentum_REVIEW_archive_20261007.md)（该归档文件未包含在当前工作树中）。主报告是面向当前决策的读者版；本附录聚合主题、关键证据与尚未解决的问题。仓库 README 的导出副本保持原样；主报告与 latest 副本应保持内容一致。
