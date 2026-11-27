# WorkBuddy + OpenCode 双智能体编排器

[![CI](https://github.com/qianluxi/conversation_AS/actions/workflows/ci.yml/badge.svg)](https://github.com/qianluxi/conversation_AS/actions/workflows/ci.yml)

一个本地 Python 协调器：**不**让两个智能体直接互发消息，而是由协调器统一管理
会话映射、消息协议、状态机、结构化审查、持久化与故障恢复。

项目名 `agentorchestrator`，当前版本 `0.10.0`，要求 Python `>=3.11`
（与 `pyproject.toml` 一致；构建后端为 setuptools>=68）。

> 首次使用请看 **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**（安装、配置、
> OAuth、命令、故障处理手册）。

```
用户目标
   │
   ▼
Orchestrator（本仓库）
   ├── WorkBuddyAdapter   ──► 本地助理 REST（OAuth 2.1，远端状态门）
   ├── WorkBuddyIpcAdapter ─► 桌面端本地 IPC（实验性，无 OAuth，见 docs/WORKBUDDY_IPC.md）
   ├── WorkBuddyACPAdapter ─► 云端任务 ACP（SSE + JSON-RPC 2.0，会话串行）
   ├── OpenCodeAdapter    ──► opencode serve（REST，legacy 或 v2）
   └── SQLite（runs / sessions / messages / steps / run_events）
```

## 快速开始（离线，不需要任何凭据）

核心依赖（`httpx` / `pydantic` / `PyYAML`）随 `pip install -e .` 安装；
测试依赖 `pytest` 属于 `.[dev]`，图形界面依赖 FastAPI/uvicorn 属于 `.[web]`。

```powershell
python -m pip install -e .
python -m agentorchestrator demo
python -m agentorchestrator transcript --run-id <run_id>
python -m agentorchestrator events --run-id <run_id>
```

安装后也可用等价的控制台入口 `agentorchestrator`（`pyproject.toml` 的
`[project.scripts]`）替代 `python -m agentorchestrator`。

## 图形界面（推荐给普通用户）

Windows 用户可以直接双击仓库根目录的 **`start.bat`**：它会检查 Python/依赖、
确认 WorkBuddy 是否运行、在 4097 启动一个专用 OpenCode 服务，然后启动界面并打开
`http://127.0.0.1:8765/`。OpenCode 访问密码保存在 `data/opencode-password.txt`
（已 gitignore），下次启动复用。

也可以手动启动（需要自己先运行 `opencode serve`）：

```powershell
python -m pip install -e ".[web]"
python -m agentorchestrator ui
```

`start.bat` 支持透传 UI 参数，例如：

```bat
start.bat --port 8766 --token my-password
```

注意：它使用 4097 的专用 OpenCode 服务。如果已有旧 `config.yaml`，请把
`agents.opencode.base_url` 写成
`${OPENCODE_BASE_URL:-http://127.0.0.1:4096}`（或删除后从示例重新复制），
否则 `start.bat` 设置的 4097 不会生效。

默认自动打开 `http://127.0.0.1:8765/`，包含：

- 一键检查各 agent 连接状态；
- 填写目标任务，选择 Planner / Worker / Reviewer、**OpenCode 工作目录**与
  **OpenCode 模型**；目录决定实际检查/修改哪个项目的代码，默认取启动 UI 的目录；
  模型默认优先选择已配置的 `opencode-go` 模型，避免默认模型鉴权失败；
- **OpenCode 执行角色**：`plan`（只读检查，默认）/ `build`（可修改代码）；
- 点击「开始协作」后实时查看运行状态、步骤耗时、消息全文、状态机事件；
- **执行结果 / 最终交付物**置顶显示（可一键复制）；审查卡只显示判定，不重复正文；
- 结构化审查结果（结论、findings、evidence、confidence）；
- 待处理/隔离消息的「核对恢复」与「强制释放」；
- ACP 工具授权请求会在界面弹出确认（允许一次/允许/拒绝，超时自动拒绝）；
- 运行中的任务可以「取消运行」；
- 历史运行列表，点击回看。

界面默认只监听 `127.0.0.1`，不对外暴露。常用参数：
`--host 127.0.0.1`、`--port 8765`、`--no-browser`。

安全限制：如果监听非回环地址（例如 `--host 0.0.0.0`），必须同时提供
`--token <密码>` 或环境变量 `AGENTORCHESTRATOR_UI_TOKEN`，否则拒绝启动。
启用 token 后，页面、`/api/*` 与 `/api/docs` 都要求 HTTP Basic Auth
（用户名任意，密码为 token）。详细说明见
[docs/USER_GUIDE.md 第 6.1 节](docs/USER_GUIDE.md#61-图形界面推荐)。

## 核心可靠性设计（v0.9）

### 1. 结构化审查（无关键字放行）

审查者必须返回 JSON，`agentorchestrator/review.py` 使用 Pydantic 严格校验：

```json
{
  "decision": "approved | revision_required | blocked",
  "summary": "不能为空",
  "findings": [
    {"severity": "high", "file": "a.py", "location": "L42",
     "problem": "存在数据泄漏", "suggested_action": "调整时间窗口"}
  ],
  "evidence": ["pytest -q 输出"],
  "confidence": 0.9
}
```

- 未知字段、缺字段、空 `summary` → 非法；
- `approved` 必须至少一条非空 `evidence`，且 `confidence >= 0.8`；
- 存在 `high`/`critical` finding 时禁止 `approved`；
- 「我不认为已经达到 APPROVED 的标准」无法解析，按失败处理，不会误批准。

### 2. 持久化状态机与审计

```
created → analyzing → executing → reviewing
                          ├── revision_required → executing ...
                          └── approved → completed
任意阶段 → failed / cancelled
```

- 终态（completed / failed / cancelled）不可回退，`transition_run()` 在同一个
  SQLite 事务里更新状态并写入 `run_events`；
- `steps` 记录每个节点开始/结束时间、attempt、输入/输出消息和错误；
- 消息状态：`created / sending / sent / acknowledged / completed / failed /
  unknown / cancelled`。

```powershell
python -m agentorchestrator events --run-id <run_id>
```

### 3. 结果不确定时不猜：UNKNOWN + 恢复

- 连接建立前失败、明确的 4xx → `FAILED`（未送达，可安全重试）；
- ReadTimeout / WriteTimeout / WriteError / RemoteProtocolError / 5xx →
  `AdapterUncertain` → 消息 `UNKNOWN`（可能已执行，禁止自动重发）；
- 等待响应超时 → `UNKNOWN`；
- `dedupe_key` 唯一索引只保证本地幂等，远端恢复依赖 `recover_reply()`。

```powershell
python -m agentorchestrator pending
python -m agentorchestrator resume  --run-id <id>                    # 只核对
python -m agentorchestrator resume  --run-id <id> --resend-unstarted # 仅重发 attempt=0
python -m agentorchestrator release --run-id <id> --message-id <id> --force
```

### 4. 原子完成，消除回复丢失窗口

`Store.complete_exchange()` 在**同一个事务**内完成：

1. 插入回复消息；2. 原消息标为 completed；3. 收尾 step；4. 更新 current_turn；
5. 写入 message_completed / step_completed 事件。

任一步失败全部回滚；`resume` 还会修复历史不一致（消息 completed 但回复缺失时
将 step 标为 failed）。

数据归属校验：

- `dedupe_key` 唯一索引为 `(run_id, dedupe_key)`，查询也限定当前 run，
  避免跨 run 命中；
- `complete_exchange()` 校验消息、回复、step、run 的归属一致；
- `release()` 在引擎和数据库两层校验消息必须属于当前 run；
- 终态 run 不允许重试 `FAILED` 消息，必须新建 run。

### 5. WorkBuddy 本地助理：显式远端状态门

```
IDLE → SUBMITTED → RUNNING → IDLE
                       └────> QUARANTINED（超时/断线/取消等待）
```

- 同一 `base_url` 的所有适配器共享一个 `LocalAssistantGate`；
- 同一 `base_url` 还会使用 `lock_file` 跨进程互斥，防止两个协调器同时操作
  同一个本地助理；
- 等待超时**不释放**远端占用，而是进入 `QUARANTINED`，新消息被立即拒绝；
- `recover_reply()` 查到回复才自动解除；否则需要
  `release --force` 人工确认；
- 跨 run 保护：只要数据库里存在 `workbuddy_local` 的未确认消息，`ask()` 就会拒绝；
- 三种取消语义：`cancel_wait()`（只停本地）、`remote_abort()`（不支持，明确抛错）、
  `cancel()`（保守等于 `cancel_wait()`）。

### 5.1 独立工作区证据与只读保护

协调器在 Worker 前后独立执行 `git HEAD / status / diff` 快照，并对纳入范围的
文件计算 **内容 SHA-256**（不只是 mtime/size），把“协调器实测工作区”写入
Reviewer 提示词和 `workflow.verification`。这不依赖模型自报的证据。

当 OpenCode 执行角色为 `plan`（只读）时：

- 前后快照发生变化 → 运行失败，原因 `read_only_violation`；
- 工作区不是 git 仓库 / git 不可用 / 快照失败 → 运行失败，原因
  `verification_unavailable`（fail-closed，不把“无法验证”当作通过）。

文件树指纹默认排除 `.git`、`data`、`logs`、`.venv`、`node_modules` 等目录
（可通过 `verification.exclude_dirs` 配置，并在 UI 校验信息中展示）；
`plan` 的原生只读权限仍是第一道边界，快照是第二道独立检查，不等于完整文件系统沙箱。

### 6. 工具权限：token/能力匹配

按 operation 分类，不做子串匹配：

| 工具 | operations | 默认 |
| --- | --- | --- |
| `read_file` | read | allow |
| `spreadsheet.write` | write | ask |
| `thread.create` | write | ask |
| `execute_command` / `Bash` | execute | deny |
| `file.remove` | delete | deny |
| 无法识别 | — | ask |

复合操作取最严格（`read+delete` → deny）；`deny` 优先于 `auto_approve`。

`ask` 在 Web UI 中会挂起并等待人工确认（默认 120 秒，超时拒绝）；CLI 没有交互
通道时按拒绝处理。`auto_approve: true` 只自动选择 `allow_once`，不会自动选择
`allow_always`。配置：

```yaml
permissions:
  allow: [read]
  ask: [write, test, network]
  deny: [execute, delete, install]
```

### 7. 并发关联

- 本地助理：全局状态门，禁止两个任务同时占用；
- ACP：`send_message → wait_response` 期间持有会话锁，第二条 prompt 直接报错，
  避免 `agent_message_chunk` 关联到错误轮次；
- OpenCode v2 恢复：仅当"基线之后恰好一条 assistant 消息"时才恢复，多候选
  一律返回 `None` 保留 UNKNOWN。

## 接入 OpenCode

OpenCode 的会话绑定一个工作目录：优先使用界面/`opencode_directory`，其次
`config.yaml` 的 `agents.opencode.directory`，最后才是 OpenCode 服务端默认目录。
WorkBuddy 只做方案与审查，不接触文件；**实际检查/修改哪个仓库由该目录决定**，
建议显式填绝对路径（例如 `F:/PyProjects/conversation_AS`）。

执行角色决定权限边界：`plan` 为只读检查，`build` 可修改甚至提交代码。
自然语言里的“只读/不要修改”只是提示，不是权限控制；检查代码请使用 `plan`。

```powershell
$env:OPENCODE_SERVER_PASSWORD = "your-strong-password"
opencode serve --hostname 127.0.0.1 --port 4096

Copy-Item config.example.yaml config.yaml
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml send --agent opencode --text "介绍这个仓库"
```

已核对本机 OpenCode 1.19.0 的接口（以 `/doc` OpenAPI 为准）：

| 用途 | legacy（默认） | v2 |
| --- | --- | --- |
| 创建会话 | `POST /session` | `POST /api/session` |
| 异步发送 | `POST /session/{id}/prompt_async` | `POST /api/session/{id}/prompt` |
| 同步发送 | `POST /session/{id}/message` | — |
| 查询消息 | `GET /session/{id}/message` | `GET /api/session/{id}/message` |
| 等待空闲 | 轮询 | `POST /api/session/{id}/wait` |
| 中止 | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |

## 接入 WorkBuddy

### 个人开发者：桌面端本地 IPC（实验性，无需 OAuth）

个人账号无法申请硬件接入的 `user.localassistant.*` Scope。作为替代，
`workbuddy_ipc` 通过 WorkBuddy 桌面端自带的本地 IPC（`wbipc`）调用内置
`POST /v2/chat/completions`，复用桌面端登录态，不需要企业认证。

```yaml
agents:
  workbuddy:
    type: workbuddy_ipc
    model: auto
```

```powershell
# WorkBuddy 桌面端保持运行并已登录
python -m agentorchestrator health
python -m agentorchestrator send --agent workbuddy --text "只回复 OK"
python -m agentorchestrator run --goal "..." --planner workbuddy --worker opencode
```

注意：内部接口、无兼容性承诺；只有 chat 能力，没有桌面端工具/文件能力；
详见 [docs/WORKBUDDY_IPC.md](docs/WORKBUDDY_IPC.md)。

### 官方 OAuth 2.1（需企业认证）

```powershell
python -m agentorchestrator auth url
# 浏览器授权后，从回调地址同时复制 code 与 state
python -m agentorchestrator auth exchange <code> --state <state>
python -m agentorchestrator auth status
```

`auth url` 会把 state 写入本地（10 分钟有效）；`auth exchange` 校验通过后才会
交换 token，缺失或不匹配会直接拒绝，防止 CSRF/复制错误。

token 保存在 `data/workbuddy_token.json`（已 gitignore），过期自动刷新。

### 本地助理

```powershell
python -m agentorchestrator send --agent workbuddy --text "帮我查一下今天的日程"
```

### 云端任务 ACP（可选）

```powershell
python -m agentorchestrator send --agent workbuddy_acp --text "分析这个项目的风险"
```

`POST /openapi/v2/tasks` → 轮询 `link/token` → SSE（`Acp-Connection-Id`）→
`initialize` / `session/load` / `session/prompt`。限制：单连接、单活动 prompt、
重启后无法恢复历史轮次。

## 常用命令

> 全局参数（`--config` / `--log-level`）需放在子命令之前。

```powershell
python -m agentorchestrator agents
python -m agentorchestrator health
python -m agentorchestrator send --agent workbuddy --text "..."
python -m agentorchestrator chat
python -m agentorchestrator run --goal "审查特征工程模块" --max-rounds 2
python -m agentorchestrator transcript [--run-id <id>]
python -m agentorchestrator events --run-id <id>
python -m agentorchestrator pending [--provider workbuddy_local]
python -m agentorchestrator resume --run-id <id> [--resend-unstarted]
python -m agentorchestrator release --run-id <id> --message-id <id> [--force]
```

## 设计要点

- **适配器隔离**：工作流只依赖 `AgentAdapter`（`health_check / create_session /
  send_message / wait_response / recover_reply / cancel / cancel_wait /
  remote_abort`）。
- **三 ID 分离**：`run_id`、`provider_session_id`、`message_id` 严格区分。
- **Outbox 式记录**：先写 `created`，发送前 `sending`，服务端接受后
  `acknowledged`，完成才 `completed`；超时/断线保留 `unknown`。
- **幂等**：`dedupe_key` 唯一索引；`FAILED` 可安全重试，`UNKNOWN` 必须核对或人工释放。
- **可扩展**：新增智能体只需实现 `AgentAdapter` 并在
  `agentorchestrator/adapters/__init__.py` 注册。

## 目录

```
agentorchestrator/
├── adapters/            # base / opencode / workbuddy_local / workbuddy_acp / workbuddy_ipc / mock
├── api/                 # FastAPI + 单页 Web UI（server.py / static/index.html）
├── messaging/schema.py  # Message / Run / Step / RunEvent / 迁移表
├── storage/database.py  # SQLite（WAL、迁移兼容、事务化完成）
├── orchestrator/engine.py
├── review.py            # 结构化审查协议
├── auth.py              # WorkBuddy OAuth 2.1
├── sse.py
├── config.py
└── cli.py
docs/USER_GUIDE.md
docs/WORKBUDDY_IPC.md
tests/                   # 95 项：单元 + fake server + 故障注入 + API/UI
```

## 继续上次任务（工作流检查点）

每次运行在 `run.metadata.workflow` 保存检查点：`stage`（plan/work/review）、
`round`、`plan`、`work`、`issues`、审查结果。

- 失败在 Worker：`stage=work`，已完成的 Plan 不重跑；
- 失败在 Reviewer：`stage=review`，Worker 结果不重跑，只重跑审查；
- 界面点击 **「继续此任务」** 会创建一个新 run（`parent_run_id`），
  从检查点继续，原 run 保持原状态（不会原地重开终态 run）；
- 若 Worker 结果未知（`stage=work` 且无 `work`），继续前需要确认
  `retry_unknown`，因为节点可能已经执行；
- 旧 run 没有检查点时，会从消息中推导 checkpoint。

API：`POST /api/runs/{run_id}/continue`。`resume` 仍只负责消息级恢复。

## 测试

```powershell
python -m pytest
```

测试不需要真实凭据，覆盖：结构化审查拒批（缺 evidence/低置信度/高危 finding）、
权限误分类（`spreadsheet.write`、`thread.create`）、超时后隔离与恢复、
UNKNOWN 不重发、完成事务回滚、终态不可逆、跨 run dedupe/release 隔离、
OAuth state 校验、UI Basic Auth、ACP 并发 prompt、OpenCode v2 多候选恢复等。

## 已知边界（当前版本）

- Planner/Reviewer（WorkBuddy）没有文件系统访问能力，可能误判仓库现状；
  Planner 也可能把“读取 README 第一行”误解成“重写 README”。
- Reviewer 现在会拿到协调器独立采集的 git HEAD/status/diff 快照，但尚未替用户
  执行测试命令或 CI；它仍是“结构化 AI 审查 + 工作区证据”，不是完整质量门禁。
- 只读任务依赖 OpenCode `plan` + git/文件树快照；非 git 工作区会被判定
  `verification_unavailable`，不会假装通过。忽略目录（如 `data`、`node_modules`）
  不在文件树指纹范围内。
- 继续任务会创建新 run，不会原地重开已失败的 terminal run；若要“同 run 续跑”
  需要进一步放宽状态机，当前不做。
- 生成类任务可能已经写出文件，但仍在跑验证命令；等待超时会标记失败。
  此时先检查工作目录里的产物，再决定是否强制释放/重试；超时后协调器会尝试中止
  OpenCode 会话，避免长驻命令继续占用。

## 后续（阶段三）

1. 工作流 YAML 化（检查点续跑已支持，当前仍是固定 分析→执行→审查 流程）。
2. 跨进程互斥（文件锁）与持久化任务队列。
3. 角色与品牌解耦、第三/第四个智能体、并行执行（代码写入保持单写者）。

## License

MIT，见 [LICENSE](LICENSE)。

真实接口兼容性记录见 [docs/INTEGRATION_LOG.md](docs/INTEGRATION_LOG.md)。
