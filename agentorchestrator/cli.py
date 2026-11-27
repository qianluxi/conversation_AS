from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import webbrowser

from .adapters import AgentAdapter, build_adapters
from .adapters.base import AdapterError, HealthStatus
from .adapters.mock import MockAdapter
from .auth import WorkBuddyAuth
from .config import AppConfig, MockSettings, load_config
from .orchestrator.engine import Conversation, format_transcript, run_collaboration
from .storage.database import Store


def _load(args: argparse.Namespace) -> tuple[AppConfig, dict[str, AgentAdapter], Store]:
    config = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, (args.log_level or config.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(config.storage.path)
    adapters = build_adapters(config)
    return config, adapters, store


def _print_agents(config: AppConfig) -> None:
    for name, settings in config.agents.items():
        print(f"{name:<16} {settings.type}")
    if not config.agents:
        print("（配置中没有任何 agent）")


async def _cmd_health(_config: AppConfig, adapters: dict[str, AgentAdapter]) -> int:
    failures = 0
    try:
        for name, adapter in adapters.items():
            try:
                health: HealthStatus = await asyncio.wait_for(adapter.health_check(), timeout=30)
                mark = "OK  " if health.ok else "DOWN"
                if not health.ok:
                    failures += 1
                print(f"[{mark}] {name:<16} {health.detail}")
            except Exception as exc:  # noqa: BLE001 - 单个 agent 失败不影响其他
                failures += 1
                print(f"[FAIL] {name:<16} {type(exc).__name__}: {exc}")
    finally:
        for adapter in adapters.values():
            await adapter.aclose()
    return 1 if failures else 0


async def _cmd_send(_config: AppConfig, adapters: dict[str, AgentAdapter],
                    store: Store, args: argparse.Namespace) -> int:
    conversation = Conversation(store, adapters, goal=args.text)
    try:
        reply = await conversation.ask(args.agent, args.text, to_agent=args.to,
                                       timeout=args.timeout)
        print(f"\n{args.agent}: {reply.text}\n")
        print(f"run_id = {conversation.run_id}")
        return 0
    finally:
        await conversation.close()


async def _cmd_chat(_config: AppConfig, adapters: dict[str, AgentAdapter],
                    store: Store, args: argparse.Namespace) -> int:
    if not adapters:
        print("配置中没有 agent。", file=sys.stderr)
        return 2
    agent = args.agent or next(iter(adapters))
    if agent not in adapters:
        print(f"未知 agent: {agent}", file=sys.stderr)
        return 2
    conversation = Conversation(store, adapters, goal=args.goal)
    print(f"run_id = {conversation.run_id}")
    print("命令：/agents  /agent <name>  /history  /quit")
    print(f"当前 agent: {agent}")
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, f"\n[{agent}] > ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/agents":
                print("  " + "  ".join(adapters))
                continue
            if line.startswith("/agent "):
                candidate = line.split(maxsplit=1)[1].strip()
                if candidate in adapters:
                    agent = candidate
                    print(f"已切换到 {agent}")
                else:
                    print(f"未知 agent: {candidate}")
                continue
            if line == "/history":
                print(format_transcript(conversation.transcript()))
                continue
            reply = await conversation.ask(agent, line, timeout=args.timeout)
            print(f"\n[{agent}] {reply.text}")
    finally:
        await conversation.close()
    print(f"\nrun_id = {conversation.run_id}")
    return 0


async def _cmd_run(_config: AppConfig, adapters: dict[str, AgentAdapter],
                   store: Store, args: argparse.Namespace) -> int:
    missing = [name for name in (args.planner, args.worker) if name not in adapters]
    if missing:
        print(f"未知 agent: {', '.join(missing)}", file=sys.stderr)
        return 2
    conversation = Conversation(store, adapters, goal=args.goal)
    workspace = None
    read_only = False
    for settings in _config.agents.values():
        if getattr(settings, "type", "") == "opencode":
            if getattr(settings, "directory", None):
                workspace = settings.directory
            if getattr(settings, "agent", None) == "plan":
                read_only = True
            break
    try:
        result = await run_collaboration(
            conversation, planner=args.planner, worker=args.worker,
            reviewer=args.reviewer, max_rounds=args.max_rounds,
            goal=args.goal, timeout=args.timeout, workspace=workspace,
            read_only=read_only, verification_excludes=_config.verification.exclude_dirs,
        )
        print(f"\nrun_id = {result['run_id']}")
        print(f"approved = {result['approved']}  rounds = {result['rounds']}")
        if result.get("error"):
            print(f"error = {result['error']}")
        if result.get("review_result"):
            print("review_result =")
            print(json.dumps(result["review_result"], ensure_ascii=False, indent=2))
        print("\n--- transcript ---")
        print(format_transcript(conversation.transcript()))
        return 0 if result["approved"] else 1
    finally:
        await conversation.close()


async def _cmd_resume(_config: AppConfig, adapters: dict[str, AgentAdapter],
                      store: Store, args: argparse.Namespace) -> int:
    conversation = Conversation(store, adapters, run_id=args.run_id)
    try:
        print(f"run_id = {conversation.run_id}  status = {conversation.run.status.value}"
              f"  phase = {conversation.run.phase.value}")
        results = await conversation.resume(
            timeout=args.timeout, allow_resend_unstarted=args.resend_unstarted,
        )
        if not results:
            print("没有未完成的消息。")
            return 0
        for item in results:
            detail = item.get("text") or item.get("reason") or ""
            print(f"- {item['message_id']}: {item['action']}  {detail[:200]}")
        unresolved = [item for item in results if item["action"] in ("unresolved", "error")]
        return 1 if unresolved else 0
    finally:
        await conversation.close()


async def _cmd_release(_config: AppConfig, adapters: dict[str, AgentAdapter],
                       store: Store, args: argparse.Namespace) -> int:
    conversation = Conversation(store, adapters, run_id=args.run_id)
    try:
        message = await conversation.release(args.message_id, force=args.force,
                                             timeout=args.timeout)
        print(f"{message.message_id} -> {message.status.value}")
        print("已解除占用。注意：--force 释放不会撤销远端可能仍在执行的代码修改。")
        return 0
    finally:
        await conversation.close()


async def _cmd_pending(_config: AppConfig, store: Store, args: argparse.Namespace) -> int:
    items = store.list_unfinished(provider=args.provider)
    if not items:
        print("没有未完成消息。")
        return 0
    for message in items:
        print(f"{message.run_id}  {message.message_id}  {message.to_agent:<14} "
              f"{message.status.value:<13} turn={message.turn_id}  "
              f"{message.created_at.isoformat()}")
    return 0


def _cmd_ui(config: AppConfig, store: Store, args: argparse.Namespace) -> int:
    try:
        import uvicorn
        from .api.server import create_app, is_loopback_host
    except ImportError:
        print(
            "缺少 Web 依赖。请运行：python -m pip install -e \".[web]\" 或 "
            "python -m pip install fastapi uvicorn",
            file=sys.stderr,
        )
        return 2
    token = args.token or os.environ.get("AGENTORCHESTRATOR_UI_TOKEN") or None
    if not is_loopback_host(args.host) and not token:
        print(
            "拒绝启动：监听非回环地址时必须启用认证。\n"
            "请设置 --token <随机密码> 或环境变量 AGENTORCHESTRATOR_UI_TOKEN。\n"
            "例如：python -m agentorchestrator ui --host 0.0.0.0 --token <密码>",
            file=sys.stderr,
        )
        return 2
    app = create_app(config, store, auth_token=token)
    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{browser_host}:{args.port}/"
    print(f"图形界面：{url}")
    if token:
        print("已启用认证：浏览器会弹出登录框；用户名任意，密码为 --token。")
    print("按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


async def _cmd_events(_config: AppConfig, store: Store, args: argparse.Namespace) -> int:
    run = store.get_run(args.run_id)
    if run is None:
        print(f"未知 run_id: {args.run_id}", file=sys.stderr)
        return 2
    print(f"run_id = {run.run_id}")
    print(f"goal   = {run.goal}")
    print(f"status = {run.status.value}  phase = {run.phase.value}  turn = {run.current_turn}")
    print("\n--- steps ---")
    for step in store.list_steps(run.run_id):
        started = step.started_at.strftime("%H:%M:%S") if step.started_at else "-"
        finished = step.finished_at.strftime("%H:%M:%S") if step.finished_at else "-"
        error = f" error={step.error[:80]}" if step.error else ""
        print(f"{step.step_id} turn={step.turn_id} {step.agent}/{step.role} "
              f"{step.state.value} {started}..{finished}{error}")
    print("\n--- events ---")
    for event in store.list_events(run.run_id):
        stamp = event.created_at.strftime("%H:%M:%S")
        payload = json.dumps(event.payload, ensure_ascii=False)
        print(f"{event.seq:>3} {stamp} {event.phase.value:<18} {event.event_type:<22} {payload[:160]}")
    return 0


async def _cmd_demo(args: argparse.Namespace) -> int:
    store = Store(args.db)
    adapters: dict[str, AgentAdapter] = {
        "workbuddy": MockAdapter("workbuddy", MockSettings(role="planner")),
        "opencode": MockAdapter("opencode", MockSettings(role="worker")),
    }
    conversation = Conversation(store, adapters, goal=args.goal)
    print(f"demo run_id = {conversation.run_id}（使用 MockAdapter，不需要任何凭据）")
    try:
        result = await run_collaboration(
            conversation, planner="workbuddy", worker="opencode",
            max_rounds=args.max_rounds, goal=args.goal,
        )
        print(f"approved = {result['approved']}  rounds = {result['rounds']}")
        print("\n--- transcript ---")
        print(format_transcript(conversation.transcript()))
        print(f"\nSQLite: {store.path.resolve()}")
        return 0
    finally:
        await conversation.close()
        store.close()


async def _cmd_auth(config: AppConfig, args: argparse.Namespace) -> int:
    settings = config.workbuddy_settings(args.agent)
    auth = WorkBuddyAuth(
        base_url=settings.base_url, client_id=settings.client_id,
        client_secret=settings.client_secret, redirect_uri=settings.redirect_uri,
        token_file=settings.token_file, scope=settings.scope,
    )
    try:
        if args.auth_command == "url":
            url, state = auth.begin_authorization(args.state)
            print(url)
            print(f"\nstate = {state}")
            print("授权后请执行：auth exchange <code> --state <state>")
        elif args.auth_command == "exchange":
            if not args.state:
                print("缺少 --state：请从回调地址复制 state，与 code 一起提交。", file=sys.stderr)
                return 2
            token = await auth.exchange_code(args.code, args.state)
            print("授权成功。access_token 已写入本地文件（不会显示）。")
            print(f"scope = {token.get('scope')}")
        elif args.auth_command == "refresh":
            token = await auth.refresh()
            print("刷新成功。")
            print(f"scope = {token.get('scope')}")
        elif args.auth_command == "status":
            print(json.dumps(auth.status(), ensure_ascii=False, indent=2))
        return 0
    finally:
        await auth.aclose()


async def _cmd_transcript(store: Store, args: argparse.Namespace) -> int:
    run_id = args.run_id
    if not run_id:
        runs = store.list_runs(1)
        if not runs:
            print("数据库中没有 run。")
            return 1
        run_id = runs[0].run_id
    run = store.get_run(run_id)
    if run is None:
        print(f"未知 run_id: {run_id}", file=sys.stderr)
        return 2
    print(f"run_id = {run.run_id}")
    print(f"goal   = {run.goal}")
    print(f"status = {run.status.value}  phase = {run.phase.value}")
    print(format_transcript(store.list_messages(run_id)))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentorchestrator",
                                     description="WorkBuddy + OpenCode 双智能体编排器（可靠性核心）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml / config.example.yaml）")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("agents", help="列出配置中的 agent")
    sub.add_parser("health", help="检查各 agent 是否可用")

    send = sub.add_parser("send", help="向一个 agent 发送单条消息")
    send.add_argument("--agent", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--to", default="orchestrator")
    send.add_argument("--timeout", type=float, default=None)

    chat = sub.add_parser("chat", help="交互式双向对话")
    chat.add_argument("--agent", default=None)
    chat.add_argument("--goal", default="手动双向通信")
    chat.add_argument("--timeout", type=float, default=None)

    run = sub.add_parser("run", help="执行 分析 -> 执行 -> 审查 流程")
    run.add_argument("--goal", required=True)
    run.add_argument("--planner", default="workbuddy")
    run.add_argument("--worker", default="opencode")
    run.add_argument("--reviewer", default=None)
    run.add_argument("--max-rounds", type=int, default=2)
    run.add_argument("--timeout", type=float, default=None)

    demo = sub.add_parser("demo", help="用 MockAdapter 离线跑一遍完整流程")
    demo.add_argument("--db", default="data/demo.db")
    demo.add_argument("--goal", default="审查项目特征工程模块，检查数据泄漏与时间序列划分")
    demo.add_argument("--max-rounds", type=int, default=2)

    transcript = sub.add_parser("transcript", help="打印某个 run 的完整消息记录")
    transcript.add_argument("--run-id", default=None)

    resume = sub.add_parser("resume", help="核对未完成消息的外部状态（默认不重发）")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--resend-unstarted", action="store_true",
                        help="仅重发从未被服务端确认过的消息（attempt=0）")
    resume.add_argument("--timeout", type=float, default=None)

    events = sub.add_parser("events", help="查看 run 的状态机事件与节点记录")
    events.add_argument("--run-id", required=True)

    release = sub.add_parser("release", help="人工释放不确定消息（默认先核对远端）")
    release.add_argument("--run-id", required=True)
    release.add_argument("--message-id", required=True)
    release.add_argument("--force", action="store_true",
                         help="确认远端已结束/愿意放弃时强制释放")
    release.add_argument("--timeout", type=float, default=None)

    pending = sub.add_parser("pending", help="列出所有未完成/可能占用远端的消息")
    pending.add_argument("--provider", default=None,
                         help="按 provider 过滤，例如 workbuddy_local")

    ui = sub.add_parser("ui", help="启动本地图形界面（推荐给普通用户）")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ui.add_argument("--token", default=None,
                    help="访问密码；非回环地址必须提供（或设置 AGENTORCHESTRATOR_UI_TOKEN）")

    auth = sub.add_parser("auth", help="WorkBuddy OAuth 授权")
    auth.add_argument("--agent", default=None)
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_url = auth_sub.add_parser("url")
    auth_url.add_argument("--state", default=None)
    auth_exchange = auth_sub.add_parser("exchange")
    auth_exchange.add_argument("code")
    auth_exchange.add_argument("--state", required=True,
                               help="auth url 输出、回调地址中的 state")
    auth_sub.add_parser("refresh")
    auth_sub.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        return asyncio.run(_cmd_demo(args))

    if args.command == "transcript":
        config = load_config(args.config)
        store = Store(config.storage.path)
        try:
            return asyncio.run(_cmd_transcript(store, args))
        finally:
            store.close()

    if args.command == "ui":
        try:
            config = load_config(args.config)
        except (FileNotFoundError, ValueError) as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 2
        logging.basicConfig(
            level=getattr(logging, (args.log_level or config.log_level).upper(), logging.INFO),
            format="%(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        store = Store(config.storage.path)
        try:
            return _cmd_ui(config, store, args)
        finally:
            store.close()

    try:
        config, adapters, store = _load(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "agents":
            _print_agents(config)
            for adapter in adapters.values():
                asyncio.run(adapter.aclose())
            return 0
        if args.command == "health":
            return asyncio.run(_cmd_health(config, adapters))
        if args.command == "send":
            return asyncio.run(_cmd_send(config, adapters, store, args))
        if args.command == "chat":
            return asyncio.run(_cmd_chat(config, adapters, store, args))
        if args.command == "run":
            return asyncio.run(_cmd_run(config, adapters, store, args))
        if args.command == "resume":
            return asyncio.run(_cmd_resume(config, adapters, store, args))
        if args.command == "events":
            return asyncio.run(_cmd_events(config, store, args))
        if args.command == "release":
            return asyncio.run(_cmd_release(config, adapters, store, args))
        if args.command == "pending":
            return asyncio.run(_cmd_pending(config, store, args))
        if args.command == "auth":
            return asyncio.run(_cmd_auth(config, args))
    except KeyError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except AdapterError as exc:
        print(f"适配器错误：{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    finally:
        store.close()
    return 0
