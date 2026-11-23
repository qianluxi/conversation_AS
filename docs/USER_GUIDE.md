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
    timeout: 300
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
    timeout: 300
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
# 浏览器打开输出链接 → 授权 → 从回调地址复制 code
python -m agentorchestrator --config config.yaml auth exchange <code>
python -m agentorchestrator --config config.yaml auth status
```

token 写入 `data/workbuddy_token.json`，过期自动用 refresh_token 刷新。

### 5.3 本地助理（`workbuddy_local`）

适合驱动 PC 端已登录的 WorkBuddy：

```powershell
python -m agentorchestrator --config config.yaml send --agent workbuddy --text "帮我查一下今天的日程"
```

要点：

- 本地助理没有独立 session，也没有远端取消接口；
- 同一 `base_url` 的所有任务共享一个远端状态门：
  `idle → submitted → running → idle`，异常进入 `quarantined`；
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
    timeout: 600
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

限制：

- 一个适配器实例只持有一个 ACP SSE 连接；
- 同一会话在等待响应期间禁止第二条 prompt（避免流式 chunk 串扰）；
- 创建任务必须带初始 prompt，因此用 `bootstrap_prompt` 初始化；
- ACP 历史轮次无法在重启后可靠关联，`resume` 会保持 `unknown`。

---

## 6. 命令速查

### 6.1 图形界面（推荐）

界面依赖是可选的 `web` extra（FastAPI + uvicorn）；未安装时 `ui` 会提示安装命令。

```powershell
python -m pip install -e ".[web]"
python -m agentorchestrator --config config.yaml ui
```

默认自动打开 `http://127.0.0.1:8765/`。界面内可以：

- 检查 agent 连接；
- 填写任务目标，选择 Planner / Worker / Reviewer、**OpenCode 模型**、最大轮数与
  超时并启动协作；模型下拉默认优先选择已配置的 `opencode-go` 模型；
- 实时查看运行状态、步骤耗时、消息全文、状态机事件和结构化审查结果；
- 在「待处理 / 隔离消息」中执行核对恢复或强制释放；
- 从历史运行列表点击回看。

只监听 `127.0.0.1`；不要改成 `0.0.0.0` 暴露到局域网。可选参数：
`--host 127.0.0.1`、`--port 8765`、`--no-browser`（不自动打开浏览器）。

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

---

## 8. 故障处理手册（Runbook）

### 8.1 401 / 鉴权失败

- OpenCode：
  - 确认 `$env:OPENCODE_SERVER_PASSWORD` 与 `config.yaml` 的 `password` 一致，
    用户名固定 `opencode`；
  - 若报 `invalid api key`（例如默认 DeepSeek key 失效），在界面「OpenCode 模型」
    中改选已配置的模型（推荐 `opencode-go/deepseek-v4.1-flash`），或在配置中设置
    `provider_id` / `model_id`。
- WorkBuddy：`auth status` 检查；必要时 `auth url` + `auth exchange` 重新授权。

### 8.2 等待超时 / 结果未知（`unknown`）

先查看：

```powershell
python -m agentorchestrator --config config.yaml pending
python -m agentorchestrator --config config.yaml events --run-id <id>
```

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

### 8.5 非法状态迁移

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
- 未识别工具默认 ask，且没有 handler/auto_approve 时拒绝。

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

- 同一时刻建议只运行一个协调器进程；本地助理的互斥是进程内共享门，
  跨进程需要额外的文件锁/任务队列（后续版本）。
- `resume` 只核对/补齐消息，不自动继续整个工作流。
- ACP 重启后无法唯一关联历史轮次，会保留 `unknown`。
- OpenCode 的工具权限由 OpenCode 自行控制，本协调器不做拦截。
- 远端 `create_session` 在结果不确定时不会写入消息记录，需要人工确认会话是否创建。

---

## 13. 最小验收清单

```powershell
python -m pytest -q                       # 全绿
python -m agentorchestrator demo          # 离线闭环，approved
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml run --goal "只读检查当前目录" --max-rounds 1
python -m agentorchestrator --config config.yaml events --run-id <上一步 run_id>
python -m agentorchestrator --config config.yaml pending
```

到此即可进入日常使用；需要扩展工作流时再进入下一阶段开发。
