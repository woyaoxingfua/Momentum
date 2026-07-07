# Momentum

> 不是又一个 Todoist。是一个会学习你行为模式的 AI 任务助手。

Momentum 是一个本地优先的任务管理工具，用 AI 帮你拆解计划、记住偏好、发现拖延模式。暗色界面 + 琥珀橙强调，长时间看不累。

## 能干什么

### 任务管理
- **自然语言创建** — `明天下午3点交水费` 自动解析时间、优先级、重复
- **子任务拆分** — 大任务丢给 AI，自动拆成 3-5 个可执行小步骤
- **番茄钟专注** — 选任务 → 定时长 → 开始倒计时，完成自动标记任务
- **重复任务** — 每天/每周/每月，完成后自动生成下一期
- **任务关系** — 依赖、阻塞、关联、父子、顺序，5 种关系类型
- **标签系统** — 打标签、按标签筛选、批量操作

### AI 助手
- **40+ 工具调用** — 自主决策：查任务、建任务、拆子任务、分析模式、记偏好
- **真流式输出** — 逐 token 打字机效果，工具调用实时推送
- **多 Agent 协作** — 主 Agent + 统计专家 + 天气专家，自动路由
- **视觉识别** — 上传截图，AI 自动提取任务（需配置视觉模型）
- **无 API Key 也能用** — 本地 regex 解析 + 模板计划，断网也不慌

### 行为洞察
- **完成率趋势** — 看你每周到底做完了多少事
- **预估偏差** — 你总是低估任务时间？数据会告诉你
- **高效时段** — 你早上 9 点最能干，还是夜猫子？
- **倦怠风险** — 连续高负荷时提醒你减速

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/woyaoxingfua/Momentum.git
cd Momentum

# 2. 安装（Python 3.11+）
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 3. 启动
momentum-agent serve
# 打开 http://127.0.0.1:8765
```

默认账号：`default` / `momentum`（登录后建议改密码）。

### 配置 AI（可选）

在界面右侧「偏好设置」里填入 API Key，或通过环境变量：

```bash
export MOMENTUM_API_KEY=sk-...
export MOMENTUM_API_BASE=https://api.deepseek.com/v1
export MOMENTUM_MODEL=deepseek-chat
```

支持任何 OpenAI 兼容的 API。

## 界面速览

```
┌─────────────────────────────────────┬──────────────┐
│  任务输入框（自然语言）              │              │
│  ────────────────────────────────   │   Agent      │
│                                     │   对话区     │
│  下一步建议（AI 推荐）              │              │
│  ────────────────────────────────   ├──────────────┤
│                                     │              │
│  待办 | 进行中 | 已完成             │   专注       │
│  ┌──────────────────────────────┐  │   计时器     │
│  │ □ 买牛奶  明天 17:00  高     │  │              │
│  │ □ 写周报  周五     中        │  ├──────────────┤
│  └──────────────────────────────┘  │              │
│                                     │   偏好设置   │
└─────────────────────────────────────┴──────────────┘
```

暗色主题，琥珀橙强调，衬线标题 + 等宽标签。

## CLI 命令

```bash
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

# AI 对话
momentum-agent chat "帮我安排今天的任务"

# 服务
momentum-agent serve --port 8765
```

## 数据库配置

默认 SQLite，数据存在 `.momentum/tasks.db`。

部署给多人用建议切 MySQL：

```bash
pip install -e ".[mysql]"
export MOMENTUM_DATABASE_URL="mysql://root:0000@localhost:3306/momentum_db"
momentum-agent serve
```

支持的 URL 格式：
- `sqlite:///absolute/path/to/db.db`
- `sqlite:///:memory:`
- `mysql://user:password@host:port/db`

## 本地模型（Ollama）

不想用云端 API？直接连本地 Ollama：

```bash
# 在界面右侧「偏好设置」里选择「Ollama 本地模型」
# 或设置环境变量
export MOMENTUM_PROVIDER=ollama
export MOMENTUM_API_BASE=http://localhost:11434
export MOMENTUM_MODEL=llama3.2
```

Ollama 需要暴露 OpenAI 兼容端点（`http://localhost:11434/v1`），Momentum 会自动补全 `/v1`。API Key 可留空或填 `ollama`。

## PWA / 离线使用

Momentum 支持安装为 PWA：

- 桌面 Chrome / Edge：地址栏右侧点击「安装 Momentum」
- 移动端：添加到主屏幕

Service Worker 会缓存静态资源，断网仍能打开应用界面（API 请求仍需联网）。

## 技术栈

| 层 | 选型 |
|---|---|
| AI SDK | OpenAI Agents SDK |
| 数据库 | SQLite（默认）/ MySQL（可选） |
| Web 服务 | Python stdlib `http.server` |
| 前端 | 原生 JS，零构建 |
| 认证 | PBKDF2-SHA256 + Session Token（7 天过期） |
| 测试 | pytest，100+ 用例 |

## 项目结构

```
src/momentum_agent/
├── cli.py              # CLI 入口
├── agent_app.py        # Agent 核心逻辑
├── auth.py             # 密码哈希 + 令牌
├── config.py           # 环境变量
├── parser.py           # 自然语言解析（fallback）
├── planner.py          # 模板任务拆分（fallback）
├── context.py          # 任务评分 + 建议
├── models.py           # 数据模型
├── logger.py           # 日志
├── insights.py         # 行为学习引擎
├── web/                # HTTP 服务
│   ├── server.py
│   ├── handlers.py
│   └── utils.py
├── storage/            # 可插拔存储层
│   ├── sqlite.py
│   ├── mysql.py
│   └── factory.py
├── agents/             # 模块化 Agent
│   ├── agent.py
│   └── tools/
│       ├── _common.py
│       ├── task_tools.py
│       ├── subtask_tools.py
│       ├── focus_tools.py
│       ├── relation_tools.py
│       ├── weather_tools.py
│       ├── heartbeat_tools.py
│       └── insight_tools.py
└── static/             # 前端资源
    ├── index.html
    ├── login.html
    ├── app.css
    └── js/
```

## 测试

```bash
pytest tests/ -v
```

MySQL 集成测试默认跳过，设置环境变量后启用：

```bash
export MOMENTUM_TEST_MYSQL_URL="mysql://root:0000@localhost:3306/momentum_test"
pytest tests/test_mysql_store.py -v
```

## 在线体验

https://myfirst.cc 

## License

MIT
