# Momentum Task Agent

一个基于 OpenAI Agents SDK 的个人任务待办助手。SQLite 存储，CLI + Web 双界面，支持自然语言交互、任务关系、标签系统、心跳提醒、AI 视觉识别。

在线体验:https://myfirst.cc.cd

## 功能

### 核心功能
- **自然语言任务管理** — "明天下午3点交水费" 自动解析时间、优先级、重复频率
- **任务全生命周期** — 待办 → 进行中 → 已完成 / 已放弃，一键切换
- **大任务拆分** — AI 自动将目标拆为 3-5 个可执行子任务
- **重复任务** — 每天 / 每周 / 每月，完成后自动生成下一期
- **AI Agent 对话** — 19 个工具的自主 agent：查任务，建任务、给建议、记偏好、识别模式
- **会话记忆** — SQLiteSession 持久化，重启不丢对话上下文
- **流式输出** — 打字机效果，边想边说
- **用户认证** — 注册 / 登录 / 会话管理，PBKDF2 密码哈希
- **偏好设置** — 每日可用时间、工作时段，影响建议逻辑
- **数据导出导入** — JSON 备份还原
- **无 API Key 也能用** — 纯本地 regex 解析器 + 模板计划，完全离线
- **输入/输出 Guardrails** — 防空输入、防超长输出，自动拦截异常内容

### 标签系统
- **任务标签** — 给任务打标签（工作、紧急、学习…），灵活分类
- **按标签筛选** — 查询某个标签下的所有任务
- **批量打标签** — 一次给多个任务加标签

### 任务关系与层级
- **5 种关系类型** — `depends_on`（依赖）、`blocks`（阻塞）、`relates_to`（关联）、`parent_of`（父子）、`follows`（顺序）
- **依赖检查** — 查询任务是否被阻塞、依赖链是否完整
- **子任务** — 创建子任务、批量创建子任务、查看子任务树
- **关系图** — 查询任务的所有关系、前序/后序任务
- **主任务与子任务区分** — 主任务可添加子任务，视觉上明显区分
- **自动完成** — 主任务完成时自动完成所有子任务

### 心跳提醒
- **主动建议** — 根据时间段、任务优先级、用户精力自动推荐下一步
- **可配置** — 设置提醒频率、工作时段、免打扰模式
- **时间感知** — 早晨推荐高优先级、下午推荐执行中任务、傍晚提醒截止日

### 批量操作
- **批量完成** — 一键完成多个任务
- **批量开始** — 一键开始多个任务
- **批量打标签** — 一键给多个任务加标签

### AI 视觉
- **图片识别** — 上传图片（image_base64），AI 视觉模型分析内容
- **Vision 模型** — 支持 OpenAI-compatible 视觉模型（如 GPT-4o、Claude-3 系列、Gemini 等）
- **手动启用** — 用户可在配置页面手动开启视觉功能，不依赖自动检测
- **配置灵活** — 支持自定义 API Key、基础地址和模型名称

### 智能建议与复盘
- **今日建议** — 根据任务优先级、截止时间智能推荐下一步
- **每日复盘** — 分析已完成任务，给出改进建议
- **AI 增强** — 配置 API Key 后由 AI Agent 提供更智能的建议和复盘

### 任务操作
- **编辑任务** — 修改标题、截止时间、优先级、预估时间、标签、备注
- **推迟任务** — Web 界面支持快速选择（1天、3天、1周、1个月）或自定义天数
- **搜索优化** — 支持搜索标题、备注、标签，不受当前状态限制

### 服务集成
- **天气服务** — 查询当前天气，辅助任务建议
- **位置服务** — 设置/获取用户位置，天气查询自动关联
- **通知服务** — 任务提醒通知框架

## 快速开始

```powershell
# 1. 克隆
git clone <repo-url>
cd Momentum

# 2. 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. 配置（可选，不配也能用本地模式）
cp .env.example .env
# 编辑 .env 填入你的 API key

# 4. 启动
momentum-agent serve
# 打开 http://127.0.0.1:8000
```

默认账号：`default` / `momentum`（登录后建议修改密码）。

## CLI 命令

```powershell
# 任务操作
momentum-agent add "明天下午交水费"          # 创建任务
momentum-agent plan "下周准备产品经理面试"    # 拆分计划
momentum-agent list                          # 待办列表
momentum-agent list --status doing           # 进行中
momentum-agent start 1                       # 开始做
momentum-agent done 1                        # 完成
momentum-agent edit 1 --priority high        # 编辑
momentum-agent edit 1 --tags 工作,紧急        # 打标签
momentum-agent postpone 1 --days 5           # 推迟
momentum-agent drop 1                        # 放弃
momentum-agent reopen 2                      # 恢复

# 查询
momentum-agent search "面试"                  # 搜索
momentum-agent advise                        # 今日建议
momentum-agent review                        # 每日复盘

# 数据
momentum-agent export > backup.json          # 导出
momentum-agent import backup.json            # 导入

# 配置
momentum-agent config set daily_capacity_minutes 120
momentum-agent config show

# AI 对话
momentum-agent chat "帮我安排今天的任务" -v    # -v 显示调用日志

# 服务
momentum-agent serve --port 8000 -v           # 启动 Web 服务
momentum-agent provider                       # 查看 AI 提供商状态
```

## Web 界面

```
http://127.0.0.1:8000
```

- 登录注册 → 任务工作台
- 状态标签筛选：待办 | 进行中 | 已完成 | 已放弃
- **标签筛选** — 按标签过滤任务
- 搜索框实时过滤（支持标题、备注、标签）
- 右侧 Agent 对话面板，流式输出
- 偏好设置面板（AI配置、工作配置、心跳提醒）
- **主任务与子任务** — 主任务显示添加子任务按钮，子任务自动缩进显示
- **推迟任务** — 点击推迟按钮可快速选择 1天/3天/1周/1个月或自定义天数
- **图片上传** — 支持上传图片提取任务信息（需在配置中启用视觉功能）
- **响应式设计** — 完美适配桌面端、平板端、手机端
- 导出 / 导入

## API

### 基础接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/register` | No | 注册 |
| POST | `/api/login` | No | 登录 |
| POST | `/api/logout` | No | 登出 |
| GET | `/api/me` | Yes | 当前用户 |
| POST | `/api/change-password` | Yes | 修改密码 |
| GET | `/api/tasks?status=todo` | Yes | 任务列表 |
| GET | `/api/tasks?q=关键词` | Yes | 搜索任务 |
| POST | `/api/tasks` | Yes | 创建任务 |
| POST | `/api/plan` | Yes | 拆分计划 |
| PUT | `/api/tasks/{id}` | Yes | 编辑任务 |
| POST | `/api/tasks/{id}/start` | Yes | 开始任务 |
| POST | `/api/tasks/{id}/done` | Yes | 完成任务 |
| POST | `/api/tasks/{id}/drop` | Yes | 放弃任务 |
| POST | `/api/tasks/{id}/postpone` | Yes | 推迟任务 |
| POST | `/api/tasks/{id}/reopen` | Yes | 恢复任务 |
| GET | `/api/advice` | Yes | 今日建议 |
| GET | `/api/review` | Yes | 每日复盘 |
| POST | `/api/chat` | Yes | Agent 对话 |
| POST | `/api/chat/stream` | Yes | Agent 流式对话 |
| GET | `/api/config` | Yes | 用户偏好 |
| POST | `/api/config` | Yes | 设置偏好 |
| GET | `/api/export` | Yes | 导出数据 |
| POST | `/api/import` | Yes | 导入数据 |
| GET | `/api/provider` | Yes | AI 服务商状态 |

### 标签接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/tags` | Yes | 获取所有标签 |
| GET | `/api/tags/{tag}/tasks` | Yes | 按标签查询任务 |
| POST | `/api/tasks/batch/tags` | Yes | 批量打标签 |

### 心跳提醒接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/heartbeat/config` | Yes | 获取心跳配置 |
| POST | `/api/heartbeat/config` | Yes | 设置心跳配置 |
| GET | `/api/heartbeat/suggestion` | Yes | 获取当前建议 |

### 天气 & 位置接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/weather` | Yes | 获取天气信息 |
| GET | `/api/location` | Yes | 获取位置信息 |
| GET | `/api/user/location` | Yes | 获取用户位置 |
| POST | `/api/user/location` | Yes | 设置用户位置 |

### 子任务 & 关系接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/tasks/{id}/subtasks` | Yes | 获取子任务 |
| GET | `/api/tasks/{id}/with-subtasks` | Yes | 获取任务+子任务 |
| POST | `/api/tasks/{id}/subtasks` | Yes | 创建子任务 |
| POST | `/api/tasks/{id}/subtasks/bulk` | Yes | 批量创建子任务 |
| GET | `/api/tasks/{id}/dependencies` | Yes | 获取前置依赖 |
| GET | `/api/tasks/{id}/dependents` | Yes | 获取后续被依赖 |
| GET | `/api/tasks/{id}/relations` | Yes | 获取所有关系 |
| POST | `/api/tasks/{id}/dependencies` | Yes | 添加依赖关系 |
| POST | `/api/tasks/{id}/relations` | Yes | 添加任意关系 |
| GET | `/api/tasks/{id}/is-blocked` | Yes | 检查是否被阻塞 |

### 批量操作接口

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/tasks/batch/complete` | Yes | 批量完成任务 |
| POST | `/api/tasks/batch/start` | Yes | 批量开始任务 |
| POST | `/api/tasks/batch/tags` | Yes | 批量打标签 |

## 技术栈

| 层 | 选型 |
|---|---|
| AI SDK | OpenAI Agents SDK (OpenAIChatCompletionsModel) |
| 模型 | DeepSeek V4 Flash / GPT-4.1-mini / 任意 OpenAI-compatible |
| Vision | OpenAI-compatible 视觉模型（如 GPT-4o、Claude-3 系列、Gemini 等） |
| 数据库 | SQLite（自动 schema 迁移，支持 tags + task_relations 表） |
| Web 服务 | Python stdlib `http.server`（零第三方依赖） |
| 前端 | 原生 JS ES Modules，零构建步骤，响应式设计 |
| 认证 | PBKDF2-SHA256 + Session Token |
| Agent Guardrails | 输入/输出双守卫（防空输入、防超长输出） |
| 日志 | Python `logging` + `RotatingFileHandler`，默认写文件（`logs/` 目录，10MB 轮转 × 5 备份） |

## 项目结构

```
src/momentum_agent/
├── cli.py              # CLI 入口（--tags 支持）
├── web.py              # HTTP 服务 + REST API（40+ 端点）
├── agent_app.py        # Agent 核心：19 工具、会话、流式、guardrails
├── storage.py          # SQLite 存储层（tags/relations/batch/heartbeat）
├── auth.py             # 密码哈希 + 令牌
├── config.py           # 环境变量加载
├── parser.py           # 中文自然语言解析（fallback）
├── planner.py          # 模板任务拆分（fallback）
├── context.py          # 任务评分 + 建议 + 复盘 + 心跳建议
├── models.py           # 数据模型（Task/TaskRelation/TaskRelationType/Priority/TaskStatus）
├── logger.py           # 日志系统（默认文件+控制台，request_id 追踪，按日期轮转）
├── agents/             # 模块化 Agent 架构
│   ├── __init__.py
│   ├── agent.py        # Agent 构建 + 工具注册
│   └── tools/          # 工具子模块
│       ├── __init__.py
│       ├── task_tools.py       # 任务 CRUD 工具
│       ├── subtask_tools.py    # 子任务工具
│       ├── relation_tools.py   # 关系工具
│       ├── weather_tools.py    # 天气工具
│       └── heartbeat_tools.py  # 心跳工具
├── services/           # 业务服务层
│   ├── __init__.py
│   ├── weather.py      # 天气服务
│   ├── location.py     # 位置服务
│   ├── heartbeat.py    # 心跳提醒服务
│   ├── task_hierarchy.py  # 任务层级服务
│   └── notification.py # 通知服务
├── web/                # Web 子包（v1 兼容层）
│   ├── __init__.py
│   ├── handlers.py     # 请求处理器
│   └── legacy_server.py # 旧版服务器兼容
└── static/
    ├── index.html      # 主界面（任务管理、Web界面）
    ├── login.html      # 登录注册
    ├── app.css         # 样式（响应式设计，适配多端）
    └── js/             # ES Modules
        ├── api.js
        ├── app.js
        ├── tasks.js     # 任务列表（子任务、标签显示/编辑）
        ├── chat.js      # Agent 对话
        ├── advice.js    # 建议与复盘
        ├── config.js    # 配置（视觉功能、工作配置、心跳设置）
        └── heartbeat.js # 心跳提醒前端
tests/
├── test_parser.py
├── test_config.py
└── test_context.py
```

## 常见问题

**Q: DeepSeek 报错 `response_format type is unavailable`？**
A: DeepSeek 不支持 structured output (json_schema)。AI 解析/规划失败后自动回退到本地解析器，不影响使用。

**Q: 如何多用户？**
A: 注册不同账号。数据完全隔离。或者设环境变量 `MOMENTUM_USER=alice` 切换隐式用户。

**Q: 不配 API key 能用吗？**
A: 能。任务 CRUD、模板规划、规则建议全都可以离线工作。只有 Agent 对话、今日建议和复盘的 AI 增强需要 API key。

**Q: 标签怎么用？**
A: CLI 用 `--tags` 参数，Web 界面在任务编辑框输入标签。API 用 `/api/tags` 系列端点。

**Q: 心跳提醒是什么？**
A: 系统根据时间、任务优先级、用户精力状态自动推荐下一步该做什么。可在 Web 界面配置提醒频率和时段。

**Q: 任务关系有什么用？**
A: 设置任务间依赖（"B 依赖 A 完成"），系统会自动检查阻塞状态，防止开始被阻塞的任务。

**Q: 主任务和子任务有什么区别？**
A: 主任务可以添加子任务，视觉上主任务有更明显的边框和背景色，子任务会缩进显示。当主任务完成时，所有子任务会自动标记为完成。

**Q: 视觉功能如何使用？**
A: 在配置页面中勾选"启用视觉功能"并保存。上传图片后，AI 会自动分析图片内容并提取任务信息（仅支持配置的视觉模型）。

**Q: 推迟任务支持哪些选项？**
A: Web 界面支持快速选择：1天、3天、1周、1个月，也可以手动输入自定义天数（1-365天）。CLI 使用 `--days` 参数指定天数。

**Q: 日志文件在哪？**
A: 默认写入项目根目录下 `logs/momentum-YYYY-MM-DD.log`，自动按日期命名、10MB 轮转、保留 5 个备份。设 `MOMENTUM_LOG_FILE=off` 可关闭文件日志。

**Q: 日志级别怎么调？**
A: CLI 加 `-v`（DEBUG），或设环境变量 `MOMENTUM_LOG_LEVEL=DEBUG`。文件日志始终记录全部级别（DEBUG 及以上），控制台跟随设定级别。

## License

MIT
