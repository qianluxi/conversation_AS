# WorkBuddy 本地 IPC（实验性接入）

> 适用：个人开发者，无法完成企业认证，拿不到开放平台
> `user.localassistant.*` / `user.task.*` Scope。
>
> 状态：**实验性**。这不是 WorkBuddy 官方开放 API，属于桌面端内部接口，
> WorkBuddy 升级后可能失效，请勿用于生产关键流程。

## 原理

WorkBuddy 桌面端会写一个本地发现文件：

```
~/.workbuddy/wbipc/endpoint.json
{
  "endpoint": "\\\\.\\pipe\\wbipc-<id>",
  "ticket": "<随机票据>"
}
```

WorkBuddy 自带一个 Python IPC 客户端（随内置 skills 安装）：

```
<WorkBuddy安装目录>/resources/app.asar.unpacked/resources/plugins/
  workbuddy-builtin/skills/library/workbuddy_ipc/
```

客户端通过命名管道握手（HMAC + ticket），拿到三个 pipe：

| pipe | 方法 | 用途 |
| --- | --- | --- |
| `wb.request` | `http.fetch` | 以已登录用户身份代理 HTTP 请求 |
| `wb.entitlement` | `subscription.check` | 订阅/权益 |
| `wb.wecom-a2a` | `authorize` | 企业微信 A2A |

本项目只用 `wb.request` → `POST /v2/chat/completions`（SSE），即 WorkBuddy
桌面端聊天使用的模型接口。请求体是 OpenAI 风格：

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "只回复 OK"}],
  "stream": true
}
```

响应是 `text/event-stream`，内容在 `choices[0].delta.content`，
推理在 `delta.reasoning_content`，结束标记 `data: [DONE]`。

## 配置

```yaml
agents:
  workbuddy:
    type: workbuddy_ipc
    client_id: conversation-as   # 任意稳定的客户端名
    client_kind: skill
    model: auto
    chat_path: /v2/chat/completions
    timeout: 300
    history_limit: 20
    # 自动发现失败时手动指定：
    # library_path: D:/Program Files/WorkBuddy/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library
```

前置条件：

1. Windows；
2. WorkBuddy 桌面端正在运行，且已登录；
3. 安装目录包含 `workbuddy_ipc`（2.165.0 已验证）；
4. 首次连接可能需要在 WorkBuddy 内确认/允许 IPC 客户端。

验证：

```powershell
python -m agentorchestrator --config config.yaml health
# [OK] workbuddy  WorkBuddy IPC 可用：wb.entitlement, wb.request, wb.wecom-a2a
```

## 与官方适配器的差异

| 能力 | `workbuddy_ipc` | `workbuddy_local`（官方 OAuth） |
| --- | --- | --- |
| 鉴权 | 复用桌面端登录态（本地 IPC） | OAuth 2.1 授权码 |
| 企业认证 | 不需要 | 需要（硬件接入应用） |
| 能力 | 纯 chat 模型 | 完整本地助理（工具、文件、日程等） |
| 流式 | 有（一次返回完整 SSE） | 轮询消息历史 |
| 恢复 | 无状态，重启后不可关联 | 可按 message_id 增量查询 |
| 稳定性 | 内部接口，可能随版本失效 | 官方接口 |

因此它适合做 **planner / reviewer**（纯文本分析与结构化审查），真正的代码执行
仍交给 OpenCode。

## 已知限制

- 不是官方 API，无兼容性承诺；
- 无法调用桌面端的工具/文件能力，只是聊天模型；
- 不能取消已发出的同步请求；
- 连接断开后需要重新 `health` 或重试；
- 不建议高频/批量调用，可能触发账号风控；
- 不要把它当作 WorkBuddy 官方 API 的替代品对外分发。

## 故障排查

| 提示 | 处理 |
| --- | --- |
| `WorkBuddy 桌面端未运行` | 启动并登录 WorkBuddy，确认 `~/.workbuddy/wbipc/endpoint.json` 存在 |
| `要求授权本地 IPC 客户端` | 在 WorkBuddy 桌面端确认授权后重试 |
| `无法加载 WorkBuddy IPC 客户端` | 检查 `library_path`；确认安装目录下存在 `workbuddy_ipc/__init__.py` |
| `wb.request 不提供 http.fetch` | WorkBuddy 版本过旧/接口变化，暂不兼容 |
| `chat 接口鉴权失败` | 桌面端登录态失效，重新登录 WorkBuddy |
| `chat 返回空回复` | 模型只返回了推理内容或触发风控，重试或换 `model` |
