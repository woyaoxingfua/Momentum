# Momentum 配置向导（Setup Wizard）设计文档

> 日期：2026-07-11
> 状态：已通过设计评审，待实施

## 1. 背景与目标

Momentum 当前有 **17 个环境变量、30+ CLI 参数、10 个 DB 用户偏好、若干硬编码默认值**，在新环境下部署时配置门槛高。此外 `.env.example` 存在两个变量名与代码不一致（`MOMENTUM_API_BASE` 应为 `MOMENTUM_BASE_URL`、`MOMENTUM_DB_PATH` 应为 `MOMENTUM_DATABASE_URL`），默认账户 `default/momentum` 是弱口令。

**目标**：提供 `momentum-agent init` 命令，以终端 TUI 向导形式引导用户完成全量配置，跑完一键可用。

**非目标**：
- 不做 Web 版配置页（已有 Web 偏好设置）
- 不做配置版本管理 / 回滚机制
- 不改变现有命令的行为（增量改动）

## 2. 设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 向导形态 | 终端 TUI | 匹配项目本地优先、CLI 优先气质；无需浏览器即可在 DB/服务启动前完成配置 |
| TUI 库 | `questionary` | 专为 CLI 向导设计，开箱即用 select/checkbox/password/confirm；依赖极轻（prompt_toolkit ~150KB） |
| 配置范围 | 全量 | 覆盖启动级 + 运行级 + 进阶级，跑完一键可用 |
| 重复运行 | 预填+逐项问 | 读取已有配置作为每项默认值，回车保留，输入新值覆盖（类 npm init / django-admin） |
| 配完后衔接 | 末尾问是否启动 | 配完汇总预览 → 问是否启动 serve |
| 依赖归属 | 新增 `[wizard]` 可选依赖组 | 不进 `[dev]`，生产部署不装 questionary |

## 3. 命令入口

### 新增命令

```bash
momentum-agent init              # 交互模式（默认）
momentum-agent init --non-interactive  # 用全部默认值+已有配置，不提问（CI 友好）
momentum-agent init --db sqlite:///.momentum/tasks.db  # 跳过 DB 选择步
momentum-agent init --skip-db-check  # 跳过 DB 连接测试
```

### 新增模块

```
src/momentum_agent/setup_wizard.py     # 向导主逻辑（~500-700 行）
src/momentum_agent/wizard_config.py    # momentum.config.json 读写
tests/test_setup_wizard.py             # 测试
```

`setup_wizard.py` 内部结构：

```text
setup_wizard.py
├── run_wizard(db_url?, non_interactive?, skip_db_check?) -> WizardResult
├── _steps/  (每步一个函数)
│   ├── step_welcome()
│   ├── step_database()       → 选 sqlite/mysql/azure + 测连接
│   ├── step_security()       → 检测 default/momentum 弱口令，强制改密
│   ├── step_ai_provider()    → provider/key/base/model + ping 测试
│   ├── step_preferences()    → daily_capacity / working_hours / vision_enabled
│   ├── step_location()       → user_location
│   ├── step_heartbeat()      → enabled / start_hour / end_hour / interval_hours
│   ├── step_web_server()     → host / port + 端口占用检测
│   ├── step_mcp()            → sse 开关 + host/port/api_key
│   ├── step_logging()        → level / dir / file / max_bytes / backups
│   └── step_advanced()       → thinking / reasoning_effort / disable_tracing
├── _persistence
│   ├── write_env_file(updates)     → 改写 .env，保留注释和未涉及行
│   ├── write_user_config(store, user_id, prefs)  → 调 store.set_memory
│   └── write_web_config(host, port) → 写 momentum.config.json
├── _preview    → rich.table 渲染配置预览
└── _validators → DB 连接 / AI key 测试 / 端口可用性 校验
```

## 4. 向导流程（执行顺序）

前置依赖决定执行顺序：

1. **欢迎页** + 检测现有 `.env` / DB
2. **DB 后端**（必须先定，后面要连库）→ sqlite 路径或 mysql/azure 连接参数 → 测连接 → 初始化 schema
3. **安全**（依赖 store）：检测 default 账户是否仍是 `momentum` 弱口令 → 强制改密
4. **AI provider**：openai 兼容 / ollama / 跳过；ollama 自动填 base_url 并拉取模型列表；openai 问 key/base/model 并可选 ping
5. **工作偏好**：daily_capacity_minutes / working_hours_start/end / vision_enabled
6. **位置**：user_location（默认「北京」）
7. **心跳**：enabled / start_hour / end_hour / interval_hours
8. **Web 服务**：host / port + 端口占用检测
9. **MCP**：是否启用 SSE → host/port/api_key
10. **日志**：level / dir / file / max_bytes / backups
11. **进阶**（可选，默认跳过）：thinking / reasoning_effort / disable_tracing
12. **配置预览**：rich.table 渲染「项 / 旧值 / 新值」
13. **确认** → 写 `.env` + 写 `user_memory` + 写 `momentum.config.json`
14. **末尾问是否启动 serve**

## 5. 各步骤详细设计

### 5.1 DB 后端步骤

**交互流程**：
1. 选择后端：`SQLite（本地文件）` / `MySQL` / `Azure MySQL（自动 SSL）`
2. 按选择展开参数：
   - **SQLite**：问文件路径，默认 `.momentum/tasks.db`；选项「在用户主目录」`~/.momentum/tasks.db`
   - **MySQL**：host / port(3306) / user(root) / password / database / charset(utf8mb4)
   - **Azure**：同 MySQL + 自动启用 SSL（无需额外参数）
3. **实时连接测试**：尝试 `create_task_store(url)` + 触发 `_ensure_default_user()`（建表 + 插默认账户）
   - 成功 → 绿色「✓ 连接成功，已初始化 schema」
   - 失败 → 红色错误 + 问「重试 / 跳过」
4. **安全检查**：若 default 账户仍是 `momentum` 弱口令 → 标记，进 §5.2

**URL 拼装规则**（跟现有 `factory.py` 一致）：
- SQLite：`sqlite:///<abs_path>` 或 `sqlite:///:memory:`
- MySQL：`mysql://user:password@host:port/database`
- Azure：`azure://user:password@host:port/database`

**密码处理**：用 `questionary.password` 掩码输入；MySQL URL 里密码做 URL encode（处理 `@:/` 等特殊字符）。

**幂等性**：读取现有 `MOMENTUM_DATABASE_URL`，解析 scheme 判断后端类型。SQLite 路径回填；MySQL/Azure 解析 host/port/user/db 回填，密码留空（提示「已配置，回车保留；输入新值覆盖」）。

**写盘**：写 `.env` 的 `MOMENTUM_DATABASE_URL=<url>`。

### 5.2 安全步骤（强制改弱口令）

**检测逻辑**：
1. `store.validate_login("default", "momentum")` 尝试用弱口令登录
2. 登录成功 → 说明 default 账户仍是弱口令 → 进强制改密流程
3. 登录失败 → 说明已改过 → 跳过

**强制改密流程**：
- `questionary.password` 掩码输入两次
- 校验：长度 ≥ 8、两次一致；不通过重新输
- `store.change_password("default", "momentum", new_password)`
- 改完不打印明文，只确认「已更新」

**可选：注册新管理员**（默认否）：
- 问 user_id / display_name / password
- `register_user` 后提示「请用新账户登录」

**幂等性**：密码不回填；重复跑 `init` 检测到弱口令就改，已改就跳过。单向安全操作。

**写盘**：改密直接走 `store.change_password()`，不写 `.env`，不留明文痕迹。

### 5.3 AI Provider 步骤

**交互流程**：
```
选择 AI 提供商：
> OpenAI 兼容（DeepSeek / OpenAI / 第三方）
  Ollama（本地模型）
  跳过（先用本地解析，稍后再配）
```

**分支 A：OpenAI 兼容**：
- API Key 用 `questionary.password` 掩码
- Base URL 给快捷选项：DeepSeek / OpenAI 官方 / 自定义
- 模型名对常见 provider 给建议值
- **测试调用**：用 `AsyncOpenAI` 发一次 `chat.completions.create(messages=[{"role":"user","content":"ping"}], max_tokens=5)`，5 秒超时

**分支 B：Ollama**：
- 自动补全 `/v1` 后缀
- API Key 自动填占位符 `"ollama"`
- 从 `{base}/api/tags` 拉取本地可用模型列表，用户选而不是手输
- 连不上 → 重试/跳过

**分支 C：跳过**：
- 写 `provider=none` 到 `user_memory`，`api_key` 留空
- 提示「AI 功能将降级到本地解析，稍后可在 Web 偏好设置里补全」

**幂等性**：读取现有 `user_memory` 作为预填。API Key 不回填明文，显示为「已配置（回车保留；输入新值覆盖）」。

**写盘**：走 `store.set_memory(key, value, user_id=user_id)` 批量写入 `user_memory` 表。写入后立即生效，无需重启。可选「写入 .env 全局生效」给多用户场景。

### 5.3b 工作偏好步骤

紧接 AI Provider 之后，问运行级偏好（均写入 `user_memory`）：

- `vision_enabled`：是否启用图片视觉识别（默认否）
- `daily_capacity_minutes`：每日可用时间（默认 240）
- `working_hours_start` / `working_hours_end`：工作时间（默认 09:00 / 18:00）

**幂等性**：读取现有 `user_memory` 回填原值。

### 5.3c 位置步骤

- `user_location`：默认城市（默认「北京」）

**幂等性**：读取现有 `user_memory` 的 `user_location` 回填。

**写盘**：走 `store.set_memory()`，写入后立即生效。

### 5.4 Web 服务步骤

**交互流程**：
- 监听地址：快捷选项 `127.0.0.1（仅本机）` / `0.0.0.0（局域网）` / 自定义（默认 127.0.0.1）
- 监听端口：默认 8765，`socket.bind` 检测占用

**写盘**：写 `momentum.config.json` 的 `web.host` / `web.port`；支持 env `MOMENTUM_WEB_HOST` / `MOMENTUM_WEB_PORT`。

### 5.5 MCP 步骤

**交互流程**：
- 默认否——大多数用户用 stdio（Claude Desktop/Cursor）就够
- 启用 SSE 时才问 host/port/api_key
- API Key 用 `questionary.password` 掩码两次，校验一致

**写盘**：
- `momentum.config.json` 的 `mcp.host` / `mcp.port`
- `MOMENTUM_MCP_API_KEY` 写入 `.env`（敏感凭据不进 config.json）

### 5.6 日志步骤

**交互流程**：
- 级别：单选 DEBUG / INFO / WARNING / ERROR（默认 INFO）
- 目录：快捷选项 `logs`（项目内）/ `~/.momentum/logs`（用户主目录）/ 自定义
- 文件名：留空则按日期 `momentum-YYYY-MM-DD.log`
- 单文件上限 MB：默认 10
- 保留备份数：默认 5

**写盘**：全部写 `.env`：`MOMENTUM_LOG_LEVEL` / `MOMENTUM_LOG_DIR` / `MOMENTUM_LOG_FILE` / `MOMENTUM_LOG_MAX_BYTES` / `MOMENTUM_LOG_BACKUPS`。

### 5.7 进阶选项（可选，默认跳过）

- `MOMENTUM_DISABLE_TRACING`：关闭 agents SDK 链路追踪（默认当使用自定义 base_url 时自动关闭）
- `MOMENTUM_THINKING`：思考模式 enabled / disabled
- `MOMENTUM_REASONING_EFFORT`：推理强度 minimal / low / medium / high

**写盘**：写 `.env`。

## 6. momentum.config.json 设计

新引入 `momentum.config.json`（项目根目录），用于存储 web/mcp 的 host/port：

```json
{
  "web": {
    "host": "127.0.0.1",
    "port": 8765
  },
  "mcp": {
    "host": "127.0.0.1",
    "port": 8766
  }
}
```

**回退链**（`cli.py` 的 serve/mcp 子命令需要扩展支持）：
```
CLI flag (--host/--port)  ← 最高优先级
  ↓
环境变量 (MOMENTUM_WEB_HOST/PORT, MOMENTUM_MCP_HOST/PORT)
  ↓
momentum.config.json
  ↓
硬编码默认 (127.0.0.1:8765 / 127.0.0.1:8766)  ← 最低
```

**对已有部署的影响**：无。回退链中硬编码默认排在最后，现有部署只要还在用 flag/env 启动，行为不变。

## 7. .env 文件写入逻辑

**`write_env_file(updates: dict)`**：
- 读取现有 `.env`（若存在）
- 逐行处理：保留注释行（`#` 开头）和空行；对已存在的 key 替换值；对新 key 追加到末尾
- 若 `.env` 不存在则新建并加 header 注释
- 不覆盖未涉及的行

**示例**：
```env
# Momentum Task Agent 配置（由 momentum-agent init 生成）
# 生成时间：2026-07-11 12:00:00

MOMENTUM_DATABASE_URL=mysql://user:pass@host:3306/momentum
MOMENTUM_LOG_LEVEL=INFO
MOMENTUM_LOG_DIR=logs
MOMENTUM_MCP_API_KEY=secret-key
```

## 8. .env.example 修正

当前 `.env.example` 有两处变量名与代码不一致：

| .env.example 中的变量 | 代码实际读取的变量 |
|---|---|
| `MOMENTUM_API_BASE` | `MOMENTUM_BASE_URL` |
| `MOMENTUM_DB_PATH` | `MOMENTUM_DATABASE_URL` |

**修法**：直接改名为正确变量名。同时补全缺失的环境变量清单：`MOMENTUM_USER`、`MOMENTUM_PROVIDER`、`MOMENTUM_DISABLE_TRACING`、`MOMENTUM_THINKING`、`MOMENTUM_REASONING_EFFORT`、`MOMENTUM_LOG_FILE`、`MOMENTUM_LOG_DIR`、`MOMENTUM_LOG_MAX_BYTES`、`MOMENTUM_LOG_BACKUPS`、`MOMENTUM_WEB_HOST`、`MOMENTUM_WEB_PORT`、`MOMENTUM_MCP_HOST`、`MOMENTUM_MCP_PORT`。

## 9. 依赖变更

### pyproject.toml

新增 `[wizard]` 可选依赖组：
```toml
wizard = [
    "questionary>=2.0",
    "rich>=13.0",
]
```

**不进 `[dev]`**：生产部署 `pip install -e '.[dev]'` 不会装 questionary；想跑向导时 `pip install -e '.[wizard]'`。

### deploy.yml 修正

第 65 行从：
```bash
pip install -e '.[dev]' --quiet
```
改为：
```bash
pip install -e '.[mysql,dev]' --quiet
```

**理由**：VM 上实际用了远程 MySQL，但 CI 部署只装 `[dev]`（不含 pymysql），依赖「之前手动装过 pymysql」的隐性前提。改后 VM 重建 `.venv` 也能直连。

## 10. 对已有部署的影响评估

| 改动 | 影响级别 | 说明 |
|------|---------|------|
| 新增 `setup_wizard.py`、`init` 命令 | 🟢 增量 | 纯新增，不碰已有逻辑 |
| 修 `.env.example` 变量名 | 🟢 增量 | 只是示例文件 |
| 新增 `momentum.config.json` 回退 | 🟡 兼容 | 回退链中硬编码默认排最后，现有 flag/env 启动不受影响 |
| 扩展 `cli.py` serve/mcp 支持 config.json | 🟡 兼容 | 有测试覆盖 |
| `deploy.yml` 改 `[dev]` → `[mysql,dev]` | 🟡 兼容 | VM 上已装 pymysql，不卸载，只是补全 |
| 新增 `[wizard]` 依赖组 | 🟢 增量 | 不影响现有安装 |

**无 🔴 破坏性变更。**

## 11. 测试策略

### 测试文件

`tests/test_setup_wizard.py`，覆盖：

1. **`write_env_file()`**：
   - 新建 .env（文件不存在）
   - 更新已有 key
   - 追加新 key
   - 保留注释和空行
   - 不覆盖未涉及的行

2. **`wizard_config.py`**（momentum.config.json 读写）：
   - 读写往返
   - 文件不存在时返回默认值
   - 部分字段缺失时回退

3. **回退链**：
   - CLI flag > env > config.json > 默认
   - 各层缺失时正确回退

4. **`_validators`**：
   - DB 连接成功/失败
   - 端口可用/占用
   - AI key 测试（mock AsyncOpenAI）

5. **`step_*` 函数**：
   - 每步的预填逻辑（mock `questionary` 输入）
   - 每步的写盘逻辑

6. **端到端**：
   - `--non-interactive` 模式全流程跑通
   - 重复运行幂等性

### CI 覆盖

测试在 Python 3.11 / 3.12 矩阵跑（跟现有 CI 一致），失败阻止部署。

## 12. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/momentum_agent/setup_wizard.py` | 新建 | 向导主逻辑 |
| `src/momentum_agent/wizard_config.py` | 新建 | momentum.config.json 读写 |
| `src/momentum_agent/cli.py` | 修改 | 新增 `init` 子命令；扩展 serve/mcp 支持 config.json 回退 |
| `src/momentum_agent/__main__.py` | 修改 | 同 cli.py |
| `pyproject.toml` | 修改 | 新增 `[wizard]` 依赖组 |
| `.env.example` | 修改 | 修正变量名 + 补全缺失变量 |
| `.github/workflows/deploy.yml` | 修改 | `[dev]` → `[mysql,dev]` |
| `tests/test_setup_wizard.py` | 新建 | 测试 |
| `.gitignore` | 修改 | 确认 `momentum.config.json`、`.env` 被忽略 |
| `README.md` | 修改 | 新增「配置向导」章节 |

## 13. 未覆盖项（明确排除）

- **前端 localStorage 配置**（主题、背景图、透明度）：纯前端偏好，不进向导
- **登录限流参数**（`MAX_LOGIN_ATTEMPTS`/`LOGIN_LOCKOUT_SECONDS`）：当前硬编码，不在向导范围
- **配置版本管理 / 回滚机制**：YAGNI
- **Web 版配置页**：已有 Web 偏好设置，不重复
