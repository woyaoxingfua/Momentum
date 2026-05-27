# Momentum Task Agent

一个基于 OpenAI Agents SDK 的个人任务待办助手。SQLite 存储，CLI + Web 双界面，支持自然语言交互。

## 功能

- **自然语言任务管理** — "明天下午3点交水费" 自动解析时间、优先级、重复频率
- **任务全生命周期** — 待办 → 进行中 → 已完成 / 已放弃，一键切换
- **大任务拆分** — AI 自动将目标拆为 3-5 个可执行子任务
- **重复任务** — 每天 / 每周 / 每月，完成后自动生成下一期
- **AI Agent 对话** — 14 个工具的自主 agent：查任务、建任务、给建议、记偏好、识别模式
- **会话记忆** — SQLiteSession 持久化，重启不丢对话上下文
- **流式输出** — 打字机效果，边想边说
- **用户认证** — 注册 / 登录 / 会话管理，PBKDF2 密码哈希
- **偏好设置** — 每日可用时间、工作时段，影响建议逻辑
- **数据导出导入** — JSON 备份还原
- **无 API Key 也能用** — 纯本地 regex 解析器 + 模板计划，完全离线

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
# 打开 http://127.0.0.1:8765
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
momentum-agent serve --port 8765 -v           # 启动 Web 服务
momentum-agent provider                       # 查看 AI 提供商状态
```

## Web 界面

```
http://127.0.0.1:8765
```

- 登录注册 → 任务工作台
- 状态标签筛选：待办 | 进行中 | 已完成 | 已放弃
- 搜索框实时过滤
- 右侧 Agent 对话面板，流式输出
- 偏好设置面板
- 导出 / 导入

## API

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

## 技术栈

| 层 | 选型 |
|---|---|
| AI SDK | OpenAI Agents SDK (OpenAIChatCompletionsModel) |
| 模型 | DeepSeek V4 Flash / GPT-4.1-mini / 任意 OpenAI-compatible |
| 数据库 | SQLite（自动 schema 迁移） |
| Web 服务 | Python stdlib `http.server`（零第三方依赖） |
| 前端 | 原生 JS ES Modules，零构建步骤 |
| 认证 | PBKDF2-SHA256 + Session Token |
| 日志 | Python `logging`（INFO/DEBUG，控制台+文件） |

## 项目结构

```
src/momentum_agent/
├── cli.py          # CLI 入口
├── web.py          # HTTP 服务 + REST API
├── agent_app.py     # Agent 核心：工具、会话、流式、guardrails
├── storage.py      # SQLite 存储层
├── auth.py         # 密码哈希 + 令牌
├── config.py       # 环境变量加载
├── parser.py       # 中文自然语言解析（fallback）
├── planner.py      # 模板任务拆分（fallback）
├── context.py      # 任务评分 + 建议 + 复盘
├── models.py       # 数据模型
├── logger.py       # 日志配置
└── static/
    ├── index.html  # 主界面
    ├── login.html  # 登录注册
    ├── app.css     # 样式
    ├── app.js      # 旧版（保留）
    └── js/         # ES Modules
        ├── api.js
        ├── app.js
        ├── tasks.js
        ├── chat.js
        ├── advice.js
        └── config.js
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
A: 能。任务 CRUD、模板规划、规则建议全都可以离线工作。只有 Agent 对话需要 API key。

## License

MIT
