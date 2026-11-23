from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _QuietHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence test server
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")


class FakeOpenCodeHandler(_QuietHandler):
    """模拟 opencode serve 的 legacy /session 接口。"""

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/health", "/global/health"):
            self._json(200, {"healthy": True, "version": "fake"})
            return
        if path == "/session/ses_e2e/message":
            self._json(200, self.server.state.get("messages", []))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/session":
            self._json(200, {"id": "ses_e2e", "title": "fake"})
            return
        if path == "/session/ses_e2e/prompt_async":
            body = self._read_json()
            text = (body.get("parts") or [{}])[0].get("text", "")
            message_id = body.get("messageID") or "msg_user"
            self.server.state["messages"] = [
                {
                    "info": {"id": message_id, "sessionID": "ses_e2e", "role": "user",
                             "time": {"created": 1000}},
                    "parts": [{"type": "text", "text": text}],
                },
                {
                    "info": {"id": "msg_assistant", "sessionID": "ses_e2e", "role": "assistant",
                             "parentID": message_id, "time": {"created": 2000, "completed": 3000}},
                    "parts": [{"type": "text", "text": f"OPENCODE 执行结果：{text[:120]}"}],
                },
            ]
            self.send_response(204)
            self.end_headers()
            return
        if path.endswith("/abort"):
            self._json(200, True)
            return
        self._json(404, {"error": "not found"})


class FakeWorkBuddyHandler(_QuietHandler):
    """模拟 WorkBuddy 本地助理 Open API。"""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/openapi/v2/localassistant":
            self._json(200, {"code": 0, "msg": "success", "request_id": "r1",
                             "data": {"online": True}})
            return
        if parsed.path == "/openapi/v2/localassistant/message":
            query = parse_qs(parsed.query)
            message_id = (query.get("message_id") or [""])[0]
            state = self.server.state
            reply = state["replies"].get(message_id)
            if reply is None:
                self._json(200, {"code": 0, "data": {"messages": []}})
                return
            messages = [
                {"message_id": message_id, "role": "user",
                 "content": [state["inputs"][message_id]], "msg_type": "text",
                 "created_at": "2026-01-01T00:00:00Z", "attachments": [], "metadata": {}},
                {"message_id": f"{message_id}-a", "role": "assistant", "content": [reply],
                 "msg_type": "text", "created_at": "2026-01-01T00:00:01Z",
                 "attachments": [], "metadata": {}},
            ]
            self._json(200, {"code": 0, "data": {"messages": messages}})
            return
        self._json(404, {"code": 404, "msg": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/openapi/v2/localassistant/message":
            self._json(404, {"code": 404, "msg": "not found"})
            return
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._json(401, {"code": 401, "msg": "unauthorized"})
            return
        content = self._read_json().get("content", "")
        state = self.server.state
        state["n"] = state.get("n", 0) + 1
        message_id = f"wb-{state['n']}"
        state["inputs"][message_id] = content
        if "审查" in content and ("OPENCODE" in content or "执行结果" in content):
            state["replies"][message_id] = json.dumps({
                "decision": "approved",
                "summary": "结构完整，可以结束",
                "findings": [],
                "evidence": ["fake-workbuddy-review"],
                "confidence": 0.95,
            }, ensure_ascii=False)
        else:
            state["replies"][message_id] = f"WORKBUDDY 分析：{content[:80]}"
        self._json(200, {"code": 0, "msg": "success", "request_id": "r2",
                         "data": {"message_id": message_id}})


class LocalServer:
    """在 127.0.0.1 随机端口启动一个线程化 HTTP 服务。"""

    def __init__(self, handler: type[_QuietHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.state = {"messages": [], "inputs": {}, "replies": {}, "n": 0}
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "LocalServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
