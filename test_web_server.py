"""F 的隔离双端口测试：仅抽取 ControlServer，不导入 GUI 或用户配置。"""
import ast
import json
import os
import queue
import secrets
import socket
import threading
import unittest
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from web_server import make_web_handler

ROOT = Path(__file__).resolve().parent


class FakeApp:
    def __init__(self):
        self.jobs = queue.Queue()
        self.current_mem = "测试.md"
        self.busy = False
        self._edit_hidx = None
        self.pending_images = []
        self.pending_files = []
        self.draft = ""
        self.sent = []
        self.entry = SimpleNamespace(insert=self.insert)
        self.stop_event = threading.Event()
        self.owner = threading.get_ident()

    def _ui_call(self, fn):
        self.jobs.put(fn)

    def insert(self, index, text):
        assert threading.get_ident() == self.owner
        self.draft = text

    def get_input_text(self):
        assert threading.get_ident() == self.owner
        return self.draft

    def send_text(self):
        assert threading.get_ident() == self.owner
        self.sent.append(self.draft)
        self.draft = ""

    def select_mem(self, name):
        assert threading.get_ident() == self.owner
        self.current_mem = name

    def stop_gen(self):
        assert threading.get_ident() == self.owner
        self.stop_event.set()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "longchat_gui.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ControlServer")
        def on_main(value):
            self.assertEqual(threading.get_ident(), self.app.owner)
            return value
        self.app = FakeApp()
        scope = dict(threading=threading, secrets=secrets, json=json, urllib=urllib,
                     BaseHTTPRequestHandler=BaseHTTPRequestHandler, ThreadingHTTPServer=ThreadingHTTPServer,
                     make_web_handler=make_web_handler, CONTROL_DEFAULT_PORT=8765,
                     load_control_token=lambda: (_ for _ in ()).throw(AssertionError("must not read token file")),
                     _resource_path=lambda name: str(ROOT / name),
                     list_mems=lambda: on_main(["测试.md", "第二.md"]),
                     is_memory_visible=lambda name: on_main(name in ("测试.md", "第二.md")),
                     load_mem=lambda name: on_main("回复 <script>alert(1)</script>"),
                     os=os, busy=False, ENGINE=None, ENGINE_KIND="llama", MODEL_ID="test-8B", MEM_DIR="private")
        self.scope = scope
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(ROOT / "longchat_gui.py"), "exec"), scope)
        self.control = scope["ControlServer"](self.app, port=free_port(), token="control-test")
        self.web = scope["ControlServer"](self.app, host="0.0.0.0", port=free_port(), web=True)
        self.assertNotEqual(self.control.token, self.web.token)
        self.assertTrue(self.control.start()[0])
        self.assertTrue(self.web.start()[0])

    def tearDown(self):
        self.web.shutdown()
        self.control.shutdown()

    def request(self, path, body=None, control=False, headers=None, auth=True):
        server = self.control if control else self.web
        hdr = {"X-MarkTalk-Token" if control else "X-MarkTalk-Web-Token": server.token} if auth else {}
        hdr.update(headers or {})
        result = queue.Queue()
        def run():
            req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                  None if body is None else json.dumps(body).encode(), hdr)
            try:
                response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            except Exception as error:
                result.put(error)
                return
            with response:
                raw = response.read()
                data = raw.decode() if response.headers.get_content_type() == "text/html" else json.loads(raw)
                result.put((response.code, data))
        worker = threading.Thread(target=run)
        worker.start()
        while worker.is_alive():
            try:
                self.app.jobs.get(timeout=.01)()
            except queue.Empty:
                pass
        worker.join()
        value = result.get_nowait()
        if isinstance(value, Exception):
            raise value
        return value

    def test_auth_and_origin(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/", auth=False)[0], 401)
        self.assertEqual(self.request("/status", auth=False, headers={"X-MarkTalk-Token": self.control.token})[0], 401)
        self.assertEqual(self.request("/status", headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.request("/status", headers={"Host": "evil.test"})[0], 403)
        self.assertEqual(self.request("/status", headers={"Origin": f"http://127.0.0.1:{self.web.port}"})[0], 200)
        self.assertEqual(self.request("/?token=" + self.web.token, auth=False)[0], 200)

    def test_actions_and_boundaries(self):
        self.assertEqual(self.request("/chats")[1]["chats"], ["测试.md", "第二.md"])
        self.assertIn("<script>", self.request("/chat")[1]["text"])
        self.assertTrue(self.request("/send", {"text": "中文\n消息"})[1]["ok"])
        self.assertEqual(self.app.sent, ["中文\n消息"])
        self.assertTrue(self.request("/switch", {"chat": "第二.md"})[1]["ok"])
        self.assertEqual(self.app.current_mem, "第二.md")
        self.assertFalse(self.request("/switch", {"chat": "../hidden.md"})[1]["ok"])
        self.app.busy = True
        self.scope["busy"] = True
        self.assertTrue(self.request("/status")[1]["busy"])
        self.assertFalse(self.request("/switch", {"chat": "测试.md"})[1]["ok"])
        self.assertTrue(self.request("/stop", {})[1]["ok"])
        for path, body in (("/switch", {"model": "27B"}), ("/send", {"text": "x", "files": []}),
                           ("/send", {"text": " "}), ("/send", {"text": "x" * 17000}), ("/stop", {"extra": True})):
            self.assertEqual(self.request(path, body)[0], 400)
        for path in ("/fs/read", "/exec", "/mobile.html", "/api_key.dat"):
            self.assertEqual(self.request(path)[0], 404)
        for attribute, value in (("draft", "电脑草稿"), ("pending_images", ["a"]),
                                 ("pending_files", ["b"]), ("_edit_hidx", 0)):
            original = getattr(self.app, attribute)
            setattr(self.app, attribute, value)
            self.assertFalse(self.request("/send", {"text": "手机消息"})[1]["ok"])
            setattr(self.app, attribute, original)
        self.assertEqual(len(self.app.sent), 1)

    def test_control_regression(self):
        self.assertTrue(self.request("/chats", control=True)[1]["ok"])
        self.assertTrue(self.request("/status", control=True)[1]["ok"])
        self.assertEqual(self.request("/status", control=True, auth=False)[0], 401)
        self.assertEqual(self.request("/status", control=True, headers={"Origin": "http://localhost"})[0], 403)
        self.assertEqual(self.request("/status", control=True, auth=False,
                                     headers={"X-MarkTalk-Web-Token": self.web.token})[0], 401)
        self.web.shutdown()
        self.assertTrue(self.request("/status", control=True)[1]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
