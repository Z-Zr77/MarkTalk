"""函数级测试；不导入/启动完整 GUI，不读取配置、密钥或 memories。"""
import ast
import faulthandler
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import agent_safety as AS


def gui_methods():
    tree = ast.parse(Path(__file__).with_name("longchat_gui.py").read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ChatApp")
    names = {"_ui_call", "confirm_agent_operation", "_show_agent_confirmation", "_cancel_agent_confirmations"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(methods) == len(names)
    namespace = {"AS": AS, "tk": tk, "time": time}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "longchat_gui.py", "exec"), namespace)
    return type("SafetyTestApp", (tk.Tk,), {n: namespace[n] for n in names})


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="safety_test_", dir=Path(__file__).parent)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.audit = self.base / "audit.jsonl"
        self.roots_patch = patch.object(AS, "ALLOWED_ROOTS", (str(self.root),))
        self.audit_patch = patch.object(AS, "AUDIT_FILE", self.audit)
        self.roots_patch.start()
        self.audit_patch.start()
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app._cancel_agent_confirmations()
            self.app.destroy()
        self.audit_patch.stop()
        self.roots_patch.stop()
        assert self.base.is_relative_to(Path(__file__).parent.resolve())
        self.temp.cleanup()

    def test_allowed_and_missing_paths(self):
        self.assertEqual(AS.safe_resolve("new/file.txt"), self.root / "new" / "file.txt")
        self.assertEqual(AS.safe_resolve(str(self.root).upper() + "\\x"), self.root / "x")
        self.assertEqual(AS.safe_resolve("."), self.root)
        self.assertFalse((self.root / "new").exists())

    def test_rejected_paths(self):
        paths = [str(self.root) + "_other\\a", str(self.base / "outside"),
                 "..\\outside", "a\\..\\b", "C:\\Windows", "E:relative", "\\rooted",
                 "\\\\server\\share", "\\\\?\\E:\\x", "\\\\.\\NUL",
                 "x:secret", "CON.txt", "COM1", "LPT¹.txt", "NUL", "x.", "x ",
                 "x?", "x*", "x\x00y", "", "x\ny"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(PermissionError):
                AS.safe_resolve(path)

    def test_missing_root_not_created(self):
        missing = self.base / "missing_workspace"
        with patch.object(AS, "ALLOWED_ROOTS", (str(missing),)):
            self.assertEqual(AS.safe_resolve("x"), missing / "x")
        self.assertFalse(missing.exists())

    def test_junction_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / "link"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(PermissionError):
                AS.safe_resolve(link / "not_created.txt")
        finally:
            # 明确路径且仅删除本次创建的联接，不递归触及目标。
            link.rmdir()

    def test_root_junction_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.base / "root_link"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with patch.object(AS, "ALLOWED_ROOTS", (str(link),)):
                with self.assertRaises(PermissionError):
                    AS.safe_resolve(link / "x")
        finally:
            link.rmdir()

    def test_confirmation_deadline_and_one_shot(self):
        request = AS.Confirmation("delete", self.root / "x", 0.01)
        time.sleep(0.02)
        request.decide(True, "approved")
        self.assertFalse(request.allowed)
        self.assertEqual(request.reason, "timeout")
        request.decide(True, "approved")
        self.assertFalse(request.allowed)
        request = AS.Confirmation("write", self.root / "x", 1)
        request.decide(False)
        request.decide(True, "approved")
        self.assertFalse(request.allowed)

    def make_app(self):
        self.app = gui_methods()()
        self.app.title("MarkTalk 护栏函数测试")
        self.app.geometry("320x120")
        self.app._closing = False
        self.app._in_tray = False
        self.app._ui_queue = queue.Queue()
        self.app._agent_confirmations = {}
        return self.app

    def run_request(self, action=None, timeout=0.2, operation="delete", path="x"):
        app = self.app or self.make_app()
        result = []
        worker = threading.Thread(target=lambda: result.append(
            app.confirm_agent_operation(operation, path, timeout)), daemon=True)
        worker.start()
        deadline = time.monotonic() + 3
        acted = False
        while worker.is_alive() and time.monotonic() < deadline:
            try:
                app._ui_queue.get_nowait()()
            except queue.Empty:
                pass
            app.update()
            if action and app._agent_confirmations and not acted:
                request, dlg = next(iter(app._agent_confirmations.items()))
                action(request, dlg)
                acted = True
            time.sleep(0.005)
        worker.join(0.1)
        self.assertFalse(worker.is_alive())
        cleanup_deadline = time.monotonic() + 0.3
        while app._agent_confirmations and time.monotonic() < cleanup_deadline:
            app.update()
            time.sleep(0.005)
        self.assertFalse(app._agent_confirmations)
        return result[0]

    def test_real_dialog_confirm_and_geometry(self):
        def approve(request, dlg):
            self.assertGreater(dlg.winfo_width(), 1)
            self.assertGreater(dlg.winfo_height(), 1)
            frame = next(w for w in dlg.winfo_children() if isinstance(w, tk.Frame))
            next(w for w in frame.winfo_children() if w.cget("text") == "确认").invoke()
        self.assertTrue(self.run_request(approve, timeout=2))
        self.assertFalse(self.app._agent_confirmations)
        records = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["outcome"] for r in records], ["requested", "confirmed"])
        self.assertEqual(records[-1]["reason"], "approved")

    def test_dialog_close_and_cancel_all(self):
        self.assertFalse(self.run_request(lambda r, dlg: dlg.destroy(), timeout=2))
        self.assertFalse(self.run_request(lambda r, dlg: self.app._cancel_agent_confirmations(), timeout=2))
        self.assertFalse(self.app._agent_confirmations)

    def test_dialog_timeout(self):
        start = time.monotonic()
        self.assertFalse(self.run_request())
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(json.loads(self.audit.read_text().splitlines()[-1])["reason"], "timeout")

    def test_unpumped_queue_and_late_delivery(self):
        app = self.make_app()
        result = []
        worker = threading.Thread(target=lambda: result.append(app.confirm_agent_operation("write", "x", 0.03)))
        worker.start()
        worker.join(1)
        self.assertEqual(result, [False])
        app._ui_queue.get_nowait()()
        self.assertFalse(app._agent_confirmations)

    def test_main_thread_and_closing_denied(self):
        app = self.make_app()
        self.assertFalse(app.confirm_agent_operation("write", "x"))
        app._closing = True
        self.assertFalse(self.run_request())
        app._closing = False
        app._in_tray = True
        self.assertFalse(self.run_request())

    def test_invalid_operation_and_audit_privacy(self):
        secret = "sk-FAKE-SECRET-body-text"
        self.assertFalse(self.run_request(operation=secret, path=secret))
        text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["operation"], "invalid")
        self.assertFalse(self.run_request(path="..\\outside"))

    def test_audit_failure_denied(self):
        self.make_app()
        with patch.object(AS, "audit", side_effect=OSError("disk failure")):
            self.assertFalse(self.run_request(timeout=1))
        self.assertFalse(self.app._agent_confirmations)

    def test_final_audit_failure_denied(self):
        real_audit = AS.audit
        def fail_final(operation, target, outcome, reason):
            if outcome != "requested":
                raise OSError("disk failure")
            real_audit(operation, target, outcome, reason)
        with patch.object(AS, "audit", side_effect=fail_final):
            self.assertFalse(self.run_request(lambda r, dlg: r.decide(True, "approved"), timeout=2))

    def test_write_execute_and_invalid_timeouts(self):
        for operation in ("write", "execute"):
            self.assertTrue(self.run_request(lambda r, dlg: r.decide(True, "approved"),
                                             timeout=2, operation=operation))
        for timeout in (0, -1, float("nan"), float("inf"), 301):
            self.assertFalse(self.run_request(timeout=timeout))

    def test_gui_exception_denied(self):
        self.make_app()
        with patch.object(tk, "Toplevel", side_effect=tk.TclError("test")):
            self.assertFalse(self.run_request(timeout=2))

    def test_path_rechecked_after_confirmation(self):
        def resolve_again(path):
            if calls[0]:
                raise PermissionError("changed")
            calls[0] += 1
            return self.root / "x"
        calls = [0]
        with patch.object(AS, "safe_resolve", side_effect=resolve_again):
            self.assertFalse(self.run_request(lambda r, dlg: r.decide(True, "approved"), timeout=2))


if __name__ == "__main__":
    faulthandler.dump_traceback_later(45, exit=True)
    try:
        unittest.main(verbosity=2)
    finally:
        faulthandler.cancel_dump_traceback_later()
