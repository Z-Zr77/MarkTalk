"""单实例验证：真实 Win32 Mutex + 裸 Tk/子进程，不构造用户 ChatApp。"""
import ctypes
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch


def deny_data(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0]))
        if path.name.lower() in ("api_key.dat", "control_token.dat") or "memories" in path.parts:
            raise AssertionError("user data/credential access forbidden")
    if event == "os.mkdir" and "memories" in Path(os.fsdecode(args[0])).parts:
        raise AssertionError("memory creation forbidden")


sys.addaudithook(deny_data)
import longchat_gui as gui


def probe(path, default=False):
    guard = gui._SingleInstance(path, default=default)
    try:
        print(json.dumps({"existing": bool(guard.existing), "keys": guard.keys}), flush=True)
    finally:
        guard.close()


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="MarkTalk-instance-test-")
        self.path = str(Path(self.temp.name) / "数据目录A")
        self.other = str(Path(self.temp.name) / "数据目录B")
        self.guards = []
        self.windows = []

    def tearDown(self):
        for guard in reversed(self.guards):
            guard.close()
        for window in self.windows:
            try:
                window.destroy()
            except tk.TclError:
                pass
        self.temp.cleanup()

    def guard(self, path, default=False):
        guard = gui._SingleInstance(path, default=default)
        self.guards.append(guard)
        return guard

    def child(self, path, default=False):
        result = subprocess.run([sys.executable, "-X", "utf8", __file__, "--probe", path,
                                 "default" if default else "explicit"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    def pump_until_exit(self, process, root):
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        if process.poll() is None:
            process.kill()
            self.fail("temporary child did not exit")
        until = time.monotonic() + .25
        while time.monotonic() < until:
            root.update()
            time.sleep(.01)
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))

    def bare_window(self, title):
        root = tk.Tk()
        self.windows.append(root)
        root.title(title)
        root.geometry("400x180")
        root._closing = False
        root._in_tray = False
        root.restored = 0
        def restore():
            root.restored += 1
            gui.ChatApp._restore_from_tray(root)
        root._restore_from_tray = restore
        root.update()
        return root

    def source_child(self, args):
        # 真正执行源码 __main__。审计钩子证明重复启动在用户数据访问前退出。
        return subprocess.Popen([sys.executable, "-X", "utf8", __file__, "--source", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_same_directory_and_aliases(self):
        owner = self.guard(self.path)
        self.assertIsNone(owner.existing)
        self.assertTrue(self.child(self.path)["existing"])
        alias = str(Path(self.path) / ".." / "数据目录A").upper() + "\\"
        self.assertTrue(self.child(alias)["existing"])
        self.assertFalse(self.child(self.other)["existing"])
        self.assertFalse(Path(self.path).exists())  # Mutex 不创建数据目录。

    def test_default_explicit_and_modes(self):
        owner = self.guard(self.path, default=True)
        self.assertIsNone(owner.existing)
        self.assertTrue(self.child(self.path, default=True)["existing"])
        self.assertTrue(self.child(self.other, default=True)["existing"])
        self.assertTrue(self.child(self.path)["existing"])
        self.assertFalse(self.child(self.other)["existing"])
        # 目录解析在同目录下源码/冻结模式一致，名称不依赖 python/exe 进程名。
        source = Path(gui.__file__).resolve().parent
        with patch.object(sys, "argv", ["longchat_gui.py"]):
            normal = gui._app_base_dir()
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(source / "MarkTalk_GUI.exe")):
                frozen = gui._app_base_dir()
        self.assertEqual(os.path.normcase(normal), os.path.normcase(frozen))
        with patch.object(sys, "argv", ["longchat_gui.py", "--data-dir=" + self.path]):
            self.assertEqual(gui._app_base_dir(), os.path.abspath(self.path))

    def test_release_and_process_exit(self):
        owner = self.guard(self.path)
        self.assertIsNone(owner.existing)
        owner.close()
        self.assertFalse(self.child(self.path)["existing"])
        self.assertFalse(self.child(self.path)["existing"])  # 上一个子进程退出后锁释放。

    def test_duplicate_source_restores_correct_hidden_window(self):
        root = self.bare_window("MarkTalk 单实例验证 A")
        other = self.bare_window("MarkTalk 单实例验证 B")
        owner = self.guard(self.path)
        second = self.guard(self.other)
        owner.attach(root)
        second.attach(other)
        root.withdraw()
        root._in_tray = True
        self.pump_until_exit(self.source_child(["--data-dir", self.path]), root)
        self.assertEqual(root.state(), "normal")
        self.assertFalse(root._in_tray)
        self.assertGreater(root.restored, 0)
        self.assertEqual(other.restored, 0)
        user = ctypes.WinDLL("user32")
        user.GetForegroundWindow.restype = ctypes.c_void_p
        self.assertEqual(user.GetForegroundWindow(), owner.hwnd)
        self.assertFalse(Path(self.path).exists())

    def test_default_duplicate_exits_before_chatapp(self):
        root = self.bare_window("MarkTalk 默认入口验证")
        owner = self.guard(gui.BASE, default=True)
        self.assertIsNone(owner.existing)
        owner.attach(root)
        self.pump_until_exit(self.source_child([]), root)
        self.assertGreater(root.restored, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        probe(sys.argv[2], default=sys.argv[3] == "default")
    elif len(sys.argv) > 1 and sys.argv[1] == "--source":
        sys.argv = [gui.__file__, *sys.argv[2:]]
        runpy.run_path(gui.__file__, run_name="__main__")
    else:
        unittest.main(verbosity=2)
