# 真实接口兼容性记录

本文件记录在真实本机环境做过的接口验证，而不是仅凭文档或 mock 推断。
每次升级 OpenCode / WorkBuddy 后建议按本清单重跑并更新日期。

## 环境

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-23 / 2026-11-27 |
| OS | Windows 11 |
| Python | 3.12.7 |
| OpenCode | 1.19.0 |
| WorkBuddy Desktop | 2.165.0 |
| 模型 | `opencode-go/deepseek-v4.1-flash` |
| 协调器 | conversation_AS 0.6.x ~ 0.10.2 |

## OpenCode

已验证：

- `GET /path`、`GET /config/providers`、`GET /api/provider`；
- legacy：`POST /session`、`POST /session/{id}/prompt_async`、
  `GET /session/{id}/message`、`POST /session/{id}/abort`；
- v2：`POST /api/session`、`POST /api/session/{id}/prompt`、
  `POST /api/session/{id}/wait`、`GET /api/session/{id}/message`；
- Basic Auth：用户名 `opencode`、密码来自 `OPENCODE_SERVER_PASSWORD`；
- 真实检查任务：OpenCode `plan` 在工作目录内读取 9 个 `.py` 文件、执行
  pyflakes/AST，审查通过；
- 真实生成任务：OpenCode `build` 生成 `index.html`（Three.js，26 KB），
  `node --check` 通过；
- 超时场景：Worker 在写出文件后卡在验证命令，协调器 300s 超时并中止会话；
  随后将默认超时提高到 600s，并在超时后自动 abort。

未验证/不可用：

- 不同 OpenCode 版本间的兼容性；
- 未经协调器直接运行 `opencode serve` 的密码轮换场景。

## WorkBuddy

### 桌面端本地 IPC（当前默认）

已验证：

- 发现文件 `~/.workbuddy/wbipc/endpoint.json`；
- `workbuddy_ipc.connect(client_kind="skill")`；
- pipe `wb.request` → `http.fetch` → `POST /v2/chat/completions`（SSE）；
- 真实 Planner/Reviewer 调用，返回结构化审查 JSON；
- `WorkBuddy × OpenCode` 完整流程：分析 → 执行 → 审查并 approved；
- 超时/隔离与恢复逻辑（fake server + 真实 IPC 健康检查）。

限制：

- 内部接口，无版本兼容承诺；
- 只有 chat 能力，没有桌面端工具/文件权限；
- 无历史轮次关联，重启后不能恢复 IPC 轮次；
- 不纳入与 OpenCode 同等级的故障恢复保证。

## 权限交互与取消（0.9.0）

- `ask` 权限通过 Web UI 的 `/api/permissions` 挂起确认，120 秒超时拒绝；
- `auto_approve` 只选择 `allow_once`；
- 取消运行会尝试 OpenCode abort / ACP session/cancel，并把 run 标记 `cancelled`；
- 只读验证增加文件内容 SHA-256、符号链接目标、扫描限制与统计；非 git 工作区
  或扫描超限返回 `verification_unavailable` 并停止只读运行。
- ACP 不确定提交进入 QUARANTINED，保持会话锁，禁止后续 prompt 覆盖流式消息归属。
- OpenCode 等待增加无进展超时，错误中显示最近工具/命令；执行提示词禁止截图、
  浏览器/GUI 等易挂起操作。

### 官方 OAuth 本地助理 / 云端 ACP

未做真实接口验证：个人开发者在开放平台无法申请硬件接入 Scope
（`user.localassistant.*` / `user.task.*`），需要企业认证。
对应适配器由 mock/fake server 测试覆盖，保留给有企业资质的环境。

## 手工回归清单

升级后至少执行：

```powershell
python -m pytest
python -m agentorchestrator --config config.yaml health
python -m agentorchestrator --config config.yaml send --agent opencode --text "只回复 OK"
python -m agentorchestrator --config config.yaml send --agent workbuddy --text "只回复 OK"
```

再做一次只读检查任务（`plan` + 指定目录），确认：

1. OpenCode 会话 `directory` 为目标项目；
2. 执行结果包含真实文件/命令证据；
3. 工作区 git 状态未变化；
4. 审查返回结构化 JSON 并通过。

再做一次生成任务（`build`），确认超时、产物和继续任务按钮行为。
