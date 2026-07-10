# Momentum

> 不是「又一个待办清单」，而是一个会陪你把事做完的 AI 任务系统。

Momentum 是一个 **本地优先（Local-first）** 的任务管理工具：
- 用自然语言快速记任务
- 用 AI 自动拆解复杂目标
- 用数据洞察你的执行模式
- 用 Web + CLI 双入口覆盖不同工作流

---

## ✨ 为什么是 Momentum

- **快**：一句话建任务，默认就能跑（SQLite + 本地服务）
- **稳**：AI 不可用时自动降级到本地解析与模板规划
- **懂你**：内置行为分析、完成率趋势、时间预估偏差等洞察
- **可扩展**：可切换 OpenAI 兼容服务 / Ollama，本地与云端都能用

---

## 🧠 核心能力

### 1) 任务与计划
- 自然语言创建任务（时间、优先级、重复规则自动解析）
- 一键拆分大任务为可执行子任务（支持 AI + 本地 fallback）
- 任务状态流转：Todo / Doing / Done / Dropped / Reopen
- 标签、搜索、推迟、编辑、导入导出
- 任务关系管理：依赖、阻塞、父子、顺序等

### 2) Agent 助手
- 统一主 Agent + 专家 Agent 协作（洞察 / 天气 / 专注等）
- 工具调用与流式输出，交互更自然
- 支持图片输入做任务提取（启用视觉配置后）
- 支持记忆偏好与上下文，连续对话体验更好

### 3) 行为洞察
- 完成率统计与趋势
- 任务预估时间偏差分析
- 今日/本周到期、逾期与进行中任务追踪
- 下一步行动建议（Next Best Action）

---

## 🚀 快速开始

### 环境要求
- Python 3.11+

### 安装与启动

```bash
git clone https://github.com/woyaoxingfua/Momentum.git
cd Momentum

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

momentum-agent serve
# 打开 http://127.0.0.1:8765
```

默认账号：`default` / `momentum`（建议登录后立即修改密码）。

---

## ⚙️ AI 配置

Momentum 支持任意 OpenAI 兼容接口。

### 方式 A：环境变量

```bash
export MOMENTUM_API_KEY="sk-..."
export MOMENTUM_BASE_URL="https://api.deepseek.com/v1"
export MOMENTUM_MODEL="deepseek-chat"
```

> 注意：变量名是 `MOMENTUM_BASE_URL`（不是 `MOMENTUM_API_BASE`）。

### 方式 B：界面内配置
在 Web UI 的偏好设置中配置 `api_key / api_base / model / provider`。

### Ollama 本地模型

```bash
export MOMENTUM_PROVIDER="ollama"
export MOMENTUM_BASE_URL="http://localhost:11434"
export MOMENTUM_MODEL="llama3.2"
```

Momentum 会自动补全 `/v1`，并兼容 Ollama 的 OpenAI 风格接口。

---

## 💻 CLI 常用命令

```bash
# 新建与规划
momentum-agent add "明天下午3点交水费"
momentum-agent plan "下周准备产品经理面试"

# 列表与状态
momentum-agent list --status todo
momentum-agent start 1
momentum-agent done 1
momentum-agent reopen 1
momentum-agent drop 1

# 编辑与组织
momentum-agent edit 1 --priority high --tags 工作 紧急
momentum-agent postpone 1 --days 3
momentum-agent search "面试"

# 建议与复盘
momentum-agent advise
momentum-agent review

# 配置与数据
momentum-agent config show
momentum-agent config set daily_capacity_minutes 240
momentum-agent export > backup.json
momentum-agent import backup.json

# Agent 对话
momentum-agent chat "帮我安排今天可完成的任务"
```

---

## 🗄️ 数据存储

默认使用 SQLite：`.momentum/tasks.db`

可选切换 MySQL / Azure MySQL（自动 SSL 处理）：

```bash
pip install -e ".[mysql]"
export MOMENTUM_DATABASE_URL="mysql://user@host:3306/momentum_db"
momentum-agent serve
```

支持 URL：
- `sqlite:///absolute/path/to/db.db`
- `sqlite:///:memory:`
- `mysql://user@host:port/db`
- `azure://user@host:port/db`

---

## 📦 项目结构

```text
src/momentum_agent/
├── cli.py                # CLI 入口
├── agent_app.py          # Agent 编排与核心能力
├── config.py             # Provider / 环境变量配置
├── context.py            # 上下文计算与建议策略
├── insights.py           # 行为洞察
├── parser.py             # 自然语言解析（fallback）
├── planner.py            # 任务拆分（fallback）
├── auth.py               # 认证与密码哈希
├── web/                  # Web 服务端
├── static/               # 前端资源（原生 JS）
├── storage/              # SQLite / MySQL 存储实现
└── agents/               # 工具与专家 Agent
```

---

## 🧪 测试

```bash
pytest tests -v
```

MySQL 集成测试默认跳过，设置后可启用：

```bash
export MOMENTUM_TEST_MYSQL_URL="mysql://user@localhost:3306/momentum_test"
pytest tests/test_mysql_store.py -v
```

---

## 🌐 在线体验

https://myfirst.cc.cd

---

## 📄 License

MIT
