"""四项优化的针对性验证：裸 Tk/假 HTTP，不启动 ChatApp 或读取用户数据。"""
import os
import ast
import json
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace, MethodType
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def deny_credentials(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).name.lower() in ("api_key.dat", "control_token.dat"):
            raise AssertionError("credential file access forbidden")


sys.addaudithook(deny_credentials)
import longchat_gui as gui
import test_web_server as web_tests


def pump(root, seconds=.15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(.005)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = gui.ChatApp.__new__(gui.ChatApp)
        tk.Tk.__init__(self.app)
        self.app.title("MarkTalk 优化隔离测试")
        self.app.refresh_arch_dialog = lambda: None  # 不扫描/迁移任何 memories。

    def tearDown(self):
        self.app.destroy()

    def test_archive_hint(self):
        self.app.open_arch_dialog()
        dialog = self.app.arch_dlg
        hint = next(w for w in dialog.winfo_children() if isinstance(w, tk.Label)
                    and w.cget("text").startswith("双击 ="))
        for size in ("600x680", "520x440"):
            dialog.geometry(size)
            pump(self.app)
            self.assertEqual(hint.winfo_height(), hint.winfo_reqheight())
            self.assertGreater(hint.winfo_height(), 1)
            self.assertEqual(int(hint.cget("wraplength")), hint.winfo_width())
            self.assertLessEqual(hint.winfo_x() + hint.winfo_width(), dialog.winfo_width())
            self.assertLessEqual(hint.winfo_y() + hint.winfo_height(), dialog.winfo_height())
            print("archive hint", size, hint.winfo_width(), hint.winfo_height(), "fully visible")

    def test_operation_tips(self):
        app = self.app
        app.geometry("700x400")
        app.chat = tk.Text(app, font=("Microsoft YaHei UI", 10))
        app.chat.pack(fill="both", expand=True)
        app._op_hl = None
        callback_errors = []
        app.report_callback_exception = lambda *args: callback_errors.append(args)
        app._copy_msg = lambda hidx: None
        app._set_top_hint = lambda text: None
        app._stop_think = lambda **kwargs: None
        app._see_end = lambda **kwargs: None
        app._add_link_tags = lambda: None
        app._loaded_from = 0
        # 执行 _build_ui 中真正的共享 tip 注册块，避免测试复制一份绑定逻辑。
        tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8-sig"))
        build = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_ui")
        loop = next(n for n in build.body if isinstance(n, ast.For)
                    and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_tip"
                            and any(k.arg == "tag" for k in c.keywords) for c in ast.walk(n)))
        exec(compile(ast.Module(body=[loop], type_ignores=[]), gui.__file__, "exec"), dict(self=app, _tip=gui._tip))
        # 真实历史渲染与新消息渲染共用 opc_*；历史内容完全为内存测试值。
        app.add_msg = lambda role, content, **kwargs: app.chat.insert("end", content + "\n")
        with patch.object(gui, "is_hidden_mem", return_value=False), patch.object(gui, "arch_list_batches", return_value=[]), \
             patch.dict(gui.sessions, {"test": {"messages": [{"role": "system", "content": ""},
                              {"role": "user", "content": "历史问题"},
                              {"role": "assistant", "content": "历史回答"}], "ctx_start": 1}}):
            app.render_session("test")
        app.add_op_bar("user", 2)
        app.add_op_bar("ai", 3)
        pump(app)
        def tips():
            return [w for w in app.chat.winfo_children() if isinstance(w, tk.Toplevel)]
        def enter_tag(key):
            # 调用真实已注册的 Tk tag 回调；不移动桌面鼠标，避免用户操作影响测试。
            script = app.chat.tk.call(app.chat._w, "tag", "bind", "opc_" + key, "<Enter>")
            callback = re.search(r"\[([^\s]+)", script).group(1)
            app.chat.tk.call(callback, "")
        for key in ("copy", "edit", "rethink", "del"):
            ranges = app.chat.tag_ranges("opc_" + key)
            self.assertGreaterEqual(len(ranges), 4)  # 历史/新消息均有该区域。
            app.chat.see(ranges[0])
            pump(app)
            enter_tag(key)
            pump(app, .5)
            self.assertEqual(len(tips()), 1, key)
            self.assertTrue(tips()[0].winfo_ismapped(), key)
            app.chat.event_generate("<MouseWheel>", delta=-120)
            pump(app)
            self.assertEqual(tips(), [])
        self.assertEqual(callback_errors, [])
        # 移出 tag、点击、隐藏窗口均撤销提示和延迟任务。
        for event in ("<ButtonPress-1>", "<Leave>", "<Unmap>"):
            app.chat.event_generate("<Leave>")
            enter_tag("copy")
            pump(app, .5)
            self.assertEqual(len(tips()), 1)
            app.chat.event_generate(event)
            pump(app)
            self.assertEqual(tips(), [])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()
        self.received = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.received.append(data)
                content = "成功摘要" if "归档助手" in data["messages"][0]["content"] else "已发送回复"
                payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                summary = "归档助手" in data["messages"][0]["content"]
                if summary and owner.phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload[:8])  # 模拟已收到部分 JSON 后迟迟不完成。
                    self.wfile.flush()
                if summary and owner.phase != "complete":
                    owner.started.set()
                    self.connection.settimeout(.1)
                    while not owner.release.is_set():
                        try:
                            if self.connection.recv(1) == b"":
                                owner.disconnected.set()
                                return
                        except socket.timeout:
                            continue
                        except OSError:
                            owner.disconnected.set()
                            return
                if not summary or owner.phase != "body":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.engine = gui.EA.EngineAdapter({"base_url": f"http://127.0.0.1:{self.server.server_port}"})
        self.log_patch = patch.object(gui.EA, "log")  # 不生成项目日志。
        self.log_patch.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.log_patch.stop()

    def test_force_interrupts_summary(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                self.phase = phase
                self.started.clear()
                self.disconnected.clear()
                self.received.clear()
                jobs = queue.Queue()
                statuses = []
                replies = []
                app = SimpleNamespace(stop_event=threading.Event(), _sum_cur=0,
                      msg_queue=[{"segments": "普通队列", "display": "普通队列"},
                                 {"segments": "强制消息", "display": "强制消息"}],
                      _arch_pending={"test": ["待归档原文"]}, _after_archive=("clear_dialog", "test"),
                      refresh_queue_bar=lambda: None, _stop_sum_timer=lambda: None,
                      _set_busy_ui=lambda flag: None, set_status=statuses.append, add_arch=lambda text: None,
                      _ui_call=jobs.put,
                      after=lambda ms, fn: threading.Timer(ms / 1000, lambda: jobs.put(fn)).start())
                def dispatch(segments, display, images):
                    app.stop_event.clear()  # 后续请求沿用原 _worker 机制。
                    replies.append(gui.api([{"role": "user", "content": display}], timeout=3))
                app.dispatch = dispatch
                for name in ("_on_summary", "_summary_worker", "queue_force", "_pump_queue"):
                    setattr(app, name, MethodType(getattr(gui.ChatApp, name), app))
                session = {"messages": [{"role": "user", "content": "原对话"}], "ctx_start": 1, "turns": 5}
                with patch.object(gui, "ENGINE", self.engine), patch.object(gui, "MODEL_ID", "test-8B"), \
                     patch.object(gui, "busy", True), patch.dict(gui.sessions, {"test": session}), \
                     patch.object(gui, "append_mem") as append, patch.object(gui, "arch_save_batch") as save, \
                     patch.object(gui, "rewrite_dialog_file") as rewrite:
                    worker = threading.Thread(target=app._summary_worker,
                              args=("test", [[{"role": "user", "content": "第一段"}],
                                             [{"role": "user", "content": "第二段"}]]))
                    worker.start()
                    self.assertTrue(self.started.wait(2), "stub must receive summary HTTP request")
                    t0 = time.monotonic()
                    app.queue_force(1)
                    deadline = t0 + 3
                    while not replies and time.monotonic() < deadline:
                        try:
                            jobs.get(timeout=.05)()
                        except queue.Empty:
                            pass
                    worker.join(timeout=.5)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(self.disconnected.wait(.5), "summary connection must close")
                    self.assertEqual(replies, [("已发送回复", None)])
                    self.assertLess(time.monotonic() - t0, 2)
                    self.assertEqual(len(self.received), 2)  # 一段摘要 + 强制消息，没有第二段摘要。
                    self.assertEqual(self.received[1]["messages"][0]["content"], "强制消息")
                    append.assert_not_called(); save.assert_not_called(); rewrite.assert_not_called()
                    self.assertEqual(session["ctx_start"], 1)
                    self.assertEqual(session["turns"], 5)
                    self.assertEqual(app._arch_pending, {})
                    self.assertIsNone(app._after_archive)
                    self.assertEqual(app.msg_queue[0]["display"], "普通队列")
                    print("summary cancelled and forced HTTP sent:", phase,
                          round(time.monotonic() - t0, 3), "s")

    def test_complete_and_pre_cancelled(self):
        self.phase = "complete"
        stop = threading.Event()
        text, error = self.engine.chat([{"role": "system", "content": "归档助手"}],
                                      "test-8B", {}, timeout=3, stop_event=stop)
        self.assertEqual((text, error), ("成功摘要", None))
        stop.set()
        text, error = self.engine.chat([{"role": "system", "content": "归档助手"}],
                                      "test-8B", {}, timeout=3, stop_event=stop)
        self.assertIsNone(text)
        self.assertIsNotNone(error)
        self.assertEqual(len(self.received), 1)


class StatusTests(unittest.TestCase):
    setUp = web_tests.WebTests.setUp
    tearDown = web_tests.WebTests.tearDown
    request = web_tests.WebTests.request

    def test_actual_busy(self):
        for actual in (False, True, False):
            self.scope["busy"] = actual
            self.app.busy = not actual  # 旧误读会得到相反值。
            control = self.request("/status", control=True)
            web = self.request("/status")
            self.assertEqual(control[0], 200)
            self.assertEqual(control[1]["busy"], actual)
            self.assertEqual(web[1]["busy"], actual)
        self.assertEqual(self.request("/status", control=True, auth=False)[0], 401)
        self.assertEqual(self.request("/status", control=True,
                                     headers={"Origin": "http://localhost"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
