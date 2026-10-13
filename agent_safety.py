"""Agent 护栏；不执行文件操作或命令，不提供 HTTP 端点。"""
import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import threading
import time

ALLOWED_ROOTS = (r"E:\agent_workspace",)
AUDIT_FILE = Path(__file__).resolve().parent / "logs" / "agent_audit.jsonl"
_AUDIT_LOCK = threading.Lock()
_OPERATIONS = {"write", "delete", "execute"}


def safe_resolve(path):
    """返回白名单内绝对路径；不创建目录。拒绝所有重解析点。

    shortcut: 校验不是文件句柄沙箱；未来执行层必须防止校验后的路径替换。
    """
    if os.name != "nt":
        raise PermissionError("护栏仅支持 Windows")
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
        raise PermissionError("无效路径")
    raw = raw.replace("/", "\\")
    drive, tail = ntpath.splitdrive(raw)
    if raw.startswith("\\") or (drive and (len(drive) != 2 or not tail.startswith("\\"))):
        raise PermissionError("拒绝 UNC、设备路径及非完整盘符路径")
    for part in tail.split("\\"):
        if not part or part == ".":
            continue
        if part == ".." or part.endswith((".", " ")) or any(c in part for c in ':*?"<>|'):
            raise PermissionError("拒绝遍历及特殊路径")
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$",
                        part.split(".", 1)[0], re.IGNORECASE):
            raise PermissionError("拒绝设备名")
    candidate = Path(raw if drive else ntpath.join(ALLOWED_ROOTS[0], raw))
    try:
        roots = tuple(Path(r) for r in ALLOWED_ROOTS)
        if not any(candidate.is_relative_to(root) for root in roots):
            raise PermissionError("路径不在白名单内")
        # 包括根自身与其祖先；不存在的目标仍检查已有父目录。
        for item in (candidate, *candidate.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise PermissionError("拒绝符号链接、目录联接及其它重解析点")
        resolved = candidate.resolve(strict=False)
        if not any(resolved.is_relative_to(root) for root in roots):
            raise PermissionError("解析后路径越界")
        return resolved
    except (OSError, ValueError, RuntimeError) as exc:
        raise PermissionError("路径校验失败") from exc


def audit(operation, target, outcome, reason):
    """仅记录固定操作标签、目标指纹和结果；不记录路径正文、命令或密钥。"""
    record = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "operation": operation if operation in _OPERATIONS else "invalid",
              "target_sha256": hashlib.sha256(str(target).encode("utf-8", "replace")).hexdigest(),
              "outcome": outcome, "reason": reason}
    with _AUDIT_LOCK:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class Confirmation:
    """一次性确认；单调时钟期限及锁阻止迟到确认改变拒绝结果。"""
    def __init__(self, operation, path, timeout):
        self.operation = operation
        self.path = path
        self.deadline = time.monotonic() + timeout
        self.done = threading.Event()
        self._lock = threading.Lock()
        self.allowed = False
        self.reason = "pending"

    def decide(self, allowed=False, reason="cancelled"):
        with self._lock:
            if self.done.is_set():
                return
            expired = time.monotonic() >= self.deadline
            self.allowed = allowed is True and not expired
            self.reason = "timeout" if expired else reason
            self.done.set()


def request_confirmation(app, operation, path, timeout=30.0):
    """仅工作线程调用；返回确认结果，不执行操作。日志失败也拒绝。"""
    request = None
    target = path
    reason = "invalid_request"
    allowed = False
    try:
        if operation not in _OPERATIONS or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("无效操作或超时")
        target = safe_resolve(path)
        if threading.current_thread() is threading.main_thread():
            reason = "ui_thread"
        elif app._closing:
            reason = "closing"
        else:
            request = Confirmation(operation, target, timeout)
            audit(operation, target, "requested", "pending")
            app._ui_call(lambda: app._show_agent_confirmation(request))
            request.done.wait(max(0.0, request.deadline - time.monotonic()))
            request.decide(False, "timeout")
            allowed, reason = request.allowed, request.reason
            # 用户等待期间路径可能改变，放行前再次校验。
            if allowed and safe_resolve(path) != target:
                allowed, reason = False, "path_changed"
    except PermissionError:
        allowed, reason = False, "path_denied"
    except Exception:
        allowed, reason = False, "error"
    finally:
        if request is not None and not request.done.is_set():
            request.decide(False, "error")
    try:
        audit(operation, target, "confirmed" if allowed else "denied", reason)
    except Exception:
        return False
    return allowed
