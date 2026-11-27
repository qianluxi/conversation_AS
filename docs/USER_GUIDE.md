# conversation_AS 用户使用指引

本文面向第一次在本机运行 WorkBuddy + OpenCode 双智能体协作的用户。所有命令均以
Windows PowerShell 为例，也可在 macOS/Linux 的 shell 中使用（环境变量写法相应调整）。

---

## 0. 先理解三件事

1. **协调器是唯一入口**。两个智能体不直接互发消息；你只与协调器交互，由它负责
   会话映射、消息状态、结构化审查、持久化与恢复。
2. **消息可能"结果未知"**。网络超时/断线时协调器不会猜测成功或失败，而是标记
   `unknown` 并锁定后续操作，直到核对远端或人工释放。这是刻意设计，不是故障。
3. **审查不是看关键词**。审查者必须返回结构化 JSON；缺少 evidence/confidence
   或包含严重问题的结果不会被批准。

```
你 → CLI / Python API → Conversation（状态机 + SQLite）
                             ├── WorkBuddy 本地助理（全局互斥）
                             ├── WorkBuddy ACP（SSE 串行）
                             └── OpenCode（legacy / v2）
```

---

## 1. 环境准备

| 依赖 | 要求 |
| --- | --- |
| Python | 3.11+（本仓库在 3.12 验证） |
| OpenCode | 已安装 `opencode`（本仓库按 1.19.0 的 OpenAPI 验证） |
| WorkBuddy | 桌面端已登录并运行（推荐 `workbuddy_ipc`），或企业版云端任务可用（可选） |
| 网络 | 仅需访问你配置的 OpenCode 与 WorkBuddy 地址 |

检查：

```powershell
python --version
opencode --version
```

---

## 2. 安装

```powershell
cd F:\PyProjects\conversation_AS
python -m pip install -e ".[dev]"   # 含 pytest；只运行程序可去掉 [dev]
python -m pytest -q                 # 离线测试，不需要任何凭据
```

看到全部测试通过即可继续。图形界面另需可选依赖 `web`（见 6.1 节）。

---

## 3. 配置

```powershell
Copy-Item config.example.yaml config.yaml
```

`config.yaml` 中所有字符串都支持 `${ENV}` 和 `${ENV:-默认值}`。常用环境变量：

| 变量 | 用途 |
| --- | --- |
| `OPENCODE_SERVER_PASSWORD` | OpenCode serve 的 Basic Auth 密码 |
| `WORKBUDDY_CLIENT_ID` | WorkBuddy OAuth 应用 ID |
| `WORKBUDDY_CLIENT_SECRET` | WorkBuddy OAuth 应用密钥（不要提交到 git） |
| `WORKBUDDY_REDIRECT_URI` | OAuth 回调地址 |

最小配置（只接 OpenCode）：

```yaml
agents:
  opencode:
    type: opencode
    base_url: http://127.0.0.1:4096
    username: opencode
    password: ${OPENCODE_SERVER_PASSWORD}
    api_style: legacy        # 兼容性最好；1.19+ 也可改 v2
    delivery: async
    timeout: 3600
    request_timeout: 30
    cancel_timeout: 8
    inactivity_timeout: 0
    poll_interval: 1
storage:
  path: data/orchestrator.db
log_level: INFO
```

> `config.yaml`、`data/`、`*.db`、token 文件都已在 `.gitignore` 中，不要提交。

---

## 4. 启动并验证 OpenCode

```powershell
$env:OPENCODE_SERVER_PASSWORD = "请换成强密码"
opencode serve --hostname 127.0.0.1 --port 4096
```

保持该窗口运行，另开一个窗口：

```powershell
python -m agentorchestrator --config config.yaml health
```

预期：

```
[OK  ] opencode         opencode @ http://127.0.0.1:4096
```

单条消息：

```powershell
python -m agentorchestrator --config config.yaml send --agent opencode --text "用一句话介绍当前目录"
```

> 注意：`--config` 等全局参数必须写在子命令之前。

---

### 4.1 OpenCode 实际检查哪个目录？

只有 **OpenCode** 会读取/修改代码；`workbuddy`（IPC/官方 API）只负责方案与审查，
它没有文件系统访问能力。因此“检查代码”具体检查哪里，完全由 OpenCode 会话的
工作目录决定。

选择顺序：

1. **界面「OpenCode 工作目录」**（每次运行可改；API 字段 `opencode_directory`）；
   目录必须存在，否则启动会被拒绝；
2. `config.yaml` 中 `agents.opencode.directory`（推荐填绝对路径）；
3. 以上都没有时，使用 OpenCode 服务端默认目录（`GET /path` 返回的 `directory`）。

注意：OpenCode 服务默认目录不一定是你的项目。本机当前默认是
`C:\Users\Administrator\.config\opencode`，所以不指定目录时，“检查代码”不会检查
`F:\PyProjects\conversation_AS`。任务目标里的“检查代码”只是自然语言，不会被
协调器解析成路径。

执行角色决定权限边界：

- `plan`：只读检查，不能修改文件（检查代码请用这个）；
- `build`：可以编辑、提交甚至执行命令。任务目标里写“只读/不要修改”**不是**
  权限控制，只是提示，不能阻止 `build` 修改代码。

选择工作目录后，协调器会把“当前工作目录”写入 Planner/Worker/Reviewer 的提示：
“检查代码/项目”时不需要再粘贴代码，执行者应在该目录内直接使用工具检查。

协调器还会在 Worker 前后独立执行 `git HEAD/status/diff` 快照，并对纳入范围的
文件计算内容 SHA-256（覆盖被 git 忽略的文件；目录符号链接记录路径与目标）。
扫描有文件数、总字节、单文件大小和超时限制，超限或快照不可用会返回
`verification_unavailable`。排除目录与限制通过 `verification.*` 配置，界面会
显示扫描统计与排除范围。
当执行角色是 `plan` 时，如果快照发生变化（修改、新增、删除文件或新 commit），
本次运行会以 `read_only_violation` 失败；快照不可用则以
`verification_unavailable` 失败，不进入审查。

另外：Planner 也看不到文件，只能根据目标写方案，可能误判仓库现状。如果只是读取
某个文件/变量，请在目标里写清楚文件名与期望输出，或把执行角色设为 `plan` 并在
审查意见中纠正。

推荐显式指定目录，并检查任务选 `plan`：

```yaml
agents:
  opencode:
    type: opencode
    directory: F:/PyProjects/conversation_AS
```

或在界面中把「OpenCode 工作目录」填成该绝对路径。启动一次后可在 OpenCode
会话信息里核对 `directory` 字段。

## 5. 接入 WorkBuddy

### 5.1 个人开发者：本地 IPC（无需 OAuth，实验性）

个人账号无法申请硬件接入的 `user.localassistant.*` Scope。`workbuddy_ipc`
走 WorkBuddy 桌面端自带的本地 IPC（`wbipc`）调用内置 chat 接口，复用桌面端
登录态，不需要企业认证。

前置条件：Windows、WorkBuddy 桌面端正在运行并已登录。

```yaml
agents:
  workbuddy:
    type: workbuddy_ipc
    model: auto
    timeout: 3600
    history_limit: 20
```

```powershell
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml send --agent workbuddy --text "只回复 OK"
python -m agentorchestrator --config config.yaml run --goal "..." --planner workbuddy --worker opencode
```

说明：

- 只有 chat 能力，没有桌面端工具/文件操作；适合做 planner / reviewer；
- 内部接口，无兼容性承诺，WorkBuddy 升级后可能失效；
- 首次连接可能需要在 WorkBuddy 内允许 IPC 客户端；
- 详细原理与排查见 [WORKBUDDY_IPC.md](WORKBUDDY_IPC.md)。

### 5.2 官方 OAuth 2.1 授权（需企业认证）

在 WorkBuddy 开放平台创建应用（硬件接入），把 `client_id` / `client_secret` /
回调地址写入 `config.yaml`，然后：

```powershell
python -m agentorchestrator --config config.yaml auth url
# 浏览器打开输出链接 → 授权 → 从回调地址同时复制 code 和 state
python -m agentorchestrator --config config.yaml auth exchange <code> --state <state>
python -m agentorchestrator --config config.yaml auth status
```

`auth url` 会把 state 写入本地（10 分钟有效）；`auth exchange` 会校验 state，
不匹配或过期会拒绝交换。token 写入 `data/workbuddy_token.json`，过期自动刷新。

### 5.3 本地助理（`workbuddy_local`）

适合驱动 PC 端已登录的 WorkBuddy：

```powershell
python -m agentorchestrator --config config.yaml send --agent workbuddy --text "帮我查一下今天的日程"
```

要点：

- 本地助理没有独立 session，也没有远端取消接口；
- 同一 `base_url` 的所有任务共享一个远端状态门：
  `idle → submitted → running → idle`，异常进入 `quarantined`；
- 同一 `base_url` 还使用 `lock_file` 跨进程互斥（默认
  `data/workbuddy_local.lock`），两个协调器进程不能同时提交；
- 超时后**不会**自动允许下一条消息，必须核对远端或人工释放（见第 8 节）。

### 5.4 云端任务 ACP（`workbuddy_acp`）
适合长任务、流式输出。在 `config.yaml` 启用：

```yaml
agents:
  workbuddy_acp:
    type: workbuddy_acp
    base_url: https://www.workbuddy.cn
    client_id: ${WORKBUDDY_CLIENT_ID}
    client_secret: ${WORKBUDDY_CLIENT_SECRET}
    redirect_uri: ${WORKBUDDY_REDIRECT_URI}
    scope: "user.task.readable user.task.invokable"
    token_file: data/workbuddy_token.json
    bootstrap_prompt: "初始化协作会话，请等待后续指令。"
    cwd: /workspace
    timeout: 3600
    inactivity_timeout: 0
    poll_interval: 2
    auto_approve: false
    permissions:
      allow: [read]
      ask: [write, test, network]
      deny: [execute, delete, install]
```

```powershell
python -m agentorchestrator --config config.yaml send --agent workbuddy_acp --text "分析这个项目的风险"
```

权限确认：

- `allow` 自动同意（优先 `allow_once`）；
- `deny` 直接拒绝；
- `ask` 在 Web UI 的运行详情弹出「需要工具授权」，默认 120 秒超时后拒绝；
- CLI/无人值守没有交互通道，`ask` 按拒绝处理；
- `auto_approve: true` 只自动选择 `allow_once`，不会自动选择永久授权。

限制：

- 一个适配器实例只持有一个 ACP SSE 连接；
- 同一会话在等待响应期间禁止第二条 prompt（避免流式 chunk 串扰）；
- 创建任务必须带初始 prompt，因此用 `bootstrap_prompt` 初始化；
- ACP 历史轮次无法在重启后可靠关联，`resume` 会保持 `unknown`。

---

## 6. 命令速查

### 6.1 图形界面（推荐）

Windows 下可直接双击项目根目录的 **`start.bat`**。它会：

1. 检查 Python 与 Web 依赖；
2. 检查 WorkBuddy 桌面端是否运行（未运行会提示）；
3. 在 4097 启动一个专用 OpenCode 服务（密码写入 `data/opencode-password.txt`）；
4. 启动界面并打开 `http://127.0.0.1:8765/`。

如果已有旧 `config.yaml`，请把 `agents.opencode.base_url` 改成
`${OPENCODE_BASE_URL:-http://127.0.0.1:4096}`，否则 `start.bat` 的 4097 不会生效。
关闭标题为 “OpenCode 4097” 的窗口会停止该 OpenCode 服务。

也可手动启动：

```powershell
python -m pip install -e ".[web]"
python -m agentorchestrator --config config.yaml ui
```

默认自动打开 `http://127.0.0.1:8765/`。界面内可以：

- 检查 agent 连接；
- 填写任务目标，选择 Planner / Worker / Reviewer、**OpenCode 工作目录**、
  **OpenCode 模型**、**执行角色**、最大轮数与**单次 Agent 响应超时**并启动协作；
  模型下拉默认优先选择已配置的 `opencode-go` 模型，工作目录默认取启动 UI 的目录；
  执行角色默认 `plan`（只读）；需要改代码时切到 `build`；
- 实时查看运行状态、步骤耗时、消息全文、状态机事件和结构化审查结果；
- **执行结果 / 最终交付物**显示在运行详情顶部，可一键复制；审查卡只显示判定；
- ACP 工具授权会弹出确认（允许一次/允许/拒绝，超时拒绝）；
- 运行中可以「取消运行」，会尝试中止远端 Agent 并把 run 标记为 `cancelled`；
- 在「待处理 / 隔离消息」中可「继续等待」原任务、「核对恢复」、「中止远端」或
  强制释放；默认不重发、不自动中止远端。
- 从历史运行列表点击回看。

默认只监听 `127.0.0.1`。如果改成非回环地址（例如 `--host 0.0.0.0`），必须提供
访问密码，否则拒绝启动：

```powershell
$env:AGENTORCHESTRATOR_UI_TOKEN = "请换成强密码"
python -m agentorchestrator --config config.yaml ui --host 0.0.0.0 --token $env:AGENTORCHESTRATOR_UI_TOKEN
```

启用后页面、API 和 `/api/docs` 都需要 HTTP Basic Auth：用户名任意，密码为 token。
可选参数：`--port 8765`、`--no-browser`（不自动打开浏览器）。

### 6.2 命令行

```powershell
python -m agentorchestrator --config config.yaml agents
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml send  --agent <name> --text "..."
python -m agentorchestrator --config config.yaml chat  [--agent <name>]
python -m agentorchestrator --config config.yaml run   --goal "..." --max-rounds 2
python -m agentorchestrator --config config.yaml transcript [--run-id <id>]
python -m agentorchestrator --config config.yaml events     --run-id <id>
python -m agentorchestrator --config config.yaml pending    [--provider workbuddy_local]
python -m agentorchestrator --config config.yaml resume     --run-id <id> [--resend-unstarted]
python -m agentorchestrator --config config.yaml release    --run-id <id> --message-id <id> [--force]
python -m agentorchestrator --config config.yaml ui         [--host 127.0.0.1] [--port 8765] [--no-browser]
python -m agentorchestrator demo
```

`run` 退出码：`0` = 审查通过；`1` = 未通过/失败；`2` = 参数或配置错误；
`3` = 适配器错误。

`chat` 内置命令：`/agents`、`/agent <name>`、`/history`、`/quit`。

---

## 7. 一次完整协作

```powershell
python -m agentorchestrator --config config.yaml run --goal "审查特征工程模块，检查数据泄漏与时间序列划分" --max-rounds 2
```

流程：

1. `workbuddy` 分析并给出方案；
2. `opencode` 按方案检查/修改；
3. `workbuddy` 用结构化 JSON 审查；
4. `approved` → 完成；`revision_required` → 把 findings 交回 OpenCode；
   `blocked` 或结构非法 → 失败；超过最大轮数 → 失败。

审查者应返回（协调器强制校验）：

```json
{
  "decision": "revision_required",
  "summary": "存在数据泄漏风险",
  "findings": [
    {
      "severity": "high",
      "file": "features/feature_builder.py",
      "location": "build_features",
      "problem": "使用了验证集统计量",
      "suggested_action": "仅用训练集拟合"
    }
  ],
  "evidence": ["python -m pytest tests/test_features.py"],
  "confidence": 0.9
}
```

校验规则：

- `approved` 至少一条非空 `evidence`，`confidence >= 0.8`；
- 存在 `high`/`critical` finding 时不能 `approved`；
- `revision_required` 必须至少一条 finding；
- 出现未定义字段、缺字段、空 summary → 视为非法，整轮失败。

### 7.1 继续上次任务（工作流检查点）

每次运行会保存 `workflow` 检查点：`stage`（plan/work/review）、`round`、
`plan`、`work`、`issues`、审查结果。

- 失败在 Worker：`stage=work`，已完成的 Plan 不会重跑；
- 失败在 Reviewer：`stage=review`，Worker 结果不会重跑，只重跑审查；
- 界面运行详情会出现 **「继续此任务」** 按钮；
- 继续会创建一个**新 run**（记录 `parent_run_id`），原 run 保持原状态，
  不会原地重开终态 run；
- 如果上次 Worker 结果未知（`stage=work` 且没有 `work`），继续前需要确认，
  因为节点可能已经执行；系统会在提示中要求先检查已有产物。

旧版本创建的 run 没有检查点，会从已有消息中推导 plan/work 后再继续。
`resume` 仍只用于消息级恢复；工作流续跑请用「继续此任务」。

---

## 8. 故障处理手册（Runbook）

### 8.1 401 / 鉴权失败

- OpenCode：
  - 确认 `$env:OPENCODE_SERVER_PASSWORD` 与 `config.yaml` 的 `password` 一致，
    用户名固定 `opencode`；
  - 若报 `invalid api key`（例如默认 DeepSeek key 失效），在界面「OpenCode 模型」
    中改选已配置的模型（推荐 `opencode-go/deepseek-v4.1-flash`），或在配置中设置
    `provider_id` / `model_id`。
- WorkBuddy：`auth status` 检查；必要时 `auth url` + `auth exchange <code> --state <state>` 重新授权。

### 8.1.1 界面返回 401

- 启动时设置了 `--token` 或 `AGENTORCHESTRATOR_UI_TOKEN`：浏览器应弹出登录框，
  用户名任意，密码为 token；
- 如果输错或取消，刷新页面重新输入；脚本调用使用
  `Authorization: Bearer <token>`。

### 8.2 等待超时 / 结果未知（`unknown`）

先查看：

```powershell
python -m agentorchestrator --config config.yaml pending
python -m agentorchestrator --config config.yaml events --run-id <id>
```

生成类任务常见情况：文件其实已经写入工作目录，但模型还在跑验证命令，导致等待超时。
先检查工作目录和时间戳，再决定下一步。等待超时只停止本地等待，**不会自动中止远端
任务**：

- OpenCode 会保留 `provider_message_id`，可在「待处理 / 隔离消息」点「继续等待」，
  继续等待原任务而不是重发；也可以「核对恢复」或明确「中止远端」。
- WorkBuddy ACP 超时后进入隔离并保留会话锁，不会自动中止；核对远端后再恢复或中止。
- 默认单次 Agent 响应超时 3600 秒，可在界面按任务调到 7200 秒；Planner / Worker /
  Reviewer 分别计时。
- HTTP 查询请求有独立的 30 秒上限，取消请求默认 8 秒，避免轮询或取消接口绕过总截止时间。
- `opencode.inactivity_timeout` 默认 0（关闭）。即使开启，也只在没有运行中工具时生效，
  `npm install`、构建、完整测试等长命令不会因暂时无输出被误杀。
- 执行提示词允许合理的长时间安装/构建/测试，但要求保存输出日志并检查退出码；仍禁止
  截图、浏览器/GUI 等容易挂起的操作。

处理：

```powershell
# 只核对，不重发（推荐先执行）
python -m agentorchestrator --config config.yaml resume --run-id <id>

# 仅当消息从未被服务端确认过（attempt=0、无 provider_message_id）才重发
python -m agentorchestrator --config config.yaml resume --run-id <id> --resend-unstarted

# 确认远端已结束但历史查不到，人工放弃等待
python -m agentorchestrator --config config.yaml release --run-id <id> --message-id <id> --force
```

`release --force` 只解除协调器占用，**不会撤销**远端可能已经执行的代码修改。

### 8.3 WorkBuddy 本地助理被隔离

表现：`工作助理处于隔离状态` 或 `禁止并发发送`。

1. 打开 WorkBuddy 桌面端确认上一轮是否结束；
2. 执行 `resume --run-id <id>`；若历史中已有回复，会自动解除并补全；
3. 若历史仍查不到、且你确认可以放弃该轮结果，执行 `release --force`。

### 8.4 ACP 提示"该会话正在等待上一轮响应"

- 先 `resume`（能恢复则恢复）；
- 需终止远端：调用 `release --force`（内部会尝试 `session/cancel`）；
- 不要同时用两个进程驱动同一个 ACP 会话。

### 8.6 取消运行

Web UI 运行详情中的「取消运行」会：

1. 尽量向 Agent 下发中止（OpenCode abort / ACP session/cancel）；
2. 取消本地协程，把当前消息标记 `cancelled`；
3. 把 run 状态写为 `cancelled`，workflow 检查点标记 `cancelled`；
4. 清理该 run 的待授权请求。

取消不等于回滚：Agent 可能已经产生文件，请检查工作目录。

### 8.7 非法状态迁移

`InvalidTransition` 说明试图从终态继续或跳过中间状态。终态（completed/failed/
cancelled）不可恢复；需要新任务请新建 run。

---

## 9. 权限策略

仅在 ACP 工具授权时生效（OpenCode 侧请使用其自身权限配置）。规则基于
**token 级 operation**，不做任意子串匹配：

| 工具名示例 | 识别结果 | 默认类别 |
| --- | --- | --- |
| `read_file` | read | allow |
| `spreadsheet.write` | write | ask |
| `thread.create` | write | ask |
| `execute_command` / `Bash` | execute | deny |
| `file.remove` | delete | deny |
| `pip install` | install | deny |
| 无法识别 | — | ask |

- 复合操作取最严格：`read + write` → ask；`read + delete` → deny；
- `deny` 永远优先于 `auto_approve`；
- `ask` 在 Web UI 中等待人工确认；CLI/无人值守默认拒绝；
- `auto_approve` 只允许 `allow_once`，不会自动选择 `allow_always`。

可通过 `permissions.allow/ask/deny` 覆盖，支持 operation 名或同义词
（`read/list/grep`、`write/edit`、`bash/shell` 等）。

---

## 10. 数据、日志与清理

| 路径 | 内容 |
| --- | --- |
| `data/orchestrator.db` | runs / sessions / messages / steps / run_events |
| `data/workbuddy_token.json` | WorkBuddy OAuth token（仅官方 OAuth 适配器使用；勿提交） |
| `logs/`（如果启用文件日志） | 运行日志 |

排查 SQL：

```powershell
python -c "import sqlite3,json; c=sqlite3.connect('data/orchestrator.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('select run_id,status,phase,current_turn from runs order by created_at desc limit 10')]"
```

清理前先备份。不要删除 `run_events`/`steps`，它们是审计与恢复依据。

---

## 11. 作为库使用

```python
import asyncio
from agentorchestrator.config import load_config
from agentorchestrator.adapters import build_adapters
from agentorchestrator.storage.database import Store
from agentorchestrator.orchestrator.engine import Conversation, run_collaboration

async def main():
    config = load_config("config.yaml")
    adapters = build_adapters(config)
    store = Store(config.storage.path)
    conv = Conversation(store, adapters, goal="example")
    try:
        result = await run_collaboration(
            conv, planner="workbuddy", worker="opencode", max_rounds=2,
            goal="检查数据泄漏",
        )
        print(result)
    finally:
        await conv.close()
        store.close()

asyncio.run(main())
```

新增智能体：实现
`agentorchestrator/adapters/base.AgentAdapter`，在
`build_adapter()` 注册即可；上层流程不需要改。

---

## 12. 已知限制

- 本地助理已用 `lock_file` 做跨进程互斥；跨进程任务队列仍未实现。
- `resume` 只核对/补齐消息；要按节点继续工作流，请用界面的「继续此任务」。
- ACP 重启后无法唯一关联历史轮次，会保留 `unknown`。
- OpenCode 的工具权限由 OpenCode 自行控制；只读任务通过 `plan` + git/文件树
  快照做双重保护。非 git 工作区会以 `verification_unavailable` 失败，不会假装
  只读验证通过。
- 远端 `create_session` 在结果不确定时不会写入消息记录，需要人工确认会话是否创建。
- Reviewer 已有独立 git 证据，但协调器尚未自动执行测试/CI；完整质量门禁仍是后续工作。

---

## 13. 最小验收清单

> 真实接口兼容性记录见 [INTEGRATION_LOG.md](INTEGRATION_LOG.md)。

```powershell
python -m pytest -q                       # 全绿
python -m agentorchestrator demo          # 离线闭环，approved
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml run --goal "只读检查当前目录" --max-rounds 1
python -m agentorchestrator --config config.yaml events --run-id <上一步 run_id>
python -m agentorchestrator --config config.yaml pending
```

到此即可进入日常使用；需要扩展工作流时再进入下一阶段开发。
