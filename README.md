# WorkBuddy + OpenCode 双智能体编排器

一个本地 Python 协调器：**不**让两个智能体直接互发消息，而是由协调器统一管理
会话映射、消息协议、状态机、结构化审查、持久化与恢复。当前实现第二阶段的核心
可靠性能力，仍然不包含 GUI 与配置化工作流。

```
用户目标
   │
   ▼
Orchestrator（本仓库）
   ├── WorkBuddyAdapter   ──► 本地助理 REST（OAuth 2.1，全局互斥）
   ├── WorkBuddyACPAdapter ─► 云端任务 ACP（SSE + JSON-RPC 2.0）
   ├── OpenCodeAdapter    ──► opencode serve（REST，legacy 或 v2）
   └── SQLite（runs / sessions / messages / steps / run_events）
```

## 快速开始（离线，不需要任何凭据）

```powershell
python -m pip install -e .
python -m agentorchestrator demo
python -m agentorchestrator transcript --run-id <run_id>
python -m agentorchestrator events --run-id <run_id>
```

`demo` 使用 MockAdapter 跑完整的 分析 → 执行 → 结构化审查 流程，消息、步骤和
状态机事件写入 `data/demo.db`。

## 可靠性设计（v0.2）

### 1. 结构化审查，不再匹配 `APPROVED` 子串

审查者必须返回 JSON，由 `agentorchestrator/review.py` 用 Pydantic 校验：

```json
{
  "decision": "approved | revision_required | blocked",
  "summary": "一句话结论",
  "findings": [
    {"severity": "high", "file": "a.py", "location": "L42",
     "problem": "存在数据泄漏", "suggested_action": "调整时间窗口"}
  ],
  "evidence": ["pytest -q 输出"],
  "confidence": 0.9
}
```

- `approved` 只在 `decision == "approved"` 时成立；
- 「我不认为已经达到 APPROVED 的标准」解析失败，**不会**被自动批准；
- JSON 缺失、字段非法、decision 未知 → 运行标记为 `FAILED`，原因 `invalid_review`；
- `blocked` 直接终止；`revision_required` 把结构化 findings 回传给执行者。

### 2. 持久化状态机与节点记录

- run：`status`（running/completed/failed/cancelled）+ `phase`
  （created → analyzing → executing → reviewing → revision_required / approved →
  completed / failed）。
- 每次状态变化写入 `run_events`（seq、事件类型、phase、step、payload）。
- 每个 agent 每轮执行记录为 `steps`（开始/结束时间、attempt、输入/输出消息、
  错误），用于定位卡在哪一步。
- 消息状态：`created / sending / sent / acknowledged / completed / failed /
  unknown / cancelled`。

查看：

```powershell
python -m agentorchestrator events --run-id <run_id>
```

### 3. 不盲目重发：UNKNOWN 与恢复

- 发送阶段确定失败（未送达）→ `FAILED`；
- 发送或等待阶段超时/断线 → `UNKNOWN`（外部可能已经执行）；
- `dedupe_key` 唯一索引只防本地重复写入，因此：

```powershell
# 只做核对，默认绝不重发
python -m agentorchestrator resume --run-id <run_id>

# 仅重发从未被服务端确认过（attempt=0、无 provider_message_id）的消息
python -m agentorchestrator resume --run-id <run_id> --resend-unstarted
```

`resume` 会通过适配器的 `recover_reply()` 查询外部会话：
OpenCode 按 `parentID`/基线消息 ID 核对，WorkBuddy 本地助理按增量消息历史核对。
ACP 目前无法可靠关联历史轮次，会保持 UNKNOWN，需人工决定。

注意：`resume` 只核对/补齐消息，不会自动继续整个工作流；阶段二会补上工作流级
断点续跑。

### 4. WorkBuddy 本地助理全局互斥与取消语义

本地助理没有独立 session，且同一账号只能有一个进行中的对话：

- 同一 `base_url` 的所有适配器实例共享同一把 `asyncio.Lock`（`build_adapters`
  注入），第二个任务会立即报错，而不是串话；
- 区分三种取消：
  - `cancel_wait()`：只停止本地等待，服务端可能继续执行；
  - `remote_abort()`：本地助理 API 不支持，明确抛错；
  - `cancel()`：保守地等同 `cancel_wait()`。
- 跨进程并发不在本版本保护范围内（同一时刻只运行一个协调器进程）。

### 5. 工具权限策略

按 agent 配置关键词策略，`deny` 优先于 `auto_approve`：

```yaml
permissions:
  allow: [read, list, search, grep, analyze]
  ask: [write, edit, test, network, http, fetch]
  deny: [install, delete, remove, shell, bash, terminal, exec, sudo]
```

ACP `session/request_permission` 会先分类工具名/标题，再决定同意、询问还是拒绝；
未识别工具默认 `ask`，未配置 handler 且未开启 `auto_approve` 时拒绝。
OpenCode 侧暂无同等拦截，需依赖其自身权限配置。

## 接入 OpenCode

```powershell
$env:OPENCODE_SERVER_PASSWORD = "your-strong-password"
opencode serve --hostname 127.0.0.1 --port 4096

Copy-Item config.example.yaml config.yaml
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml send --agent opencode --text "介绍这个仓库"
```

已核对本机 OpenCode 1.19.0 的实际接口（以 `/doc` OpenAPI 为准）：

| 用途 | legacy（默认） | v2 |
| --- | --- | --- |
| 创建会话 | `POST /session` | `POST /api/session` |
| 异步发送 | `POST /session/{id}/prompt_async` | `POST /api/session/{id}/prompt` |
| 同步发送 | `POST /session/{id}/message` | — |
| 查询消息 | `GET /session/{id}/message` → `[{info, parts}]` | `GET /api/session/{id}/message` → `{data}` |
| 等待空闲 | 轮询 | `POST /api/session/{id}/wait` |
| 中止 | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |

鉴权：`OPENCODE_SERVER_PASSWORD` + 用户名 `opencode`（Basic Auth）。

## 接入 WorkBuddy

### 1. OAuth 2.1

```powershell
python -m agentorchestrator auth url
# 授权后拿到 code
python -m agentorchestrator auth exchange <code>
python -m agentorchestrator auth status
```

token 保存在 `data/workbuddy_token.json`（已 gitignore），过期自动刷新。

### 2. 本地助理（默认）

```powershell
python -m agentorchestrator send --agent workbuddy --text "帮我查一下今天的日程"
```

接口：`GET /openapi/v2/localassistant`、`POST /openapi/v2/localassistant/message`、
`GET /openapi/v2/localassistant/message?message_id=...`。

### 3. 云端任务 ACP（可选）

启用 `config.yaml` 中的 `workbuddy_acp` 后：

```powershell
python -m agentorchestrator send --agent workbuddy_acp --text "分析这个项目的风险"
```

流程：`POST /openapi/v2/tasks` → 轮询 `GET /api/v2/tasks/{id}` 拿 `link`/`token` →
`GET {link}` 建立 SSE（`Acp-Connection-Id`）→ `initialize` / `session/load` /
`session/prompt`。流式文本来自 `session/update` 的 `agent_message_chunk`。

限制：一个适配器实例只持有一个 ACP SSE 连接；建任务时必须带初始 prompt，因此用
`bootstrap_prompt` 初始化；ACP 历史轮次无法在重启后可靠恢复。

## 常用命令

> 全局参数（`--config` / `--log-level`）需放在子命令之前。

```powershell
python -m agentorchestrator agents
python -m agentorchestrator health
python -m agentorchestrator send --agent workbuddy --text "..."
python -m agentorchestrator chat
python -m agentorchestrator run --goal "审查特征工程模块" --max-rounds 2
python -m agentorchestrator transcript --run-id <run_id>
python -m agentorchestrator events --run-id <run_id>
python -m agentorchestrator resume --run-id <run_id>
```

`run` 默认 `workbuddy` 做分析与审查、`opencode` 做执行；只有结构化审查返回
`approved` 才提前结束，否则把 findings 回传执行者，直到 `max_rounds`（超限记为失败）。

## 设计要点

- **适配器隔离**：工作流只依赖 `AgentAdapter`（`health_check / create_session /
  send_message / wait_response / recover_reply / cancel / cancel_wait /
  remote_abort`）。
- **三 ID 分离**：`run_id`、`provider_session_id`、`message_id` 严格区分。
- **Outbox 式记录**：先写 `created`，发送前 `sending`，服务端接受后
  `acknowledged`，完成才 `completed`；超时/断线保留 `unknown`。
- **幂等**：`dedupe_key` 唯一索引；`FAILED` 消息可安全重试，`UNKNOWN` 不允许自动
  重发。SQLite 只能保证本地幂等，远端恢复依赖 `recover_reply`。
- **可扩展**：新增智能体只需实现 `AgentAdapter` 并在
  `agentorchestrator/adapters/__init__.py` 注册。

## 目录

```
agentorchestrator/
├── adapters/            # base / opencode / workbuddy_local / workbuddy_acp / mock
├── messaging/schema.py  # Message / Run / Step / RunEvent / 枚举
├── storage/database.py  # SQLite（WAL，迁移兼容旧库）
├── orchestrator/engine.py
├── review.py            # 结构化审查协议
├── auth.py              # WorkBuddy OAuth 2.1
├── sse.py
├── config.py
└── cli.py
tests/                   # 单元测试 + fake server 双向通信 + 恢复/权限测试
```

## 测试

```powershell
python -m pytest
```

测试不需要真实凭据：OpenCode / WorkBuddy 本地助理使用 `httpx.MockTransport`，
ACP 使用自定义 SSE 传输，端到端测试在 `127.0.0.1` 启动两个模拟服务，覆盖
结构化审查拒批、UNKNOWN 恢复、未确认消息不重发、本地助理互斥和权限策略。

## 后续（阶段二/三）

1. 工作流 YAML 化与工作流级断点续跑（当前只恢复消息，不自动继续流程）。
2. 重启时主动查询 OpenCode/ACP 会话状态并自动补齐节点。
3. 跨进程互斥（文件锁）与本地任务队列。
4. FastAPI 管理接口 + Web 界面（`web` extra）。
5. 第三个智能体、角色与品牌解耦、并行执行（代码写入保持单写者）。
