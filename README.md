# WorkBuddy + OpenCode 双智能体编排器（阶段一）

一个本地 Python 协调器：**不**让两个智能体直接互发消息，而是由协调器统一管理
会话映射、消息格式、发送/等待、持久化与流程控制。当前实现阶段一（双向通信）
与最小可用的「分析 → 执行 → 审查」闭环，后续可替换为配置化工作流。

```
用户目标
   │
   ▼
Orchestrator（本仓库）
   ├── WorkBuddyAdapter  ──► 本地助理 REST（OAuth 2.1）
   ├── WorkBuddyACPadapter ─► 云端任务 ACP（SSE + JSON-RPC 2.0）
   ├── OpenCodeAdapter   ──► opencode serve（REST，legacy 或 v2）
   └── SQLite（runs / sessions / messages）
```

## 快速开始（离线，不需要任何凭据）

```powershell
python -m pip install -e .
python -m agentorchestrator demo
python -m agentorchestrator transcript --run-id <上一步输出的 run_id>
```

`demo` 使用 MockAdapter 跑完整的 分析 → 执行 → 审查 流程，消息写入
`data/demo.db`，用来验证消息协议与持久化。

## 接入 OpenCode

```powershell
# 本机已安装 opencode 1.19.0；服务默认要求 Basic Auth
$env:OPENCODE_SERVER_PASSWORD = "your-strong-password"
opencode serve --hostname 127.0.0.1 --port 4096
```

```powershell
Copy-Item config.example.yaml config.yaml
python -m agentorchestrator health
python -m agentorchestrator send --agent opencode --text "用一句话介绍这个仓库"
```

已核对本机 OpenCode 1.19.0 的实际接口（以 `/doc` OpenAPI 为准）：

| 用途 | legacy（默认） | v2 |
| --- | --- | --- |
| 创建会话 | `POST /session` | `POST /api/session` |
| 异步发送 | `POST /session/{id}/prompt_async` | `POST /api/session/{id}/prompt` |
| 同步发送 | `POST /session/{id}/message` | — |
| 查询消息 | `GET /session/{id}/message` → `[{info, parts}]` | `GET /api/session/{id}/message` → `{data: [...]}` |
| 等待空闲 | —（轮询） | `POST /api/session/{id}/wait` |
| 中止 | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |

鉴权：`OPENCODE_SERVER_PASSWORD` + 用户名 `opencode`（Basic Auth）。

## 接入 WorkBuddy

### 1. OAuth 2.1 授权

在 WorkBuddy 开放平台创建应用，拿到 `client_id` / `client_secret` / 回调地址，
配置到 `config.yaml`，然后：

```powershell
python -m agentorchestrator auth url
# 浏览器打开输出的链接，授权后从回调地址拿到 code
python -m agentorchestrator auth exchange <code>
python -m agentorchestrator auth status
```

token 保存在 `data/workbuddy_token.json`（已加入 .gitignore），过期自动用
`refresh_token` 刷新。

### 2. 本地助理（默认）

```powershell
python -m agentorchestrator send --agent workbuddy --text "帮我查一下今天的日程"
```

使用接口：`GET /openapi/v2/localassistant`、`POST /openapi/v2/localassistant/message`、
`GET /openapi/v2/localassistant/message?message_id=...`。

限制：本地助理没有独立 `session_id`，同一时间只允许一个进行中的对话；
该 API 也没有取消接口，`cancel` 只能停止本地等待。

### 3. 云端任务 ACP（可选）

在配置里启用 `workbuddy_acp`，然后：

```powershell
python -m agentorchestrator send --agent workbuddy_acp --text "分析这个项目的风险"
```

流程：`POST /openapi/v2/tasks` 创建任务 → `GET /api/v2/tasks/{id}` 轮询
`link`/`token` → `GET {link}` 建立 SSE（响应头 `Acp-Connection-Id`）→
`initialize` / `session/load` / `session/prompt`。流式文本来自
`session/update` 的 `agent_message_chunk`；`session/prompt` 的 JSON-RPC
response 是「本轮结束」的权威信号。

注意：
- 一个适配器实例只持有一个 ACP SSE 连接（多会话请创建多个适配器）。
- 创建任务时必须带初始 prompt，因此阶段一用一条很短的 `bootstrap_prompt` 初始化，
  真实内容仍通过 `session/prompt` 发送。
- 工具授权默认拒绝；设置 `auto_approve: true` 或注入 `permission_handler`
  才会自动同意。

## 常用命令

> 全局参数（`--config` / `--log-level`）需放在子命令之前，例如
> `python -m agentorchestrator --config config.yaml health`。

```powershell
python -m agentorchestrator agents          # 列出配置的 agent
python -m agentorchestrator health          # 逐个健康检查
python -m agentorchestrator send --agent workbuddy --text "..."
python -m agentorchestrator chat            # 交互式；/agent 切换，/history 查看记录
python -m agentorchestrator run --goal "审查特征工程模块" --max-rounds 2
python -m agentorchestrator transcript      # 最近一次 run 的完整消息
```

`run` 默认使用 `workbuddy` 做分析与审查、`opencode` 做执行；审查文本包含
`APPROVED` 即提前结束，否则把问题回传给执行者，直到 `max_rounds`。

## 设计要点

- **统一消息协议**：`Message` 固定包含 `run_id / turn_id / message_id /
  from_agent / to_agent / provider_session_id / provider_message_id /
  content / status / reply_to / dedupe_key / created_at / metadata`。
  `run_id`、`session_id`、`message_id` 三者严格区分。
- **适配器隔离**：工作流只依赖 `AgentAdapter`（`health_check /
  create_session / send_message / wait_response / cancel / chat`），
  不感知 OAuth、SSE 或具体 HTTP 路径。
- **幂等**：`ask(..., dedupe_key=...)` 在 SQLite 上有唯一索引；重复调用直接返回
  已保存的响应，避免网络重试导致重复提交。
- **持久化**：SQLite + WAL，记录运行状态、会话映射和每条消息；SSE 只作为传输层，
  不作为唯一数据源。
- **可扩展**：新增智能体只需实现 `AgentAdapter` 并在
  `agentorchestrator/adapters/__init__.py` 注册。

## 目录

```
agentorchestrator/
├── adapters/            # base / opencode / workbuddy_local / workbuddy_acp / mock
├── messaging/schema.py  # 统一消息、run、会话映射
├── storage/database.py  # SQLite（WAL）
├── orchestrator/engine.py
├── auth.py              # WorkBuddy OAuth 2.1
├── sse.py               # SSE 解析
├── config.py
└── cli.py
tests/                   # 单元测试 + 本地 fake server 双向通信测试
```

## 测试

```powershell
python -m pytest
```

测试不需要真实凭据：OpenCode / WorkBuddy 本地助理使用 `httpx.MockTransport`，
ACP 使用自定义 SSE 传输，端到端测试在 `127.0.0.1` 上启动两个模拟 HTTP 服务，
验证 WorkBuddy ↔ OpenCode 的完整双向消息流。

## 后续（阶段二/三）

1. 工作流配置化（YAML：步骤、条件、重试、超时、最大轮数）。
2. 失败重试与断点恢复（基于 SQLite 中的 pending/sent 状态）。
3. FastAPI 管理接口 + Web 界面（依赖已列为 `web` extra）。
4. 第三个智能体接入与并行执行（代码写入必须单写者或独立 worktree）。
