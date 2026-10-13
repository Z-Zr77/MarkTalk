"""手机 Web HTTP Handler；与 C 共用 ControlServer 实例类，不接触 Tk。"""
import json
import secrets
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WEB_PORT = 8766


def make_web_handler(server, html_path):
    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # URL 可能携带 Web token，不写访问日志。

        def respond(self, data, code=200, html=False):
            payload = data if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def guard(self):
            host = self.headers.get("Host", "")
            try:
                address = urlsplit("//" + host)
                local_ip, local_port = self.connection.getsockname()[:2]
                if address.hostname not in (local_ip, "localhost" if local_ip == "127.0.0.1" else local_ip) or address.port != local_port:
                    raise ValueError()
                origin = self.headers.get("Origin")
                if origin is not None and origin != "http://" + host:
                    raise ValueError()
            except ValueError:
                self.respond({"ok": False, "error": "拒绝非同源或错误 Host 请求"}, 403)
                return False
            token = self.headers.get("X-MarkTalk-Web-Token") or parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            if not token or not secrets.compare_digest(token.encode("utf-8"), server.token.encode("utf-8")):
                self.respond({"ok": False, "error": "缺少或错误的 Web token"}, 401)
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    self.respond(Path(html_path).read_bytes(), html=True)
                elif path == "/chats":
                    self.respond(server.chats())
                elif path == "/status":
                    self.respond(server.web_status())
                elif path == "/chat":
                    self.respond(server.web_chat())
                else:
                    self.respond({"ok": False, "error": "未知端点"}, 404)
            except Exception:
                self.respond({"ok": False, "error": "Web 请求处理失败"}, 500)

        def do_POST(self):
            if not self.guard():
                return
            path = urlsplit(self.path).path
            if path not in ("/send", "/switch", "/stop"):
                self.respond({"ok": False, "error": "未知端点"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 16384:
                    raise ValueError()
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                allowed = {"/send": {"text"}, "/switch": {"chat"}, "/stop": set()}[path]
                if not isinstance(body, dict) or set(body) != allowed:
                    raise ValueError()
                if any(not isinstance(v, str) or not v.strip() for v in body.values()):
                    raise ValueError()
            except (ValueError, UnicodeError):
                self.respond({"ok": False, "error": "参数无效或消息超过 16 KiB"}, 400)
                return
            try:
                action = {"/send": lambda: server.web_send(body),
                          "/switch": lambda: server.web_switch(body), "/stop": server.stop}[path]
                self.respond(action())
            except Exception:
                self.respond({"ok": False, "error": "Web 请求处理失败"}, 500)

        def setup(self):
            super().setup()
            self.connection.settimeout(20)  # 不完整请求不能无限占用 HTTP 线程。

    return WebHandler
