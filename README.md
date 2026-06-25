# Momentum Task Agent

AI 驱动的智能任务管理助手。不是又一个 Todoist —— 它会学习你的行为模式，提供数据驱动的洞察。

## 核心差异化

**行为学习引擎** — 从 `task_events` 表中挖掘你的完成率、预估准确率、高效时段、倦怠风险，给出真正的个性化建议。

在线体验:http://myfirst.cc.cd    （ssl证书未正确配置无法使用https协议）

## 功能

### 行为洞察（新）
- **行为画像** — 完成率、预估偏差、高效时段、倦怠风险自动分析
- **洞察生成** — 自动发现风险模式、拖延类型、产出趋势
- **战略摘要** — 一段话总结你的行为模式和改进建议
- **智能预估** — 基于历史数据给出更准确的任务时间预估

### 任务管理
- **自然语言创建** — "明天下午3点交水费" 自动解析时间、优先级、重复
- **任务全生命周期** — 待办 → 进行中 → 已完成 / 已放弃
- **大任务拆分** — AI 自动拆为 3-5 个可执行子任务
- **重复任务** — 每天/每周/每月，完成后自动生成下一期
- **标签系统** — 打标签、按标签筛选、批量操作
- **任务关系** — 5 种关系类型（依赖/阻塞/关联/父子/顺序）

### AI Agent
- **40+ 工具** — 自主决策：查任务、建任务、拆子任务、分析模式、给建议、记偏好
- **对话记忆** — 基于 `to_input_list()` 的多轮上下文，自动管理 40 条历史
- **真流式输出** — `Runner.run_streamed` 逐 token 打字机效果，工具调用实时推送
- **多 Agent 协作** — `handoffs` 自动路由：主 Agent + InsightAgent(统计专家) + WeatherAgent(天气专家)
- **视觉识别** — 上传图片，AI 自动提取任务（需配置视觉模型）
- **输入/输出 Guardrails** — 防空输入、防超长输出

### 多端 UI
- **响应式设计** — 桌面、平板、手机完美适配
- **深色模式** — 自动跟随系统主题
- **移动端导航** — 底部 Tab 切换任务/Agent/洞察/设置
- **PWA 支持** — 可添加到主屏幕

### 其他
- **用户认证** — 注册/登录，PBKDF2 密码哈希
- **心跳提醒** — 根据时间、优先级、精力自动推荐
- **数据导出导入** — JSON 备份还原
- **无 API Key 也能用** — 纯本地 regex 解析 + 模板计划

## 快速开始

```powershell
# 1. 克隆
git clone <repo-url>
cd Momentum

# 2. 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. 配置（可选）
cp .env.example .env
# 编辑 .env 填入 API key

# 4. 启动
momentum-agent serve
# 打开 http://127.0.0.1:8765
```

默认账号：`default` / `momentum`（登录后建议修改密码）。

## CLI 命令

```powershell
# 任务操作
momentum-agent add "明天下午交水费"
momentum-agent plan "下周准备产品经理面试"
momentum-agent list
momentum-agent done 1
momentum-agent edit 1 --priority high --tags 工作,紧急
momentum-agent postpone 1 --days 5

# 查询
momentum-agent search "面试"
momentum-agent advise
momentum-agent review

# 数据
momentum-agent export > backup.json
momentum-agent import backup.json

# 配置
momentum-agent config set daily_capacity_minutes 120

# AI 对话
momentum-agent chat "帮我安排今天的任务"

# 服务
momentum-agent serve --port 8765
```

## 技术栈

| 层 | 选型 |
|---|---|
| AI SDK | OpenAI Agents SDK |
| 数据库 | SQLite（默认）/ PostgreSQL（可选，适合多用户部署） |
| Web 服务 | Python stdlib `http.server` |
| 前端 | 原生 JS，零构建步骤 |
| 认证 | PBKDF2-SHA256 + Session Token |

## 项目结构

```
src/momentum_agent/
├── cli.py              # CLI 入口
├── web.py              # HTTP 服务 + REST API
├── agent_app.py        # Agent 核心逻辑
├── storage/            # 可插拔存储层
│   ├── sqlite.py       # SQLite 后端
│   ├── postgresql.py   # PostgreSQL 后端
│   └── factory.py      # 根据 DATABASE_URL 创建后端
├── auth.py             # 密码哈希 + 令牌
├── config.py           # 环境变量加载
├── parser.py           # 自然语言解析（fallback）
├── planner.py          # 模板任务拆分（fallback）
├── context.py          # 任务评分 + 建议
├── models.py           # 数据模型
├── logger.py           # 日志系统
├── insights.py         # 行为学习引擎
├── agents/             # 模块化 Agent
│   ├── agent.py        # Agent 构建
│   └── tools/          # 工具子模块
│       ├── task_tools.py
│       ├── subtask_tools.py
│       ├── focus_tools.py
│       ├── relation_tools.py
│       ├── weather_tools.py
│       ├── heartbeat_tools.py
│       └── insight_tools.py
├── services/           # 业务服务层
└── static/             # 前端资源
    ├── index.html
    ├── login.html
    ├── app.css
    ├── manifest.json   # PWA 配置
    └── js/
tests/
├── test_parser.py
├── test_config.py
├── test_context.py
├── test_storage.py           # 存储层核心测试
├── test_storage_factory.py   # 后端路由测试
├── test_postgresql_store.py  # PostgreSQL 集成测试（可选）
└── test_insights.py          # 行为学习测试
```

## 数据库配置

默认使用 SQLite，数据保存在 `.momentum/tasks.db`。

如果要部署给多人使用，建议切换到 PostgreSQL：

```powershell
# 1. 安装 PostgreSQL 依赖
pip install -e ".[postgresql]"

# 2. 通过环境变量指定数据库 URL
$env:MOMENTUM_DATABASE_URL = "postgresql://user:password@localhost:5432/momentum_db"

# 3. 启动服务
momentum-agent serve
```

也支持通过命令行参数指定：

```powershell
momentum-agent serve --db "postgresql://user:password@localhost:5432/momentum_db"
```

支持的 URL 格式：
- `sqlite:///absolute/path/to/db.db`
- `sqlite:///:memory:`
- `postgresql://user:pass@host/db`
- `postgres://user:pass@host/db`

## 测试

```powershell
pytest tests/ -v
```

PostgreSQL 集成测试默认跳过，设置环境变量后启用：

```powershell
$env:MOMENTUM_TEST_POSTGRES_URL = "postgresql://postgres:postgres@localhost:5432/momentum_test"
pytest tests/test_postgresql_store.py -v
```

## License

MIT
