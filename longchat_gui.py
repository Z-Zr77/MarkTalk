# -*- coding: utf-8 -*-
"""
MarkTalk GUI v3
========================
功能：
  - 多会话：每个记忆文件(.md) = 一个独立对话，点击左侧切换，互不打扰
  - 新建对话：第一句话自动生成记忆文件名，右键可重命名/删除
  - 默认会话：长对话记忆.md（不新建直接聊）
  - 自动归档：上下文将满时自动提炼写入当前记忆文件，无限续聊
  - 模型切换：右下角下拉展开已有模型，点击切换
  - 图片识别：📎 选择图片，走模型视觉配套(mmproj)处理
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading, base64, json, os, sys, glob, datetime, shutil, re, queue, time, urllib.request, urllib.error
import secrets, socket, urllib.parse, ipaddress, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import agent_safety as AS
from web_server import WEB_PORT, make_web_handler

# 拖拽进窗口：tkinter 原生不支持，用 tkinterdnd2（tkdnd 的 Python 封装）。
# 库不存在时优雅降级为普通 tk.Tk，其余功能不受影响。
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_OK = True
    _RootBase = TkinterDnD.Tk
except Exception:
    _DND_OK = False
    _RootBase = tk.Tk

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# 双引擎：实际请求由 ENGINE 适配器发出（llama.cpp :8080 / FreeToken :8000）。
# 这一行仅为向后兼容保留，代码里已不再使用。
API = "http://127.0.0.1:8080/v1/chat/completions"

def _app_base_dir():
    """程序的数据目录（memories、gui_settings.json 都放这里）。
    规则：
      1. 命令行 --data-dir <路径> 优先（想指定别的文件夹就用它）
      2. 打包成 exe 后 → **exe 所在目录**
         （绝不能用 __file__：冻结后它指向临时解包目录，
          会读不到也让写不进用户的聊天记录，重启就丢）
      3. 直接跑 .py → 脚本所在目录"""
    for i, a in enumerate(sys.argv):
        if a == "--data-dir" and i + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[i + 1])
        if a.startswith("--data-dir="):
            return os.path.abspath(a.split("=", 1)[1])
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

BASE = _app_base_dir()


class _SingleInstance:
    """默认入口统一互斥；显式数据目录按实际路径互斥，源码/exe 共用。"""
    def __init__(self, data_dir, default=False):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.user.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
        self.user.SetPropW.restype = wintypes.BOOL
        self.user.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.user.GetPropW.restype = wintypes.HANDLE
        self.user.RemovePropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.user.RemovePropW.restype = wintypes.HANDLE
        self.user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = (self.enum_proc, wintypes.LPARAM)
        path = os.path.normcase(os.path.realpath(data_dir))
        key = "Local\\MarkTalk.GUI." + hashlib.sha256(path.encode("utf-8")).hexdigest()
        # 所有默认启动还共用固定 Mutex，避免不同位置的程序副本无参数双开。
        self.keys = (["Local\\MarkTalk.GUI.Default"] if default else []) + [key]
        self.handles = []
        self.existing = None
        self.hwnd = None
        self.app = None
        self.timer = None
        try:
            for key in self.keys:
                ctypes.set_last_error(0)
                handle = self.kernel.CreateMutexW(None, False, key)
                error = ctypes.get_last_error()
                if not handle:
                    raise ctypes.WinError(error)
                self.handles.append(handle)
                if error == 183:  # ERROR_ALREADY_EXISTS；句柄一直保留到进程退出。
                    self.existing = key
                    break
        except Exception:
            self.close()
            raise

    def activate_existing(self):
        from ctypes import wintypes
        target = []
        @self.enum_proc
        def find(hwnd, _):
            if self.user.GetPropW(hwnd, self.existing):
                target.append(hwnd)
                return False
            return True
        # 首个进程可能刚拿到 Mutex、尚未创建窗口，给它短暂注册时间。
        for _ in range(50):
            self.user.EnumWindows(find, 0)
            if target:
                hwnd = target[0]
                pid = wintypes.DWORD()
                self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self.user.AllowSetForegroundWindow(pid.value)
                self.user.SetPropW(hwnd, self.existing + ".activate", 1)
                self.user.ShowWindow(hwnd, 9)  # SW_RESTORE，同时恢复最小化/隐藏窗口。
                self.user.SetForegroundWindow(hwnd)
                return True
            time.sleep(.1)
        return False

    def attach(self, app):
        self.app = app
        app.update_idletasks()
        self.hwnd = self.user.GetAncestor(app.winfo_id(), 2)  # GA_ROOT：Tk 外层主窗口。
        for key in self.keys:
            if not self.user.SetPropW(self.hwnd, key, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        self._poll()

    def _poll(self):
        for key in self.keys:
            if self.user.GetPropW(self.hwnd, key + ".activate"):
                self.user.RemovePropW(self.hwnd, key + ".activate")
                self.app._restore_from_tray()  # 主线程恢复 Tk 状态及 _in_tray。
                self.user.SetForegroundWindow(self.hwnd)
        if not getattr(self.app, "_closing", False):
            self.timer = self.app.after(100, self._poll)

    def close(self):
        if self.timer is not None:
            try:
                self.app.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None
        if self.hwnd:
            for key in self.keys:
                self.user.RemovePropW(self.hwnd, key)
                self.user.RemovePropW(self.hwnd, key + ".activate")
            self.hwnd = None
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles.clear()


def _resource_path(name):
    """随程序打包的资源（如图标）：冻结后在临时解包目录，否则在脚本目录"""
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, name)

ICON_FILE = _resource_path("app.ico")
MEM_DIR_BASE = os.path.join(BASE, "memories")     # 记忆总根
# ★★ 记忆分级（隐私隔离）：
#   memories/cloud/    ← 公开话题，**云端引擎也能看到**
#   memories/private/  ← 私密内容，**只有本地引擎能看到**
#   MEM_DIR 是**当前生效的那一级**，切引擎时由 set_mem_tier() 重设 ✓
#   ★ 关键：云端模式 MEM_DIR **只**指向 cloud/ →
#     private/ 的内容**物理上不可能**进入发往云端的请求体 ✓
CLOUD_TIER = "cloud"
PRIVATE_TIER = "private"
MEM_DIR = os.path.join(MEM_DIR_BASE, PRIVATE_TIER)


def set_mem_tier(cloud):
    """按引擎切换记忆根。返回新的 MEM_DIR。"""
    global MEM_DIR
    MEM_DIR = os.path.join(MEM_DIR_BASE, CLOUD_TIER if cloud else PRIVATE_TIER)
    try:
        os.makedirs(MEM_DIR, exist_ok=True)
    except Exception:
        pass
    return MEM_DIR


def mem_roots():
    """当前**可见**的记忆根（顺序 = 查找优先级）。

    · **云端引擎 → 只有 cloud/** ✓（隔离的核心 ✓）
    · **本地引擎 → private/ 优先，其次 cloud/** ✓（本地不外泄，两边都能看到 ✓）
    """
    cloud = os.path.join(MEM_DIR_BASE, CLOUD_TIER)
    if os.path.abspath(MEM_DIR) == os.path.abspath(cloud):
        return [cloud]
    return [os.path.join(MEM_DIR_BASE, PRIVATE_TIER), cloud]


def is_memory_visible(name):
    """这个名字在当前引擎模式下可见吗（云端模式下 private/ 的不可见）"""
    for root in mem_roots():
        if os.path.isfile(os.path.join(root, name or "")):
            return True
    return False

# ---------- 双引擎：适配器 ----------
# GUI 不直接碰端口/私有端点，一律通过 ENGINE 适配器。
# engine_adapter.py 与主程序同目录；打包时被 PyInstaller 自动收进去。
try:
    import engine_adapter as EA
    _EA_OK = True
except Exception:
    EA = None
    _EA_OK = False

CONFIG_FILE = os.path.join(BASE, "config.json")
ENGINE = None            # 当前引擎适配器（init_connect 里探测后赋值）
ENGINE_KIND = None       # "llama" / "freetoken"
DEFAULT_MEM = "长对话记忆.md"
MAX_TURNS = 12            # 每 12 轮自动提炼
MAX_EST_TOKENS = 5500     # 或估算达到 5500 tokens 提前提炼
# 模型上下文预算（启动后由 detect_ctx_limit() 从服务端自动覆盖）
CTX_LIMIT = 8192          # 模型上下文上限（n_ctx）
RESERVE_OUTPUT = 2048     # 给回复预留（与请求 max_tokens 一致，否则输入吃满就没地方出字）
CTX_MARGIN = 320          # 安全余量
SYSTEM_MEM_BUDGET = 1800  # 系统提示词里「长期记忆」最多占多少 token（否则摘要会无限膨胀）
# ---- A. 分层摘要：把最老的一批摘要压缩成常驻的「开篇总纲」----
# 以前的做法是：摘要攒超预算 → 最老的**直接丢弃** → 模型完全不知道早期聊过什么。
# 现在改成：超预算的最老那批 → 让模型再压缩一次 → 存成「开篇总纲」永久保留。
# 于是"很久以前的事"从"彻底没有"变成"有梗概"，而平时每一轮的上下文大小完全不变。
META_BUDGET = 600         # 「开篇总纲」最多占多少 token（从 SYSTEM_MEM_BUDGET 里划出来）
META_MARK = "开篇总纲"     # 存进 md 的块标题标记
META_PROMPT = (
    "请把以上若干段对话摘要，合并压缩成一份**更精炼的开篇总纲**。要求：\n"
    "1. 按「主题 / 阶段」分条，不要写成时间流水账；\n"
    "2. 务必保留贯穿性的事实：人物、项目名、关键决定与结论、长期偏好、未完成的待办；\n"
    "3. 丢掉一次性的琐碎细节（某天具体问了什么）；\n"
    "4. 总长控制在 300 字以内，直接给总纲正文，不要客套话、不要标题。"
)
# ---- C. 回忆：按关键词去归档原文里检索相关旧内容 ----
RECALL_BUDGET = 1200      # 检索到的旧对话最多注入多少 token
RECALL_MIN_HITS = 2       # 至少要命中几个关键词才注入（防噪声）
SUMMARY_CHUNK_TOKENS = 1400  # 归档摘要按块切分，保证摘要请求自身不超限
IMAGE_TOKENS = 1200       # 单张图在视觉模型里大致占的 token（图片很占上下文，必须算进去）
RECONNECT_MS = 5000       # 连不上服务时的自动重试间隔
UI_PUMP_MS = 25           # 主线程 UI 队列泵的间隔（越小越顺滑，25ms ≈ 40 次/秒）
ATTACH_MAX_H = 132        # 附件栏最大高度（像素）；超出后纵向滚动，不挤压输入框
STREAM_FLUSH_MS = 110     # 流式文本写入界面的节流间隔。
# 为什么必须节流：wrap="word" 的 Text 每次往长行插入内容，都要把整行重新折行一次，
# 代价 ≈ 行长；插入 K 次的总代价 ≈ K × 行长 ÷ 2。
# 实测 3200 字时单次插入要 330ms，一次 3000 字回复按 80 次/秒写会累计十几秒主线程工作量，
# 直接把界面压死（点不动、拖不动、关不掉）。降到约 9 次/秒后总代价约降 9 倍。
# ★★ 身份噪音过滤器（**代码级兜底**）
#   背景：提示词里已经写了"不要记录 AI 对自己身份的说法" ✗
#   但**真机实测**（Qwen 3.8 8B）：摘要照样写出
#     "- 用户问候，AI 自报身份为本地部署的中文 AI 助手 MiMo（非 Qwen）。" ✗
#   → 小模型**不服从否定式指令** ✗ 光靠提示词靠不住 ✓
#   所以这里做**确定性**过滤：一句话里**同时**出现「AI 主语」和「身份词」就丢掉 ✓
#   （这类自我声明本来就是噪音 ✓ 而且会每轮注入 system 污染之后的对话 ✗）
# ★ 词表**只留"身份自曝场景"特有的词** ✓
#   为什么不放通用模型名（qwen / deepseek / gpt / llama …）✗ —— 会**误杀**：
#     用户经常跟模型聊"对比 deepseek 和 qwen""推荐个模型" ✓
#     那些句子里有 AI 主语（"AI 对比了…"）+ 模型名 ✗ → 会被整行删掉 ✗
#   而真正的身份自曝一定带下面这些**自述性**词 ✓（自称 / 本地部署 / 底层模型 …）
#   → 收紧后：`AI 自称是 MiMo` 仍被过滤 ✓ `AI 对比了 deepseek 和 qwen` 不再误杀 ✓
_IDENTITY_WORDS = re.compile(
    r"(?i)mimo|小米|"
    r"自报身份|自称为|自称是|"
    r"本地部署|本地运行|本地模型|"
    r"底层模型|模型身份|是什么模型|哪个模型|模型架构")
# ★ 别锚定行首 ✗ —— 真机那句是「用户问候，AI 自报身份为…」
#   AI 前面是**全角逗号**，用位置字符类会漏掉 ✗（实测只抓到一半）
#   只认 AI / assistant / 助手 这三种"明确的 AI 主语" ✓
#   → 「用户问模型是什么」这种**用户侧**的话不会被误杀 ✓
_AI_SUBJECT = re.compile(r"(?i)(\bai\b|assistant|助手)")


def sanitize_summary(text):
    """把摘要里"AI 对自己身份的说法/猜测"整行去掉 ✓（确定性，不依赖模型听话）

    保留「用户问了模型身份」这类事实行（没有 AI 主语 ✓）
    """
    if not text:
        return text
    kept, dropped = [], 0
    for line in str(text).splitlines():
        s = line.strip()
        if s and _IDENTITY_WORDS.search(s) and _AI_SUBJECT.search(s):
            dropped += 1
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    if dropped:
        try:
            print(f"[sanitize_summary] 丢掉 {dropped} 行身份噪音")
        except Exception:
            pass
    return out


SUMMARY_PROMPT = (
    "请把以上整个对话提炼成精炼的中文要点摘要。要求：分条列出，保留关键事实、决定、"
    "数字、人名、结论和未完成事项，每条一句话，不要客套话，不要复述寒暄。"
)
LOAD_N = 15  # 懒加载：每次渲染/向上加载的对话条数

# 记忆文件里的「来源标记」：终端版写 [终端]，图形版写 [软件]。
# 终端版和图形版共用同一批 memories\*.md，加这个标记就能看出某条是哪儿来的。
# 放在解析器**会跳过的行**上（以 ">" 开头的行、以及 "## " 归档标题的尾部），
# 所以两个程序的解析器都不用改，老记录（没有标记）也照常解析。
SRC_TAG = "软件"

MODEL_ID = None
MODELS = []
sessions = {}   # mem_name -> {"messages": [...], "turns": int}
busy = False    # 是否正在请求/归档


def model_size_rank(mid, path=None):
    """估算一个模型的"体量"（约等于参数量，单位 B），用于把列表按性能从低到高排。

    优先从**模型名**里读参数量（最可靠，例如 "Qwen 3.8 8B" → 8、"35B-A3B" → 35）；
    名字里没有就退而用 **GGUF 文件大小**换算（Q4 量化约 0.6 GB / B）。
    两者都判不出来时返回 None —— 调用方会把它排在后面并保持原有相对顺序。
    """
    m = re.findall(r"(\d+(?:\.\d+)?)\s*[Bb](?![a-zA-Z0-9])", str(mid))
    if m:
        try:
            return max(float(x) for x in m)
        except ValueError:
            pass
    if path:
        try:
            gb = os.path.getsize(path) / (1024.0 ** 3)
            if gb > 0:
                return gb / 0.6
        except Exception:
            pass
    return None


def sort_models_low_to_high(models, paths=None):
    """模型列表按体量**从低到高**排列，让最小的排在最前（默认就会选中它）。

    体量判不出来的模型排在最后，且彼此保持引擎返回的原始顺序
    （sorted 是稳定排序，所以相等项的相对次序不会乱）。
    """
    paths = paths or {}
    scored = []
    for i, mid in enumerate(models):
        r = model_size_rank(mid, paths.get(mid))
        scored.append((r is None, r if r is not None else 0.0, i, mid))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in scored]


def est_tokens(text):
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = max(0, len(text) - cjk)
    return cjk + other // 4

def est_content_tokens(content):
    """消息内容 token 估算：文字 + 图片都要算（图片极占上下文，漏算就会超限）"""
    if isinstance(content, str):
        return est_tokens(content)
    total = 0
    for seg in (content or []):
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "image_url":
            total += IMAGE_TOKENS
        else:
            total += est_tokens(str(seg.get("text") or ""))
    return total

def est_messages_tokens(msgs):
    return sum(est_content_tokens(m.get("content")) for m in msgs)

def input_budget():
    """单次请求允许的输入 token 上限（上下文 - 回复预留 - 余量）"""
    return max(512, CTX_LIMIT - RESERVE_OUTPUT - CTX_MARGIN)

def detect_ctx_limit():
    """模型上下文上限（n_ctx），拿不到返回 None"""
    return fetch_server_models()["ctx"]

def is_overflow_error(err):
    """判断是否是「上下文超限」类错误"""
    e = (err or "").lower()
    return ("exceed_context_size" in e or "exceeds the available context size" in e
            or "context size" in e or "n_prompt_tokens" in e)

def short_name(fname, maxlen=18):
    """文件名过长时中间省略（保留扩展名），如 2d47f7c8…49.jpg"""
    fname = os.path.basename(fname)
    if len(fname) <= maxlen:
        return fname
    stem, ext = os.path.splitext(fname)
    keep = maxlen - len(ext) - 1  # 留 1 个省略号
    if keep < 4:
        return fname[:maxlen]
    return stem[:keep] + "…" + ext

IMG_MD_RE = re.compile(r"!\[图片\]\(([^)]+)\)")

def archive_image(path):
    """把发送的图片复制到 memories/_images/，返回归档路径（重启后缩略图可恢复）"""
    try:
        arch_dir = os.path.join(MEM_DIR, "_images")
        os.makedirs(arch_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        arch_name = f"{ts}_{os.path.basename(path)}"
        arch_path = os.path.join(arch_dir, arch_name)
        shutil.copy(path, arch_path)
        return arch_path
    except Exception:
        return path

def fmt_mem_content(content):
    """消息内容 → md 归档文本（图片用 markdown 链接记录本地路径）"""
    if isinstance(content, str):
        return content
    parts = []
    for seg in content:
        if seg.get("type") == "text":
            parts.append(seg["text"])
        elif seg.get("type") == "image_url":
            p = seg.get("_path") or ""
            if p and p.startswith(MEM_DIR):
                rel = os.path.relpath(p, MEM_DIR).replace("\\", "/")
                parts.append(f"![图片]({rel})")
            else:
                parts.append(f"[图片] {short_name(seg.get('_name') or '')}")
    return "\n".join(parts)

def _tip(widget, text, delay=400, tag=None):
    """给控件加悬停提示：鼠标停 delay 毫秒后，在下方弹出一个小黄条。

    用 Toplevel + overrideredirect 实现（Tk 没有原生 tooltip）。
    bind 都带 add="+"，不会覆盖控件原有的 <Enter>/<Leave> 行为。
    tag 可指定 Text 的动作区域，历史与新消息共享同一套提示绑定。
    """
    st = {"win": None, "job": None}

    def _cancel():
        if st["job"]:
            try:
                widget.after_cancel(st["job"])
            except Exception:
                pass
            st["job"] = None

    def _hide():
        _cancel()
        if st["win"]:
            try:
                st["win"].destroy()
            except Exception:
                pass
            st["win"] = None

    def _show():
        st["job"] = None
        if st["win"]:
            return
        try:
            if tag is None:
                x = widget.winfo_rootx() + 10
                y = widget.winfo_rooty() + widget.winfo_height() + 6
            else:
                x, y = widget.winfo_pointerxy()
                y += 18  # Text 按钮的提示靠近所在区域，不放到整个聊天框下方。
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{x}+{y}")
            try:
                w.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(w, text=text, bg="#ffffe1", fg="#333333", relief="solid", bd=1,
                     justify="left", font=("Microsoft YaHei UI", 9),
                     padx=7, pady=4).pack()
            st["win"] = w
        except Exception:
            st["win"] = None

    def _enter(_e=None):
        _hide()
        try:
            st["job"] = widget.after(delay, _show)
        except Exception:
            st["job"] = None

    try:
        bind = widget.bind if tag is None else lambda seq, fn, add: widget.tag_bind(tag, seq, fn, add)
        bind("<Enter>", _enter, add="+")
        bind("<Leave>", lambda e: _hide(), add="+")
        bind("<ButtonPress>", lambda e: _hide(), add="+")
        widget.bind("<Destroy>", lambda e: _hide(), add="+")
        if tag is not None:
            for seq in ("<Leave>", "<MouseWheel>", "<ButtonPress>", "<FocusOut>", "<Unmap>"):
                widget.bind(seq, lambda e: _hide(), add="+")
    except Exception:
        pass


def is_arch_header(line):
    """这一行是不是「## 会话归档 …」块的标题行"""
    return line.startswith("## ") and line[3:].startswith("会话归档")


def extract_arch_blocks(text):
    """精确取出所有「## 会话归档 …」块（标题 + 正文 + 结尾的 ---）。

    ★ 这里绝不能写成 `for block in text.split("## ")`：
      split 出来的每一段会一直延伸到**下一个 "## " 之前**，于是紧跟在
      归档块后面的那些对话会被一起吞进这个"归档块"；下一步按 messages
      重写对话时又写了一遍 → **内容直接翻倍**（真实数据损坏 bug）。

    改成逐行扫描：遇到归档标题开始收集，遇到单独一行 `---` 结束。
    这与 append_mem() 写出的结构完全对应（`## 标题\\n正文\\n\\n---\\n`）。
    """
    blocks, cur = [], None
    for raw in text.splitlines():
        s = raw.rstrip()
        if cur is None:
            if is_arch_header(s):
                cur = [s]
        else:
            cur.append(s)
            if s.strip() == "---":
                blocks.append("\n".join(cur).strip())
                cur = None
    if cur:                                  # 没有结尾 --- 的残块也保留
        blocks.append("\n".join(cur).strip())
    return blocks


def rewrite_dialog_file(name, messages):
    """删除记录后重写 md：**完整保留归档摘要块**，按内存消息重建对话记录。

    ★ 必须"先把全部内容拼好、再一次性写入"：
      以前是先 open(...,"w") 把文件清空、再逐条 append；只要中途任何一条出问题
      （例如 user 消息的 content 是 list 时 fmt_mem_block 报
      'list' object has no attribute 'strip'），文件就已经被清空、内容全丢。
      现在先在内存里拼出全部文本，成功后才落盘，中途出错也不会毁掉原文件。
    """
    text = load_mem(name)
    arch_blocks = extract_arch_blocks(text)
    out = []
    for b in arch_blocks:
        out.append(b + "\n")
    for m in messages:
        if m.get("role") == "system":
            continue
        try:
            if m["role"] == "user":
                blk = fmt_mem_block("**Q：", m.get("content"))
                first, _, rest = blk.partition("\n")
                blk = first + "**" + (("\n" + rest) if rest else "")
            else:
                blk = fmt_mem_block("A：", m.get("content"))
        except Exception:
            continue                      # 单条出问题就跳过它，绝不让整份文件受损
        if not blk:
            continue
        # 重写时时间戳本来就拿不到了（messages 里没存），但来源是确定的：
        # 走这条路的记录一定来自图形界面，所以补一个来源行
        out.append(f"\n> [{SRC_TAG}]\n{blk}\n")
    with open(mem_path(name), "w", encoding="utf-8") as f:
        f.write("".join(out))


def clear_dialog_keep_arch(name):
    """只清对话记录，**保留全部归档摘要**（长期记忆不动）。
    返回 (清掉的对话条数, 保留的归档块数)"""
    pairs, _ = parse_dialogues(name)
    arch_blocks = extract_arch_blocks(load_mem(name))
    with open(mem_path(name), "w", encoding="utf-8") as f:
        f.write("")
    if arch_blocks:
        with open(mem_path(name), "a", encoding="utf-8") as f:
            for b in arch_blocks:
                f.write(b + "\n")
    return len(pairs), len(arch_blocks)


# ---------------------------------------------------------------- 归档隐藏区
# 归档 = 把这段对话**原文完整搬进隐藏区**（memories\_archived\<记忆名>\），
# 同时让模型提炼一份摘要留在 .md 里当长期记忆。归档后原文从聊天区隐掉，
# 只在左下角「本地用户 · 归档」面板里能看到；删除/清空也只对隐藏区里的归档做。
ARCHIVE_DIR = "_arch_batches"     # 自动归档的**原文批次**（每批一个文件）
HIDDEN_DIR = "_archived"          # **归档（隐藏）的记忆文件**放这里

def arch_dir(name):
    """某个记忆文件的归档隐藏区目录"""
    return os.path.join(MEM_DIR, ARCHIVE_DIR, os.path.splitext(name)[0])


def arch_save_batch(name, ts, pairs, summary):
    # ★ 落盘前**必过滤**（真机实测 8B 会把身份猜测记进摘要 ✗）
    summary = sanitize_summary(summary)
    """把一批归档（原文 + 摘要）完整存进隐藏区，返回文件路径。

    pairs: [(role, content), ...] —— 归档那一刻的对话原文，一行不丢。
    """
    d = arch_dir(name)
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_]", "", ts.replace(":", "").replace("-", "").replace(" ", "_"))
    path = os.path.join(d, f"{safe}_{len(pairs)}条.md")
    lines = [
        f"# 归档原文 · {name}",
        "",
        f"- 归档时间：{ts}",
        f"- 来源：{SRC_TAG}",
        f"- 对话条数：{len(pairs)}",
        "",
        "## 摘要",
        (summary or "").strip(),
        "",
        "## 原文",
        "",
    ]
    for role, content in pairs:
        blk = fmt_mem_block("**Q：" if role == "user" else "A：", content)
        if not blk:
            continue
        if role == "user":
            first, _, rest = blk.partition("\n")
            blk = first + "**" + (("\n" + rest) if rest else "")
        lines.append(blk)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def arch_list_batches(name):
    """列出该记忆的归档批次（**新的在前**），
    返回 [{ts, src, path, count, summary, name}]"""
    d = arch_dir(name)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d), reverse=True):
        if not fn.lower().endswith(".md"):
            continue
        p = os.path.join(d, fn)
        try:
            t = open(p, "r", encoding="utf-8").read()
        except Exception:
            continue
        ts = src = ""
        cnt = 0
        summ = ""
        mo = re.search(r"归档时间：(.+)", t)
        if mo:
            ts = mo.group(1).strip()
        mo = re.search(r"来源：(.+)", t)
        if mo:
            src = mo.group(1).strip()
        mo = re.search(r"对话条数：(\d+)", t)
        if mo:
            cnt = int(mo.group(1))
        mo = re.search(r"## 摘要\s*\n(.*?)\n## 原文", t, re.S)
        if mo:
            summ = mo.group(1).strip()
        out.append({"ts": ts, "src": src, "path": p, "count": cnt,
                    "summary": summ, "name": fn})
    return out


def arch_batch_body(path):
    """取某批次归档的原文（面板里显示这个）"""
    try:
        t = open(path, "r", encoding="utf-8").read()
    except Exception:
        return ""
    mo = re.search(r"## 原文\s*\n(.*)", t, re.S)
    return (mo.group(1).strip() if mo else t.strip())


def arch_delete_batch(path):
    """删除一个归档批次（隐藏区文件）；顺带清掉空的目录"""
    try:
        if os.path.isfile(path):
            os.remove(path)
        d = os.path.dirname(path)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
        return True, None
    except Exception as e:
        return False, str(e)


def arch_delete_all(name):
    """删除该记忆的全部归档批次，返回删掉的个数"""
    d = arch_dir(name)
    n = 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            try:
                os.remove(os.path.join(d, fn))
                n += 1
            except Exception:
                pass
        try:
            os.rmdir(d)
        except Exception:
            pass
    return n


def remove_arch_block(name, ts):
    """从 .md 里移除某个时间戳对应的 ## 会话归档 摘要块（配合删除归档批次）"""
    text = load_mem(name)
    blocks = extract_arch_blocks(text)
    keep = [b for b in blocks if ts and ts not in b.splitlines()[0]]
    if len(keep) == len(blocks):
        return 0
    with open(mem_path(name), "w", encoding="utf-8") as f:
        f.write("")
    if keep:
        with open(mem_path(name), "a", encoding="utf-8") as f:
            for b in keep:
                f.write(b + "\n")
    return len(blocks) - len(keep)


def arch_import_blocks(name):
    """把 .md 里的**老式** `## 会话归档` 摘要块导入隐藏区（改造前的归档只有摘要，
    没有隐藏区批次，所以面板里看不到）。原文当时已丢，这里只搬摘要。
    返回导入条数。"""
    blocks = extract_arch_blocks(load_mem(name))
    d = arch_dir(name)
    os.makedirs(d, exist_ok=True)
    n = 0
    for i, b in enumerate(blocks, 1):
        head = b.splitlines()[0]
        mo = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", head)
        ts = mo.group(1) if mo else "(时间未知)"
        safe = re.sub(r"[^0-9A-Za-z_]", "", ts.replace(":", "").replace("-", "").replace(" ", "_"))
        path = os.path.join(d, f"{safe}_旧归档{i}.md")
        try:
            lines = [f"# 归档原文 · {name}", "",
                     f"- 归档时间：{ts}", f"- 来源：{SRC_TAG}",
                     "- 对话条数：0", "- 备注：改造前的老归档，只留了摘要（原文已不在）",
                     "", "## 摘要", b, "", "## 原文", "",
                     "（这批是改造前的老归档，原文当时没有单独保存）"]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            n += 1
        except Exception:
            pass
    return n

class ToolTip:
    """简单的悬浮提示（鼠标停在控件上显示说明）"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        if self.tip or not self.text:
            return
        x, y = self.widget.winfo_rootx() + 8, self.widget.winfo_rooty() + 20
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#2b2b2b", fg="#ffffff",
                 font=("Microsoft YaHei UI", 9), padx=8, pady=4).pack()

    def hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

def get_models():
    """当前引擎的模型 id 列表"""
    try:
        return list((ENGINE.fetch_models() or {}).get("ids") or []) if ENGINE else []
    except Exception:
        return []

def check_server():
    """服务是否可用（走当前引擎适配器；未探测到引擎时为 False）"""
    try:
        return bool(ENGINE and ENGINE.health())
    except Exception:
        return False

# ---------- 模型状态 / 显存释放 / 选择记忆 ----------
SETTINGS_FILE = os.path.join(BASE, "gui_settings.json")
MODEL_POLL_MS = 5000    # 正在运行模型的状态轮询间隔

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_EMPTY_INFO = {"ids": [], "paths": {}, "ctx": None, "running": None,
               "running_state": None, "ok": False}

def get_model_status():
    """返回 [(id, status), ...]；服务不可达时返回 None（区别于「可达但没有模型」的空列表）"""
    try:
        return ENGINE.model_status() if ENGINE else None
    except Exception:
        return None

# 动态发现结果缓存 —— 模型面板每次打开都重扫太慢（~1.6 秒），缓存 20 秒。
# 手动点「重新扫描」时传 max_age=0 强制刷新。
_DISCOVER_CACHE = {"t": 0.0, "data": []}


def _is_chat_model(name):
    """判断模型名是否是**对话模型**（排除 ASR / TTS / 嵌入 / 重排等非对话变体）。

    云端 API 的 /v1/models 常常把所有变体都列出来 ✗
    对话界面只需要能聊天的模型 ✓（用户反馈："Harness 就没有那么多后缀"）
    """
    n = str(name).lower()
    # 非对话模型的关键词（后缀/名称里含这些就不该出现在对话模型列表里）
    _non_chat = ("-asr", "-tts", "voiceclone", "voicedesign",
                 "-embed", "-rerank", "-speech", "-audio", "-whisper",
                 "-vl", "-vision")   # ← -vl/-vision 只是视觉增强，**能聊天** ✓ 放行
    # 注意：-vl / -vision 是多模态对话模型，能聊天 → 不过滤 ✓
    _block = ("-asr", "-tts", "voiceclone", "voicedesign",
              "-embed", "-rerank", "-speech", "-audio", "-whisper")
    return not any(x in n for x in _block)

def backup_config(keep=3):
    """写 config.json 前调它：轮转备份 .bak → .bak1 → .bak2 …

    ★ 单份 .bak 不够 —— 真出过"云端地址被覆盖，而 .bak 也已经是坏的"事故 ✗
    """
    try:
        if not os.path.exists(CONFIG_FILE):
            return
        for i in range(keep - 1, 0, -1):
            src = CONFIG_FILE + (".bak" if i == 1 else f".bak{i-1}")
            dst = CONFIG_FILE + f".bak{i}"
            if os.path.exists(src):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
        shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
    except Exception:
        pass


def discover_engines_cached(max_age=20.0):
    """带缓存的动态发现。失败时退回上次的结果（绝不抛异常打断界面）。"""
    now = time.time()
    if _DISCOVER_CACHE["data"] and (now - _DISCOVER_CACHE["t"]) < max_age:
        return _DISCOVER_CACHE["data"]
    data = None
    try:
        data = EA.discover_engines()
    except Exception:
        data = None
    if data is None:
        return _DISCOVER_CACHE["data"]
    _DISCOVER_CACHE["t"] = now
    _DISCOVER_CACHE["data"] = data
    return data


def running_model():
    """当前正在运行的模型 (id, status, 服务可达)。没有模型在跑时 id/status 为 None"""
    try:
        return ENGINE.running_model() if ENGINE else (None, None, False)
    except Exception:
        return None, None, False

def unload_model(mid):
    """停止/卸载模型以释放显存。返回 (ok, err)
    llama = 调 /models/unload；FreeToken = 结束 ft serve 进程"""
    if not mid:
        return False, "空模型名"
    if not ENGINE:
        return False, "尚未探测到引擎"
    try:
        return ENGINE.unload_model(mid)
    except Exception as e:
        return False, str(e)

def load_model(mid, timeout=300):
    """按需加载模型。返回 (ok, err)
    llama = 热加载；FreeToken = 重启 ft serve（首次可能要几分钟）"""
    if not mid:
        return False, "空模型名"
    if not ENGINE:
        return False, "尚未探测到引擎"
    try:
        return ENGINE.load_model(mid, timeout)
    except Exception as e:
        return False, str(e)

def fetch_server_models(timeout=6):
    """一次拿全：模型列表 + 路径 + 上下文上限 + 正在运行的模型。
    由适配器实现：llama 走 /v1/models 的扩展字段；FreeToken 走目录扫描 + /v1/models。"""
    if not ENGINE:
        return dict(_EMPTY_INFO)
    try:
        return ENGINE.fetch_models(timeout)
    except Exception:
        return dict(_EMPTY_INFO)

def get_model_paths():
    """模型 → 路径（llama 是 GGUF 文件；FreeToken 是 HF 模型目录），用于扫描推理能力"""
    return fetch_server_models()["paths"]

def _read_gguf_chat_template(path, max_mb=200):
    """分块扫描 GGUF，定位 tokenizer.chat_template 并返回模板文本（找不到返回 None）。
    不写死任何模型名——直接读模型自己的对话模板。"""
    if not path or not os.path.exists(path):
        return None
    needle = b"tokenizer.chat_template"
    chunk = 8 * 1024 * 1024
    try:
        total = os.path.getsize(path)
    except Exception:
        return None
    off, carry = 0, b""
    limit = min(total, max_mb * 1024 * 1024)
    while off < limit:
        try:
            with open(path, "rb") as f:
                f.seek(off)
                buf = carry + f.read(chunk)
        except Exception:
            return None
        i = buf.find(needle)
        if i >= 0:
            return buf[i:i + 24000].decode("utf-8", "ignore")
        carry = buf[-len(needle):]
        off += chunk
    return None

def scan_model_reasoning(path):
    """自动扫描模型模板，识别推理能力（不写死）：
       返回 {"thinking": bool, "efforts": [值...], "default": 默认强度 or None}
       交给适配器：llama 读 GGUF 文件头；FreeToken 读 HF 目录的 config.json / chat_template.jinja"""
    if ENGINE:
        try:
            return ENGINE.scan_reasoning(path)
        except Exception:
            pass
    # 兜底：适配器不可用时，仍按 GGUF 本地识别（保证 llama 老行为不丢）
    try:
        return EA.caps_from_template(_read_gguf_chat_template(path))
    except Exception:
        return {"thinking": False, "efforts": [], "default": None}

# GUI 当前选择的推理参数（会作为 chat_template_kwargs 发给服务端）
REASONING_KWARGS = {}

# 请求体构造已下沉到 engine_adapter.py（两个引擎共用，带推理参数时走 chat_template_kwargs）

def api(messages, timeout=900, stop_event=None):
    """非流式请求（走当前引擎适配器）"""
    if not ENGINE:
        return None, ("尚未探测到引擎：请先启动推理服务"
                      "（llama.cpp 8080 / FreeToken 8000 / Ollama 11434 / LM Studio 1234 等，"
                      "程序会自动发现）")
    try:
        if stop_event is not None:
            return ENGINE.chat(messages, MODEL_ID, REASONING_KWARGS, timeout, stop_event=stop_event)
        return ENGINE.chat(messages, MODEL_ID, REASONING_KWARGS, timeout)
    except Exception as e:
        return None, str(e)

def api_stream(messages, stop_event, on_delta, timeout=900):
    """流式请求：逐字回调 on_delta(str)；stop_event 置位则中断。返回 (完整文本, err)"""
    if not ENGINE:
        return None, ("尚未探测到引擎：请先启动推理服务"
                      "（llama.cpp 8080 / FreeToken 8000 / Ollama 11434 / LM Studio 1234 等，"
                      "程序会自动发现）")
    try:
        return ENGINE.chat_stream(messages, MODEL_ID, REASONING_KWARGS,
                                  stop_event, on_delta, timeout)
    except Exception as e:
        return None, str(e)

def hidden_mem_dir():
    """「归档（隐藏）」的记忆文件存放目录"""
    return os.path.join(MEM_DIR, HIDDEN_DIR)

def is_hidden_mem(name):
    """这个记忆文件是否已被归档（藏进了隐藏区）"""
    try:
        return os.path.isfile(os.path.join(hidden_mem_dir(), name))
    except Exception:
        return False

# ================= 本地控制接口（C · Agent 化的地基）=================
# 设计死规矩（方案 v1.1 §C 安全三件套）：
#   ① 只绑 127.0.0.1 —— 外网/局域网都进不来
#   ② 每个请求必须带 X-MarkTalk-Token（token 存 control_token.dat，DPAPI 加密，已 gitignore）
#   ③ 带 http(s)://网页域名 Origin 的请求直接拒绝
#      （防浏览器里打开的恶意网页跨端调本地接口 —— localhost 服务的经典坑）
CONTROL_FILE = os.path.join(BASE, "control_token.dat")
CONTROL_DEFAULT_PORT = 8765


def load_control_token():
    """读回控制接口 token（DPAPI 加密存 control_token.dat）；没有就生成一个并存下 ✓"""
    try:
        if os.path.isfile(CONTROL_FILE):
            with open(CONTROL_FILE, "rb") as f:
                tok = (EA.dpapi_decrypt(f.read()) or "").strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        with open(CONTROL_FILE, "wb") as f:
            f.write(EA.dpapi_encrypt(tok))
        try:
            os.chmod(CONTROL_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return tok


class ControlServer:
    """MarkTalk 的本地控制接口。

    ★★ 铁律：HTTP 跑在**后台线程** ✗ —— 任何 GUI 动作都必须经 `app._ui_call(fn)`
       投到主线程执行 ✓（后台线程直接碰 Tk 会静默失败 / 随机闪退，见接续文档坑 12）

    端点（都用 POST/GET + JSON）：
      GET  /chats   列出所有记忆文件
      GET  /chat    读**当前对话**全文（可加 ?name=xxx.md 读别的会话）
      GET  /status  当前引擎 / 模型 / 对话 / 是否在生成 / 最近一次回复
      POST /send    发消息（**默认非阻塞**，返回 task_id；结果看 /status）
      POST /switch  切引擎（llama/freetoken/openai/local/auto）+ 切对话 + 切模型
      POST /stop    中断生成（尽力而为）
    """

    def __init__(self, app, host="127.0.0.1", port=None, token=None, web=False):
        self.app = app
        self.host = host
        self.web = web
        self.token = token or (secrets.token_urlsafe(32) if web else load_control_token())
        self.port = int(port or CONTROL_DEFAULT_PORT)
        self.httpd = None
        self.thread = None
        self._lock = threading.Lock()
        # ★ /send 非阻塞：只记 task_id 与最近一次回复，供 /status 查询 ✓
        self.task_seq = 0
        self.last_task = None
        self.last_reply = None
        self.last_error = None

    # ---------- 生命周期 ----------
    def start(self):
        if self.web:
            return self._start_web()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass                                  # 别往控制台刷日志

            # ---- 工具 ----
            def _json(self, obj, code=200):
                b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except Exception:
                    pass

            def _body(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    return json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    return {}

            def _guard(self):
                """安全三件套的②③。返回 None 表示放行，否则返回错误码。"""
                # ③ 拒网页 Origin（浏览器跨端调用）
                origin = (self.headers.get("Origin") or "").strip().lower()
                if origin.startswith("http://") or origin.startswith("https://"):
                    self._json({"ok": False, "error": "拒绝来自网页的请求（Origin 校验）"}, 403)
                    return 403
                # ③ 顺带校验 Host（必须指向本机）
                host = (self.headers.get("Host") or "").strip().lower()
                if host and not (host.startswith("127.0.0.1") or host.startswith("localhost")
                                 or host.startswith("[::1]")):
                    self._json({"ok": False, "error": "Host 不是本机"}, 403)
                    return 403
                # ② token
                tok = (self.headers.get("X-MarkTalk-Token") or "").strip()
                if not tok or not secrets.compare_digest(tok, srv.token):
                    self._json({"ok": False, "error": "缺少或错误的 X-MarkTalk-Token"}, 401)
                    return 401
                return None

            # ---- 路由 ----
            def do_GET(self):
                if self._guard():
                    return
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path == "/status":
                    self._json(srv.status())
                elif path == "/chats":
                    self._json(srv.chats())
                elif path == "/chat" or path.startswith("/chat/"):
                    # ★ 传**原始** self.path —— 上面已经把 query 剥掉了 ✗
                    #   再传 path 的话 chat() 里 "?" in path 永远 False → name= 静默失效 ✗
                    self._json(srv.chat(self.path))
                elif path == "/":
                    self._json({"ok": True, "service": "MarkTalk control",
                                "endpoints": ["/status", "/chats", "/send", "/switch", "/stop"]})
                else:
                    self._json({"ok": False, "error": "未知端点"}, 404)

            def do_POST(self):
                if self._guard():
                    return
                path = self.path.split("?")[0].rstrip("/") or "/"
                body = self._body()
                if path == "/send":
                    self._json(srv.send(body))
                elif path == "/switch":
                    self._json(srv.switch(body))
                elif path == "/stop":
                    self._json(srv.stop())
                else:
                    self._json({"ok": False, "error": "未知端点"}, 404)

        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            self.httpd.daemon_threads = True
        except Exception as e:
            return False, f"控制接口启动失败（端口 {self.port}）：{e}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return True, None

    def _start_web(self):
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port),
                          make_web_handler(self, _resource_path("mobile.html")))
            self.httpd.daemon_threads = True
        except Exception as e:
            return False, f"手机 Web 启动失败（端口 {self.port}）：{e}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return True, None

    def web_chat(self):
        def read():
            name = self.app.current_mem
            if not name or name not in list_mems() or not is_memory_visible(name):
                return {"ok": False, "error": "当前会话不可见或不存在"}
            return {"ok": True, "name": name, "text": load_mem(name)}
        return self._call(read)

    def web_status(self):
        # GUI 用模块级 busy；C 的历史 status 实现保持原样。
        return self._call(lambda: {"ok": True, "engine_kind": ENGINE_KIND,
                         "model": MODEL_ID, "chat": self.app.current_mem,
                         "busy": bool(busy), "memory_tier": os.path.basename(MEM_DIR)})

    def web_switch(self, body):
        def switch_chat():
            name = body["chat"]
            if busy:
                return {"ok": False, "error": "生成期间不能切换会话，请先停止"}
            if name not in list_mems() or not is_memory_visible(name):
                return {"ok": False, "error": "会话不可见或不存在"}
            self.app.select_mem(name)
            return {"ok": True, "chat": self.app.current_mem}
        return self._call(switch_chat)

    def web_send(self, body):
        def send_text():
            app = self.app
            name = app.current_mem
            if not name or name not in list_mems() or not is_memory_visible(name):
                return {"ok": False, "error": "请先在电脑端选择可见会话"}
            if not MODEL_ID:
                return {"ok": False, "error": "电脑端尚未选择模型"}
            # 手机不能覆盖电脑草稿、提交电脑编辑或夹带电脑待发送的附件。
            if app._edit_hidx is not None or app.pending_images or app.pending_files or app.get_input_text():
                return {"ok": False, "error": "电脑端有草稿、附件或编辑，请先处理"}
            app.entry.insert("1.0", body["text"])
            app.send_text()
            self.task_seq += 1
            self.last_task = f"w{self.task_seq}"
            return {"ok": True, "task_id": self.last_task}
        return self._call(send_text)

    def shutdown(self):
        """关闭 HTTP 服务（★ 注意：端点用的那个叫 stop()，别撞名 ✗）"""
        try:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    # ---------- 各端点实现（★ 全部经 _ui_call 回主线程）----------
    def _call(self, fn, timeout=15.0):
        """把动作投到主线程执行并等结果（HTTP 线程阻塞等 ✓ 主线程不阻塞 ✓）"""
        app = self.app
        done = threading.Event()
        box = {}

        def wrapped():
            try:
                box["r"] = fn()
            except Exception as e:
                box["e"] = e
            finally:
                done.set()

        try:
            app._ui_call(wrapped)
        except Exception as e:
            return {"ok": False, "error": f"投递主线程失败：{e}"}
        if not done.wait(timeout):
            return {"ok": False, "error": "等待主线程超时（GUI 可能在忙）"}
        if "e" in box:
            return {"ok": False, "error": str(box["e"])}
        return box.get("r")

    def status(self):
        def snap():
            try:
                cloud = bool(ENGINE and ENGINE.is_cloud())
            except Exception:
                cloud = False
            try:
                tier = os.path.basename(MEM_DIR)
            except Exception:
                tier = ""
            return {"ok": True,
                    "engine_kind": ENGINE_KIND,
                    "engine": (ENGINE.base_url if ENGINE else None),
                    "cloud": cloud,
                    "model": MODEL_ID,
                    "chat": self.app.current_mem,
                    "busy": bool(busy),
                    "memory_tier": tier,
                    "task_id": self.last_task,
                    "last_reply": self.last_reply,
                    "last_error": self.last_error}
        return self._call(snap)

    def chats(self):
        def snap():
            try:
                cur = self.app.current_mem
            except Exception:
                cur = None
            return {"ok": True, "current": cur, "chats": list_mems()}
        return self._call(snap)

    def chat(self, path=""):
        """读会话全文（默认当前会话）。query 支持 ?name=xxx.md"""
        name = ""
        try:
            if "?" in path:
                q = path.split("?", 1)[1]
                for kv in q.split("&"):
                    if kv.startswith("name="):
                        # ★ unquote 在 urllib.parse 里 ✗ 写成 urllib.request.unquote 会 AttributeError
                        #   被 except 吞掉 → name 参数静默失效 ✗（实测踩到）
                        name = urllib.parse.unquote(kv[5:])
        except Exception:
            name = ""

        def snap():
            nm = name or self.app.current_mem
            if not nm:
                return {"ok": False, "error": "当前没有打开会话"}
            # ★ 区分文件不存在和存在但当前模式看不见 —— 提示才有用 ✓
            if not any(os.path.isfile(os.path.join(r, nm)) for r in mem_roots()):
                _all = []
                for _r in (os.path.join(MEM_DIR_BASE, PRIVATE_TIER),
                           os.path.join(MEM_DIR_BASE, CLOUD_TIER)):
                    if os.path.isdir(_r):
                        _all += [f for f in os.listdir(_r) if f.endswith(".md")]
                _hidden = nm in _all
                return {"ok": False,
                        "error": (f"{nm} 存在，但在当前引擎模式下不可见（属于另一级记忆）"
                                   if _hidden else f"找不到会话 {nm}"),
                        "hint": "用 GET /chats 看当前可见的会话"}
            return {"ok": True, "name": nm, "tier": os.path.basename(MEM_DIR),
                    "text": load_mem(nm)}
        return self._call(snap)

    def switch(self, body):
        """切引擎 / 切对话 / 切模型（定案：必须支持切引擎 ✓）"""
        eng = str((body or {}).get("engine") or "").strip().lower()
        chat = str((body or {}).get("chat") or "").strip()
        model = str((body or {}).get("model") or "").strip()
        if not (eng or chat or model):
            return {"ok": False, "error": "至少要给 engine / chat / model 之一"}

        def do():
            msgs = []
            if eng:
                if eng not in ("auto", "llama", "freetoken", "openai", "local"):
                    return {"ok": False, "error": f"未知 engine：{eng}"}
                backup_config()
                c = EA.load_config(CONFIG_FILE)
                c["engine"] = eng
                EA.save_config(CONFIG_FILE, c)
                try:
                    self.app._engine_override = None
                except Exception:
                    pass
                self.app.init_connect(silent=True)      # ★ 会重探引擎 + 刷新模型 + 切记忆分级
                msgs.append("engine=" + eng)
            if chat:
                if not is_memory_visible(chat):
                    return {"ok": False,
                            "error": f"会话 {chat} 在当前引擎模式下不可见（可能是 private/ 的）",
                            "hint": "切回本地引擎再看，或改用 /chats 列出的名字"}
                self.app.select_mem(chat)
                msgs.append("chat=" + chat)
            if model:
                if model not in (self.app.model_choices or []):
                    self.app.model_choices = list(self.app.model_choices or []) + [model]
                self.app._pick_model(model)
                msgs.append("model=" + model)
            return {"ok": True, "done": msgs,
                    "engine_kind": ENGINE_KIND, "model": MODEL_ID,
                    "chat": self.app.current_mem}
        return self._call(do, timeout=90.0)

    def send(self, body):
        """发消息 —— **非阻塞**：立刻返回 task_id ✓ 结果看 /status ✓"""
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text 为空"}
        wait = bool((body or {}).get("wait"))
        before_len = [0]
        with self._lock:
            self.task_seq += 1
            tid = f"t{self.task_seq}"
            self.last_task = tid
            self.last_reply = None
            self.last_error = None

        def do():
            try:
                app = self.app
                if chat := app.current_mem:
                    if not is_memory_visible(chat):
                        return {"ok": False, "error": "当前会话在当前引擎模式下不可见"}
                # ★ 记下发送前聊天区的长度 —— 监听线程靠"变长"判断回复到了 ✓
                #   （比等 busy 可靠：busy 的置位/复位时机受排队影响 ✗）
                try:
                    before_len[0] = len(app.chat.get("1.0", "end"))
                except Exception:
                    before_len[0] = 0
                app.entry.delete("1.0", "end")
                app.entry.insert("1.0", text)
                app.send_text()
                return {"ok": True, "task_id": tid}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        r = self._call(do)
        if not r or not r.get("ok"):
            return r or {"ok": False, "error": "投递失败"}

        if wait:
            # 可选：阻塞等到生成结束（最长 300 秒）✓
            t0 = time.time()
            while time.time() - t0 < 300:
                st = self.status()
                if st.get("ok") and not st.get("busy"):
                    break
                time.sleep(0.5)
            return dict(r, waited=True, status=self.status())

        # 非阻塞：起一个观察线程，生成结束时把回复记到 last_reply ✓
        def watch():
            try:
                # ★ 判断依据：**聊天区文本变长了** ✓
                #   比看 busy 可靠（busy 的置位/复位受排队影响 ✗；文本变长是最终事实 ✓）
                t0 = time.time()
                grew = False
                while time.time() - t0 < 900:
                    time.sleep(0.4)
                    try:
                        cur_len = len(self.app.chat.get("1.0", "end"))
                    except Exception:
                        cur_len = 0
                    if cur_len > before_len[0] + 2:
                        grew = True
                        break
                if not grew:
                    self.last_error = "等回复超时"
                    return
                # 从聊天区末尾抓最后一段助手回复（尽力而为 ✓）
                def grab():
                    try:
                        txt = self.app.chat.get("1.0", "end")
                    except Exception:
                        return None
                    blocks = [b.strip() for b in re.split(r"\n—\s.*?\s—\n", txt) if b.strip()]
                    return blocks[-1][:4000] if blocks else None
                self.last_reply = self._call(grab, timeout=10)
            except Exception as e:
                self.last_error = str(e)
        threading.Thread(target=watch, daemon=True).start()
        return dict(r, note="非阻塞：用 GET /status 查 busy 与 last_reply")

    def stop(self):
        def do():
            try:
                self.app.stop_gen()
                return {"ok": True, "stopped": True,
                        "note": "已置中断标志（流式生成可中断；摘要类请求不响应）"}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return self._call(do)


CONTROL = None          # 全局：ControlServer 实例（或 None）


def start_control_server(app, cfg=None):
    """按 config 启动控制接口。返回 (ok, err)"""
    global CONTROL
    try:
        sec = dict((cfg or {}).get("control") or {})
    except Exception:
        sec = {}
    if not sec.get("enabled", True):
        return False, "控制接口在 config 里被关掉了（control.enabled=false）"
    try:
        port = int(sec.get("port") or CONTROL_DEFAULT_PORT)
    except Exception:
        port = CONTROL_DEFAULT_PORT
    CONTROL = ControlServer(app, port=port)
    ok, err = CONTROL.start()
    if not ok:
        CONTROL = None
    return ok, err


def mem_path(name):
    """记忆文件路径：已归档的在隐藏区，其余在 memories\\ 根目录。

    ★ 全程序都走这里取路径，所以"归档 = 把文件挪进隐藏区"对上层完全透明。
    """
    if is_hidden_mem(name):
        return os.path.join(hidden_mem_dir(), name)
    # ★ 分级：先在本级找，找不到再看另一级（云端模式下另一级不在 mem_roots 里 ✓）
    for root in mem_roots():
        p = os.path.join(root, name)
        if os.path.isfile(p):
            return p
    return os.path.join(MEM_DIR, name)          # 新建 → 落在当前级

def list_hidden_mems():
    """已归档（隐藏）的记忆文件列表"""
    d = hidden_mem_dir()
    if not os.path.isdir(d):
        return []
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.md")))

def hide_mem(name):
    """把记忆文件归档（隐藏）：挪进隐藏区。返回 (ok, err)"""
    src = os.path.join(MEM_DIR, name)
    if not os.path.isfile(src):
        return False, "文件不存在"
    dst_dir = hidden_mem_dir()
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    if os.path.exists(dst):
        return False, "隐藏区里已经有同名文件"
    try:
        shutil.move(src, dst)
        return True, None
    except Exception as e:
        return False, str(e)

def unhide_mem(name):
    """取消归档：从隐藏区挪回 memories\\ 根目录。返回 (ok, err)"""
    src = os.path.join(hidden_mem_dir(), name)
    if not os.path.isfile(src):
        return False, "隐藏区里没有这个文件"
    dst = os.path.join(MEM_DIR, name)
    if os.path.exists(dst):
        return False, "根目录里已经有同名文件"
    try:
        shutil.move(src, dst)
        return True, None
    except Exception as e:
        return False, str(e)

def load_mem(name):
    p = mem_path(name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

def append_mem(name, title, content):
    with open(mem_path(name), "a", encoding="utf-8") as f:
        f.write(f"\n## {title} [{SRC_TAG}]\n{content}\n\n---\n")

def content_to_text(content):
    """把消息 content 统一转成文本。

    ★ 很关键：`s["messages"]` 里 **user 消息的 content 可能是 list**
      （带图片/文件时是 [{type:"text",...}, {type:"image_url",...}]）。
      以前 fmt_mem_block 直接 `.strip()`，遇到 list 就报
      `'list' object has no attribute 'strip'`；而"删除记录"会先清空文件再重写，
      崩在中途就把整个记忆文件清空了（用户反馈"发送和回复没写进 md"就是这个）。
    """
    if isinstance(content, str):
        return content
    if not content:
        return ""
    parts = []
    for seg in content:
        if not isinstance(seg, dict):
            parts.append(str(seg))
            continue
        t = seg.get("type")
        if t == "text":
            parts.append(seg.get("text") or "")
        elif t == "image_url":
            p = seg.get("_path") or ""
            if p and p.startswith(MEM_DIR):
                try:
                    rel = os.path.relpath(p, MEM_DIR).replace("\\", "/")
                    parts.append(f"![图片]({rel})")
                    continue
                except Exception:
                    pass
            nm = seg.get("_name") or ""
            parts.append(f"[图片] {short_name(nm)}" if nm else "[图片]")
    return "\n".join(parts)


def fmt_mem_block(prefix, content):
    """把一条消息格式化成 md 里的块（**保留换行**）。
    多行内容用「缩进两格的续行」保存：解析时靠**行首空白**区分"续行"和"新消息标记"，
    这样 AI 输出里的 `## 标题` / `A：` 之类不会被误判成新消息。
    旧做法把换行折成空格，既丢掉排版，又让整条回复变成一个超长逻辑行
    （Tk 每次插入都要重折整行 → 流式输出时越来越卡）。"""
    body = content_to_text(content).strip()
    if not body:
        return ""
    lines = body.split("\n")
    out = [prefix + lines[0].rstrip()]
    for ln in lines[1:]:
        out.append("\t" + ln)          # 制表符 = 续行标记（内容原样保留）
    return "\n".join(out)

def append_dialog(name, role, content, ts):
    """把一条对话（Q 或 A）追加进 md 文件，结构化可恢复"""
    body = content_to_text(content).strip()
    if not body:
        return
    # 用户消息用 **Q：…** 包裹（粗体只包首行；续行靠缩进，解析器照样能拼回）
    block = fmt_mem_block("**Q：" if role == "user" else "A：", body)
    if role == "user":
        first, _, rest = block.partition("\n")
        block = first + "**" + (("\n" + rest) if rest else "")
    with open(mem_path(name), "a", encoding="utf-8") as f:
        f.write(f"\n> {ts} [{SRC_TAG}]\n{block}\n")

def parse_dialogues(name):
    """从 md 文件解析出对话记录，返回 (pairs, boundary)。
    pairs = [(role, content), ...]（忽略归档摘要块）
    boundary = 最后一个「## 会话归档」块之前解析出的对话条数；
               用于重载时定位模型上下文起点 ctx_start（归档摘要已覆盖其前内容）"""
    text = load_mem(name)
    if not text:
        return [], 0
    pairs = []
    boundary = 0
    cur_role, cur_buf = None, ""
    for raw in text.splitlines():
        # 行首是制表符 = 上一条消息的续行（多行内容），去掉这一个制表符后原样拼回。
        # 先判这个，AI 输出里的 `## 标题` / `A：` 才不会把消息截断；
        # 用制表符而不是空格，是为了保留内容自身的缩进（代码块/嵌套列表）。
        if raw[:1] == "\t":
            if cur_role is not None:
                cur_buf += "\n" + raw[1:]
            continue
        line = raw.strip()
        if line.startswith("**Q："):
            if cur_role and cur_buf.strip():
                pairs.append((cur_role, cur_buf.strip()))
            cur_role, cur_buf = "user", line.replace("**Q：", "").rstrip("**")
        elif line.startswith("A："):
            if cur_role and cur_buf.strip():
                pairs.append((cur_role, cur_buf.strip()))
            cur_role, cur_buf = "assistant", line[2:]
        elif line.startswith(">"):
            continue  # 时间戳行跳过
        elif line.startswith("## "):
            # 归档块：其之前的对话已被摘要覆盖 → 记录边界（取最后一个）
            if cur_role and cur_buf.strip():
                pairs.append((cur_role, cur_buf.strip()))
            cur_role, cur_buf = None, ""
            boundary = len(pairs)
        else:
            if cur_role:
                cur_buf += "\n" + line
    if cur_role and cur_buf.strip():
        pairs.append((cur_role, cur_buf.strip()))
    return pairs, boundary

def list_mems():
    """列出**当前可见**的所有会话。

    ★ 分级：只列 mem_roots() 里的目录 —— 云端模式下 private/ **根本不会被扫到** ✓
    """
    names = set()
    for root in mem_roots():
        if os.path.isdir(root):
            names |= {os.path.basename(p)
                      for p in glob.glob(os.path.join(root, "*.md"))}
    return sorted(names)

# ★★ 基础系统提示**刻意不提身份 / 运行环境** ✓
#
# 为什么不提（两次教训 + 用户明确要求）：
#   ① 最早写死 `你是本地部署的中文 AI 助手` ✗
#      → 连云端（小米 MiMo）时，模型照着念"我是本地部署的" ✗
#   ② 于是改成按引擎写 `你由云端 API 提供能力（服务商：X，模型：Y）` ✗
#      → 这**同样是喂答案** ✗ 模型只是复述我们给的字 ✗
#   用户原话："我不想要你给他提示词他输出，我想要模型真实经过思考输出的" ✓
#
# 所以这里**只保留功能性指令**（用中文回答）✓
# 身份 / 底层模型是什么 —— 交给模型自己判断和回答 ✓ 我们不预设 ✓
SYSTEM_BASE = "你是中文 AI 助手，请用中文回答。"


def system_base():
    """基础系统提示 —— **不描述身份，也不描述本地/云端** ✓

    想让模型说真话，就别把答案写在提示里 ✗（见上面那两条教训）
    """
    base = SYSTEM_BASE
    # ★ 如果本会话中途**换过引擎 / 模型**，历史里那些"我是 XXX"的说法
    #   其实是**另一个模型**说的 ✗ 小模型会照着学 ✗
    #   所以加一句**提醒**（只说"可能不是同一个模型" ✓ 不透露它到底是什么 ✗）
    try:
        st = load_settings()
        cur = f"{ENGINE_KIND or ''}|{MODEL_ID or ''}"
        last = str(st.get("_identity_ctx") or "")
        if last and last != cur:
            base += ("\n注意：本会话之前的回答**可能由另一个模型产生**，"
                     "不要把那些内容当作你自身身份或能力的依据。")
    except Exception:
        pass
    return base


def block_body(block):
    """取一个归档块的正文（去掉标题行和结尾的 ---）"""
    body = block.split("---")[0].strip()
    lines = body.splitlines()
    return "\n".join(lines[1:]).strip() if lines else ""


def is_meta_block(block):
    """是不是「开篇总纲」块（A. 分层摘要用）"""
    lines = block.splitlines()
    return bool(lines) and META_MARK in lines[0]


def build_system(name):
    """构建 system 提示词 = 基础指令 + 开篇总纲（永久）+ 近期摘要（按预算）。

    A. 分层摘要：
      第 1 层 = 每次归档的摘要（细节级）
      第 2 层 = 开篇总纲（把最老的一批摘要再压缩成梗概，永久常驻）
    每轮请求的总量仍然是 SYSTEM_MEM_BUDGET，所以**响应速度和以前完全一样**。
    """
    base = system_base()          # ★ 按当前引擎动态取（不要用写死的常量 ✗）
    parts = [base]
    used = est_tokens(base)
    try:
        blocks = extract_arch_blocks(load_mem(name))
    except Exception:
        blocks = []
    metas, normals = [], []
    for b in blocks:
        body = block_body(b)
        if not body:
            continue
        (metas if is_meta_block(b) else normals).append(body)

    # ① 开篇总纲：永久保留（已经是压缩过的，最多 META_BUDGET）
    if metas:
        mt = "\n\n".join(metas)
        if est_tokens(mt) > META_BUDGET:
            mt = mt[: int(META_BUDGET * 1.7)] + "…"
        seg = "【开篇总纲（更早的对话已压缩成梗概，回答早期相关问题时参考）】\n" + mt
        parts.append(seg)
        used += est_tokens(seg)

    # ② 近期摘要：从最近的往前取，直到用完总预算
    kept = []
    for line in reversed(normals):
        t = est_tokens(line)
        if kept and used + t > SYSTEM_MEM_BUDGET:
            break
        kept.append(line)
        used += t
    kept.reverse()
    if kept:
        hidden = len(normals) - len(kept)
        head = "（中间更早的归档已压缩进上面的『开篇总纲』）\n" if hidden > 0 else ""
        parts.append("【长期记忆（此前对话摘要，回答相关问题时可参考）】\n"
                     + head + "\n\n".join(kept))
    return "\n\n".join(parts)


def set_meta_block(name, text, drop_blocks):
    # ★ 同上：总纲是**永久常驻**的，更不能带身份噪音 ✗
    text = sanitize_summary(text)
    """写入新的「开篇总纲」，并把已被它吸收的旧摘要块从 md 里删掉。

    drop_blocks 里的内容已经在 text 里被压缩概括过了，原文也完整留在
    memories\_arch_batches\ 里，所以从 md 里移除不会丢东西。
    """
    md = load_mem(name)
    for b in drop_blocks:
        md = md.replace(b, "")
    for b in extract_arch_blocks(md):          # 去掉可能存在的旧总纲
        if is_meta_block(b):
            md = md.replace(b, "")
    while "\n\n\n\n" in md:
        md = md.replace("\n\n\n\n", "\n\n")
    md = md.strip()
    block = f"## 会话归档 {META_MARK} [{SRC_TAG}]\n{text.strip()}\n\n---\n"
    with open(mem_path(name), "w", encoding="utf-8") as f:
        f.write((md + "\n\n" if md else "") + block)
    return True


def arch_records(name):
    """当前记忆文件的**归档记忆**清单（新→旧），供「本地用户」弹窗展示。

    两类都收进来：
      · 自动归档的**原文批次**（memories\_arch_batches\<名>\，最详细）
      · md 里的**摘要块**（含「开篇总纲」，永久保留）
    这样即便原文早被精简掉，也能看到提炼后的摘要。
    """
    recs = []
    try:
        for b in arch_list_batches(name):
            recs.append({"label": f"🗂 {b.get('ts') or '?'} · {b.get('count', 0)} 条原文",
                         "summary": b.get("summary") or "",
                         "body": arch_batch_body(b["path"]),
                         "ts": b.get("ts") or ""})
    except Exception:
        pass
    try:
        for blk in extract_arch_blocks(load_mem(name)):
            head = blk.splitlines()[0] if blk.splitlines() else ""
            body = block_body(blk)
            mo = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", head)
            ts = mo.group(1) if mo else ""
            if is_meta_block(blk):
                recs.append({"label": "📌 开篇总纲（永久保留）", "summary": body,
                             "body": "", "ts": ""})
            else:
                recs.append({"label": f"📄 {ts or head[:26]} · 摘要", "summary": body,
                             "body": "", "ts": ts})
    except Exception:
        pass
    return recs


def meta_overflow(name, budget=None):
    """算出「近期摘要装不下、应该被压缩进总纲」的那批最老块。没有就返回 []"""
    budget = budget if budget is not None else (SYSTEM_MEM_BUDGET - META_BUDGET)
    try:
        blocks = [b for b in extract_arch_blocks(load_mem(name)) if not is_meta_block(b)]
    except Exception:
        return []
    kept, used, over = 0, 0, []
    for b in reversed(blocks):
        body = block_body(b)
        t = est_tokens(body)
        if kept and used + t > budget:
            over.append(b)
            continue
        kept += 1
        used += t
    return over


# ---------------- C. 回忆：按关键词检索归档原文 ----------------
_STOP_GRAMS = set(
    "的了是我你他她它们在有和就不人都一这那什么怎么为可以吗吧呢啊把被让给对从到与及等很也还要会能之其所以因为但是如果然后而且或者一个一些这个那个我们你们他们时候自己现在就是没有不是知道觉得应该可能真的这样那样如果"
)


def keywords_of(text):
    """从一段话里抽关键词：ASCII 词（≥3）+ 中文二字组（去停用词）"""
    out = set()
    for w in re.findall(r"[A-Za-z0-9_]{3,}", text or ""):
        out.add(w.lower())
    for han in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(han) - 1):
            g = han[i:i + 2]
            if g not in _STOP_GRAMS:
                out.add(g)
    return out


def split_segments(body, max_lines=14):
    """把归档原文切成便于检索的小段（按空行切，过长再切）"""
    segs, cur = [], []
    for ln in (body or "").splitlines():
        if not ln.strip():
            if cur:
                segs.append("\n".join(cur)); cur = []
            continue
        cur.append(ln)
        if len(cur) >= max_lines:
            segs.append("\n".join(cur)); cur = []
    if cur:
        segs.append("\n".join(cur))
    return [s for s in segs if s.strip()]


def search_archives(name, query, budget=None):
    """C. 回忆：在归档原文 + 摘要里找和 query 相关的片段，返回可直接塞进上下文的文本。

    纯本地字符串匹配（约 10ms 级），**不调模型**；没命中就返回空串，
    所以平时的普通对话完全不增加上下文、也不影响响应速度。
    """
    budget = budget or RECALL_BUDGET
    kws = keywords_of(query)
    if len(kws) < 2:
        return "", 0
    cands = []
    try:
        for b in arch_list_batches(name):          # ① 归档原文（最详细）
            for seg in split_segments(arch_batch_body(b["path"])):
                cands.append(("原文", b.get("ts") or "", seg))
    except Exception:
        pass
    try:
        for blk in extract_arch_blocks(load_mem(name)):   # ② md 里的摘要块
            cands.append(("摘要", "", blk))
    except Exception:
        pass
    scored = []
    for kind, ts, seg in cands:
        hits = sum(1 for k in kws if k in seg)
        if hits >= RECALL_MIN_HITS:
            scored.append((hits, kind, ts, seg))
    if not scored:
        return "", 0
    scored.sort(key=lambda x: -x[0])
    picked, used = [], 0
    for hits, kind, ts, seg in scored[:10]:
        t = est_tokens(seg)
        if used + t > budget:
            continue
        picked.append(f"[{kind}{(' ' + ts) if ts else ''}]\n{seg}")
        used += t
    return "\n\n".join(picked), len(picked)

# ---------- 圆角组件（tkinter 原生 Button/Frame/Combobox 都是直角，统一用 Canvas 画） ----------
def round_rect_points(x1, y1, x2, y2, r):
    """圆角矩形的平滑多边形路径点（tkinter 没有原生圆角控件，用平滑多边形近似）"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

def draw_round_rect(canvas, x1, y1, x2, y2, r=10, **kw):
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r),
                                 smooth=True, **kw)

class RoundedButton(tk.Canvas):
    """Canvas 画的圆角按钮（支持悬停高亮、动态文字与描边色）"""
    def __init__(self, parent, text="", command=None, *, width=90, height=30,
                 radius=12, bg="#ffffff", fill="#ffffff", hover="#eef1f5",
                 outline="#d9d9d9", fg="#444444",
                 font=("Microsoft YaHei UI", 9), padx=10):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._cmd = command
        self._fill, self._hover = fill, hover
        self._padx = padx
        # 注意：不能用 self._w / self._h 命名（tkinter 内部用 _w 存控件路径，覆盖会炸）
        self._btn_w, self._btn_h, self._btn_r = width, height, radius
        self._shape = draw_round_rect(self, 1, 1, width - 1, height - 1, radius,
                                      fill=fill, outline=outline, width=1)
        self._label = self.create_text(width / 2, height / 2, text=text,
                                       fill=fg, font=font)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self.itemconfigure(self._shape, fill=self._hover))
        self.bind("<Leave>", lambda e: self.itemconfigure(self._shape, fill=self._fill))

    def _on_click(self, event=None):
        if self._cmd:
            self._cmd()

    def set_text(self, text, fg=None):
        self.itemconfigure(self._label, text=text)
        if fg:
            self.itemconfigure(self._label, fill=fg)

    def set_outline(self, color):
        self.itemconfigure(self._shape, outline=color)

    def set_command(self, command):
        """换掉点击回调（Canvas 按钮没有 -command 选项，必须用这个方法）"""
        self._cmd = command

    def set_fill(self, fill, hover=None):
        self._fill = fill
        if hover is not None:
            self._hover = hover
        self.itemconfigure(self._shape, fill=fill)

    def cget_text(self):
        return self.itemcget(self._label, "text")

class RoundedPanel(tk.Canvas):
    """圆角面板容器：内部控件用 place/inner frame 摆放"""
    def __init__(self, parent, *, radius=14, fill="#ffffff", outline="#e3e3e3",
                 bg="#ffffff", **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, **kw)
        self._radius, self._fill, self._outline = radius, fill, outline
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.delete("panelbg")
        draw_round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                        fill=self._fill, outline=self._outline, width=1, tags="panelbg")
        self.tag_lower("panelbg")

class ChatApp(_RootBase):
    def __init__(self):
        super().__init__()
        self.title("MarkTalk GUI")
        # 按屏幕逻辑尺寸自适应（适配小屏+系统缩放，非全屏也完整可见）
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(1280, max(960, int(sw * 0.94)))
        h = min(820, max(560, int(sh * 0.94)))
        self.geometry(f"{w}x{h}")
        self.minsize(880, max(500, int(sh * 0.72)))
        # 窗口/任务栏图标
        try:
            if os.path.exists(ICON_FILE):
                self.iconbitmap(ICON_FILE)
        except Exception:
            pass
        self._tray = None        # 托盘图标（pystray）
        self._in_tray = False    # 是否已收进托盘（防止 <Unmap> 递归）

        self.current_mem = None     # 当前会话文件名
        self.pending_new = False    # 等待第一句话生成文件名
        self.pending_images = []    # 待发送的图片路径（先选图，再带文字一起发）
        self.pending_files = []     # 待发送的文本文件路径（拖入或选择，内容会一并送给模型）
        self.stop_event = threading.Event()  # 手动停止生成
        self._loaded_from = 0       # 懒加载：已渲染的对话条数起点（反向索引）
        self._imgs = []             # 防止 PhotoImage 被回收
        self._thumb_cache = {}      # 图片路径→PhotoImage 缓存（避免每次渲染重开 PIL，提速上翻）
        self._load_more_after_id = None  # 防抖定时器 id（真防抖：拖动中不触发，停手才加载）
        self._state_refreshing = False   # 模型状态查询中（防轮询线程重叠）
        self._poll_after_id = None       # 模型状态轮询定时器
        self._think_after_id = None      # 「思考中」动效定时器
        self._think_t0 = None            # 「思考中」起始时间（算已等待秒数）
        self._think_i = 0                # 动效帧序号（转圈点数）
        self._first_token = False        # 本次生成是否已收到首个正文 token
        self._closing = False            # 窗口是否正在关闭（后台线程据此停止调度 UI）
        self._model_paths = {}           # 模型 id → GGUF 路径（用于扫描能力）
        self._reason_cache = {}          # 模型 id → 推理能力缓存（避免重复扫描大文件）
        self._reason_options = []        # 推理下拉的 [(显示文本, chat_template_kwargs)]
        self._model_power_busy = False   # 启停模型请求进行中
        self._ovf_retry = False          # 本次请求是否已因上下文超限重试过
        self._op_hl = None               # 当前悬停高亮的操作条按钮 (行,起,止)
        self._reconnect_after_id = None  # 自动重连定时器（服务没起来时轮询）
        # ★ 跨线程 UI 通道：子线程只入队，主线程定时泵（tkinter 非线程安全）
        self._ui_queue = queue.Queue()
        self._agent_confirmations = {}   # 主线程管理；隐藏/退出时全部拒绝
        self._stream_buf = []            # 流式文本缓冲（子线程写，主线程读）
        self._stream_lock = threading.Lock()
        self._pump_after_id = None       # 主线程泵的定时器
        self._last_flush = 0.0           # 上次把流式文本写进界面的时刻（节流用）
        self._follow = True              # 是否"跟随底部"：用户上翻后置 False，不再自动拽回底部
        self._edit_hidx = None      # 编辑模式：正在编辑的用户消息历史索引（None=非编辑态）
        self._inline_edit = None    # 就地编辑框：{"hidx":.., "frame":.., "text":..}
        self._in_regen = False      # 是否处于重新生成回答的流程（编辑/重新思考）
        self._regen_after = []      # 重新生成后要恢复的后续对话（非破坏性保留）
        self._regen_backup = []     # 被移除的旧回复+后续（失败/停止时整体还原，不丢内容）
        self._regen_edit_orig = None  # 编辑场景：原问题 content（失败/停止时一并撤销编辑）

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)   # 点 X 只收进托盘，不退出
        self._setup_tray()
        # ★ 2026-10-10 改：最小化 = 正常最小化（留任务栏 ✓）
        #   点 X 才走 _hide_to_tray（收进托盘 ✓ 托盘图标继续活着 ✓）
        #   原来这里绑了 <Unmap> → _on_unmap → 最小化也收进托盘 ✗ 用户不要这个行为
        #   _on_unmap 方法保留（以后要改回来只需加回一行 bind）
        self.bind("<FocusOut>", self._on_main_focus_out)  # 点外面 → 关模型面板
        self._pump_ui_queue()                    # 启动主线程 UI 队列泵
        self._setup_dnd()                        # 整窗支持拖入文件
        self.after(300, self.init_connect)

    # ---------- 托盘（最小化收到任务栏通知区域） ----------
    def _tray_image(self):
        """托盘图标：优先用打包的 app.ico，取不到就现画一个"""
        try:
            from PIL import Image
        except Exception:
            return None
        try:
            if os.path.exists(ICON_FILE):
                return Image.open(ICON_FILE)
        except Exception:
            pass
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([3, 3, 61, 61], radius=15, fill=(59, 130, 246, 255))
            d.rounded_rectangle([14, 17, 50, 42], radius=7, fill=(255, 255, 255, 255))
            return img
        except Exception:
            return None

    def _setup_tray(self):
        """建立托盘图标；pystray 不可用时静默跳过（不影响主功能）"""
        try:
            import pystray
        except Exception:
            return
        img = self._tray_image()
        if img is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", lambda *a: self._ui_call(self._restore_from_tray),
                                 default=True),
                pystray.MenuItem("退出（完全关闭）", lambda *a: self._ui_call(self._quit_app)),
            )
            self._tray = pystray.Icon("marktalk", img, "MarkTalk GUI（点 X 只会收进托盘）", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception:
            self._tray = None

    def _on_unmap(self, event=None):
        """⚠️ 已解绑（2026-10-10）：原来窗口最小化会隐藏到托盘 ✗ 用户不要这个行为。
        现在最小化 = 正常最小化（留任务栏 ✓）；点 X 才走 _hide_to_tray ✓。
        如需恢复旧行为，在 __init__ 里加回 self.bind("<Unmap>", self._on_unmap) 即可。

        ★★ 必须**只处理"主窗口自己被收起"**的事件 ✗
        踩过的坑：子控件被 `pack_forget()`（比如云端时隐藏「停止模型」按钮）
        也会冒泡出一个 `<Unmap>` ✗ → 这里被误触发 →
        结果**切到云端时模型面板被无辜关掉** ✗（用户实测："他还是会闪退"）
        """
        # ① 事件不是主窗口自己发出来的 → 直接忽略
        try:
            w = getattr(event, "widget", None)
            if w is not None and w is not self:
                return
        except Exception:
            pass
        # ② 真的最小化了才算（原来就有这层判断，只是"收弹窗"那段写在了它前面 ✗）
        try:
            iconic = (self.state() == "iconic")
        except Exception:
            iconic = False
        if iconic:
            # 顺手把浮在最上面的那些弹窗收掉 —— 它们是 overrideredirect + topmost，
            # 不跟着主人一起收，主窗口藏了它们还悬在桌面上 ✗
            # （用户实测："我都把这个GUI页面隐藏了，这个选择的居然还在"）
            try:
                self.close_settings_panel()
            except Exception:
                pass
            for _attr in ("_settings_win", "cloud_dlg"):
                _w = getattr(self, _attr, None)
                try:
                    if _w is not None and _w.winfo_exists():
                        _w.destroy()
                except Exception:
                    pass
                try:
                    setattr(self, _attr, None)
                except Exception:
                    pass
        if self._closing or self._tray is None or self._in_tray:
            return
        if iconic:
            try:
                self._in_tray = True
                self.after(20, self.withdraw)
            except Exception:
                pass

    def _restore_from_tray(self):
        """从托盘恢复窗口"""
        self._in_tray = False
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _notify_tray(self, title, msg):
        """托盘气泡提示（可选）"""
        try:
            if self._tray is not None:
                self._tray.notify(msg, title)
        except Exception:
            pass

    # ---------- 关闭 / 退出 ----------
    def _hide_to_tray(self):
        """点窗口的 X：**不退出**，只把窗口收进托盘，程序继续在后台跑。
        想彻底退出请右键托盘图标 →「退出（完全关闭）」。
        若托盘不可用（pystray 缺失/创建失败），必须真退出 ——
        否则窗口藏起来就再也叫不回来了。"""
        self._cancel_agent_confirmations()
        if self._tray is None:
            self._quit_app()
            return
        try:
            self._in_tray = True
            self.withdraw()
            self._notify_tray("仍在后台运行",
                              "已收进托盘。要完全退出，请右键托盘图标选「退出（完全关闭）」。")
        except Exception:
            self._quit_app()

    def _quit_app(self):
        """真正退出：停定时器、撤托盘、销毁窗口"""
        if self._closing:
            return
        self._closing = True
        self._stop_web()
        self._cancel_agent_confirmations()
        self.close_settings_panel()
        self._cancel_reconnect()
        try:
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
        except Exception:
            pass
        for aid in (self._poll_after_id, self._think_after_id, self._load_more_after_id,
                    self._reconnect_after_id, self._pump_after_id):
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
        try:
            self.destroy()
        except Exception:
            pass

    # 兼容旧命名
    def _on_close(self):
        try:
            self._stop_control()
        except Exception:
            pass
        self._quit_app()

    def confirm_agent_operation(self, operation, path, timeout=30.0):
        """工作线程调用的护栏入口；只确认，不执行任何文件操作/命令。"""
        return AS.request_confirmation(self, operation, path, timeout)

    def _cancel_agent_confirmations(self):
        for request, dlg in list(self._agent_confirmations.items()):
            request.decide(False, "closing")
            try:
                dlg.destroy()
            except tk.TclError:
                pass
        self._agent_confirmations.clear()

    def _show_agent_confirmation(self, request):
        """仅由 _ui_call 队列在主线程创建弹窗；不阻塞队列泵。"""
        dlg = None
        try:
            if request.done.is_set():
                return
            if self._closing or self._in_tray or time.monotonic() >= request.deadline:
                request.decide(False, "unavailable")
                return
            dlg = tk.Toplevel(self)
            after_id = [None]
            self._agent_confirmations[request] = dlg
            dlg.title("Agent 危险操作确认")
            dlg.transient(self)
            dlg.resizable(False, False)
            tk.Label(dlg, text=f"操作：{request.operation}\n目标：{request.path}\n"
                     "仅明确确认后放行；关闭或超时默认拒绝。",
                     wraplength=480, justify="left", padx=20, pady=16).pack()
            buttons = tk.Frame(dlg)
            buttons.pack(padx=20, pady=(0, 16), fill="x")

            def finish(allowed=False, reason="cancelled"):
                request.decide(allowed, reason)
                dlg.destroy()

            reject = tk.Button(buttons, text="拒绝", command=finish)
            reject.pack(side="right")
            tk.Button(buttons, text="确认", command=lambda: finish(True, "approved")).pack(side="right", padx=12)
            dlg.protocol("WM_DELETE_WINDOW", finish)
            dlg.bind("<Escape>", lambda event: finish())

            def destroyed(event):
                if event.widget is dlg:  # 子控件销毁事件不能取消整个请求
                    if after_id[0] is not None:
                        dlg.after_cancel(after_id[0])
                        after_id[0] = None
                    request.decide(False, "closed")
                    self._agent_confirmations.pop(request, None)

            dlg.bind("<Destroy>", destroyed)

            def tick():
                after_id[0] = None
                if time.monotonic() >= request.deadline:
                    request.decide(False, "timeout")
                if request.done.is_set():
                    dlg.destroy()
                else:
                    after_id[0] = dlg.after(50, tick)

            reject.focus_set()
            tick()
        except Exception:
            request.decide(False, "error")
            self._agent_confirmations.pop(request, None)
            if dlg is not None:
                try:
                    dlg.destroy()
                except tk.TclError:
                    pass

    def _ui_call(self, fn):
        """从后台线程把回调排进主线程队列。

        ★ 绝对不能在这里直接调 self.after()：tkinter 不是线程安全的，
          子线程每次调用 after() 都会 createcommand 抢一次 Tcl 解释器锁。
          流式输出时每秒几十上百次 → 主线程被抢死，
          表现为「AI 输出期间窗口完全无响应，不能点、不能拖、不能关」。
          正解就是「子线程只入队，主线程定时泵」。"""
        if self._closing:
            return
        self._ui_queue.put(fn)

    def _pump_ui_queue(self):
        """主线程定时泵：排空队列 + 刷新流式文本缓冲（只在主线程里碰 Tk）"""
        self._pump_after_id = None
        if self._closing:
            return
        # ① 普通回调（含状态、提示、回复收尾等）
        n = 0
        while n < 400:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
            n += 1
        # ② 流式文本：**节流**写入。这是解决「AI 输出时界面卡死」的关键：
        #    Text 折行代价随行长增长，写得越频繁总代价越大。
        now = time.monotonic()
        if self._stream_buf and (now - self._last_flush) * 1000.0 >= STREAM_FLUSH_MS:
            self._last_flush = now
            try:
                self._flush_stream()
            except Exception:
                pass
        try:
            self._pump_after_id = self.after(UI_PUMP_MS, self._pump_ui_queue)
        except Exception:
            self._pump_after_id = None

    def _stream_feed(self, delta):
        """工作线程调用：只往缓冲里追加，完全不碰 Tk"""
        if not delta:
            return
        with self._stream_lock:
            self._stream_buf.append(delta)

    def _flush_stream(self):
        """主线程调用：把缓冲里的流式文本一次性写进界面"""
        with self._stream_lock:
            if not self._stream_buf:
                return
            text = "".join(self._stream_buf)
            self._stream_buf.clear()
        self._stream_append(text)

    # ---------- UI ----------
    def _build_ui(self):
        # 左侧栏
        self.left = tk.Frame(self, width=250, bg="#fafafa")
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        tk.Label(self.left, text="MarkTalk GUI", bg="#fafafa",
                 fg="#1f1f1f", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Label(self.left, text="多会话 · 自动归档 · 无限上下文", bg="#fafafa",
                 fg="#9a9a9a", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12)

        self.new_btn = RoundedButton(self.left, text="⊕ 新会话", command=self.new_session,
                                     width=196, height=36, radius=12, bg="#fafafa",
                                     fg="#333333")
        self.new_btn.pack(fill="x", padx=12, pady=(14, 8))
        _tip(self.new_btn, "新建一个记忆文件，开始一段全新的对话\n（旧记忆不会被删除）")

        tk.Label(self.left, text="记忆文件", bg="#fafafa", fg="#9a9a9a",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14, pady=(4, 2))

        list_frame = tk.Frame(self.left, bg="#fafafa")
        list_frame.pack(fill="both", expand=True, padx=8)
        self.mem_list = tk.Listbox(list_frame, bg="#fafafa", fg="#2b2b2b",
                                   height=4,   # 只影响**最小**高度；expand=True 时照样占满
                                   selectmode="extended",   # 支持 Shift 连续选 / Ctrl 点选
                                   selectbackground="#e6ecf5", selectforeground="#1f1f1f",
                                   relief="flat", activestyle="none", takefocus=True,
                                   font=("Microsoft YaHei UI", 10), bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.mem_list.yview)
        self.mem_list.configure(yscrollcommand=sb.set)
        self.mem_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.mem_list.bind("<<ListboxSelect>>", self.on_select_mem)
        # 键盘连选：↑↓/PgUp/PgDn/Home/End 选到哪就切到哪
        for _k in ("<Up>", "<Down>", "<Prior>", "<Next>", "<Home>", "<End>"):
            self.mem_list.bind(_k, self._mem_list_key, add="+")

        # 记忆文件的右键菜单（5 项）
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="归档", command=self.archive_mem)
        self.menu.add_separator()
        self.menu.add_command(label="复制原文", command=self.copy_arch)
        self.menu.add_command(label="导出全部", command=self.export_arch)
        self.menu.add_separator()
        self.menu.add_command(label="重命名", command=self.rename_mem)
        self.menu.add_command(label="清空全部", command=self.clear_mem)
        self.mem_list.bind("<Button-3>", self.on_right_click)

        # 已归档文件的右键菜单：只留归档相关的
        self.menu_arch = tk.Menu(self, tearoff=0)
        self.menu_arch.add_command(label="取消归档", command=self.unarchive_mem)
        self.menu_arch.add_separator()
        self.menu_arch.add_command(label="删除归档", command=self.delete_arch_mem)
        self.menu_arch.add_command(label="一键全部清空归档", command=self.clear_all_arch_mem)

        # 底部：本地用户（点它弹出「已归档（隐藏）」对话框）
        # 归档 = 把这个 .md 挪进 memories\_archived\，不占主列表；
        # 归档了哪些、要取消/删除，都在这个弹窗里管理。
        self.arch_btn = RoundedButton(self.left, text="👤 本地用户",
                                      command=self.open_arch_dialog,
                                      width=222, height=32, radius=12, bg="#fafafa",
                                      fill="#fafafa", fg="#2b2b2b",
                                      font=("Microsoft YaHei UI", 10))
        self.arch_btn.pack(fill="x", padx=12, pady=(8, 12))
        _tip(self.arch_btn, "本地用户 · 归档库\n· 查看已归档（隐藏）的记忆文件\n"
                            "· 查看当前文件历次归档的摘要与原文")
        self.arch_dlg = None            # 弹窗（单实例）

        # 自动归档的原文批次（后台保留，供导出用；界面不再展示）
        self._arch_batches = []
        self._arch_pending = {}     # name -> 本次归档的原文（摘要写完后搬进批次区）
        self.msg_queue = []         # 排队中的消息（模型忙时输入的不再被吞掉）

        # 右侧主区
        self.main = tk.Frame(self, bg="#ffffff")
        self.main.pack(side="left", fill="both", expand=True)

        # 头部
        head = tk.Frame(self.main, bg="#ffffff")
        head.pack(fill="x", padx=20, pady=(10, 4))
        self.title_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.title_var, bg="#ffffff", fg="#111111",
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(head, text="标准模式", bg="#ffffff", fg="#666666",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(12, 0))
        # 刷新：重连服务 + 重读记忆文件列表。
        # （先开程序、后启动服务时必须靠它，否则界面一直空白连不上）
        self.refresh_btn = RoundedButton(head, text="⟳ 刷新", command=self.refresh_all,
                                         width=78, height=28, radius=10, fg="#2f6fd6")
        self.refresh_btn.pack(side="right")
        _tip(self.refresh_btn, "重新读取磁盘上的记忆文件\n（在别处改过 .md 时用它，不用重启）")

        # 顶部历史提示（固定在聊天区上方，不随滚动、不参与文本插入 → 上翻零抖动）
        self.top_hint_var = tk.StringVar(value="")
        self.top_hint = tk.Label(self.main, textvariable=self.top_hint_var, bg="#ffffff",
                                 fg="#9aa3b2", font=("Microsoft YaHei UI", 9))
        self.top_hint.pack(side="top", fill="x", pady=(0, 2))

        # 对话区容器（聊天 + 滚动条，占满中间）
        chat_area = tk.Frame(self.main, bg="#ffffff")
        chat_area.pack(side="top", fill="both", expand=True)

        self.chat = tk.Text(chat_area, bg="#ffffff", fg="#242424",
                            font=("Microsoft YaHei UI", 10), wrap="word",
                            relief="flat", state="disabled", padx=6, pady=6, height=6)
        # 注意 spacing1/spacing3 是**按逻辑行**生效的：
        # 正文现在保留换行（每个段落一个逻辑行），若还按段落各加 6/8px，
        # 段间距会被撑得非常大。所以段落间距**完全交给内容自身的换行**，
        # 标签这里一律不加；消息之间的间隔由下方的 opbar 负责。
        self.chat.tag_configure("user", background="#e8f0fe", foreground="#1f1f1f",
                                spacing1=0, spacing3=0, lmargin1=240, lmargin2=240,
                                justify="right", rmargin=28)
        self.chat.tag_configure("ai", background="#f7f8fa", foreground="#242424",
                                spacing1=0, spacing3=0, rmargin=140)
        self.chat.tag_configure("meta", font=("Microsoft YaHei UI", 8), foreground="#9a9a9a",
                                spacing1=3)
        # 「思考中」动态指示样式（灰色斜体感，与正文区分）
        self.chat.tag_configure("thinking", foreground="#7f8fa6",
                                font=("Microsoft YaHei UI", 9))
        self.chat.tag_configure("arch", font=("Microsoft YaHei UI", 9), foreground="#9aa3b2",
                                justify="center", spacing1=8, spacing3=8)
        # 操作条（纯文本 + 标签点击，不用嵌入控件 → 历史上千条也不卡）
        # ★ AI 回答**上方**的统计行（参考 Harness：统计在回答上面 ✓）
        self.chat.tag_configure("aistat", font=("Microsoft YaHei UI", 8),
                                foreground="#9aa3b2", spacing1=4, spacing3=2)
        self.chat.tag_configure("opbar", font=("Microsoft YaHei UI", 8), foreground="#8a8a8a",
                                spacing1=4, spacing3=6)
        # 操作条各按钮用不同颜色区分（复制/编辑/重新思考/删除）
        for key, text in (("copy", "复制这条消息"), ("edit", "编辑这条问题并重新回答"),
                          ("rethink", "不改问题，重新生成这条回答"), ("del", "删除这条消息")):
            _tip(self.chat, text, tag=f"opc_{key}")
        self.chat.tag_configure("opc_copy", foreground="#5a6b7d")
        self.chat.tag_configure("opc_edit", foreground="#8a5a00")
        self.chat.tag_configure("opc_rethink", foreground="#0b7285")
        self.chat.tag_configure("opc_del", foreground="#d33")
        # 点击反馈：悬停 / 按下 / 成功 / 失败
        # 注意：这里**只能用前景色 + 下划线**，绝不能用 background！
        # Tk 在"右对齐行"上画标签背景时，会把背景从【行首/控件最左】一直填到文字处，
        # 实测『复制』文字仅 47px，绿色背景却铺了 872px（18.6 倍），非常难看。
        # 前景色天然只覆盖文字，且下划线提供了"可点"的视觉暗示。
        self.chat.tag_configure("ophover", foreground="#1f6feb", underline=True)
        self.chat.tag_configure("oppress", foreground="#0b4bb3", underline=True)
        self.chat.tag_configure("opok", foreground="#1a9c3d", underline=True)
        self.chat.tag_configure("opbad", foreground="#c62828", underline=True)
        self.chat.tag_configure("op_user", justify="right", lmargin1=240, lmargin2=240, rmargin=28)
        self.chat.tag_configure("op_ai", justify="left", rmargin=140)
        self.chat.pack(side="left", fill="both", expand=True, padx=(18, 2), pady=(4, 6))
        self.chat.bind("<MouseWheel>", self._on_wheel)
        # 选中文字高亮（复制时有视觉反馈）
        # 关键：tkinter 在创建 Text 时就**预建**了 sel 标签，所以它天然是【最低优先级】，
        # 会被 user/ai 的背景色完全盖住 → 用鼠标拖选时看不到任何高亮（实测像素为 0）。
        # 必须显式 tag_raise 把它提到最上层。
        self.chat.tag_configure("sel", background="#9dc0f5", foreground="#0b1b33")
        self.chat.tag_raise("sel")
        # 右侧可拖动滚动条（滑动查看对话位置，拖到顶部自动加载更早记录）
        self.scrollbar = ttk.Scrollbar(chat_area, orient="vertical", command=self._sb_scroll)
        self.scrollbar.pack(side="right", fill="y", padx=(0, 16), pady=(4, 6))
        self.chat.configure(yscrollcommand=self.scrollbar.set)

        # 输入区（固定在窗口底部）
        inp = tk.Frame(self.main, bg="#ffffff")
        inp.pack(side="bottom", fill="x", padx=18, pady=(0, 2))
        # 圆角外框（原来是 highlight 直角边框 → 改圆角）
        self.box_panel = RoundedPanel(inp, height=40, radius=16,
                                      fill="#ffffff", outline="#d9d9d9")
        self.box_panel.pack(fill="x")
        box = tk.Frame(self.box_panel, bg="#ffffff")
        self._box_win = self.box_panel.create_window(0, 0, anchor="nw", window=box)
        box.bind("<Configure>", self._on_box_inner_configure)
        self.box_panel.bind("<Configure>", self._on_box_panel_configure, add="+")

        # 附件待发送栏（输入框上方）：**横向排列、排满自动换行**，条目多时可滚动。
        # 结构：attach_outer
        #         ├─ attach_bar   （工具行：全选 / 删除选中 / 清空全部；有附件时才显示）
        #         └─ attach_body  （attach_canvas + 竖滚动条）
        self.attach_outer = tk.Frame(box, bg="#ffffff")
        self.attach_outer.pack(fill="x", padx=10, pady=0)
        # ★ 关掉尺寸传播：整块嵌在 Canvas 的 window item 里，
        #   只靠子控件 reqheight 传播的话，"清空后回缩"不生效
        #   （实测 bar/body 都已 pack_forget，attach_outer 仍卡在 138px）。
        #   高度统一由 _apply_attach_height() 显式决定。
        self.attach_outer.pack_propagate(False)

        self.attach_bar = tk.Frame(self.attach_outer, bg="#ffffff")
        self.attach_info_var = tk.StringVar(value="")
        tk.Label(self.attach_bar, textvariable=self.attach_info_var, bg="#ffffff",
                 fg="#7a828e", font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.attach_clear_btn = RoundedButton(
            self.attach_bar, text="清空全部", command=self.clear_attachments,
            width=68, height=22, radius=8, fg="#b03030", fill="#fdf2f2",
            hover="#f8e3e3", outline="#e8c4c4", font=("Microsoft YaHei UI", 8))
        self.attach_clear_btn.pack(side="right")
        self.attach_del_btn = RoundedButton(
            self.attach_bar, text="删除选中", command=self.remove_selected_attachments,
            width=68, height=22, radius=8, fg="#1f6feb", fill="#f2f6fd",
            hover="#e4eefb", outline="#c4d8f5", font=("Microsoft YaHei UI", 8))
        self.attach_del_btn.pack(side="right", padx=(0, 6))
        self.attach_all_btn = RoundedButton(
            self.attach_bar, text="全选", command=self.select_all_attachments,
            width=48, height=22, radius=8, fg="#444444", fill="#f5f6f8",
            hover="#e9ebef", outline="#dcdfe4", font=("Microsoft YaHei UI", 8))
        self.attach_all_btn.pack(side="right", padx=(0, 6))

        self.attach_body = tk.Frame(self.attach_outer, bg="#ffffff")
        self.attach_canvas = tk.Canvas(self.attach_body, bg="#ffffff",
                                       highlightthickness=0, bd=0, height=0)
        self.attach_canvas.pack(side="left", fill="x", expand=True)
        self.attach_sb = ttk.Scrollbar(self.attach_body, orient="vertical",
                                       command=self.attach_canvas.yview)
        self.attach_frame = tk.Frame(self.attach_canvas, bg="#ffffff")
        self._attach_win = self.attach_canvas.create_window(0, 0, anchor="nw",
                                                            window=self.attach_frame)
        self.attach_canvas.configure(yscrollcommand=self.attach_sb.set)
        self._attach_chips = []          # 当前附件 chip 列表（流式排版用）
        self._attach_chip_paths = []     # 与 _attach_chips 一一对应的路径（框选用）
        self._attach_sel = set()         # 已选中的附件路径（批量删除用）
        self._attach_anchor = None       # Shift 范围选择的锚点
        self._attach_drag_on = False     # 是否正在拖动涂抹选择
        # 注意：这里**不绑定 attach_frame 的 <Configure>** —— 排版改由 _relayout_attachments
        # 主动驱动；若绑了，configure(width/height) 会再次触发 <Configure> 形成死循环
        self.attach_canvas.bind("<Configure>", self._on_attach_canvas_configure)
        self.attach_canvas.bind("<MouseWheel>", self._on_attach_wheel)
        self.attach_canvas.bind("<Button-1>", self._clear_attach_sel)   # 点空白取消选中
        self.attach_frame.bind("<MouseWheel>", self._on_attach_wheel)
        self.attach_frame.bind("<Button-1>", self._clear_attach_sel)

        # 圆角输入框：Canvas 画圆角底，内嵌 Text + 右侧可拖动滚动条
        self.input_canvas = tk.Canvas(box, bg="#ffffff", highlightthickness=0, bd=0, height=38)
        self.input_canvas.pack(fill="x", padx=10, pady=(8, 2))
        self.entry = tk.Text(self.input_canvas, bg="#ffffff", fg="#2b2b2b", height=1,
                             font=("Microsoft YaHei UI", 10), relief="flat", bd=0,
                             wrap="word", undo=True, highlightthickness=0, padx=0, pady=0)
        self._entry_win = self.input_canvas.create_window(12, 8, anchor="nw", window=self.entry)
        # 输入框滚动条：内容超出可见行数时才出现（可拖动查看）
        self.entry_sb = ttk.Scrollbar(self.input_canvas, orient="vertical", command=self.entry.yview)
        self._entry_sb_win = self.input_canvas.create_window(0, 8, anchor="ne", window=self.entry_sb)
        self.input_canvas.itemconfigure(self._entry_sb_win, state="hidden")
        self._entry_sb_shown = False
        self.entry.configure(yscrollcommand=self._entry_yscroll)
        self._entry_lines = 1
        self.input_canvas.bind("<Configure>", self._redraw_input_box)
        self.entry.bind("<FocusIn>", lambda e: self.update_placeholder())
        self.entry.bind("<FocusOut>", lambda e: self.update_placeholder())
        self.entry.bind("<KeyRelease>", self._on_entry_key)
        # 空输入框里按下/拖动本来会选中 Text 隐含的结尾换行 → 出现一条没意义的蓝条。
        # 注意：**只拦按下和拖动，绝不能拦松开** —— Tk 在按下时抓取指针、
        # 松开时释放；拦掉松开会让指针一直被抓住，输入框看起来"卡死"、点哪都没反应
        # （用户就踩到了这个）。所以这里没有 <ButtonRelease-1> 的绑定。
        self.entry.bind("<ButtonPress-1>", self._on_entry_press)
        self.entry.bind("<B1-Motion>", self._on_entry_drag)
        self.entry.bind("<Return>", self._on_entry_enter)
        self.entry.bind("<Shift-Return>", self._on_entry_newline)
        self.entry.bind("<Escape>", lambda e: self._cancel_edit())

        # ---------- 排队区（模型忙时输入的消息排在这里，不再被吞掉）----------
        # 参照 Harness：模型正在生成时你还能继续输入，消息进入队列，
        # 每条都能「重编辑 ✏ / 删除 🗑 / 强制发送 ⬆」。上一条回复结束自动发下一条。
        self.msg_queue = []                 # [{"segments":..., "display":..., "images":...}]
        self.queue_bar = tk.Frame(box, bg="#f2f7fd")
        # 计数固定在左边不滚动，右边才是可横向滚动的排队条
        self.queue_head = tk.Label(self.queue_bar, text="", bg="#f2f7fd", fg="#5b7ba6",
                                   font=("Microsoft YaHei UI", 8))
        self.queue_head.pack(side="left", padx=(8, 6), pady=(6, 2))
        # 用 Canvas 装排队条：条数多的时候可以横向滚动，不会像直接 pack 那样被右边裁掉
        self.queue_canvas = tk.Canvas(self.queue_bar, bg="#f2f7fd", highlightthickness=0,
                                      bd=0, height=34)
        self.queue_canvas.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=(6, 2))
        self.queue_inner = tk.Frame(self.queue_canvas, bg="#f2f7fd")
        self._queue_win = self.queue_canvas.create_window(0, 0, anchor="nw",
                                                          window=self.queue_inner)
        self._queue_bar_shown = False
        for _w in (self.queue_canvas, self.queue_inner):
            try:
                _w.bind("<MouseWheel>", self._queue_scroll)
                _w.bind("<Shift-MouseWheel>", self._queue_scroll)
            except Exception:
                pass

        # 编辑模式横幅（默认隐藏）：提示正在编辑哪条、如何确认/取消
        self.edit_banner = tk.Frame(box, bg="#fff7e6")
        self.edit_banner_inner = tk.Frame(self.edit_banner, bg="#fff7e6")
        self.edit_banner_inner.pack(fill="x", padx=10, pady=(6, 6))
        tk.Label(self.edit_banner_inner, text="✎ 正在编辑你的消息",
                 bg="#fff7e6", fg="#8a5a00",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Label(self.edit_banner_inner, text="  发送=替换该条并重新回答 · Esc=取消",
                 bg="#fff7e6", fg="#a97400",
                 font=("Microsoft YaHei UI", 8)).pack(side="left")
        # 初始不 pack（进入编辑模式时才显示）

        # 占位文字用覆盖式 Label（输入框本身永远是空的，绝不粘连）
        # ★ 坐标必须和 Text 里真正开始写字的位置一致：entry 的 padx/pady 都是 0，
        #   所以它的文字从控件左上角 (0,0) 开始；Label 也要放 (0,0) 才对得齐。
        #   以前放在 (8,1)，占位文字比真实输入的字向右偏了 8px（用户反馈"没放在一起"）。
        self.ph = tk.Label(box, text="请发送消息或文件等", bg="#ffffff", fg="#b5b5b5",
                           font=("Microsoft YaHei UI", 10), anchor="w")
        self.ph.place(in_=self.entry, x=0, y=0, anchor="nw")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())

        # ---------- 控制行：图片/文件靠左，其余（模型/推理/启停/状态/回车）全部靠右 ----------
        ctrl_row = tk.Frame(box, bg="#ffffff")
        ctrl_row.pack(fill="x", padx=8, pady=(2, 8))

        # 圆形「+」按钮：图片和文件合并成一个入口（radius = 边长/2 即为正圆）
        self.add_btn = RoundedButton(ctrl_row, text="+", command=self.pick_attachment,
                                     width=34, height=34, radius=17,
                                     fill="#f0f2f5", hover="#e2e6ea", outline="#d9dde3",
                                     fg="#333333", font=("Microsoft YaHei UI", 15),
                                     padx=0)
        self.add_btn.pack(side="left")
        ToolTip(self.add_btn, "添加图片或文件（也可直接把文件拖进窗口）")

        self.model_var = tk.StringVar()
        self.reason_var = tk.StringVar(value="推理：—")
        self.model_state_var = tk.StringVar(value="○ 未连接")
        self.model_choices = []      # 可选模型列表（面板用）
        self._reason_labels = []     # 可选推理项文本（面板用）
        self._settings_panel = None  # 合并设置面板（Toplevel，按需创建）

        # 靠右排列（pack side="right" 先 pack 的在最右）→ 视觉顺序：
        #   模型·推理 [合并控件]  [启停]  ●状态  [↑]
        self.send_btn = RoundedButton(ctrl_row, text="↑", command=self.send_text,
                                      width=40, height=32, radius=16,
                                      fill="#3b82f6", hover="#2f6fd6",
                                      outline="#3b82f6", fg="#ffffff",
                                      font=("Microsoft YaHei UI", 11, "bold"))
        self.send_btn.pack(side="right")
        _tip(self.send_btn, "发送（Enter）\n模型正在回答时：这条会自动排队，不会丢")

        # 正在运行的模型 · 颜色指示（绿=运行中 / 橙=加载中或待切换 / 灰=未加载 / 红=连不上）
        self.model_state_lb = tk.Label(ctrl_row, textvariable=self.model_state_var,
                                       bg="#ffffff", fg="#999999",
                                       font=("Microsoft YaHei UI", 9))
        self.model_state_lb.pack(side="right", padx=(0, 10))

        # 模型启停按钮：一键启动/停止当前所选模型（停止即释放显存）
        self.model_power_btn = RoundedButton(ctrl_row, text="▶ 启动模型",
                                             command=self.toggle_model_power,
                                             width=96, height=32, radius=12, fg="#2f6fd6")
        self.model_power_btn.pack(side="right", padx=(0, 10))
        _tip(self.model_power_btn,
             "一键启动 / 停止当前模型（**仅本地引擎**）\n"
             "停止会释放显存（换大模型前先停）\n"
             "云端模型不由本机启停，按钮会变灰")

        # 模型切换 + 推理强度 合并成一个控件：点击弹出设置面板
        self.model_pill = RoundedButton(ctrl_row, text="选择模型",
                                        command=self.toggle_settings_panel,
                                        width=250, height=32, radius=12, fg="#444444")
        self.model_pill.pack(side="right", padx=(0, 10))
        _tip(self.model_pill, "选择模型 + 推理强度\n（这里改的是「上次选择」，程序会记住）")

        # 状态条（固定在底部，输入框下方）
        self.status_var = tk.StringVar(value="正在连接服务…")
        tk.Label(self.main, textvariable=self.status_var, bg="#ffffff", fg="#8a8a8a",
                 font=("Microsoft YaHei UI", 8)).pack(side="bottom", anchor="w", padx=20, pady=(0, 8))

        # 点击面板外部 / Esc → 收起合并设置面板
        self.bind_all("<Button-1>", self._on_global_click, add="+")
        self.bind_all("<Escape>", lambda e: self.close_settings_panel(), add="+")
        # ★ C · 本地控制接口：界面起来之后再启动（避免构造函数里做网络动作 ✓）
        self.control_server = None
        self.web_server = None
        try:
            self.after(600, self._start_control_later)
        except Exception:
            pass

    # ---------- 服务连接 ----------
    def _start_control_later(self):
        try:
            EA.log_trim()
            EA.log(f"=== MarkTalk 启动 === 记忆分级={os.path.basename(MEM_DIR)} "
                   f"会话={self.current_mem!r}")
        except Exception:
            pass
        """启动本地控制接口（失败不影响主程序 ✓ 只在状态栏说一句）"""
        try:
            cfg = EA.load_config(CONFIG_FILE)
        except Exception:
            cfg = {}
        ok, err = start_control_server(self, cfg)
        if ok and CONTROL is not None:
            self.control_server = CONTROL
            try:
                EA.log(f"控制接口就绪 {CONTROL.url}")
            except Exception:
                pass
            self.set_status(f"🔌 控制接口已就绪：{CONTROL.url}"
                            f"（token 见「设置 → 通用」，已存 control_token.dat）")
        else:
            self.control_server = None
            try:
                self.add_arch("🔌 本地控制接口未启动：" + str(err))
            except Exception:
                pass

    def _stop_control(self):
        """关程序时把控制接口收掉"""
        try:
            if self.control_server is not None:
                self.control_server.shutdown()
        except Exception:
            pass
        self.control_server = None

    def _toggle_web(self):
        if self.web_server is not None:
            self._stop_web()
            self.set_status("手机 Web 已关闭；旧 token 已失效")
        else:
            server = ControlServer(self, host="0.0.0.0", port=WEB_PORT, web=True)
            ok, err = server.start()
            if not ok:
                messagebox.showerror("手机 Web", err)
                return
            self.web_server = server
            self.set_status(f"手机 Web 已开启（{WEB_PORT}）；请在通用设置复制访问地址")
        self.open_settings_window("general")

    def _stop_web(self):
        server = getattr(self, "web_server", None)
        self.web_server = None
        if server is not None:
            server.shutdown()
            server.token = ""  # 关闭后不在内存中保留有效访问凭据。

    def _copy_web_access(self, token_only=False):
        server = self.web_server
        if server is None:
            self.set_status("请先开启手机 Web")
            return
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                                if ipaddress.ip_address(info[4][0]).is_private
                                and not info[4][0].startswith(("127.", "169.254."))})
        except OSError:
            addresses = []
        ip = addresses[0] if addresses else "127.0.0.1"
        value = server.token if token_only else f"http://{ip}:{server.port}/?token={server.token}"
        self.clipboard_clear()
        self.clipboard_append(value)
        self.set_status("Web token 已复制" if token_only else "手机访问地址已复制；请选择同 WiFi 的电脑 IP")

    def _load_local_data(self):
        """加载**与服务器无关**的本地数据：记忆文件列表 + 默认会话。
        必须先于服务检测执行 —— 否则"先开程序、后启动服务"时界面会一片空白
        （记忆列表、会话标题全都不显示），用户以为程序坏了。"""
        if not os.path.exists(mem_path(DEFAULT_MEM)):
            try:
                with open(mem_path(DEFAULT_MEM), "w", encoding="utf-8") as f:
                    f.write("")
            except Exception:
                pass
        self.refresh_mem_list()
        if self.current_mem is None:          # 已有会话就别重置，避免刷新时丢现场
            self.select_mem(DEFAULT_MEM)

    # ---------- 引擎探测（多引擎 + 自动发现 + 云端 API） ----------
    def _engine_label(self):
        if not ENGINE:
            return "未连接"
        # 前缀映射：手动/自动发现出来的 kind → 状态栏展示名
        prefix = {"llama": "llama.cpp ", "freetoken": "FreeToken ",
                  "openai": "OpenAI "}.get(ENGINE_KIND, "")
        # 云端标记：一眼看出当前是不是在云端（对话内容会离开本机）
        try:
            cloud = bool(ENGINE.is_cloud())
        except Exception:
            cloud = False
        return ("☁ 云端 " + prefix if cloud else prefix) + ENGINE.base_url

    def _setup_engine(self, silent=False):
        """探测并绑定引擎适配器。返回 True=可用，False=没探测到。

        规则（config.json 的 engine 字段）：auto / llama / freetoken
          · 手动指定 llama/freetoken：用配置里的 base_url，**不扫描**（行为与以前一致）
          · auto：先探测配置的 llama 8080 / freetoken 8000
              - 两个都活 → 用「上次选的」，没记录才弹窗问
              - 只活一个 → 用它
              - 都死/没配 → **自动扫描常见端口**（Ollama 11434 / LM Studio 1234 /
                vLLM 8000 / text-gen-webui 5000 / TabbyAPI 5000 …），
                扫到就用，引擎名只作展示标注
        自动发现的结果**不写回 config.json**（只是启动时的便利，下次重新扫）。"""
        global ENGINE, ENGINE_KIND
        if not _EA_OK:
            # engine_adapter.py 缺失/导入失败：明确告诉用户，别只说"连不上"
            self.set_status("❌ 缺少 engine_adapter.py（需与主程序同目录），无法连接任何引擎")
            if not silent:
                messagebox.showerror(
                    "缺少文件",
                    "找不到 engine_adapter.py。\n\n"
                    "它必须和 MarkTalk_GUI.exe（或 longchat_gui.py）放在同一个文件夹里。\n"
                    "请从源码包 backup\\longchat源码_*.zip 重新解压一份。")
            return False
        cfg = EA.load_config(CONFIG_FILE)
        if not os.path.exists(CONFIG_FILE):
            EA.save_config(CONFIG_FILE, cfg)      # 首次运行生成一份，方便用户手改
        # 密钥安全提醒：config.json 里手写了明文 api_key → 建议改用 DPAPI 加密存储
        if cfg.get("_api_key_plaintext_in_config"):
            self.add_arch("🔐 检测到你在 config.json 里**明文写了 api_key** —— 那是个明文文件，"
                          "容易泄露。建议改用加密存储：双击「设置云端密钥.bat」→ 选 1 粘贴密钥，"
                          "然后把 config.json 里那行 api_key 删掉。")
        # detect_engine_ex 会额外告诉我们是哪个 base_url 被扫到的（配置里的则是 None）
        kind, scan_url = EA.detect_engine_ex(cfg)
        if kind == "both":
            # 优先用上次记住的（避免每次启动都弹窗）
            last = str(load_settings().get("engine_kind") or "")
            if last in ("llama", "freetoken") and EA.probe(
                    (cfg.get(last) or {}).get("base_url")):
                kind = last
            elif silent:
                kind = last if last in ("llama", "freetoken") else "llama"
            else:
                ans = messagebox.askyesnocancel(
                    "检测到两个引擎",
                    "llama.cpp（8080）和 FreeToken（8000）都在运行。\n\n"
                    "「是」   = 用 llama.cpp\n"
                    "「否」   = 用 FreeToken\n"
                    "「取消」 = 用上次的选择\n\n"
                    "（想固定用某一个，可改 config.json 里的 \"engine\" 字段）")
                if ans is True:
                    kind = "llama"
                elif ans is False:
                    kind = "freetoken"
                else:
                    kind = last if last in ("llama", "freetoken") else "llama"
        if not kind:
            ENGINE = None
            ENGINE_KIND = None
            return False
        # ★ 运行期引擎覆盖：模型面板里点了**另一个引擎**的模型时设置
        #   （扫到的本地引擎不能写进 config.openai —— 那会把用户配好的云端地址覆盖掉 ✗，
        #    所以只在本进程内生效，重启后回到 config 里的设置。）
        _ov = getattr(self, "_engine_override", None)
        if _ov:
            kind, ov_url = _ov[0], _ov[1]
            scan_url = ov_url or None
            # 如果覆盖的正是配置里的云端地址，就走 cfg（需要带 api_key）
            if (kind == "openai" and ov_url and
                    str((cfg.get("openai") or {}).get("base_url") or "").strip() == ov_url):
                scan_url = None
            # llama / freetoken 的地址也是用户配置，交给 cfg 处理更一致
            if kind in ("llama", "freetoken"):
                scan_url = None
        ENGINE = EA.make_adapter(kind, cfg, base_url_override=scan_url)
        ENGINE_KIND = kind
        # ★★ 记忆分级跟着引擎走（云端只看 cloud/，私密记忆物理隔离 ✓）
        try:
            _was = MEM_DIR
            set_mem_tier(bool(ENGINE is not None and ENGINE.is_cloud()))
            if os.path.abspath(_was) != os.path.abspath(MEM_DIR):
                # 分级变了 → 左侧列表必须重刷，
                # 否则会列着"云端其实看不到"的会话 ✗（那是隐私漏洞 ✓）
                _cur = self.current_mem
                self.refresh_mem_list()
                if _cur and not is_memory_visible(_cur):
                    # ★ 定案 #4：自动跳到**当前级里的第一个会话**（没有就新建空会话）✓
                    #   物理隔离最彻底 —— 不让用户继续在 private 会话里发消息泄给云端 ✗
                    _vis = list_mems()
                    if _vis:
                        self.current_mem = None
                        self.select_mem(_vis[0])
                        _picked = _vis[0]
                    else:
                        self.current_mem = None
                        self._load_local_data()      # 会新建并选中默认会话 ✓
                        _picked = self.current_mem
                    self.set_status(f"☁ 已切到云端模式：私密记忆（private/）已隐藏，"
                                    f"已自动切到「{_picked or '新会话'}」")
                try:
                    EA.log(f"记忆分级切换 → {os.path.basename(MEM_DIR)} "
                           f"(云端={os.path.basename(MEM_DIR) == CLOUD_TIER})")
                except Exception:
                    pass
                else:
                    self.set_status("☁ 已切到云端模式：只显示 cloud/ 的会话"
                                    if os.path.basename(MEM_DIR) == CLOUD_TIER else
                                    "🔒 已切到本地模式：private/ 和 cloud/ 都能看到")
        except Exception:
            pass
        # 连上云端 → 明确提醒"数据出本机"（纯本地用户看不到这条）
        try:
            if ENGINE is not None and bool(ENGINE.is_cloud()):
                self.add_arch("☁ 已连接**云端 API**：" + ENGINE.base_url
                              + "　—— 对话内容会发送给该服务商，**不再是纯本地**。"
                              + "想全程离线请把 config.json 的 engine 改成 llama 或 freetoken。")
        except Exception:
            pass
        st = load_settings()
        if st.get("engine_kind") != kind:
            # 记住 kind（含自动发现的 "openai"）。注意这只是记住"上次是哪种"，
            # 地址不记 —— 下次启动会重新扫（探测很快，且不会把结果写进 config.json）
            st["engine_kind"] = kind
            save_settings(st)
        return ENGINE is not None

    def init_connect(self, silent=False, auto_retry=True):
        """连接服务并初始化。
        ① 本地数据（记忆列表/默认会话）**无论服务是否可用都先加载**
        ② 服务连不上 → 弹一次提示 + 启动自动重连（也可点「⟳ 刷新」手动重连）
        返回是否连接成功。"""
        global MODELS, MODEL_ID, CTX_LIMIT
        self._load_local_data()
        self._setup_engine(silent=silent)          # ① 先探测引擎（双引擎核心）
        if not check_server():
            self._cancel_reconnect()
            self.set_status("❌ 连不上引擎服务：请先启动本地引擎（llama.cpp / FreeToken / Ollama / LM Studio 等）"
                            + ("（正在自动重试…）" if auto_retry else ""))
            if not silent:
                messagebox.showwarning(
                    "未连接",
                    "连不上引擎服务。\n\n"
                    "请自行启动本地引擎（llama.cpp / FreeToken /\n"
                    "Ollama / LM Studio 等，任选其一）。\n\n"
                    "启动好之后程序会自动连上（也可以点标题栏右边的「⟳ 刷新」手动重连）。")
            if auto_retry:
                self._schedule_reconnect()
            # ★ 连不上也要刷新状态指示 + 启停按钮 —— 否则从云端切到"没启动的本地引擎"时，
            #   按钮会一直留着上一家的「☁ 云端模型」✗（而且轮询也没启动 ✗）
            try:
                self.refresh_model_state()
                self._poll_model_state()
            except Exception:
                pass
            return False
        # 一次请求拿全：模型列表 + GGUF 路径 + 上下文上限 + 正在运行的模型
        # （原来分 4 次请求 /v1/models，服务忙时会把启动拖到十几秒，像卡死）
        self.refresh_model_list()
        return True      # ★ 连上了就算成功 —— 云端没有模型列表也是连上了，不能返回 False

    def refresh_model_list(self):
        """★ 按**当前引擎**重新拉模型列表。

        必须单独成方法 —— 因为切换引擎（模型面板点别的引擎 / 设置窗口切提供商）
        之后也要调它，否则会出现"切到云端了，模型列表还是本地那份 Qwen" ✗
        （用户实测反馈："配置好了云端，这个位置怎么还是显示本地的千问模型"）
        """
        global MODELS, MODEL_ID, CTX_LIMIT
        info = fetch_server_models()
        # 模型列表按体量从低到高排列：最小的排在最前，
        # 这样"从没选过模型"的新用户默认拿到的是最省显存的那个，
        # 而不是恰好排在第一个的大模型（8G 显存档上大模型会溢出到内存分页）
        MODELS = sort_models_low_to_high(info["ids"], info["paths"])
        self._model_paths = info["paths"]
        if info["ctx"]:
            CTX_LIMIT = info["ctx"]
        if MODELS:
            # 默认模型优先级：
            #   ① 上次选择（gui_settings.json）—— 用户选过就绝对尊重
            #   ② 服务端当前正在运行的 —— 沿用它可以省掉一次重新加载
            #   ③ 列表第一个 —— 列表已按体量从低到高排，所以这就是**最小**的模型
            saved = str(load_settings().get("model") or "").strip()
            if saved in MODELS:
                MODEL_ID = saved
                tag = "（上次选择）"
            else:
                run_id = info["running"]
                if run_id in MODELS:
                    MODEL_ID, tag = run_id, "（沿用当前运行）"
                else:
                    MODEL_ID, tag = MODELS[0], "（默认：最小的模型）"
            self.model_choices = [m for m in MODELS if _is_chat_model(m)]
            self.model_var.set(MODEL_ID)
            self.set_status(f"✅ {self._engine_label()} · 模型：{MODEL_ID}{tag} · 自动归档已开启")
            # 后台预热「引擎发现」缓存 —— 打开模型面板时就能瞬时列出各引擎
            # （discover_engines 要 ~1.6 秒，同步跑会卡界面 ✗）
            try:
                threading.Thread(target=discover_engines_cached, daemon=True).start()
            except Exception:
                pass
            st = load_settings()
            _changed = (st.get("model") != MODEL_ID)
            if _changed:
                st["model"] = MODEL_ID
            # ★ 记下本次的引擎+模型，下次如果不一样 → system_base() 会加一句提醒 ✓
            try:
                _cur = f"{ENGINE_KIND or ''}|{MODEL_ID or ''}"
                if str(st.get("_identity_ctx") or "") != _cur:
                    st["_identity_ctx"] = _cur
                    _changed = True
            except Exception:
                pass
            if _changed:
                save_settings(st)
        else:
            # ★ 探测不到模型时**必须清掉过期的模型名** —— 否则会出现
            #   "切到云端了，模型却还挂着本地那个 Qwen 3.8 8B" ✗
            #   （用户实测反馈：配置好云端，这个位置还是显示本地的千问模型）
            #   云端的 /v1/models 常常不可用，所以这里要**
            #   让用户去手动填模型名**，而不是默默沿用上一个引擎的选择。
            prev = self.model_var.get()
            self.model_choices = []
            if prev:
                self.add_arch(f"⚠️ 已连上 {self._engine_label()}，但它没有返回模型列表"
                              f"（云端常见）。当前选中的「{prev}」可能属于上一个引擎，"
                              f"请在「选择模型 → ✎ 手动输入模型名…」里填上正确的模型名。")
            self.model_var.set("")
            try:
                MODEL_ID = ""
            except Exception:
                pass
            self.set_status(f"⚠️ 已连上 {self._engine_label()}，但未探测到模型 —— "
                            f"请在「选择模型」里手动填模型名")
        self.rebuild_reason_combo(self.model_var.get())
        # 正在运行模型的颜色指示：立即刷新一次 + 周期轮询
        self.refresh_model_state()
        self._poll_model_state()
        return True

    # ---------- 刷新 / 自动重连 ----------
    def refresh_all(self):
        """点「⟳ 刷新」：重连服务 + 重读记忆文件列表"""
        self._cancel_reconnect()
        self.set_status("🔄 正在刷新…（重连服务 + 重读记忆文件）")
        try:
            self.update_idletasks()
        except Exception:
            pass
        # 稍微延后执行，让状态提示先画出来（init_connect 里有阻塞的 HTTP）
        self.after(60, lambda: self.init_connect(silent=True, auto_retry=True))

    def _cancel_reconnect(self):
        if self._reconnect_after_id is not None:
            try:
                self.after_cancel(self._reconnect_after_id)
            except Exception:
                pass
            self._reconnect_after_id = None

    def _schedule_reconnect(self):
        """服务没起来时，定时重试；探测放后台线程，避免卡住界面"""
        if self._closing or self._reconnect_after_id is not None:
            return
        try:
            self._reconnect_after_id = self.after(RECONNECT_MS, self._auto_reconnect)
        except Exception:
            self._reconnect_after_id = None

    def _auto_reconnect(self):
        self._reconnect_after_id = None
        if self._closing:
            return

        def work():
            ok = check_server()
            self._ui_call(lambda: self._on_reconnect_probe(ok))

        threading.Thread(target=work, daemon=True).start()

    def _on_reconnect_probe(self, ok):
        if self._closing:
            return
        if ok:
            self.init_connect(silent=True, auto_retry=False)   # 连上了 → 完整初始化
            if MODELS:
                self.add_arch("✅ 已连上引擎服务")
        else:
            self._schedule_reconnect()

    # ---------- 会话管理 ----------
    def refresh_mem_list(self):
        self.mem_list.delete(0, "end")
        for name in list_mems():
            self.mem_list.insert("end", name)
        if not list_mems():
            self.mem_list.insert("end", "(暂无记忆文件)")
        # 「本地用户」按钮上带上已归档数量，并同步刷新弹窗
        try:
            hid = list_hidden_mems()
            self.arch_btn.set_text(f"👤 本地用户 · 已归档 {len(hid)}" if hid else "👤 本地用户")
            self.refresh_arch_dialog()
        except Exception:
            pass

    def new_session(self):
        self.pending_new = True
        self.current_mem = None
        self.title_var.set("新会话（发送第一句话后自动创建记忆文件）")
        self.clear_chat()
        self.add_arch("⊕ 新会话已创建，直接输入第一句话开始")

    def select_mem(self, name):
        if name not in sessions:
            # 从 md 文件恢复对话历史（重启/切换都能找回）
            pairs, boundary = parse_dialogues(name)
            msgs = [{"role": "system", "content": build_system(name)}]
            for r, c in pairs:
                msgs.append({"role": r, "content": c})
            # boundary = 最后归档块前的对话数 → 模型上下文从其后开始（之前内容已入摘要）
            sessions[name] = {"messages": msgs, "turns": len(pairs) // 2,
                              "ctx_start": 1 + boundary}
        self.current_mem = name
        self.pending_new = False
        self.title_var.set(name)
        self.render_session(name)
        # 高亮列表。
        # ★ 不能无脑 selection_clear：那样 Shift/Ctrl 连续选好的多个文件
        #   会被每次切换会话清掉，用户就"选不中多个要归档的文件"了。
        #   目标已经在选中集合里 → 原样保留。
        try:
            cur = self.mem_list.curselection()
        except Exception:
            cur = ()
        for i, item in enumerate(self.mem_list.get(0, "end")):
            if item == name:
                if i not in cur:
                    self.mem_list.selection_clear(0, "end")
                    self.mem_list.selection_set(i)
                self.mem_list.see(i)
                break

    def on_select_mem(self, event):
        """选中记忆文件。列表支持多选，这里始终取**最后一个**（= 刚点的那个）"""
        sel = self.mem_list.curselection()
        if not sel:
            return
        try:
            idx = self.mem_list.index("active")     # 键盘 ↑↓ 时 active 跟着走
        except Exception:
            idx = sel[-1]
        if idx not in sel:
            idx = sel[-1]
        name = self.mem_list.get(idx)
        if name == "(暂无记忆文件)":
            return
        self.select_mem(name)

    def _mem_list_key(self, event):
        """方向键选中后立刻切换（键盘也能连续选择）"""
        try:
            self.after(10, lambda: self.on_select_mem(None))
        except Exception:
            pass
        return None

    def on_right_click(self, event):
        idx = self.mem_list.nearest(event.y)
        # ★ 右键点在**已有的多选里面**时不能改选区，否则连续选好的一批就没了
        try:
            cur = self.mem_list.curselection()
        except Exception:
            cur = ()
        if idx not in cur:
            self.mem_list.selection_clear(0, "end")
            self.mem_list.selection_set(idx)
        else:
            self.mem_list.activate(idx)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def rename_mem(self):
        sel = self.mem_list.curselection()
        if not sel:
            return
        old = self.mem_list.get(sel[0])
        if old == "(暂无记忆文件)":
            return
        new = simpledialog.askstring("重命名", "新的文件名：", initialvalue=old, parent=self)
        if not new:
            return
        if not new.endswith(".md"):
            new += ".md"
        if new == old:
            return
        if os.path.exists(mem_path(new)):
            messagebox.showwarning("重命名", "该文件名已存在！")
            return
        os.rename(mem_path(old), mem_path(new))
        # 归档原文批次目录是按记忆文件名生成的，改名时要跟着改，否则旧归档会"找不到"
        old_d, new_d = arch_dir(old), arch_dir(new)
        try:
            if os.path.isdir(old_d) and not os.path.isdir(new_d):
                os.rename(old_d, new_d)
        except Exception:
            pass
        if old in sessions:
            sessions[new] = sessions.pop(old)
        if self.current_mem == old:
            self.current_mem = new
            self.title_var.set(new)
        self.refresh_mem_list()
        self.select_mem(new)

    def delete_mem(self):
        sel = self.mem_list.curselection()
        if not sel:
            return
        name = self.mem_list.get(sel[0])
        if name == "(暂无记忆文件)":
            return
        if not messagebox.askyesno("删除确认",
                                   f"确定删除「{name}」这个对话记忆吗？\n\n"
                                   "（.md 文件 + 它归档区里的全部归档原文都会被删除，不可恢复）"):
            return
        try:
            os.remove(mem_path(name))
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
            return
        arch_delete_all(name)          # 归档隐藏区一并删除
        sessions.pop(name, None)
        self.refresh_mem_list()
        if self.current_mem == name:
            if DEFAULT_MEM in sessions:
                self.select_mem(DEFAULT_MEM)
            else:
                self.new_session()

    def clear_mem(self):
        """清空该记忆文件的全部内容（对话记录+归档摘要），保留文件本身"""
        sel = self.mem_list.curselection()
        if not sel:
            return
        name = self.mem_list.get(sel[0])
        if name == "(暂无记忆文件)":
            return
        if not messagebox.askyesno("清空确认",
                                   f"确定清空「{name}」的全部记忆内容吗？\n\n"
                                   "（对话记录、归档摘要、以及归档区里的原文都会清空，"
                                   "文件保留，不可恢复）"):
            return
        try:
            with open(mem_path(name), "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            messagebox.showerror("清空失败", str(e))
            return
        arch_delete_all(name)          # 归档隐藏区一并清空
        # 重置会话（保留文件、清空内容）
        sessions[name] = {"messages": [{"role": "system", "content": build_system(name)}],
                          "turns": 0, "ctx_start": 1}
        self._loaded_from = 0
        if self.current_mem == name:
            self.render_session(name)
        self.set_status(f"🧹 已清空记忆：{name}")

    def _archive_count(self, name):
        """该记忆有多少「归档」：隐藏区批次 + .md 里的老式摘要块。

        ★ 必须把 .md 里的 `## 会话归档` 摘要块也算进去：改造前的归档只写了摘要、
          没有隐藏区批次；只数隐藏区的话，那种文件会被误判成"0 个归档"而被闸门拦住。"""
        n = 0
        try:
            n += len(arch_list_batches(name))
        except Exception:
            pass
        try:
            n += len(extract_arch_blocks(load_mem(name)))
        except Exception:
            pass
        return n

    def _require_archive(self, name):
        """安全闸门：**没有任何归档**时不允许清空/删除这个记忆。

        归档是这个程序的"保险"：对话先被提炼成摘要存进 `## 会话归档`（长期记忆），
        之后清掉逐条对话才不会真的丢内容。一个归档都没有就清，等于把内容直接抹掉。

        ★ 例外：**已经没有对话可归档**时直接放行 —— 此时没有任何东西会被弄丢，
          再拦着只会让人既归档不了、也删不掉文件（用户踩过这个死结）。"""
        if self._archive_count(name) > 0:
            return True
        try:
            leftover = len(parse_dialogues(name)[0])
        except Exception:
            leftover = 0
        if leftover == 0:
            return True                     # 空文件：删了不丢任何东西
        messagebox.showinfo(
            "请先归档",
            f"「{name}」里还有 {leftover} 条对话，但一个归档都没有。\n\n"
            "归档是这个程序的「保险」：\n"
            "  对话内容 → 提炼成归档摘要（长期记忆）→ 之后清掉对话记录才不会丢东西。\n\n"
            "现在清掉就真的找不回来了，所以拦住了。\n\n"
            "想继续的话：右键这个记忆文件 →「归档」，\n"
            "它会先把对话归档（原文进归档区、摘要进长期记忆），再清掉对话记录。")
        return False

    def clear_dialog_only(self):
        """「只保留归档对话」= **归档 + 清掉逐条对话，只留归档**。

        ★ 以前这里直接清对话，但被"没归档就不许清"的安全闸门拦住 —— 而归档原本
          只有"上下文将满"时才自动发生，于是用户永远清不了。
          现在改成：还有没归档的对话，就**先让模型把它们提炼成归档摘要**（保险），
          归档完成后再清掉逐条记录。所以这个操作永远可用，也不会丢内容。"""
        sel = self.mem_list.curselection()
        if not sel:
            return
        name = self.mem_list.get(sel[0])
        if name == "(暂无记忆文件)":
            return
        n_pair = len(parse_dialogues(name)[0])
        if n_pair == 0:
            # 没有逐条对话了，但 .md 里可能还留着改造前的老摘要块（没进归档区）
            blocks = extract_arch_blocks(load_mem(name))
            if blocks and not arch_list_batches(name):
                if messagebox.askyesno(
                        "导入旧归档",
                        f"「{name}」里没有未归档的对话，但有 {len(blocks)} 个"
                        "**改造前留下的归档摘要**（还没进归档区）。\n\n"
                        "要把它们导入归档区吗？导入后就能在左下角「本地用户 · 归档」"
                        "里看到、并单独删除了。\n\n"
                        "（改造前的老归档只保存了摘要，当时的对话原文已经不在。）"):
                    n = arch_import_blocks(name)
                    self.refresh_arch_panel()
                    self.set_status(f"📥 已把 {n} 个旧归档导入归档区")
                return
            messagebox.showinfo("无需处理", f"「{name}」里既没有未归档的对话，也没有旧归档可导入。")
            return
        # 还有没归档的对话吗？
        s = sessions.get(name)
        pending = [m for m in self._ctx_messages(s) if m["role"] != "system"] if s else []
        if pending:
            if not messagebox.askyesno(
                    "归档并只保留归档",
                    f"「{name}」还有 {len(pending)} 条对话没有归档。\n\n"
                    "点「是」会先让模型把这些对话提炼成**归档摘要**（长期记忆），\n"
                    "完成后再清掉逐条对话记录 —— 也就是只保留归档。\n\n"
                    "· 归档摘要由模型生成，可能需要 10-60 秒\n"
                    "· 清掉的是逐条对话记录，归档摘要会完整保留"):
                return
            self._after_archive = ("clear_dialog", name)
            self._do_summary(name, manual=True)
            return
        self._do_clear_dialog(name)

    def _do_clear_dialog(self, name):
        """真正执行"清掉逐条对话、保留全部归档"（归档已就绪时才调用）"""
        try:
            cleared, kept = clear_dialog_keep_arch(name)
        except Exception as e:
            messagebox.showerror("清空失败", str(e))
            return
        sessions[name] = {"messages": [{"role": "system", "content": build_system(name)}],
                          "turns": 0, "ctx_start": 1}
        self._loaded_from = 0
        if self.current_mem == name:
            self.render_session(name)
        self.set_status(f"🧹 已清空 {cleared} 条对话")

    # ---------- 对话渲染 ----------
    def clear_chat(self):
        # 重渲染前先停掉「思考中」动效：否则定时器会把文字插进新渲染的会话里
        try:
            self._stop_think(clear=False)
        except Exception:
            pass
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        try:
            self.chat.mark_unset("_ai_mark")
        except Exception:
            pass

    def add_msg(self, role, content, meta=""):
        """插入一条消息文字（操作条由 add_op_bar 单独插入到其下方的独立一行）"""
        # 去掉尾部换行/空白：模型偶尔会在结尾多吐换行，
        # 不清掉的话气泡下方会留出一大片空白，浪费界面
        if content:
            content = content.rstrip()
        self.chat.configure(state="normal")
        if role == "user":
            self.chat.insert("end", content + "\n", "user")
        else:
            self.chat.insert("end", content + "\n", "ai")
        if meta:
            self.chat.insert("end", meta + "\n", "meta")
        self.chat.configure(state="disabled")
        self._see_end()

    def add_arch(self, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", "— " + text + " —\n", "arch")
        self.chat.configure(state="disabled")
        self._see_end()

    # ---------- 归档查看面板（底部「本地用户 · 归档」按钮）----------
    def _selected_name(self):
        sel = self.mem_list.curselection()
        return self.mem_list.get(sel[0]) if sel else None

    # ---------- 「本地用户」弹窗：已归档（隐藏）的文件都在这儿管理 ----------
    def open_arch_dialog(self):
        """点左下角「本地用户」→ 弹出「归档库」：
        ① 已归档（隐藏）的**文件**  ② 当前文件的**归档记忆**（摘要 + 原文）
        """
        dlg = getattr(self, "arch_dlg", None)
        try:
            if dlg is not None and dlg.winfo_exists():
                dlg.lift()
                dlg.focus_set()
                self.refresh_arch_dialog()
                return
        except Exception:
            pass
        dlg = tk.Toplevel(self)
        self.arch_dlg = dlg
        dlg.title("本地用户 · 归档库")
        dlg.configure(bg="#fafafa")
        try:
            dlg.transient(self)
        except Exception:
            pass
        try:
            x = self.winfo_rootx() + 260
            y = self.winfo_rooty() + 110
        except Exception:
            x = y = 200
        dlg.geometry(f"600x680+{x}+{y}")
        dlg.minsize(520, 440)
        dlg.protocol("WM_DELETE_WINDOW", self.close_arch_dialog)

        # 先为底部提示留出空间，缩小窗口时也不能被预览/列表挤掉。
        hint = tk.Label(dlg, text="双击 = 打开文件 / 显示内容　　右键列表 = 更多操作　　"
                                 "选中一条归档记忆 = 下方显示它的摘要（有原文则一并显示）",
                        bg="#fafafa", fg="#b5b5b5", justify="left", anchor="w",
                        font=("Microsoft YaHei UI", 8))
        hint.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(1, e.width)))

        # ★ 除先预留的底部提示外，正文自上而下；只有预览框 expand。
        #   之前把底部控件用 side="bottom" 插进来，pack 顺序被倒置，整块上下颠倒了。
        tk.Label(dlg, text="🗄 本地用户 · 归档库", bg="#fafafa", fg="#1f1f1f",
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 4))

        # ===== ① 已归档（隐藏）的文件 =====
        tk.Label(dlg, text="① 已归档（隐藏）的文件", bg="#fafafa", fg="#3b6ea5",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=16)
        self.arch_dlg_stat = tk.Label(dlg, text="", bg="#fafafa", fg="#9a9a9a",
                                      font=("Microsoft YaHei UI", 9))
        self.arch_dlg_stat.pack(anchor="w", padx=16, pady=(0, 6))

        file_wrap = tk.Frame(dlg, bg="#ffffff")
        file_wrap.pack(fill="x", padx=16)
        self.arch_dlg_list = tk.Listbox(file_wrap, bg="#ffffff", fg="#2b2b2b", height=4,
                                        selectmode="extended",   # Shift 连续选 / Ctrl 点选
                                        selectbackground="#e6ecf5", selectforeground="#1f1f1f",
                                        relief="flat", activestyle="none", bd=0, takefocus=True,
                                        highlightthickness=1, highlightbackground="#e5e5e5",
                                        font=("Microsoft YaHei UI", 9))
        fsb = ttk.Scrollbar(file_wrap, orient="vertical", command=self.arch_dlg_list.yview)
        self.arch_dlg_list.configure(yscrollcommand=fsb.set)
        self.arch_dlg_list.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        # 单击 = 直接切到这个文件（② 跟着刷新）；双击/回车同效（再点一次也切）
        self.arch_dlg_list.bind("<<ListboxSelect>>", self.on_pick_arch_mem)
        self.arch_dlg_list.bind("<Double-Button-1>", self.on_open_arch_mem)
        self.arch_dlg_list.bind("<Return>", self.on_open_arch_mem)
        self.arch_dlg_list.bind("<Button-3>", self.on_right_click_arch)

        fb = tk.Frame(dlg, bg="#fafafa")
        fb.pack(fill="x", padx=16, pady=(6, 4))
        b_open = RoundedButton(fb, text="打开", command=self.on_open_arch_mem,
                               width=64, height=28, radius=9, bg="#fafafa", fill="#ffffff",
                               font=("Microsoft YaHei UI", 9))
        b_open.pack(side="left")
        _tip(b_open, "打开这个已归档文件，右侧显示它的最近对话精简版")
        b_un = RoundedButton(fb, text="取消归档", command=self.unarchive_mem,
                             width=76, height=28, radius=9, bg="#fafafa", fill="#ffffff",
                             font=("Microsoft YaHei UI", 9))
        b_un.pack(side="left", padx=(6, 0))
        _tip(b_un, "把这个文件移回主列表（不再隐藏）")
        b_del = RoundedButton(fb, text="删除归档", command=self.delete_arch_mem,
                              width=76, height=28, radius=9, bg="#fafafa", fill="#fff1f0",
                              fg="#b3261e", font=("Microsoft YaHei UI", 9))
        b_del.pack(side="left", padx=(6, 0))
        _tip(b_del, "删除这个已归档文件（连同它的归档原文），不可恢复")
        b_clr = RoundedButton(fb, text="一键全部清空归档", command=self.clear_all_arch_mem,
                              width=134, height=28, radius=9, bg="#fafafa", fill="#fff1f0",
                              fg="#b3261e", font=("Microsoft YaHei UI", 9))
        b_clr.pack(side="left", padx=(6, 0))
        _tip(b_clr, "把所有已归档文件全部删除，不可恢复")

        tk.Frame(dlg, bg="#e8e8e8", height=1).pack(fill="x", padx=16, pady=(8, 2))

        # ===== ② 当前文件的归档记忆（摘要 + 原文）=====
        self.arch_rec_head = tk.Label(dlg, text="② 当前文件的归档记忆", bg="#fafafa",
                                      fg="#3b6ea5",
                                      font=("Microsoft YaHei UI", 10, "bold"))
        self.arch_rec_head.pack(anchor="w", padx=16, pady=(4, 4))

        # 面包屑 + 上一级按钮（年月日目录树）
        nav = tk.Frame(dlg, bg="#fafafa")
        nav.pack(fill="x", padx=16, pady=(0, 4))
        # 面包屑做成一串可点的标签：点「2026」直接跳到年层，不用一级级退
        self.rec_crumb = tk.Frame(nav, bg="#fafafa")
        self.rec_crumb.pack(side="left")
        self.rec_path_lb = self.rec_crumb          # 兼容旧引用
        b_up = RoundedButton(nav, text="⬆ 上一级", command=self.rec_up,
                             width=78, height=24, radius=8, bg="#fafafa", fill="#ffffff",
                             font=("Microsoft YaHei UI", 8))
        b_up.pack(side="right")
        _tip(b_up, "回到上一级目录（快捷键 ← 或 退格）\n"
                   "进入下一级：→ 或 回车，或双击目录")

        rec_wrap = tk.Frame(dlg, bg="#ffffff")
        rec_wrap.pack(fill="x", padx=16)
        self.arch_rec_list = tk.Listbox(rec_wrap, bg="#ffffff", fg="#2b2b2b", height=5,
                                        selectbackground="#e6ecf5", selectforeground="#1f1f1f",
                                        relief="flat", activestyle="none", bd=0, takefocus=True,
                                        highlightthickness=1, highlightbackground="#e5e5e5",
                                        font=("Microsoft YaHei UI", 9))
        rsb = ttk.Scrollbar(rec_wrap, orient="vertical", command=self.arch_rec_list.yview)
        self.arch_rec_list.configure(yscrollcommand=rsb.set)
        self.arch_rec_list.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")
        self.arch_rec_list.bind("<<ListboxSelect>>", self.on_rec_select)
        # 目录树导航：→/回车 进下一级，←/退格 回上一级，双击=进入
        for _k in ("<Right>", "<Return>"):
            self.arch_rec_list.bind(_k, lambda e: self.rec_enter(), add="+")
        for _k in ("<Left>", "<BackSpace>"):
            self.arch_rec_list.bind(_k, lambda e: self.rec_up(), add="+")
        self.arch_rec_list.bind("<Double-Button-1>", lambda e: self.rec_enter(), add="+")

        rbtn = tk.Frame(dlg, bg="#fafafa")
        rbtn.pack(fill="x", padx=16, pady=(6, 4))
        b_cp = RoundedButton(rbtn, text="复制内容", command=self.copy_rec,
                             width=84, height=28, radius=9, bg="#fafafa", fill="#ffffff",
                             font=("Microsoft YaHei UI", 9))
        b_cp.pack(side="left")
        _tip(b_cp, "把下方预览框里的内容复制到剪贴板")
        b_ex = RoundedButton(rbtn, text="导出全部归档", command=self.export_arch,
                             width=108, height=28, radius=9, bg="#fafafa", fill="#ffffff",
                             font=("Microsoft YaHei UI", 9))
        b_ex.pack(side="left", padx=(6, 0))
        _tip(b_ex, "把当前记忆的全部归档（摘要 + 原文）导出成一个 .md 文件")

        # 预览框：唯一 expand 的控件，窗口变大它就变大
        prev_wrap = tk.Frame(dlg, bg="#ffffff")
        prev_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        self.arch_rec_text = tk.Text(prev_wrap, height=8, wrap="word", bg="#ffffff",
                                     fg="#3b3b3b", relief="flat", bd=0,
                                     highlightthickness=1, highlightbackground="#e5e5e5",
                                     font=("Microsoft YaHei UI", 9), padx=6, pady=4)
        tsb = ttk.Scrollbar(prev_wrap, orient="vertical", command=self.arch_rec_text.yview)
        self.arch_rec_text.configure(yscrollcommand=tsb.set, state="disabled")
        self.arch_rec_text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")

        self.refresh_arch_dialog()

    def close_arch_dialog(self):
        try:
            if self.arch_dlg is not None:
                self.arch_dlg.destroy()
        except Exception:
            pass
        self.arch_dlg = None

    # ---------- ② 归档记忆：年 / 月 / 日 目录树 ----------
    def _rec_tree_build(self, recs):
        """按 年/月/日 分组；没有日期的（如「开篇总纲」）单独放在根层"""
        tree, loose = {}, []
        for r in recs:
            ts = r.get("ts") or ""
            mo = re.match(r"(\d{4})-(\d{2})-(\d{2})", ts)
            if not mo:
                loose.append(r)
                continue
            y, m, d = mo.group(1), mo.group(2), mo.group(3)
            tree.setdefault(y, {}).setdefault(m, {}).setdefault(d, []).append(r)
        return tree, loose

    def _current_rec_nodes(self):
        """当前层要显示的节点：[("dir", 标签, key) | ("leaf", 标签, rec)]"""
        tree = getattr(self, "_rec_tree", {})
        loose = getattr(self, "_rec_loose", [])
        path = getattr(self, "_rec_path", [])
        out = []
        try:
            if len(path) == 0:
                for y in sorted(tree, reverse=True):
                    cnt = sum(len(v) for mm in tree[y].values() for v in mm.values())
                    out.append(("dir", f"📁 {y} 年　({cnt} 条)", y))
                for r in loose:
                    out.append(("leaf", r["label"], r))
            elif len(path) == 1:
                for m in sorted(tree.get(path[0], {}), reverse=True):
                    cnt = sum(len(v) for v in tree[path[0]][m].values())
                    out.append(("dir", f"📁 {m} 月　({cnt} 条)", m))
            elif len(path) == 2:
                for d in sorted(tree.get(path[0], {}).get(path[1], {}), reverse=True):
                    cnt = len(tree[path[0]][path[1]][d])
                    out.append(("dir", f"📁 {d} 日　({cnt} 条)", d))
            else:
                lst = tree.get(path[0], {}).get(path[1], {}).get(path[2], [])
                for r in sorted(lst, key=lambda x: x.get("ts") or "", reverse=True):
                    lab = r["label"]
                    # 已经在"日"这一层了，标签里的日期是多余的 → 只留时间
                    lab = re.sub(r"^([^ ]+ )\d{4}-\d{2}-\d{2} ", r"\1", lab)
                    out.append(("leaf", lab, r))
        except Exception:
            pass
        return out

    def rec_enter(self):
        """进入下一级目录；已经是叶子就显示内容"""
        try:
            sel = self.arch_rec_list.curselection()
            nodes = getattr(self, "_rec_nodes", [])
            if not sel or sel[0] >= len(nodes):
                return
            kind, _label, payload = nodes[sel[0]]
            if kind == "dir":
                self._rec_path = getattr(self, "_rec_path", []) + [payload]
                self.refresh_rec_list()
            else:
                self.on_rec_select()
        except Exception:
            pass

    def _rec_render_crumb(self):
        """画面包屑：📁 全部 / 2026 / 10 / 08，每一级都能点（最后一级不可点）"""
        try:
            for w in self.rec_crumb.winfo_children():
                w.destroy()
            path = list(getattr(self, "_rec_path", []))
            names = ["📁 全部"] + path
            for i, nm in enumerate(names):
                if i:
                    tk.Label(self.rec_crumb, text=" / ", bg="#fafafa", fg="#c0c0c0",
                             font=("Microsoft YaHei UI", 9)).pack(side="left")
                last = (i == len(names) - 1)
                lb = tk.Label(self.rec_crumb, text=nm, bg="#fafafa",
                              fg="#9a9a9a" if last else "#3b6ea5",
                              cursor="arrow" if last else "hand2",
                              font=("Microsoft YaHei UI", 9,
                                    "normal" if last else "underline"))
                lb.pack(side="left")
                if not last:
                    lb.bind("<Button-1>", lambda e, lv=i: self.rec_goto(lv))
                    _tip(lb, f"跳到「{nm}」这一层")
            if not getattr(self, "_rec_nodes", None):
                tk.Label(self.rec_crumb, text="　（空）", bg="#fafafa", fg="#b5b5b5",
                         font=("Microsoft YaHei UI", 9)).pack(side="left")
        except Exception:
            pass

    def rec_goto(self, level):
        """点面包屑：直接跳到第 level 层（0 = 全部）"""
        try:
            self._rec_path = list(getattr(self, "_rec_path", []))[:level]
            self.refresh_rec_list()
        except Exception:
            pass

    def rec_up(self):
        """回到上一级目录"""
        try:
            p = getattr(self, "_rec_path", [])
            if p:
                self._rec_path = p[:-1]
                self.refresh_rec_list()
        except Exception:
            pass

    def refresh_rec_list(self):
        """按当前目录层级重画列表 + 面包屑"""
        try:
            nodes = self._current_rec_nodes()
            self._rec_nodes = nodes
            self.arch_rec_list.delete(0, "end")
            for _k, label, _p in nodes:
                self.arch_rec_list.insert("end", label)
            self._rec_render_crumb()
            if nodes:
                self.arch_rec_list.selection_clear(0, "end")
                self.arch_rec_list.selection_set(0)
                self.arch_rec_list.activate(0)
            self.on_rec_select()
        except Exception:
            pass

    def on_rec_select(self, event=None):
        """选中一条归档记忆 → 显示内容；选中的是目录就提示怎么进"""
        try:
            sel = self.arch_rec_list.curselection()
            nodes = getattr(self, "_rec_nodes", [])
            self.arch_rec_text.configure(state="normal")
            self.arch_rec_text.delete("1.0", "end")
            if sel and sel[0] < len(nodes):
                kind, label, payload = nodes[sel[0]]
                if kind == "dir":
                    self.arch_rec_text.insert(
                        "end", f"{label}\n\n这是一个目录。\n"
                               f"· 进入下一级：按 → 或 回车，或双击\n"
                               f"· 回到上一级：按 ← 或 退格，或点右上「⬆ 上一级」")
                else:
                    r = payload
                    if r.get("summary"):
                        self.arch_rec_text.insert("end", "── 提炼摘要 ──\n" + r["summary"] + "\n\n")
                    if r.get("body"):
                        self.arch_rec_text.insert("end", "── 原文 ──\n" + r["body"])
                    if not r.get("summary") and not r.get("body"):
                        self.arch_rec_text.insert("end", "（这条归档没有内容）")
            self.arch_rec_text.configure(state="disabled")
        except Exception:
            try:
                self.arch_rec_text.configure(state="disabled")
            except Exception:
                pass

    def copy_rec(self):
        """复制预览框里的内容"""
        try:
            txt = self.arch_rec_text.get("1.0", "end-1c").strip()
            if not txt:
                self.set_status("⚠️ 没有选中的归档记忆")
                return
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.set_status("📋 已复制归档内容到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def refresh_arch_dialog(self):
        """刷新弹窗内容（弹窗没开就什么都不做）"""
        dlg = getattr(self, "arch_dlg", None)
        if dlg is None:
            return
        try:
            if not dlg.winfo_exists():
                self.arch_dlg = None
                return
            lst = self.arch_dlg_list
            cur = lst.curselection()
            keep = lst.get(cur[0]) if cur else None
            lst.delete(0, "end")
            hid = list_hidden_mems()
            for i, nm in enumerate(hid):
                lst.insert("end", nm)
                if nm == keep:
                    lst.selection_set(i)
            self.arch_dlg_stat.configure(
                text=f"共 {len(hid)} 个已归档文件" if hid else "目前没有已归档（隐藏）的文件")
            # ---- 刷新「当前文件的归档记忆」（年/月/日 目录树）----
            recs = arch_records(self.current_mem) if self.current_mem else []
            self._arch_recs = recs
            self._rec_tree, self._rec_loose = self._rec_tree_build(recs)
            # 只有**换了文件**才把目录退回根；同一文件刷新时保留当前层级
            if getattr(self, "_rec_for", None) != self.current_mem:
                self._rec_path = []
                self._rec_for = self.current_mem
            self._rec_path = getattr(self, "_rec_path", [])
            nm = self.current_mem or "（未选择）"
            self.arch_rec_head.configure(
                text=f"② 当前文件「{nm}」的归档记忆　共 {len(recs)} 条"
                     + ("" if recs else "　（这个文件还没有归档过）"))
            self.refresh_rec_list()
        except Exception:
            pass

    def on_pick_arch_mem(self, event=None):
        """① 里**单击**某个已归档文件 → 立刻切换过去（右侧显示精简版），② 跟着刷新

        这样不用先"打开"再关弹窗：点一下就等于切换当前记忆文件。
        """
        try:
            sel = self.arch_dlg_list.curselection()
            if not sel:
                return
            try:
                idx = self.arch_dlg_list.index("active")   # 刚点的那个
            except Exception:
                idx = sel[-1]
            if idx not in sel:
                idx = sel[-1]
            name = self.arch_dlg_list.get(idx)
            if name == self.current_mem:
                return                      # 已经是当前文件，不重复切
            self.select_mem(name)
            self.refresh_arch_dialog()      # ② 换成这个文件的归档记忆
            self.set_status(f"🔀 已切换到已归档文件：{name}")
        except Exception:
            pass

    def on_open_arch_mem(self, event=None):
        """打开选中的已归档文件（右侧显示精简版）"""
        try:
            sel = self.arch_dlg_list.curselection()
            if not sel:
                return
            self.select_mem(self.arch_dlg_list.get(sel[0]))
        except Exception:
            pass

    def _selected_arch_name(self):
        try:
            sel = self.arch_dlg_list.curselection()
            return self.arch_dlg_list.get(sel[0]) if sel else None
        except Exception:
            return None

    def _selected_arch_names(self):
        """① 列表里当前选中的**全部**文件名（支持 Shift 连续选 / Ctrl 点选）"""
        try:
            return [self.arch_dlg_list.get(i) for i in self.arch_dlg_list.curselection()]
        except Exception:
            return []

    def on_right_click_arch(self, event):
        try:
            idx = self.arch_dlg_list.nearest(event.y)
            self.arch_dlg_list.selection_clear(0, "end")
            self.arch_dlg_list.selection_set(idx)
            self.menu_arch.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.menu_arch.grab_release()
            except Exception:
                pass

    # ---------- 归档（隐藏）/ 取消归档 ----------
    def _selected_mem_names(self):
        """侧栏里当前选中的**全部**记忆文件（支持 Shift 连续选 / Ctrl 点选）"""
        try:
            out = [self.mem_list.get(i) for i in self.mem_list.curselection()]
        except Exception:
            return []
        return [x for x in out if x and x != "(暂无记忆文件)"]

    def archive_mem(self):
        """右键「归档」：把选中的记忆文件挪进隐藏区（支持一次归档多个）"""
        names = self._selected_mem_names()
        if not names:
            name = self._selected_name()
            if not name or name == "(暂无记忆文件)":
                return
            names = [name]
        head = f"「{names[0]}」" if len(names) == 1 else f"选中的 {len(names)} 个文件"
        if not messagebox.askyesno(
                "归档确认",
                f"确定把 {head} 归档（隐藏）吗？\n\n"
                "· 文件会移到 memories\\_archived\\\n"
                "· 左侧「👤 本地用户 → ① 已归档」里仍然看得到、点得开\n"
                "· 随时可以「取消归档」还原"):
            return
        ok_n, errs = 0, []
        for name in names:
            ok, err = hide_mem(name)
            if ok:
                sessions.pop(name, None)
                ok_n += 1
            else:
                errs.append(f"{name}：{err}")
        self.refresh_mem_list()
        self.refresh_arch_dialog()
        if errs:
            messagebox.showerror("部分归档失败", "\n".join(errs[:5]))
        self.set_status(f"🗄 已归档 {ok_n} 个文件" + (f"（{len(errs)} 个失败）" if errs else ""))

    def unarchive_mem(self):
        """取消归档：从隐藏区挪回主列表"""
        names = self._selected_arch_names()
        if not names:
            return
        done, errs = 0, []
        for name in names:
            ok, err = unhide_mem(name)
            if ok:
                sessions.pop(name, None)
                done += 1
            else:
                errs.append(f"{name}：{err}")
        self.refresh_mem_list()
        self.refresh_arch_dialog()
        if errs:
            messagebox.showerror("取消归档失败", "\n".join(errs[:5]))
        self.set_status(f"📤 已取消归档 {done} 个文件"
                        + (f"（{len(errs)} 个失败）" if errs else ""))

    def delete_arch_mem(self):
        """删除归档：把选中的已归档文件**连同它的归档原文批次**一起删掉"""
        names = self._selected_arch_names()
        if not names:
            return
        n_batch = 0
        for nm0 in names:
            try:
                n_batch += len(arch_list_batches(nm0))
            except Exception:
                pass
        head = f"「{names[0]}」" if len(names) == 1 else f"选中的 {len(names)} 个文件"
        if not messagebox.askyesno(
                "删除归档确认",
                f"确定删除已归档的 {head} 吗？\n\n"
                f"· 隐藏区里的 .md 会被删除\n"
                f"· 它们的 {n_batch} 批归档原文也会一并删除\n"
                "· 不可恢复"):
            return
        dead = 0
        for name in names:
            try:
                os.remove(os.path.join(hidden_mem_dir(), name))
                dead += 1
            except Exception as e:
                print("删除失败", name, e)
            try:
                arch_delete_all(name)
            except Exception:
                pass
            sessions.pop(name, None)
            if self.current_mem == name:
                self.current_mem = None
        self.refresh_mem_list()
        self.refresh_arch_dialog()
        if self.current_mem is None:
            if list_mems():
                self.select_mem(list_mems()[0])
            else:
                self.new_session()
        self.set_status(f"🗑 已删除 {dead} 个归档（含 {n_batch} 批原文）")

    def clear_all_arch_mem(self):
        """一键全部清空归档：把所有已归档文件 + 全部归档原文批次都删掉"""
        hid = list_hidden_mems()
        if not hid:
            messagebox.showinfo("没有归档", "当前没有已归档（隐藏）的记忆文件。")
            return
        n_batch = 0
        for nm in hid:
            try:
                n_batch += len(arch_list_batches(nm))
            except Exception:
                pass
        if not messagebox.askyesno(
                "一键全部清空归档",
                f"确定清空**全部 {len(hid)} 个**已归档文件吗？\n\n"
                f"· {len(hid)} 个 .md 会被删除\n"
                f"· 它们的 {n_batch} 批归档原文也会一并删除\n"
                "· 不可恢复"):
            return
        dead = 0
        for nm in hid:
            try:
                os.remove(os.path.join(hidden_mem_dir(), nm))
                dead += 1
            except Exception:
                pass
            try:
                arch_delete_all(nm)
            except Exception:
                pass
            sessions.pop(nm, None)
        if self.current_mem in hid:
            self.current_mem = None
        self.refresh_mem_list()
        self.refresh_arch_dialog()
        if list_mems():
            self.select_mem(list_mems()[0])
        else:
            self.new_session()
        self.set_status(f"🗑 已一键清空 {dead} 个归档（含 {n_batch} 批原文）")

    # ---------- 复制原文 / 导出全部（针对记忆文件本身）----------
    def copy_arch(self):
        """复制原文：把当前记忆文件的内容整份复制到剪贴板"""
        name = self.current_mem
        if not name:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(load_mem(name))
            self.set_status(f"📋 已复制「{name}」的全部原文")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def export_arch(self):
        """导出全部：把当前记忆文件 + 它的归档原文批次导出成一个 .md"""
        name = self.current_mem
        if not name:
            return
        batches = []
        try:
            batches = arch_list_batches(name)
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            title="导出全部", defaultextension=".md",
            initialfile=f"{os.path.splitext(name)[0]}_导出.md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\n")
                if is_hidden_mem(name):
                    f.write("（这个记忆已归档 / 隐藏）\n\n")
                f.write("## 记忆文件内容\n\n")
                f.write(load_mem(name) or "（空）")
                if batches:
                    f.write(f"\n\n---\n\n# 归档原文（{len(batches)} 批）\n")
                    for bt in batches:
                        f.write(f"\n## {bt.get('ts') or '?'}　{bt.get('count', 0)} 条\n\n")
                        if bt.get("summary"):
                            f.write("### 摘要\n" + bt["summary"] + "\n\n")
                        f.write("### 原文\n" + (arch_batch_body(bt["path"]) or "") + "\n")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.set_status(f"💾 已导出「{name}」（含 {len(batches)} 批归档原文）")


    def _set_top_hint(self, text):
        """更新顶部提示（固定 Label，不参与文本插入，避免上翻时顶部行删插抖动）"""
        self.top_hint_var.set("— " + text + " —" if text else "")

    def _at_bottom(self):
        """视图是否已经在底部（yview 的第二个值到 1.0 即到底）"""
        try:
            return self.chat.yview()[1] >= 0.999
        except Exception:
            return True

    def _see_end(self, force=False):
        """只有"跟随底部"时才自动滚到底。
        流式输出期间如果用户正在往上翻，绝不能把他拽回底部 —— 这是"输出时无法上翻、
        拖进度条就跳到底"的根因。force=True 用于切换会话/清屏等应当归位的场景。"""
        if force or self._follow:
            try:
                self.chat.see("end")
            except Exception:
                pass

    def _sb_scroll(self, *args):
        """滚动条拖动联动 + 拖到顶部时加载更早记录"""
        self.chat.yview(*args)
        self._follow = self._at_bottom()          # 拖动后重新判定是否跟随底部
        if args and args[0] == "moveto" and float(args[1]) <= 0.001:
            self._schedule_load_more()

    def _on_wheel(self, event):
        """滚到聊天区顶部时，加载更早的对话记录"""
        self._follow = self._at_bottom()
        if event.delta > 0 and self.chat.yview()[0] <= 0.001:
            self._schedule_load_more()

    def _schedule_load_more(self):
        """上翻加载真防抖：持续滚动/拖动期间不断推迟，停手 120ms 后才真正加载。
        这样滚动条拖动过程中不会被插入的历史内容"抢位置"，也不会每刻度都重排。"""
        if self._load_more_after_id is not None:
            self.after_cancel(self._load_more_after_id)
        self._load_more_after_id = self.after(120, self._do_load_more)

    def _do_load_more(self):
        self._load_more_after_id = None
        self.load_more_history()

    # ---------- 文本操作条（复制/编辑/重新思考/删除：纯文字+标签点击，零嵌入控件） ----------
    # 为什么不用嵌入控件：嵌入 Tk 控件会让滚动开销暴涨（实测 600 条消息滚动往返
    # 嵌入控件 4578ms vs 纯文本 479ms，慢约 10 倍）。纯文本方案历史上千条也不卡。
    OP_SEP = "    "

    def _op_buttons(self, role):
        """该角色操作条的按钮列表 [(显示文字, 动作键), ...]"""
        if role == "user":
            return [("⧉ 复制", "copy"), ("✎ 编辑", "edit"), ("✕ 删除", "del")]
        return [("⧉ 复制", "copy"), ("↻ 重新思考", "rethink"), ("✕ 删除", "del")]

    def _op_line(self, role):
        """构造操作条文本 + 每个按钮在行内的字符区间 [(起, 止, 动作键), ...]"""
        line, spans = "", []
        for i, (label, key) in enumerate(self._op_buttons(role)):
            if i:
                line += self.OP_SEP
            s = len(line)
            line += label
            spans.append((s, len(line), key))
        return line, spans

    def _insert_op_bar(self, at, role, hidx):
        """在 at 处插入一行操作条：逐段插入并带上各自颜色标签
        （逐段插入比"整行插入后再算字符区间"稳得多，不用做索引运算）"""
        tag = f"opm{hidx}"
        common = ("opbar", "op_user" if role == "user" else "op_ai", tag)
        for i, (label, key) in enumerate(self._op_buttons(role)):
            if i:
                self.chat.insert(at, self.OP_SEP, common)
            self.chat.insert(at, label, common + (f"opc_{key}",))
        self.chat.insert(at, "\n", common)
        return tag

    def add_op_bar(self, role, hidx):
        """在消息下方插入一行纯文本操作条（不创建任何控件）"""
        self.chat.configure(state="normal")
        tag = self._insert_op_bar("end", role, hidx)
        self.chat.configure(state="disabled")
        self._bind_op_tag(tag, role, hidx)

    def _bind_op_tag(self, tag, role, hidx):
        """给一行操作条绑定点击/悬停行为（标签级绑定，不占控件）"""
        self.chat.tag_configure(tag, background="")   # 重置，避免上次高亮残留
        # 动态创建的操作条标签会排到 sel 之上，重新把选区高亮提上来
        try:
            self.chat.tag_raise("sel")
        except Exception:
            pass
        self.chat.tag_bind(tag, "<Button-1>",
                           lambda e, h=hidx, r=role: self._on_op_click(e, h, r))
        # 悬停：只高亮鼠标所在的那个按钮（整行高亮反馈太弱、看不出点了哪个）
        self.chat.tag_bind(tag, "<Motion>",
                           lambda e, h=hidx, r=role: self._on_op_hover(e, h, r))
        self.chat.tag_bind(tag, "<Leave>", lambda e: self._op_leave())
        self.chat.tag_bind(tag, "<ButtonRelease-1>", lambda e: self._op_clear("oppress"))

    def _op_leave(self):
        self._op_clear("ophover")
        self._op_hl = None

    def _op_flash(self, line, s, e, tag, ms=None):
        """给操作条某个按钮区间闪一下背景色（点击反馈）。
        ms=None 表示不自动清除（悬停用）；注意必须 tag_raise，
        否则会被后来的 opm* 标签（background=""）盖掉。"""
        try:
            frm, to = f"{line}.{s}", f"{line}.{e}"
            self.chat.tag_raise(tag)
            self.chat.tag_add(tag, frm, to)
            if ms:
                self.after(ms, lambda: self._op_clear(tag))
        except Exception:
            pass

    def _op_clear(self, tag, *tags):
        try:
            self.chat.tag_remove(tag, "1.0", "end")
        except Exception:
            pass

    def _op_hit(self, event, role):
        """点击/悬停位置 → (行号, 起始列, 结束列, 动作键)；不在按钮上返回 None"""
        try:
            idx = self.chat.index(f"@{event.x},{event.y}")
            line, col = idx.split(".")
            col = int(col)
        except Exception:
            return None
        _, spans = self._op_line(role)
        for s, e, key in spans:
            if s <= col < e:
                return line, s, e, key
        return None

    def _on_op_hover(self, event, hidx, role):
        """悬停到某个按钮上 → 只高亮那个按钮（比整行高亮直观得多）"""
        hit = self._op_hit(event, role)
        if not hit:
            if self._op_hl is not None:
                self._op_clear("ophover")
                self._op_hl = None
            return
        line, s, e, key = hit
        if self._op_hl == (line, s, e):
            return
        self._op_clear("ophover")
        self._op_hl = (line, s, e)
        self._op_flash(line, s, e, "ophover")

    def _on_op_click(self, event, hidx, role):
        """按点击位置落在哪个按钮字符区间，派发 复制/编辑/重新思考/删除（带视觉反馈）"""
        hit = self._op_hit(event, role)
        if not hit:
            return
        line, s, e, key = hit
        # ① 按下反馈（深一点的底色）
        self._op_flash(line, s, e, "oppress", 150)
        # ② 执行动作
        if key == "copy":
            self._copy_msg(hidx)
        elif key == "edit":
            self._edit_msg(hidx)
        elif key == "rethink":
            self._rethink(hidx)
        elif key == "del":
            self._del_msg(hidx)
        # ③ 结果反馈：成功绿闪，删除红闪（删除后该行已消失，闪烁失败也无妨）
        color = "opbad" if key == "del" else "opok"
        self.after(120, lambda: self._op_clear("ophover"))
        self.after(120, lambda: self._op_flash(line, s, e, color, 420))
        self._op_hl = None

    def _current_hist(self):
        s = sessions.get(self.current_mem)
        if not s:
            return None
        return [m for m in s["messages"] if m["role"] != "system"]

    def _copy_msg(self, hidx):
        hist = self._current_hist()
        if not hist or not (0 <= hidx < len(hist)):
            return
        m = hist[hidx]
        text = self._fmt_user_segments(m["content"]) if m["role"] == "user" else m["content"]
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.set_status(f"✅ 已复制（{len(text)} 字）")

    def _del_msg(self, hidx):
        s = sessions.get(self.current_mem)
        if not s:
            return
        hist = [m for m in s["messages"] if m["role"] != "system"]
        if not (0 <= hidx < len(hist)):
            return
        m = hist[hidx]
        preview = (m["content"][:24] if isinstance(m["content"], str)
                   else "图片消息（含图片记录）")
        if not messagebox.askyesno("删除确认",
                                   f"确定删除这条记录吗？\n「{preview}…」\n（md 文件中的对应记录也会同步删除）"):
            return
        cnt = 0
        for i, mm in enumerate(s["messages"]):
            if mm["role"] == "system":
                continue
            if cnt == hidx:
                s["messages"].pop(i)
                s["turns"] = max(0, s["turns"] - 1)
                break
            cnt += 1
        else:
            return
        try:
            rewrite_dialog_file(self.current_mem, s["messages"])
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
        self.render_session(self.current_mem)
        self.set_status("🗑 已删除该条记录")

    # ---------- 编辑用户消息 / 重新思考 AI 回复 ----------
    def _extract_text(self, content):
        """从消息 content（str 或 list）提取纯文字（去掉图片标记），用于回填输入框"""
        if isinstance(content, str):
            return IMG_MD_RE.sub("", content).strip()
        parts = []
        for seg in content:
            if seg.get("type") == "text":
                parts.append(seg["text"])
        return "\n".join(parts).strip()

    def _extract_images(self, content):
        """保留用户消息里已有的图片段（编辑文字时不动图片）"""
        if isinstance(content, list):
            return [seg for seg in content if seg.get("type") == "image_url"]
        return []

    def _edit_msg(self, hidx):
        """点击「编辑」：**就地**在该条消息的位置展开编辑框（不再跳到下方输入框）

        参照 Harness：编辑框直接出现在那条消息旁边，带「取消 / 发送」，
        不用把内容搬到最底下的输入框再改。

        ★ 思考中也**允许**打开编辑 ✗→✓（用户要求 ✓）
          打开编辑**不打断**正在进行的生成 ✓
          只有**内容真改了**再点「发送」才中断并重新思考 ✓ 见 _inline_edit_commit ✓
        """
        hist = self._current_hist()
        if not hist or not (0 <= hidx < len(hist)) or hist[hidx]["role"] != "user":
            return
        text = self._extract_text(hist[hidx]["content"])
        self._inline_edit_start(hidx, text)

    def _inline_edit_start(self, hidx, text):
        """在聊天区里就地插入一个编辑框"""
        # ★★ 防重入（日志抓到的真凶 ✓）
        #   实测日志：hidx=17 被**每秒调用一次、持续十几秒** ✗
        #   每调一次都会「先 close 再重建」→ 卡片闪 1-2 帧就没了 ✓
        #   → 同一条消息已经在编辑时，**直接返回** ✓
        #     （语义本来就该这样：点一次进入编辑 ✓ 再点同一个不该重建 ✓）
        _cur = getattr(self, "_inline_edit", None)
        if _cur and _cur.get("hidx") == hidx:
            try:
                _cur["text"].focus_set()
            except Exception:
                pass
            return
        # ★ 把调用栈写进日志 ✓ 一次就能钉死"谁在反复调它" ✓
        try:
            import traceback as _tb
            _st = [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
                   for f in _tb.extract_stack()[-6:-1]]
            EA.log("_inline_edit_start(hidx=%s) 调用栈: %s" % (hidx, " ← ".join(reversed(_st))), "I")
        except Exception:
            pass
        try:
            self._inline_edit_close()          # 先收掉上一个
        except Exception:
            pass
        try:
            # ★★ 真正的"就地"编辑（按用户澄清重做 ✓）：
            #   编辑框要**长得跟这条消息的气泡一样** ✓ 位置也一样 ✓
            #   —— 点击前后外观几乎不变，只是内容可改 + 多出「取消/发送」✓
            #   （上一版是"另插一张全宽灰卡片" ✗ 看着不像在编辑那条消息 ✗）
            #
            # 结构：外层 Canvas（透明 = 聊天区底色，撑满一行）
            #         └ 气泡 Canvas（**靠右**，用该消息自己的底色 ✓）
            #              ├ 文字 Text（无边框、与气泡同色 ✓）
            #              └ 按钮行（气泡右下角 ✓）
            _hist = [m for m in sessions.get(self.current_mem, {}).get("messages", [])
                     if m.get("role") != "system"]
            _role = _hist[hidx].get("role") if 0 <= hidx < len(_hist) else "user"
            BUB_BG = "#e8f0fe" if _role == "user" else "#f7f8fa"   # 跟消息气泡一致 ✓
            ROW_BG = "#ffffff"
            RADIUS = 14
            PAD_X = 14
            PAD_TOP = 10
            BTN_H = 28
            BTN_ROW = 42

            outer = tk.Canvas(self.chat, bg=ROW_BG, highlightthickness=0, bd=0)
            lines = max(1, min(14, text.count("\n") + 1 + len(text) // 46))
            text_h = lines * 21 + 4
            bub_h = PAD_TOP + text_h + BTN_ROW
            outer.configure(height=bub_h + 8)

            bub = tk.Canvas(outer, bg=ROW_BG, highlightthickness=0, bd=0,
                            height=bub_h, width=560)
            tx = tk.Text(bub, bg=BUB_BG, fg="#1f2328", relief="flat", bd=0,
                         font=("Microsoft YaHei UI", 10), wrap="word",
                         padx=0, pady=0, highlightthickness=0,
                         insertbackground="#1f2328", selectbackground="#cfe0ff")
            tx.insert("1.0", text)
            # ★ 快捷键（用户要求 ✓ 原来只有 Ctrl+Enter ✗）
            #   Enter = 发送（豆包习惯 ✓）　Shift+Enter = 换行（Tk 里是独立事件 ✓ 不会被吞 ✓）
            #   Ctrl+Enter = 发送（保留 ✓）　Esc = 取消
            tx.bind("<Escape>", lambda e: (self._inline_edit_close(), "break")[1])
            tx.bind("<Return>", lambda e: (self._inline_edit_commit(), "break")[1])
            tx.bind("<KP_Enter>", lambda e: (self._inline_edit_commit(), "break")[1])
            tx.bind("<Control-Return>",
                    lambda e: (self._inline_edit_commit(), "break")[1])
            tx.bind("<Control-KP_Enter>",
                    lambda e: (self._inline_edit_commit(), "break")[1])
            # ★ hover 必须显式给 ✗ —— RoundedButton 默认 hover 是浅灰 #eef1f5
            #   套在蓝底按钮上 = 悬停"蓝变灰白" ✗ 很影响体验（用户反馈"好淡"✓）
            b_cancel = RoundedButton(bub, text="取消", command=self._inline_edit_close,
                                     width=64, height=BTN_H, radius=9, bg=BUB_BG,
                                     fill="#ffffff", hover="#eef1f5",
                                     outline="#dcdfe4", fg="#3a3f45",
                                     font=("Microsoft YaHei UI", 9))
            b_send = RoundedButton(bub, text="发送", command=self._inline_edit_commit,
                                   width=72, height=BTN_H, radius=9, bg=BUB_BG,
                                   fill="#2f6fd0", hover="#1f5bb5",
                                   outline="#2f6fd0", fg="#ffffff",
                                   font=("Microsoft YaHei UI", 9))
            t_win = bub.create_window(0, 0, anchor="nw", window=tx)
            c_win = bub.create_window(0, 0, anchor="se", window=b_cancel)
            s_win = bub.create_window(0, 0, anchor="se", window=b_send)
            bub_win = outer.create_window(0, 4, anchor="nw", window=bub)

            # ★★ 布局缓存 —— 这是「卡 + 闪动」的根因 ✗
            #   流式输出时 Text 不停重排 → 嵌入式窗口的 <Configure> **疯狂触发** ✓
            #   而原来的 _layout 每次都 `delete("bubbg")` + 重画圆角 ✗
            #     → 肉眼看到的就是 **闪动** ✓
            #     → 且每个 token 都要重新布局一遍 ✓ 就是 **卡** ✓
            #   修法：算出的宽度没变 → **直接 return，一次都不重画** ✓
            _lay = {"key": None}

            def _layout(event=None):
                """气泡靠右 + 宽度自适应（Tk 的 Text 无法直接右对齐嵌入式窗口 ✗ → 在外层 Canvas 里定位 ✓）"""
                try:
                    ow = outer.winfo_width()
                    if ow <= 1:
                        try:
                            ow = max(320, self.chat.winfo_width() - 24)
                            outer.configure(width=ow)
                        except Exception:
                            return
                    bw = max(320, min(int(ow * 0.62), 680))
                    key = (ow, bw)
                    if _lay["key"] == key:
                        return            # ★★ 宽度没变 → 什么都不做 ✓ 不闪、不卡 ✓
                    _lay["key"] = key
                    bx = max(0, ow - bw - 6)          # ★ 靠右 ✓
                    outer.coords(bub_win, bx, 4)
                    bub.configure(width=bw, height=bub_h)
                    bub.delete("bubbg")
                    draw_round_rect(bub, 1, 1, bw - 2, bub_h - 1, RADIUS,
                                    fill=BUB_BG, outline=BUB_BG, width=1, tags="bubbg")
                    bub.tag_lower("bubbg")
                    bub.coords(t_win, PAD_X, PAD_TOP)
                    bub.itemconfigure(t_win, width=max(120, bw - PAD_X * 2),
                                      height=text_h)
                    bub.coords(s_win, bw - PAD_X, bub_h - 8)
                    bub.coords(c_win, bw - PAD_X - 72 - 8, bub_h - 8)
                except Exception:
                    pass

            outer.bind("<Configure>", _layout)
            try:
                self.after(30, _layout)       # 首帧画一次 ✓ 之后靠缓存兜住 ✓
            except Exception:
                pass
            f = outer
            # ★★ 顺序与位置都很关键（踩过 ✗）：
            #   1. **先算隐藏范围并隐藏** ✓ —— 因为卡片位置要靠它的**右端点** ✓
            #   2. 卡片插在**右端点** ✓ —— tag_add 范围是**左闭右开** ✓ 右端点必在范围**外** ✓
            #      · 不是最后一条 → 右端点 = _edm{hidx+1} ✓
            #      · 是最后一条   → 右端点 = end-1c        ✓
            #      以前"最后一条"会落回起点 ✗ → 插进 elide 范围里 ✓ **卡片被自己藏掉** ✗
            #   3. 消息被隐藏后整段塌陷 ✓ 卡片视觉上就落在它**原来的位置** ✓
            self._edit_hidden = self._hide_msg_for_edit(hidx)
            pos = (self._edit_hidden or {}).get("end") or "end-1c"
            try:
                f.configure(width=max(320, self.chat.winfo_width() - 24))
            except Exception:
                pass
            self.chat.configure(state="normal")
            self.chat.window_create(pos, window=f, stretch=False, padx=2, pady=4)
            self.chat.configure(state="disabled")
            self._inline_edit = {"hidx": hidx, "frame": f, "text": tx,
                                 "orig": text}      # ★ 存原文 → 发送时比对 ✓
            # ★★ 滚动：只在**卡片看不见时**才滚 ✓（两边的坑都踩过 ✗）
            #   · 无条件 see() → 流式时每次都强制重排 ✓ 卡 ✓
            #   · 完全不 see() → 卡片建在视口外 ✓ 用户以为"没反应" ✗（实测 ✓）
            #   正解：bbox(pos) 为空 = 不在可见区 → 这时候才滚 ✓ 平时一次都不动 ✓
            try:
                if self.chat.bbox(pos) is None:
                    self.chat.see(pos)
                    EA.log("编辑卡片在视口外 → 已滚动过去", "I")
            except Exception:
                pass
            tx.focus_set()
            self.set_status("✎ 就地编辑中（可点「发送」替换并重新回答，「取消」放弃）")
        except Exception as e:
            self._inline_edit = None
            self.set_status(f"⚠️ 打开编辑框失败：{e}")

    def _clear_edm_marks(self):
        """★ 清掉所有 `_edm*` 定位标记 ✗

        为什么必须清（真机复现出来的 ✓）：
          「编辑某条消息并发送」会让历史**变短** ✗
          （`_apply_edit` 里 `del s["messages"][hidx+2:]` 删掉旧回复 ✓）
          但 **Tk 的 mark 不会因为文字被删而消失** ✗ —— 它只会塌到同一位置 ✓
          → 于是旧的 `_edm21` 和新的 `_edm20` **重合** ✗
          → `tag_add("_edithide", a, b)` 里 `a == b` → **空范围** ✗
          → 什么都不隐藏 ✓ + 编辑卡片算不出位置（1x1 不可见）✓
          → 用户看到的就是「重启后编辑那条再发就坏」✓

        所以每次**重新渲染之前**先全部清掉 ✓ 渲染时会重新设 ✓ 安全 ✓
        """
        try:
            for n in list(self.chat.mark_names()):
                if n.startswith("_edm"):
                    self.chat.mark_unset(n)
        except Exception:
            pass

    def _hide_msg_for_edit(self, hidx):
        """★ 就地编辑时**隐藏**被编辑的那条消息本体（用户要求：只留左侧编辑卡片）

        做法：给消息的整段加一个 elide 标签 ✓ **只隐藏不删除** ✓ 随时可恢复 ✓
        范围 = `_edm{hidx}`（这条的起点）→ `_edm{hidx+1}`（下一条的起点）
              （渲染时 mark 是在这条消息**之前**设的 ✓ 所以 `_edm{hidx}` 就是起点 ✓）

        注意：**嵌入式窗口（操作条）不吃 elide** ✗ 要单独 window_configure(hide=True) ✓
        """
        info = {"tag": "_edithide", "wins": [], "end": "end-1c"}
        try:
            self.chat.tag_configure(info["tag"], elide=True)
            a = f"_edm{hidx}"
            names = set(self.chat.mark_names())
            _edm_all = sorted(n for n in names if n.startswith("_edm"))
            try:
                EA.log("编辑隐藏：hidx=%s 找 %s → %s；现有 %s"
                       % (hidx, a, "在" if a in names else "**不在**", _edm_all[:12]), "I")
            except Exception:
                pass
            if a not in names:
                # 这条消息没被渲染（懒加载窗口外 ✓）→ 不隐藏 ✓
                # 但**编辑框照样要能插** ✓ 位置退回文档末尾（一定在任何 elide 范围之外 ✓）
                return info
            # 右边界优先级（越精确越好 ✓）：
            #   ① _edm{hidx+1}  —— 下一条消息的起点（最精确 ✓ 历史消息都走这条 ✓）
            #   ② _ai_mark      —— ★ 流式**进行中**时用它 ✓
            #      AI 气泡的起点 ✓ 此刻已存在 ✓ 正好等于"这条用户消息的终点" ✓
            #      （助手消息要到收尾才会设 _edm{hidx+1} ✓ 所以流式中只能靠它 ✓）
            #   ③ end-1c        —— 兜底
            if (hidx + 1) in [int(n[4:]) for n in names
                              if n.startswith("_edm") and n[4:].isdigit()]:
                b = f"_edm{hidx + 1}"
            elif "_ai_mark" in names and hidx == len(
                    [m for m in sessions.get(self.current_mem, {}).get("messages", [])
                     if m.get("role") != "system"]) - 1:
                b = "_ai_mark"          # ★ 正在流式输出的那条 AI 气泡起点 ✓
            else:
                b = "end-1c"
            info["end"] = b            # ★ 右端点（插编辑框就用它 ✓）
            # ★ 防御：起点终点重合就是**空范围** ✗ → 不 elide（否则卡片会被判成"不可见"✓）
            try:
                if self.chat.compare(a, "==", b):
                    EA.log("编辑隐藏：%s == %s（空范围 ✗ 不 elide；可能是残留标记）" % (a, b), "W")
                    return info
            except Exception:
                pass
            self.chat.tag_add(info["tag"], a, b)
            # ★★ 这里**不能**遍历 window_names() 去 hide ✗（曾经有，引发冲突 ✓）
            #   因为**编辑卡片本身就是 create_window 嵌进去的** ✗
            #   顺序稍有变化它就被**自己藏掉** ✓ → 现象正是"状态栏说在编辑、
            #   屏幕上却什么都没有" ✗（用户实测到的 ✓）
            #   而且**实测确认**：操作条是**文本标签**（不是窗口 ✓）
            #   → elide 已经能把它一起藏掉 ✓ 这段**根本不需要** ✓
            try:
                EA.log("编辑隐藏标记 tag_add %s→%s（_edm%s 存在=%s）"
                       % (a, b, hidx, a in names), "I")
            except Exception:
                pass
        except Exception:
            pass
        return info

    def _show_msg_after_edit(self, info):
        """把上面隐藏的消息恢复显示 ✓"""
        if not info:
            return
        try:
            self.chat.tag_remove(info.get("tag") or "_edithide", "1.0", "end")
        except Exception:
            pass
        for name in info.get("wins") or []:
            try:
                self.chat.window_configure(name, hide=False)
            except Exception:
                pass

    def _inline_edit_close(self):
        """收掉就地编辑框（不改任何内容），并把隐藏的原消息恢复 ✓"""
        try:
            self._show_msg_after_edit(getattr(self, "_edit_hidden", None))
        except Exception:
            pass
        self._edit_hidden = None
        info = getattr(self, "_inline_edit", None)
        self._inline_edit = None
        if not info:
            return
        try:
            self.chat.configure(state="normal")
            self.chat.delete(info["frame"])
            self.chat.configure(state="disabled")
        except Exception:
            try:
                info["frame"].destroy()
            except Exception:
                pass

    def _inline_edit_commit(self):
        """就地编辑的「发送」

        ★★ 关键规则（用户明确要求 ✓）：
           · **内容没改** → 收起编辑框就完事 ✓ **不打断生成、不重新思考** ✓
           · **内容改了** → 才中断上一轮生成 + 重新思考 ✓（见 _apply_edit ✓）
        """
        info = getattr(self, "_inline_edit", None)
        if not info:
            return
        try:
            new_text = info["text"].get("1.0", "end-1c").strip()
        except Exception:
            new_text = ""
        orig = (info.get("orig") or "").strip()
        hidx = info["hidx"]
        # ★ 没改动 → 直接收起，什么都不动 ✓（思考中的生成**照常继续** ✓）
        if new_text == orig:
            self._inline_edit_close()
            self.set_status("内容未改动，已退出编辑（未打断生成）")
            return
        self._inline_edit_close()
        if not new_text:
            self.set_status("内容为空，未修改")
            return
        self._apply_edit(hidx, new_text)

    def _cancel_edit(self):
        """退出编辑模式，清空输入框"""
        if self._edit_hidx is None:
            return
        self._edit_hidx = None
        self.clear_input()
        self._show_edit_banner(False)
        self.set_status("已取消编辑")

    def _show_edit_banner(self, show):
        if show:
            self.edit_banner.pack(fill="x", before=self.entry)
        else:
            self.edit_banner.pack_forget()

    def _commit_edit(self):
        """编辑确认：替换该条用户消息，并重新生成其回答（其后旧内容作废）"""
        global busy
        if busy:
            self.set_status("⏳ 正在处理上一条，请稍候…")
            return
        hidx = self._edit_hidx
        if hidx is None:
            return
        self._edit_hidx = None
        self.clear_input()
        self._show_edit_banner(False)
        self._apply_edit(hidx, self.get_input_text())

    def _apply_edit(self, hidx, new_text):
        """把第 hidx 条用户消息替换成 new_text，并重新生成回答（编辑确认的公共逻辑）

        ★ 走到这里说明**内容确实改了** ✓
          如果此时正在生成（用户是在思考过程中就点编辑的 ✓）→ **现在才打断** ✓
          做法：先置中断标志 ✓ 再**有界等待**收尾 ✓（最多 3 秒 ✓）
          等不到（如摘要那种不响应中断的请求 ✗）→ 明确提示稍后重试 ✓ **不改数据** ✓
        """
        global busy
        if busy:
            self.set_status("⏹ 内容已改，正在打断上一轮生成并重新思考…")
            try:
                self.stop_gen()          # 置中断标志 ✓（流式生成会立刻停 ✓）
            except Exception:
                pass
            t0 = time.time()
            while busy and time.time() - t0 < 3.0:
                try:
                    self.update()        # 让主线程继续跑，_on_reply 才有机会收尾 ✓
                except Exception:
                    pass
                time.sleep(0.05)
            if busy:
                self.set_status("⏳ 上一轮还在收尾（可能是摘要请求，不响应中断），"
                                "请稍等一两秒再点「发送」")
                return
        if MODEL_ID is None:
            messagebox.showwarning("未连接", "请先启动本地引擎服务（llama.cpp / FreeToken / Ollama / LM Studio 等），程序会自动发现。")
            self._cancel_edit()
            return
        name = self.current_mem
        s = sessions.get(name)
        if not s:
            self._cancel_edit()
            return
        imgs = self._extract_images(s["messages"][hidx + 1]["content"] if hidx + 1 < len(s["messages"]) else [])
        if not new_text and not imgs:
            self.set_status("内容为空，未修改（Esc 取消编辑）")
            return
        # 组装新内容（保留图片，替换文字）
        segs = [{"type": "text", "text": new_text}] if new_text else []
        segs.extend(imgs)
        new_content = segs if segs else ""
        # 1) 替换该条用户消息（备份原内容，失败/停止时一并撤销编辑）
        self._regen_edit_orig = s["messages"][hidx + 1]["content"]
        s["messages"][hidx + 1]["content"] = new_content
        # 2) 旧 AI 回复作废重答；其后对话暂存，答完恢复（非破坏性）
        self._in_regen = True
        self._regen_backup = s["messages"][hidx + 2:]   # 含旧回复，失败/停止时整体还原
        self._regen_after = s["messages"][hidx + 3:]    # 旧回复之后的对话，成功后恢复
        del s["messages"][hidx + 2:]
        self._edit_hidx = None
        self.clear_input()
        self._show_edit_banner(False)
        self.render_session(name)
        # 重新生成回答（流式）
        self._regenerate(name)

    def _rethink(self, hidx):
        """点击「重新思考」：对同一问题重新生成这条 AI 回复（不改用户消息/输入框）"""
        global busy
        if busy:
            self.set_status("⏳ 正在处理上一条，请稍候…")
            return
        if MODEL_ID is None:
            messagebox.showwarning("未连接", "请先启动本地引擎服务（llama.cpp / FreeToken / Ollama / LM Studio 等），程序会自动发现。")
            return
        name = self.current_mem
        s = sessions.get(name)
        if not s:
            return
        hist = [m for m in s["messages"] if m["role"] != "system"]
        if not (0 <= hidx < len(hist)) or hist[hidx]["role"] != "assistant":
            return
        # 该 AI 消息在 s["messages"][hidx+1]，其提问在 s["messages"][hidx]
        # 只重答这一条：旧回答作废，其后对话暂存、答完恢复（非破坏性）
        self._in_regen = True
        self._regen_backup = s["messages"][hidx + 1:]   # 含旧回复，失败/停止时整体还原
        self._regen_after = s["messages"][hidx + 2:]    # 旧回复之后的对话，成功后恢复
        del s["messages"][hidx + 1:]
        self.render_session(name)
        self._regenerate(name)

    def _regenerate(self, name):
        """触发重新生成回答（编辑/重新思考共用）：历史已截断到提问，流式补一条新回答。
        两者都只影响 AI 输出；区别仅在是否改了用户消息（编辑改，重新思考不改）。"""
        global busy
        self.stop_event.clear()
        busy = True
        # 先起线程：UI 更新即使出错也不能阻断请求（否则 busy 会永久卡住）
        threading.Thread(target=self._worker, args=(name,), daemon=True).start()
        try:
            self._set_busy_ui(True)
            self.set_status(f"⏳ 正在请求 {MODEL_ID}…（可点 ↑ 变 ■ 停止）")
        except Exception as e:
            self.set_status(f"⚠️ 界面更新失败：{e}（请求仍在进行）")

    def _reset_regen(self):
        self._in_regen = False
        self._regen_after = []
        self._regen_backup = []
        self._regen_edit_orig = None

    def _add_tooltip(self, widget, text):
        ToolTip(widget, text)

    def _fmt_user_segments(self, content):
        """把 user 消息 content（str 或 list）转成可显示文本，图片显示文件名"""
        if isinstance(content, str):
            # 归档 markdown 图片标记 → 显示文件名
            return IMG_MD_RE.sub(lambda m: f"[图片] {short_name(os.path.basename(m.group(1)))}", content)
        parts = []
        for seg in content:
            if seg.get("type") == "text":
                parts.append(seg["text"])
            elif seg.get("type") == "image_url":
                nm = seg.get("_name") or seg.get("_path") or ""
                parts.append(f"[图片] {short_name(nm)}" if nm else "[图片]")
        return "\n".join(parts)

    def _mem_user_segments(self, content):
        """归档格式：图片用 markdown 链接记录本地路径，重启后可恢复缩略图"""
        return content_to_text(content)

    def _get_thumb(self, path):
        """取缓存的缩略图 PhotoImage；未缓存则打开 PIL 生成并缓存（提速重复渲染）"""
        photo = self._thumb_cache.get(path)
        if photo is None:
            try:
                from PIL import Image, ImageTk
                img = Image.open(path)
                img.thumbnail((180, 180))
                photo = ImageTk.PhotoImage(img)
                self._thumb_cache[path] = photo
            except Exception:
                return None
        self._imgs.append(photo)
        return photo

    def _extract_thumb_paths(self, content):
        """从消息 content 提取归档图片路径

        ★ 记忆分级后：图片要按**会话自己所在的那一级**解析 ✗ 不能用 MEM_DIR
          （本地模式下 MEM_DIR=private/ ✗ 而 cloud/ 会话的图片在 cloud/_images/ ✗
           用 MEM_DIR 拼就会找不到 ✗）
        """
        paths = []
        # 会话的 md 在哪一级，它的 _images/ 就在同一级 ✓（附属目录跟着会话走）
        try:
            base = os.path.dirname(mem_path(self.current_mem)) \
                if getattr(self, "current_mem", None) else MEM_DIR
        except Exception:
            base = MEM_DIR
        if isinstance(content, str):
            for m in IMG_MD_RE.finditer(content):
                rel = m.group(1).replace("/", os.sep)
                p = os.path.join(base, rel)
                if not os.path.exists(p):        # 兜底：当前级也试一下
                    p = os.path.join(MEM_DIR, rel)
                paths.append(p)
        elif isinstance(content, list):
            for seg in content:
                if seg.get("type") == "image_url" and seg.get("_path"):
                    paths.append(seg["_path"])
        return [p for p in paths if os.path.exists(p)]

    def _restore_thumbs(self, content):
        """从消息 content 中提取归档图片路径，插入缩略图（历史/切换都能显示）"""
        for p in self._extract_thumb_paths(content):
            self.add_image_thumb(p)

    def _prepend_block(self, role, text, content, hidx):
        """在聊天区最顶部插入一条消息块（文字 + 缩略图 + 文本操作条），顺序与正常渲染一致。
        用 mark 前向组装，增量插入（上翻只构建新批，O(LOAD_N)，不全量重渲染）。"""
        thumb_paths = self._extract_thumb_paths(content) if role == "user" else []
        tag = "user" if role == "user" else "ai"
        self.chat.configure(state="normal")
        self.chat.mark_set("_ins", "1.0")
        self.chat.mark_gravity("_ins", "right")   # 插入后标记后移 → 前向组装
        self.chat.insert("_ins", text + "\n", tag)
        for p in thumb_paths:
            photo = self._get_thumb(p)
            if photo:
                self.chat.image_create("_ins", image=photo, padx=4, pady=2)
            self.chat.insert("_ins", "\n", tag)
        op_tag = self._insert_op_bar("_ins", role, hidx)
        self.chat.mark_unset("_ins")
        self.chat.configure(state="disabled")
        self._bind_op_tag(op_tag, role, hidx)

    def render_session(self, name, keep_loaded=False):
        """渲染会话：每条消息下方插入独立一行的嵌入式操作条（复制/删除）。

        ★ 已归档的部分**不显示**：归档把那段对话原文搬进了隐藏区
          （memories\\_archived\\），聊天区只保留 ctx_start 之后的内容 ——
          要看归档内容请点左下角「本地用户 · 归档」。
        keep_loaded=False：重置懒加载起点为最近 LOAD_N 条；
        keep_loaded=True：沿用当前 _loaded_from（上翻加载更早时用）。"""
        self.clear_chat()
        # ★★ 清掉上一轮留下的 _edm* 旧标记 ✗（否则历史变短时会重合 → 见函数注释）
        self._clear_edm_marks()
        s = sessions[name]
        hist = [m for m in s["messages"] if m["role"] != "system"]
        # 已归档（隐藏）的文件：只显示最后两条 Q/A 的**精简版**，完整内容请用
        # 右键「复制原文 / 导出全部」取走（归档的意义就是让它退出日常视线）
        condensed = is_hidden_mem(name)
        if condensed:
            hist = hist[-4:]
        # messages[1:ctx_start] 是已被归档的那几批 → 从聊天区隐掉。
        # 注意仍用**绝对索引** hidx（复制/删除按它定位），所以下面只抬高渲染起点。
        try:
            hidden = 0 if condensed else max(0, min(len(hist), int(s.get("ctx_start", 1) or 1) - 1))
        except Exception:
            hidden = 0
        total = len(hist) - hidden                     # 聊天区里可见的条数
        if not keep_loaded:
            self._loaded_from = max(hidden, len(hist) - LOAD_N)
        self._loaded_from = max(self._loaded_from, hidden)
        # 顶部提示（固定 Label）
        more = self._loaded_from - hidden
        if condensed:
            self._set_top_hint(f"🗄 已归档（隐藏）· 仅显示最近 {total} 条对话的精简版"
                               f"　（完整内容请右键「复制原文 / 导出全部」）")
        elif more > 0:
            self._set_top_hint(f"↑ 还有更早 {more} 条（已加载 {total - more}/{total}），继续向上滚动加载")
        elif total:
            n_arch = len(arch_list_batches(name))
            tail = f"（另有 {n_arch} 批归档，见左下角）" if n_arch else ""
            self._set_top_hint(f"共 {total} 条对话记录{tail}")
        else:
            n_arch = len(arch_list_batches(name))
            if n_arch:
                self._set_top_hint(f"当前没有未归档的对话（已有 {n_arch} 批归档，见左下角「本地用户 · 归档」）")
            else:
                self._set_top_hint("新会话，直接输入第一句话开始")
        for hi, m in enumerate(hist[self._loaded_from:]):
            hidx = self._loaded_from + hi  # 真实历史索引（复制/删除按此定位，避免懒加载错位）
            # 为这条消息打一个 mark：点「编辑」时就地展开编辑框要用它定位
            try:
                self.chat.mark_set(f"_edm{hidx}", "end-1c")
                self.chat.mark_gravity(f"_edm{hidx}", "left")
            except Exception:
                pass
            if condensed:
                c = m["content"] if isinstance(m["content"], str) else "[图片/附件]"
                c = c.strip()
                if len(c) > 300:
                    c = c[:300].rstrip() + " …（已截断）"
                self.add_msg("user" if m["role"] == "user" else "ai", c)
            else:
                if m["role"] == "user":
                    self.add_msg("user", self._fmt_user_segments(m["content"]))
                    self._restore_thumbs(m["content"])
                else:
                    self.add_msg("ai", m["content"])
                self.add_op_bar(m["role"], hidx)  # 每条消息下方固定一行操作条
        self._see_end(force=True)   # 切换/重绘会话：归位到底部

    def load_more_history(self):
        """向上滚动到顶部时，增量加载更早的 LOAD_N 条并插到顶部（不全量重渲染）。
        只构建新批 → O(LOAD_N)。插入后用锚点把视图钉回原处，绝不跳动
        （旧实现在结尾调 see("end") 会把视图硬拽到底部，即"拖到最上跳到底下"的 bug）。"""
        if self._loaded_from <= 0:
            return
        s = sessions.get(self.current_mem)
        if not s:
            return
        hist = [m for m in s["messages"] if m["role"] != "system"]
        start = max(0, self._loaded_from - LOAD_N)
        batch = hist[start:self._loaded_from]  # 新批：更早的消息（时序正序）
        if not batch:
            return
        self._loaded_from = start
        # 记录当前顶部可见位置，插入更早内容后让它回到原处（视图不跳）
        self.chat.mark_set("_anchor", "@0,0")
        self.chat.mark_gravity("_anchor", "right")   # 跟随原有内容，不被新插入内容带走
        for j in range(len(batch) - 1, -1, -1):
            m = batch[j]
            hidx = start + j
            text = self._fmt_user_segments(m["content"]) if m["role"] == "user" else m["content"]
            self._prepend_block(m["role"], text, m["content"], hidx)
        # 更新顶部提示（固定 Label）
        total = len(hist)
        if self._loaded_from > 0:
            self._set_top_hint(f"↑ 还有更早 {self._loaded_from} 条（已加载 {total - self._loaded_from}/{total}），继续向上滚动加载")
        else:
            self._set_top_hint(f"共 {total} 条对话记录")
        # 关键：把原先顶部那条钉回视图顶部 —— 不跳底部、不跳顶部
        try:
            self.chat.yview("_anchor")
        except Exception:
            pass
        self.chat.mark_unset("_anchor")

    # ---------- 输入 ----------
    def update_placeholder(self):
        """占位 Label 的显示规则：**空、没有待发图片、且没有焦点** 时才显示。

        ★ 占位 Label 是白底、盖在输入框上的，所以它一显示就会挡住输入框左边那一段：
          · 光标（闪烁竖线）在位置 0 → 被它盖住，看起来"光标没了"
          · 选中的蓝色条 → 左半段被盖住，只剩右半段，看起来"蓝条只有后半段"
          所以在有焦点 / 有内容 / 有选区时都必须隐藏它（这也是标准占位文字的做法：
          点进来提示就消失）。"""
        try:
            has_sel = bool(self.entry.tag_ranges("sel"))
        except Exception:
            has_sel = False
        try:
            focused = (self.focus_get() is self.entry)
        except Exception:
            focused = False
        if self.get_input_text() or self.pending_images or has_sel or focused:
            self.ph.place_forget()
        else:
            # 坐标要和 entry 里真正开始写字的位置一致（entry 的 padx/pady = 0 → 从 (0,0) 开始）
            self.ph.place(in_=self.entry, x=0, y=0, anchor="nw")

    def _on_entry_press(self, event=None):
        """空输入框里按下：把光标放到行首并给焦点，但**不让 Tk 建立选区**。

        ★ 关键在于 return "break" 拦掉类绑定：类绑定会开始一次选区、
          并且对指针做 grab。空框里那次选区毫无意义（选的是 Text 隐含的结尾换行），
          还会让输入框看起来"卡住"。光标我们手动放好，所以拦掉没有副作用。
        有内容时正常返回，完全不影响正常的选择/拖动。"""
        try:
            if not self.get_input_text():
                self.entry.focus_set()
                self.entry.mark_set("insert", "1.0")
                self.entry.tag_remove("sel", "1.0", "end")
                self.update_placeholder()
                return "break"
        except Exception:
            pass

    def _on_entry_drag(self, event=None):
        """空输入框里拖动：清掉那条没意义的选中蓝条（有内容时正常选择）
        ★ 不要把这里绑到 <ButtonRelease-1>：拦掉松开会让 Tk 的指针 grab 释放不掉，
          输入框会"卡死"。"""
        try:
            if not self.get_input_text():
                self.entry.tag_remove("sel", "1.0", "end")
                self.update_placeholder()
                return "break"
        except Exception:
            pass

    def _on_entry_key(self, event=None):
        self.update_placeholder()
        self._autosize_entry()

    def _on_entry_enter(self, event=None):
        """Enter 发送（阻止默认换行）"""
        self.send_text()
        return "break"

    def _on_entry_newline(self, event=None):
        """Shift+Enter 手动换行"""
        self.entry.insert("insert", "\n")
        self._autosize_entry()
        return "break"

    def _autosize_entry(self):
        """按显示行数自动长高（1~6 行），超出滚动"""
        try:
            n = self.entry.count("1.0", "end-1c", "displaylines")
            n = n[0] if isinstance(n, tuple) else (n or 1)
        except Exception:
            n = 1
        n = max(1, min(int(n), 6))
        if n != self._entry_lines:
            self._entry_lines = n
            try:
                self.entry.configure(height=n)
            except Exception:
                pass
        # 高度可能变了 → 同步重绘圆角底与内嵌控件
        self._redraw_input_box()

    def _entry_yscroll(self, first, last):
        """输入框滚动条联动：内容放得下时隐藏，超出时才出现（可拖动）"""
        try:
            self.entry_sb.set(first, last)
            need = not (float(first) <= 0.0 and float(last) >= 1.0)
        except Exception:
            return
        if need != self._entry_sb_shown:
            self._entry_sb_shown = need
            try:
                self.input_canvas.itemconfigure(self._entry_sb_win,
                                                state="normal" if need else "hidden")
            except Exception:
                pass
            self._redraw_input_box()

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r=12, **kw):
        """在 Canvas 上画圆角矩形（用平滑多边形近似，tkinter 原生无圆角控件）"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def _on_box_inner_configure(self, event=None):
        """内容高度变化 → 圆角外框跟着长高（外框只是背景，内容在 window item 里）"""
        try:
            h = (event.height if event is not None else self.box_panel.winfo_reqheight()) + 4
            if abs(int(float(self.box_panel.cget("height"))) - h) > 1:
                self.box_panel.configure(height=h)
        except Exception:
            pass

    def _on_box_panel_configure(self, event=None):
        """圆角外框宽度变化 → 内嵌内容宽度跟着变（留 2px 内边距）"""
        try:
            w = event.width if event is not None else self.box_panel.winfo_width()
            self.box_panel.coords(self._box_win, 2, 2)
            self.box_panel.itemconfigure(self._box_win, width=max(w - 4, 100))
        except Exception:
            pass

    def _redraw_input_box(self, event=None):
        """重绘圆角输入框：圆角底 + 定位/定尺寸内嵌的输入框与滚动条"""
        c = self.input_canvas
        try:
            w = c.winfo_width()
            if w <= 1:
                w = c.winfo_reqwidth()
            eh = self.entry.winfo_reqheight()
        except Exception:
            return
        pad_x, pad_y = 12, 8
        sb_w = 14 if self._entry_sb_shown else 0
        h = max(eh + pad_y * 2, 36)
        # 只有高度真的变了才重设，避免 <Configure> 递归
        try:
            if abs(int(float(c.cget("height"))) - h) > 1:
                c.configure(height=h)
        except Exception:
            pass
        try:
            c.delete("boxbg")
            # 内层框透明（不描边）：外层的圆角面板才是可见边框
            self._round_rect(c, 1, 1, w - 1, h - 1, 12,
                             fill="#ffffff", outline="", width=0, tags="boxbg")
            c.tag_lower("boxbg")
            c.coords(self._entry_win, pad_x, pad_y)
            c.itemconfigure(self._entry_win, width=max(w - pad_x * 2 - sb_w, 40), height=eh)
            c.coords(self._entry_sb_win, w - pad_x + 2, pad_y)
            c.itemconfigure(self._entry_sb_win, height=eh)
        except Exception:
            pass

    def get_input_text(self):
        return self.entry.get("1.0", "end-1c").strip()

    def clear_input(self):
        self.entry.delete("1.0", "end")
        self._autosize_entry()
        self.update_placeholder()

    # ---------- 发送 ----------
    def send_text(self):
        # 编辑模式：发送=替换该条用户消息并重新回答
        if self._edit_hidx is not None:
            self._commit_edit()
            return
        text = self.get_input_text()
        if not text and not self.pending_images and not self.pending_files:
            return
        self.clear_input()
        segments = []
        label_parts = []
        if text:
            segments.append({"type": "text", "text": text})
            label_parts.append(text)
        # 文本文件：内容作为附加文本段一起发给模型（拖入或选择）
        file_paths = list(self.pending_files)
        self.pending_files = []
        for fp in file_paths:
            name, body = self._read_text_file(fp)
            if name is None:
                self.add_arch(f"⚠️ 读取文件失败：{os.path.basename(fp)}（{body}）")
                continue
            segments.append({"type": "text", "text": f"\n\n【文件：{name}】\n{body}"})
            label_parts.append(f"[文件] {short_name(name)}")
        paths = list(self.pending_images)
        self.pending_images = []
        self.refresh_attachments()
        for path in paths:
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception as e:
                messagebox.showerror("读取图片失败", str(e))
                continue
            ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            img_url = f"data:image/{ext};base64,{b64}"
            arch_path = archive_image(path)
            segments.append({"type": "image_url", "image_url": {"url": img_url},
                             "_name": os.path.basename(path), "_path": arch_path})
            label_parts.append(f"[图片] {short_name(os.path.basename(path))}")
        if not segments:
            return
        self.dispatch(segments, "\n".join(label_parts), paths)

    # ---------- 附件：多选 + 拖拽进窗口 ----------
    IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    TXT_EXT = (".txt", ".md", ".py", ".json", ".csv", ".log", ".xml", ".yaml", ".yml",
               ".ini", ".cfg", ".html", ".htm", ".js", ".ts", ".java", ".c", ".cpp",
               ".h", ".cs", ".go", ".rs", ".sql", ".bat", ".ps1", ".sh")
    FILE_MAX_BYTES = 200 * 1024     # 单个文本文件读取上限，防止把上下文一次撑爆

    def _setup_dnd(self):
        """整窗支持拖入文件。托盘不可用/库缺失时静默跳过"""
        if not _DND_OK:
            return
        targets = [self]
        for name in ("main", "chat", "entry", "box_panel", "input_canvas", "attach_frame"):
            w = getattr(self, name, None)
            if w is not None:
                targets.append(w)
        for w in targets:
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _looks_texty(self, path):
        """粗略判断是不是文本文件：能干净地按 UTF-8 解码、且几乎没有控制字符。
        用来决定"后缀不认识的文件"要不要当文本读 —— 避免把二进制垃圾塞进上下文。"""
        try:
            with open(path, "rb") as f:
                raw = f.read(4096)
        except Exception:
            return False
        if b"\x00" in raw:
            return False
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if not txt:
            return True
        bad = sum(1 for ch in txt if ord(ch) < 32 and ch not in "\r\n\t")
        return bad / max(len(txt), 1) < 0.02

    def _add_paths(self, paths, source="选择"):
        """把路径按类型分流进图片/文件队列（**拖拽和按钮共用这一条逻辑**）"""
        n_img = n_file = 0
        skipped = []
        for p in paths:
            p = str(p).strip().strip("{}")
            if not p or not os.path.isfile(p):
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext in self.IMG_EXT:
                if p not in self.pending_images:
                    self.pending_images.append(p)
                    n_img += 1
            elif ext in self.TXT_EXT or self._looks_texty(p):
                if p not in self.pending_files:
                    self.pending_files.append(p)
                    n_file += 1
            else:
                skipped.append(os.path.basename(p))
        self.refresh_attachments()
        parts = []
        if n_img:
            parts.append(f"图片 {n_img} 张")
        if n_file:
            parts.append(f"文件 {n_file} 个")
        if parts:
            self.set_status(f"📥 已添加 " + "、".join(parts)
                            + f"（共 {len(self.pending_images)} 图 / {len(self.pending_files)} 文件），"
                              "可继续输入文字后一起发送")
        elif not skipped:
            self.set_status("未添加任何附件（可能已在列表中）")
        if skipped:
            self.set_status(f"⚠️ 已跳过非文本/非图片文件：{', '.join(skipped[:3])}"
                            + ("…" if len(skipped) > 3 else ""))

    def _on_drop(self, event):
        """拖入文件：图片进图片队列，文本类进文件队列；支持一次拖多个"""
        try:
            paths = list(self.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        self._add_paths(paths, source="拖入")
        return "break"

    def pick_attachment(self):
        """合并入口：一次可以选图片**和**文本文件，按类型自动分流（支持多选）"""
        paths = filedialog.askopenfilenames(
            title="选择图片或文件（可按住 Ctrl / Shift 多选）", parent=self,
            filetypes=[
                ("图片和文本文件",
                 "*.png *.jpg *.jpeg *.webp *.bmp *.gif "
                 "*.txt *.md *.py *.json *.csv *.log *.xml *.yaml *.yml *.ini *.cfg "
                 "*.html *.htm *.js *.ts *.java *.c *.cpp *.h *.cs *.go *.rs *.sql *.bat *.ps1 *.sh"),
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("文本/代码", "*.txt *.md *.py *.json *.csv *.log *.xml *.yaml *.yml "
                              "*.ini *.cfg *.html *.js *.ts *.java *.c *.cpp *.h *.cs "
                              "*.go *.rs *.sql *.bat *.ps1 *.sh"),
                ("所有文件", "*.*"),
            ])
        if paths:
            self._add_paths(paths)

    def _apply_attach_height(self, total_h):
        """统一决定附件区的最终高度（= 附件区可视高度 + 工具行高度）。
        ★ 必须显式设置 attach_outer 的高度：内容嵌在 Canvas 的 window item 里，
          仅靠子控件 reqheight 传播时，"回缩"方向不生效（清空后会留一大块空白）。"""
        if not self.attach_frame.winfo_children():
            try:
                # 注意：Tk 里 Frame 的 -height **≤0 表示"不请求尺寸"**，会被忽略、
                # 保留原来的高度（实测清空后仍卡在 108px）。所以这里用 1。
                self.attach_outer.configure(height=1)
            except Exception:
                pass
            return
        need_sb = total_h > ATTACH_MAX_H
        vis = ATTACH_MAX_H if need_sb else total_h
        try:
            self.attach_canvas.configure(
                height=vis,
                scrollregion=(0, 0, self.attach_frame.winfo_reqwidth(), total_h))
        except Exception:
            return
        if need_sb:
            if not self.attach_sb.winfo_ismapped():
                self.attach_sb.pack(side="right", fill="y")
        else:
            if self.attach_sb.winfo_ismapped():
                self.attach_sb.pack_forget()
            try:
                self.attach_canvas.yview_moveto(0)
            except Exception:
                pass
        # 显式算总高：上间距 8 + 工具行 + 间距 3 + 附件区
        try:
            bar_h = self.attach_bar.winfo_reqheight()
            self.attach_outer.configure(height=8 + bar_h + 3 + vis)
        except Exception:
            pass

    def _relayout_attachments(self):
        """附件**横向排列、排满一行就自动换行**（流式布局）。
        Tk 没有原生 flow layout，pack/grid 都做不到"排满换行"，所以用 place 精确摆放。
        代价：place 的子控件不参与父容器尺寸计算 → 必须显式设置内层 Frame 的尺寸。"""
        chips = getattr(self, "_attach_chips", None)
        if chips is None:
            return
        try:
            avail = max(self.attach_canvas.winfo_width(), 120)
        except Exception:
            avail = 400
        x = y = row_h = 0
        GAP_X, GAP_Y = 6, 4
        for chip in chips:
            try:
                w, h = chip.winfo_reqwidth(), chip.winfo_reqheight()
            except Exception:
                continue
            if x and x + w > avail:          # 这一行放不下了 → 换到下一行
                x = 0
                y += row_h + GAP_Y
                row_h = 0
            try:
                chip.place(x=x, y=y)
            except Exception:
                continue
            x += w + GAP_X
            row_h = max(row_h, h)
        total_h = (y + row_h) if chips else 0
        try:
            self.attach_frame.configure(width=avail, height=total_h)
        except Exception:
            return
        self._apply_attach_height(total_h)

    def _on_attach_canvas_configure(self, event):
        """画布宽度变化 → 重新流式排版（换行位置要跟着变）"""
        try:
            self.attach_canvas.itemconfigure(self._attach_win, width=event.width)
        except Exception:
            pass
        self._relayout_attachments()

    def _on_attach_wheel(self, event):
        """在附件栏上滚轮也能滚动（只有内容超高时才拦）"""
        try:
            if self.attach_frame.winfo_reqheight() <= ATTACH_MAX_H:
                return None
            self.attach_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        except Exception:
            return None

    def _attach_ids(self):
        """当前所有附件的路径集合"""
        return list(self.pending_images) + list(self.pending_files)

    def _restyle_attach_chips(self):
        """只更新选中样式，**不重建控件**。
        拖动框选时每划过一条都会触发，重建整个 chip 列表会很卡。"""
        SEL_BG, SEL_BD, SEL_FG = "#dbe8ff", "#5a9bf6", "#1a3f7a"
        NRM_BG, NRM_BD, NRM_FG = "#eef1f5", "#d9dde3", "#333333"
        for chip, p in zip(self._attach_chips, self._attach_chip_paths):
            sel = p in self._attach_sel
            bg, bd, fg = (SEL_BG, SEL_BD, SEL_FG) if sel else (NRM_BG, NRM_BD, NRM_FG)
            try:
                chip.configure(bg=bg, highlightbackground=bd)
                kids = chip.winfo_children()
                for c in kids:
                    c.configure(bg=bg)
                if kids:
                    kids[0].configure(fg=fg)      # [0] 是文件名标签
            except Exception:
                continue
        self._set_attach_info()

    def _set_attach_info(self):
        """工具行提示文字"""
        n_all = len(self._attach_chip_paths)
        n_sel = len(self._attach_sel)
        try:
            self.attach_info_var.set(
                f"📎 共 {n_all} 个附件"
                + (f"（已选 {n_sel}）" if n_sel
                   else "　点击选中 · Ctrl 加选 · Shift 范围选 · 按住拖动框选"))
        except Exception:
            pass

    def _toggle_attach_sel(self, path):
        """选中/取消选中单个附件"""
        if path in self._attach_sel:
            self._attach_sel.discard(path)
        else:
            self._attach_sel.add(path)
        self._restyle_attach_chips()

    def _attach_press(self, path, chip, event):
        """按下条目。三种用法：
           · 直接点        → 只选它（标准文件管理器行为），并进入"拖动涂抹"模式
           · Ctrl + 点     → 切换这一项的选中状态
           · Shift + 点    → 从上次点击处范围选择"""
        self._attach_drag_on = False
        state = getattr(event, "state", 0)
        if state & 0x0004:                       # Ctrl
            self._attach_anchor = path
            self._toggle_attach_sel(path)
            return
        if state & 0x0001:                       # Shift → 范围
            ids = self._attach_ids()
            if self._attach_anchor in ids and path in ids:
                a, b = ids.index(self._attach_anchor), ids.index(path)
                lo, hi = min(a, b), max(a, b)
                self._attach_sel |= set(ids[lo:hi + 1])
            else:
                self._attach_sel.add(path)
            self._attach_anchor = path
            self._restyle_attach_chips()
            return
        # 普通点击：只选它，并允许继续拖动扩大选择
        self._attach_anchor = path
        self._attach_sel = {path}
        self._attach_drag_on = True
        self._restyle_attach_chips()

    def _attach_drag(self, chip, event):
        """按住左键拖动 → 划过的条目都被选中（涂抹式框选）。
        注意：Tk 在按下后会把指针「抓」给按下的那个控件，所有 B1-Motion 都发回它，
        所以必须把 chip 内坐标换算成 attach_frame 坐标，再判断落在哪个 chip 上。"""
        if not self._attach_drag_on:
            return
        try:
            x = chip.winfo_x() + event.x
            y = chip.winfo_y() + event.y
        except Exception:
            return
        changed = False
        for c, p in zip(self._attach_chips, self._attach_chip_paths):
            try:
                if (c.winfo_x() <= x <= c.winfo_x() + c.winfo_reqwidth()
                        and c.winfo_y() <= y <= c.winfo_y() + c.winfo_reqheight()):
                    if p not in self._attach_sel:
                        self._attach_sel.add(p)
                        changed = True
            except Exception:
                continue
        if changed:
            self._restyle_attach_chips()

    def _attach_release(self):
        self._attach_drag_on = False

    def _clear_attach_sel(self, event=None):
        """点空白处 → 取消全部选中"""
        if self._attach_sel:
            self._attach_sel.clear()
            self._restyle_attach_chips()

    def select_all_attachments(self):
        """全选 / 全不选（再点一次取消）"""
        allp = self._attach_ids()
        self._attach_sel = set() if len(self._attach_sel) == len(allp) and allp else set(allp)
        self._restyle_attach_chips()

    def remove_selected_attachments(self):
        """删除所有选中的附件（一次搞定，不用一个个点 ×）"""
        if not self._attach_sel:
            self.set_status("⚠️ 还没选中任何附件：直接点附件条目即可选中")
            return
        n = len(self._attach_sel)
        self.pending_images = [p for p in self.pending_images if p not in self._attach_sel]
        self.pending_files = [p for p in self.pending_files if p not in self._attach_sel]
        self._attach_sel.clear()
        self.refresh_attachments()
        self.set_status(f"🗑 已移除 {n} 个附件")

    def clear_attachments(self):
        """清空全部待发送附件"""
        n = len(self._attach_ids())
        if not n:
            return
        self.pending_images, self.pending_files = [], []
        self._attach_sel.clear()
        self.refresh_attachments()
        self.set_status(f"🗑 已清空全部 {n} 个附件")

    def refresh_attachments(self):
        """刷新待发送附件栏：横向排列、排满换行；点条目选中，工具行可批量删除"""
        for w in self.attach_frame.winfo_children():
            w.destroy()
        self._attach_chips = []
        self._attach_chip_paths = []
        items = ([(p, "📷") for p in self.pending_images]
                 + [(p, "📄") for p in self.pending_files])
        # 已删除的路径要从选中集合里清掉，避免"选中了不存在的东西"
        self._attach_sel &= set(self._attach_ids())

        SEL_BG, SEL_BD = "#dbe8ff", "#5a9bf6"
        NRM_BG, NRM_BD = "#eef1f5", "#d9dde3"
        for path, icon in items:
            sel = path in self._attach_sel
            bg = SEL_BG if sel else NRM_BG
            bd = SEL_BD if sel else NRM_BD
            chip = tk.Frame(self.attach_frame, bg=bg, highlightbackground=bd,
                            highlightthickness=1, cursor="hand2")
            lb = tk.Label(chip, text=f"{icon} " + short_name(os.path.basename(path), 26),
                          bg=bg, fg="#1a3f7a" if sel else "#333333",
                          font=("Microsoft YaHei UI", 9), cursor="hand2")
            lb.pack(side="left", padx=(8, 2), pady=3)
            del_lb = tk.Label(chip, text="×", bg=bg, fg="#999999",
                              font=("Microsoft YaHei UI", 10), cursor="hand2")
            del_lb.pack(side="left", padx=(2, 8))
            # 点 × → 只删这一个；点条目本身 → 选中/取消（右键直接删这一个）
            del_lb.bind("<Button-1>", lambda e, p=path: self.remove_attachment(p))
            for w in (chip, lb):
                w.bind("<Button-1>", lambda e, p=path, c=chip: self._attach_press(p, c, e))
                w.bind("<B1-Motion>", lambda e, c=chip: self._attach_drag(c, e))
                w.bind("<ButtonRelease-1>", lambda e: self._attach_release())
                w.bind("<Button-3>", lambda e, p=path: self.remove_attachment(p))
                w.bind("<MouseWheel>", self._on_attach_wheel)
            self._attach_chips.append(chip)
            self._attach_chip_paths.append(path)

        # 工具行 / 附件区：有附件才显示，没附件整块收起（不占界面）
        if items:
            if not self.attach_bar.winfo_ismapped():
                self.attach_bar.pack(fill="x", pady=(0, 3))
            if not self.attach_body.winfo_ismapped():
                self.attach_body.pack(fill="x")
            n_sel = len(self._attach_sel)
            self._set_attach_info()
        else:
            if self.attach_bar.winfo_ismapped():
                self.attach_bar.pack_forget()
            if self.attach_body.winfo_ismapped():
                self.attach_body.pack_forget()
            self.set_status(f"📦 模型：{MODEL_ID} · 自动归档已开启" if MODEL_ID else "未连接")

        self.attach_frame.update_idletasks()
        self._relayout_attachments()

    def remove_attachment(self, path):
        if path in self.pending_images:
            self.pending_images.remove(path)
        if path in self.pending_files:
            self.pending_files.remove(path)
        self._attach_sel.discard(path)
        self.refresh_attachments()

    def _read_text_file(self, path):
        """读文本文件（带上限），返回 (文件名, 内容) 或 (None, 错误说明)"""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                raw = f.read(self.FILE_MAX_BYTES)
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                txt = raw.decode("gbk", errors="replace")
            if size > self.FILE_MAX_BYTES:
                txt += f"\n…（文件较大，仅读取前 {self.FILE_MAX_BYTES // 1024} KB）"
            return os.path.basename(path), txt
        except Exception as e:
            return None, str(e)

    def add_image_thumb(self, path):
        """在聊天区插入图片缩略图（新发送时显示）；带缓存，重复渲染不重开 PIL"""
        photo = self._get_thumb(path)
        if photo is None:
            return
        self.chat.configure(state="normal")
        idx = self.chat.index("end-1c")
        self.chat.image_create("end", image=photo, padx=4, pady=2)
        self.chat.tag_add("user", idx, "end-1c")  # image_create 不接受 tags=，需事后 tag_add
        self.chat.insert("end", "\n", "user")
        self.chat.configure(state="disabled")

    # ---------- 排队区 ----------
    def _queue_scroll(self, event):
        """排队区横向滚动（鼠标滚轮 / Shift+滚轮）"""
        try:
            step = -1 if getattr(event, "delta", 0) > 0 else 1
            self.queue_canvas.xview_scroll(step * 2, "units")
        except Exception:
            pass
        return "break"

    def refresh_queue_bar(self):
        """重画排队区（队列空就整条隐藏）"""
        try:
            for w in self.queue_inner.winfo_children():
                w.destroy()
            if not self.msg_queue:
                if self._queue_bar_shown:
                    self.queue_bar.pack_forget()
                    self._queue_bar_shown = False
                    try:
                        self._on_box_panel_configure()
                    except Exception:
                        pass
                return
            if not self._queue_bar_shown:
                # 压在输入框上方（input_canvas 已经 pack 过了，用 before 插到它前面）
                self.queue_bar.pack(fill="x", before=self.input_canvas)
                self._queue_bar_shown = True
            self.queue_head.configure(text=f"⏳ 排队中 {len(self.msg_queue)} 条")
            for i, item in enumerate(self.msg_queue):
                self._queue_chip(self.queue_inner, i, item)
            # 更新滚动范围，并把视口拉回最左边（新入队的在最右，方便看到"又加了一条"）
            try:
                self.queue_inner.update_idletasks()
                w = max(self.queue_inner.winfo_reqwidth(), 1)
                self.queue_canvas.configure(scrollregion=(0, 0, w, 34))
                self.queue_canvas.configure(height=34)
            except Exception:
                pass
            try:
                self._on_box_panel_configure()
            except Exception:
                pass
        except Exception:
            pass

    def _queue_chip(self, parent, idx, item):
        """一个排队条：文字 + ✏重编辑 / 🗑删除 / ⬆强制发送"""
        try:
            txt = (item.get("display") or "").strip().replace("\n", " ")
            if len(txt) > 16:
                txt = txt[:16] + "…"
            f = tk.Frame(parent, bg="#ffffff", highlightbackground="#cfe0f5",
                         highlightthickness=1)
            f.pack(side="left", padx=(0, 6))
            tk.Label(f, text=f"{idx + 1}. {txt}", bg="#ffffff", fg="#2b2b2b",
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 4), pady=2)
            for sym, tip, cb in (("✏", "重编辑（放回输入框）", self.queue_edit),
                                 ("🗑", "删除这条排队消息", self.queue_del),
                                 ("⬆", "强制发送（先停掉当前生成）", self.queue_force)):
                lb = tk.Label(f, text=sym, bg="#ffffff", fg="#5b7ba6", cursor="hand2",
                              font=("Microsoft YaHei UI", 9))
                lb.pack(side="left", padx=(2, 2), pady=2)
                lb.bind("<Button-1>", lambda e, c=cb, k=idx: c(k))
                try:
                    _tip(lb, tip)
                except Exception:
                    pass
            tk.Frame(parent, bg="#f2f7fd", width=0).pack(side="left")
        except Exception:
            pass

    def queue_del(self, idx):
        """删除第 idx 条排队消息"""
        try:
            if 0 <= idx < len(self.msg_queue):
                self.msg_queue.pop(idx)
                self.refresh_queue_bar()
                self.set_status("🗑 已删除该条排队消息")
        except Exception:
            pass

    def queue_edit(self, idx):
        """把排队消息放回输入框，重新编辑（从队列移除）"""
        try:
            if not (0 <= idx < len(self.msg_queue)):
                return
            item = self.msg_queue.pop(idx)
            self.refresh_queue_bar()
            txt = item.get("display") or ""
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", txt)
            self._autosize_entry()
            self.update_placeholder()
            self.entry.focus_set()
            self.set_status("✏ 已放回输入框，改完再发")
        except Exception:
            pass

    def queue_force(self, idx):
        """强制发送：排到最前，并停掉当前生成，好让它立刻发出去"""
        global busy
        try:
            if not (0 <= idx < len(self.msg_queue)):
                return
            item = self.msg_queue.pop(idx)
            self.msg_queue.insert(0, item)
            self.refresh_queue_bar()
            if busy:
                self.stop_event.set()
                self.set_status("⏹ 已停止当前生成，正在强制发送排队的这条…")
            else:
                self._pump_queue()
        except Exception:
            pass

    def _pump_queue(self):
        """一条回复结束：队列里还有排队的就自动发下一条"""
        global busy
        try:
            if busy or not self.msg_queue:
                return
            item = self.msg_queue.pop(0)
            self.refresh_queue_bar()
            self.after(200, lambda: self.dispatch(item["segments"], item["display"],
                                                  item.get("images")))
        except Exception:
            pass

    def dispatch(self, segments, display_text, image_paths=None):
        global busy
        if busy:
            # ★ 以前这里直接 return，等于把你刚打的字**静静吞掉**。
            #   现在改成入队：上一条回复结束后自动发送，期间可以重编辑/删除/强制发送。
            self.msg_queue.append({"segments": segments, "display": display_text,
                                   "images": image_paths})
            self.refresh_queue_bar()
            self.set_status(f"⏳ 模型还在回答，这条已排队（第 {len(self.msg_queue)} 条）"
                            f"—— 可点 ✏ 重编辑 / 🗑 删除 / ⬆ 强制发送")
            return
        if MODEL_ID is None:
            messagebox.showwarning("未连接", "请先启动本地引擎服务（llama.cpp / FreeToken / Ollama / LM Studio 等），程序会自动发现。")
            return
        # 新会话第一句话 → 生成文件名
        if self.pending_new or self.current_mem is None:
            if not self.auto_create_mem(display_text):
                return
        name = self.current_mem
        s = sessions.setdefault(name, {"messages": [{"role": "system", "content": build_system(name)}], "turns": 0,
                                       "ctx_start": 1})
        hidx = len([m for m in s["messages"] if m["role"] != "system"])  # 新用户消息的真实历史索引
        # ★★★ 必须和 render_session 一样：**画之前先设 _edm{hidx} 标记** ✗→✓
        #   真机复现确认（真引擎 + 真发送路径）：
        #     发送后 _edm 标记最大只到 _edm18 ✗ 而新消息是 hidx=19 ✗ → 没有 _edm19 ✓
        #   → 就地编辑找不到标记 → 提前返回 → 原消息不隐藏 ✗
        #     + 卡片位置退回文档末尾 ✗ 被流式输出不停推挤 ✗ = 用户看到的"闪动 / 点不动" ✓
        #   历史消息都走过 render_session ✓ 有标记 ✓ → 所以**只有最近那条坏** ✓
        try:
            self.chat.mark_set(f"_edm{hidx}", "end-1c")
            self.chat.mark_gravity(f"_edm{hidx}", "left")
        except Exception:
            pass
        self.add_msg("user", display_text)
        for p in (image_paths or []):
            self.add_image_thumb(p)
        self.add_op_bar("user", hidx)  # 该条消息下方固定一行操作条
        s["messages"].append({"role": "user", "content": segments})
        s["turns"] += 1
        # 用户消息立即写入 md（持久化，重启可恢复）
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            append_dialog(name, "user", self._mem_user_segments(segments), now)
        except Exception:
            pass
        busy = True
        self._ovf_retry = False   # 新一轮请求，重置超限重试标志
        # 先起线程：UI 更新即使出错也不能阻断请求（否则 busy 会永久卡住、界面像死了一样）
        threading.Thread(target=self._worker, args=(name,), daemon=True).start()
        try:
            self._set_busy_ui(True)
            self.set_status(f"⏳ 正在请求 {MODEL_ID}…（可点 ↑ 变 ■ 停止）")
        except Exception as e:
            self.set_status(f"⚠️ 界面更新失败：{e}（请求仍在进行）")

    def _set_busy_ui(self, on):
        """发送按钮 ⇄ 停止按钮 切换（send_btn 是 RoundedButton，只能用 set_* 接口）"""
        if on:
            self.send_btn.set_text("■", "#d33")
            self.send_btn.set_fill("#f0f0f0", hover="#e6e6e6")
            self.send_btn.set_outline("#f0b4b4")
            self.send_btn.set_command(self.stop_gen)
        else:
            self.send_btn.set_text("↑", "#ffffff")
            self.send_btn.set_fill("#3b82f6", hover="#2f6fd6")
            self.send_btn.set_outline("#3b82f6")
            self.send_btn.set_command(self.send_text)

    def stop_gen(self):
        self.stop_event.set()
        self.set_status("⏹ 正在停止…")

    def auto_create_mem(self, first_text):
        """用第一句话生成记忆文件名"""
        if first_text.startswith("[图片]"):
            raw = first_text.replace("[图片] ", "").strip()
            base_name = os.path.splitext(raw)[0][:12] or "图片对话"
        else:
            base_name = first_text.replace("\n", " ").strip()[:12]
        name = base_name.rstrip("。，！？.!? ") or "新对话"
        name = name.replace("/", "／").replace("\\", "＼").replace(":", "：")
        name = name.replace("*", "＊").replace("?", "？").replace('"', "＂")
        name = name.replace("<", "＜").replace(">", "＞").replace("|", "｜")
        if not name.endswith(".md"):
            name += ".md"
        i = 2
        base = name
        while os.path.exists(mem_path(name)) or name in sessions:
            name = base.replace(".md", "") + f"({i}).md"
            i += 1
        # 创建空文件
        try:
            with open(mem_path(name), "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            messagebox.showerror("创建失败", str(e))
            return False
        sessions[name] = {"messages": [{"role": "system", "content": build_system(name)}], "turns": 0,
                          "ctx_start": 1}
        self.current_mem = name
        self.pending_new = False
        self.title_var.set(name)
        self.refresh_mem_list()
        self.select_mem(name)
        self.add_arch(f"📄 已创建记忆文件：{name}")
        return True

    def _ctx_messages(self, s):
        """构建发给模型的上下文：system（含长期记忆摘要）+ 未归档的近期对话。
        显示历史保留在 s["messages"] 全量里，模型只看 ctx_start 之后的部分，避免上下文无限膨胀。"""
        cs = s.get("ctx_start", 1)
        if cs < 1 or cs > len(s["messages"]):
            cs = 1
        return s["messages"][:1] + s["messages"][cs:]

    def _fit_ctx(self, s, budget=None):
        """发送前的硬保护：把模型上下文裁到预算内，从最旧的一轮开始丢（推进 ctx_start）。
        必须保留最后一轮问答，否则请求就没意义了。
        返回 (丢弃条数, 裁剪后估算 tokens)。"""
        budget = budget or input_budget()
        cs = s.get("ctx_start", 1)
        if cs < 1 or cs > len(s["messages"]):
            cs = 1
        head = s["messages"][:1]
        tail = list(s["messages"][cs:])
        total = est_messages_tokens(head) + est_messages_tokens(tail)
        dropped = 0
        while total > budget and len(tail) > 2:
            total -= est_content_tokens(tail[0].get("content"))
            tail.pop(0)
            cs += 1
            dropped += 1
        if dropped:
            s["ctx_start"] = cs
        return dropped, total

    # ---------- 「思考中」动态指示 ----------
    # 转圈动画：每帧换一个字符，比点号更醒目
    SPINNER = "◐◓◑◒"

    def _begin_think(self):
        """开始动态思考指示（在 AI 占位行里跑动效，首字到达前一直转）"""
        self._think_t0 = datetime.datetime.now()
        self._think_i = 0
        self._first_token = False
        self._tick_think()

    def _tick_think(self):
        """动效帧：转圈 + 已等待秒数（超 8 秒补一句加载提示）。
        整个函数包了 try/except —— 动效失败绝不能影响生成流程或让 busy 卡住。"""
        self._think_after_id = None
        if self._first_token or self._closing:
            return
        try:
            if not self.winfo_exists():
                return
            elapsed = (datetime.datetime.now() - self._think_t0).total_seconds()
            spin = self.SPINNER[self._think_i % len(self.SPINNER)]
            text = f"{spin} 思考中…   已等待 {elapsed:.0f} 秒"
            if elapsed >= 8:
                text += "   （首次使用需加载模型，可能要 10-60 秒）"
            rng = self.chat.tag_ranges("thinking")
            self.chat.configure(state="normal")
            try:
                if rng:
                    start = rng[0]
                    self.chat.delete(rng[0], rng[1])
                    self.chat.insert(start, text, ("ai", "thinking"))
                else:
                    # "_ai_mark" 是 Tk 的 mark 名（字符串），不是 Python 属性
                    self.chat.insert("_ai_mark", text, ("ai", "thinking"))
            except Exception:
                # mark 不在（异常路径）→ 退回行尾插入，保证提示一定可见
                self.chat.insert("end-1c", text, ("ai", "thinking"))
            self.chat.configure(state="disabled")
            self._see_end()
        except Exception:
            pass
        self._think_i += 1
        try:
            self._think_after_id = self.after(400, self._tick_think)
        except Exception:
            self._think_after_id = None

    def _stop_think(self, clear=True):
        """停止动效；clear=True 同时清掉指示文字（首字到达 / 生成结束时用）"""
        if self._think_after_id is not None:
            try:
                self.after_cancel(self._think_after_id)
            except Exception:
                pass
            self._think_after_id = None
        if clear:
            try:
                rng = self.chat.tag_ranges("thinking")
                if rng:
                    self.chat.configure(state="normal")
                    self.chat.delete(rng[0], rng[1])
                    self.chat.configure(state="disabled")
            except Exception:
                pass

    def _worker(self, name):
        global busy
        s = sessions[name]
        t0 = datetime.datetime.now()
        self.stop_event.clear()
        # 发送前硬保护：超限就先裁掉最旧的对话，绝不让服务端回 400
        try:
            dropped, total = self._fit_ctx(s)
            if dropped:
                self._ui_call(lambda d=dropped, t=total: self.add_arch(
                    f"⚠️ 上下文超出模型上限，已临时丢弃最早 {d} 条（当前约 {t} tokens）"))
        except Exception:
            pass
        # 流式占位 + 动态思考指示
        def _placeholder():
            # 关键：占位行的起点必须在 add_msg **之前**取。
            # add_msg 是插到 "end"（末尾隐含换行之后），插入后 end-1c 会指向
            # 多出来的空行，mark 就会偏一行 → 思考文字和正文都跑到气泡外面。
            try:
                base = self.chat.index("end-1c")
            except Exception:
                base = None
            self.add_msg("ai", "", meta="")
            try:
                self.chat.mark_set("_ai_mark", base or "end-1c")
                self.chat.mark_gravity("_ai_mark", "left")
                # _ai_end：跟随正文末尾的右重力标记。
                # 用它做插入锚点才能正确处理流式内容里的换行；
                # 之前用 "_ai_mark lineend" 会把换行后的文字插到换行**之前**，
                # 导致换行全部堆积到末尾（气泡下方一大片空行，输出完才恢复）。
                self.chat.mark_set("_ai_end", base or "end-1c")
                self.chat.mark_gravity("_ai_end", "right")
            except Exception:
                pass
            self._begin_think()
        self._ui_call(_placeholder)
        # ---- C. 回忆：拿你这句话当查询，去归档原文/摘要里检索相关旧内容 ----
        # 纯本地字符串匹配（约 10ms 级）、**不调模型**；没命中就什么都不加，
        # 所以平时的普通对话上下文大小完全不变、响应速度不受影响。
        msgs = self._ctx_messages(s)
        try:
            q = ""
            for m in reversed(s["messages"]):
                if m["role"] == "user":
                    q = content_to_text(m["content"])
                    break
            hit, cnt = search_archives(name, q)
            if hit:
                # ★★ 必须**并入第 1 条 system**，不能新增一条 ✗
                #   有的模型 chat template 强制要求 system 只能在最前面
                #   （27B 那个模板第 106 行直接 raise_exception('System message must be at
                #    the beginning.') → 服务端返 500 ✓ 实测踩到 ✓）
                #   并进去语义完全一样 ✓ 而且对所有模型都安全 ✓
                _recall = ("【从归档里检索到的相关旧对话（供参考，可能和当前问题有关；"
                           "如与当前问题无关请忽略）】\n" + hit)
                msgs = list(msgs)
                if msgs and msgs[0].get("role") == "system":
                    _c = msgs[0].get("content")
                    _c = _c if isinstance(_c, str) else content_to_text(_c)
                    msgs[0] = {"role": "system", "content": _c + "\n\n" + _recall}
                else:
                    msgs = [{"role": "system", "content": _recall}] + msgs
                self._ui_call(lambda c=cnt: self.add_arch(
                    f"🔍 从归档里找到 {c} 段相关旧内容，已加入本次上下文"))
        except Exception:
            pass
        reply, err = api_stream(
            msgs, self.stop_event,
            lambda d: self._stream_feed(d))
        dt = (datetime.datetime.now() - t0).total_seconds()
        self._ui_call(lambda: self._on_reply(name, reply, err, dt))
        # 队里还有排队的 → 接着发（_on_reply 会把 busy 置回 False）
        self._ui_call(self._pump_queue)

    def _trim_ai_tail(self):
        """收尾：把 AI 气泡尾部的连续空行压成一行。
        流式生成过程中可能留下若干个被 ai 标签覆盖的空行，
        不清掉气泡就会比正文高出十几行、白白占掉界面。
        只删「连续的第 2 个及以后」的换行，保留正文行末尾那个换行，
        否则后面的 meta 会挤到正文同一行。"""
        try:
            r = self.chat.tag_ranges("ai")
            if not r:
                return
            end = r[-1]
            self.chat.configure(state="normal")
            guard = 0
            while guard < 80:
                guard += 1
                if self.chat.get(f"{end} -1c", end) != "\n":
                    break
                prev = self.chat.index(f"{end} -1c")
                if self.chat.get(f"{prev} -1c", prev) != "\n":
                    break          # 只剩一个换行 → 是正文行的结尾，保留
                self.chat.delete(prev, end)
                end = prev
            self.chat.configure(state="disabled")
        except Exception:
            pass

    def _stream_append(self, delta):
        """流式增量追加到 AI 正文末尾；首个 token 到达时撤掉「思考中」指示

        ★ 带**耗时探针** ✓（只记慢的 ✓ 不刷屏 ✓）
          用户反馈过"思考/输出时会卡"✗ 但拿不到数据 ✗ 只能猜 ✓
          现在只要有 >150ms 的追加 ✓ 就写一条日志 ✓ 直接指明卡在哪 ✓
        """
        _t0 = time.perf_counter()
        try:
            self._stream_append_inner(delta)
        finally:
            try:
                _ms = (time.perf_counter() - _t0) * 1000
                if _ms > 150:
                    _n = 0
                    try:
                        _n = len(self.chat.get("1.0", "end-1c"))
                    except Exception:
                        pass
                    _lines = int(self.chat.index("end-1c").split(".")[0]) if True else 0
                    EA.log("慢追加 %.0fms  本次 %d 字  总 %d 字 / %d 行  编辑框=%s"
                           % (_ms, len(delta or ""), _n, _lines,
                              "开" if getattr(self, "_inline_edit", None) else "关"), "W")
            except Exception:
                pass

    def _stream_append_inner(self, delta):
        if not self._first_token:
            self._first_token = True
            # ★ 算出「思考用时」= 发出请求 → 第一个正文 token 到达
            try:
                self._think_secs = ((datetime.datetime.now() - self._think_t0)
                                    .total_seconds() if self._think_t0 else 0.0)
            except Exception:
                self._think_secs = 0.0
            self._stop_think(clear=True)
            # 统计行**不在这里插** —— 统一由 _on_reply 插 ✓
            # （那里一定会执行 ✓ 而且 _ai_mark 正好是正文起点，插进去就在正文**上方** ✓）
            # 思考文字被清掉后，把末尾标记收回正文起点
            try:
                self.chat.mark_set("_ai_end", "_ai_mark")
            except Exception:
                pass
            self._stat_t0 = time.time()
        # 保留模型输出的换行：既让输出过程中的排版和最终一致（标题/列表/分段不会糊成一坨），
        # 也让每次插入只影响一条**短行** —— Tk 折行代价与行长成正比，
        # 折成空格会让整条回复变成一个超长行，插入越来越慢（实测 3200 字时单次 330ms）。
        if not delta:
            return
        self.chat.configure(state="normal")
        # 用右重力标记 _ai_end 追加：插入后标记自动后移，追加位置始终在正文末尾
        try:
            self.chat.insert("_ai_end", delta, "ai")
        except Exception:
            self.chat.insert("end-1c", delta, "ai")
        self.chat.configure(state="disabled")
        self._see_end()      # 用户上翻时不跟随（否则输出期间无法查看上面内容）

    def _on_reply(self, name, reply, err, dt):
        global busy
        stopped = self.stop_event.is_set()
        busy = False
        # ★ 收尾前必须先把流式缓冲里剩下的文字写进界面，
        #   否则节流会把最后一段（最多 110ms 的量）丢掉
        try:
            self._flush_stream()
        except Exception:
            pass
        # ★★ 「思考用时」统计 —— 插在 AI 正文**上方**（Harness 的位置 ✓ 回答上面 ✓）
        #   原理：`_ai_mark` 是这条 AI 气泡的起点 ✓ 正文是从它**之后**追加的 ✓
        #        所以在 _ai_mark 处插入 → 统计行天然落在正文**上方** ✓
        #   （放在这里而不是"首个 token 时"，是因为**这里一定会执行** ✓
        #     首个 token 的路径在测试里出现过没走到的情况 ✗）
        try:
            total = float(dt or 0)
            think = float(getattr(self, "_think_secs", 0.0) or 0.0)
            if think <= 0.05 and self._think_t0:
                # 没记到首字时长 → 用开始时间兜底（至少给个总用时 ✓）
                think = 0.0
            line = (f"⏱ 思考 {think:.1f} 秒 · 总用时 {total:.1f} 秒\n"
                    if think > 0.05 else f"⏱ 总用时 {total:.1f} 秒\n")
            self.chat.configure(state="normal")
            self.chat.insert("_ai_mark", line, ("aistat",))
            self.chat.configure(state="disabled")
        except Exception:
            pass
        self._stop_think(clear=True)   # 生成结束：停动效并清掉「思考中」残留
        self._set_busy_ui(False)
        if name not in sessions:
            self._reset_regen()
            return
        s = sessions[name]
        in_regen = self._in_regen
        if err:
            # 上下文超限 → 自动裁剪后重试一次（兜底，正常已被 _worker 的预裁剪挡住）
            if is_overflow_error(err) and not self._ovf_retry:
                self._ovf_retry = True
                dropped, total = self._fit_ctx(s, budget=max(512, int(input_budget() * 0.7)))
                self.set_status(f"🔁 上下文超限，已裁剪 {dropped} 条后自动重试…")
                self.add_arch(f"上下文超限，自动裁剪 {dropped} 条后重试（约 {total} tokens）")
                # 清掉失败的空 AI 占位行，重新请求
                self.chat.configure(state="normal")
                self.chat.delete("end-2c linestart", "end-1c")
                self.chat.configure(state="disabled")
                busy = True
                try:
                    self._set_busy_ui(True)
                except Exception:
                    pass
                threading.Thread(target=self._worker, args=(name,), daemon=True).start()
                return
            # 裁剪后仍然超限 → 明确告诉用户原因和办法
            if is_overflow_error(err):
                self.set_status("❌ 上下文仍然超限：请缩短这条消息，或先归档/新建会话")
                self.add_arch("❌ 上下文超限（已裁剪仍不够），请缩短输入或新建会话")
                self.chat.configure(state="normal")
                self.chat.delete("end-2c linestart", "end-1c")
                self.chat.configure(state="disabled")
                if in_regen:
                    self._restore_regen(name, s, "上下文超限，已还原原对话")
                    return
                if s["messages"] and s["messages"][-1]["role"] == "user":
                    s["messages"].pop()
                    s["turns"] = max(0, s["turns"] - 1)
                return
            self.set_status(f"❌ 调用失败：{err}")
            # 移除空 AI 占位行
            self.chat.configure(state="normal")
            self.chat.delete("end-2c linestart", "end-1c")
            self.chat.configure(state="disabled")
            if in_regen:
                # 重新生成失败：整体还原（旧回复+后续都放回），不丢内容
                self._restore_regen(name, s, "重新生成失败，已还原原对话")
                return
            # 回滚用户消息
            if s["messages"] and s["messages"][-1]["role"] == "user":
                s["messages"].pop()
                s["turns"] = max(0, s["turns"] - 1)
            return
        if stopped and not reply:
            # 手动停止且无内容：移除 AI 占位行，不写入历史
            self.chat.configure(state="normal")
            self.chat.delete("end-2c linestart", "end-1c")
            self.chat.configure(state="disabled")
            if in_regen:
                # 重新生成被停止：整体还原（旧回复+后续都放回）
                self._restore_regen(name, s, "⏹ 已停止，已还原原对话")
                return
            self.set_status("⏹ 已手动停止")
            return
        s["messages"].append({"role": "assistant", "content": reply})
        if in_regen:
            # 重新生成成功：恢复后续对话（非破坏性），整段重写 md 保持一致
            s["messages"].extend(self._regen_after)
            self._reset_regen()
            try:
                rewrite_dialog_file(name, s["messages"])
            except Exception:
                pass
            self.render_session(name)
            speed = int(len(reply) / max(dt, 0.1))
            total = est_messages_tokens(self._ctx_messages(s))
            self.set_status(f"✅ 已重新生成 · {dt:.1f}秒 · ~{speed}字/秒 · 估算{total} tokens")
            # 自动归档判断（按模型上下文估算，归档后 ctx_start 推进，旧内容不再重复触发）
            if not stopped and (s["turns"] >= MAX_TURNS
                                  or total >= min(MAX_EST_TOKENS, int(input_budget() * 0.65))):
                self.after(100, self._do_summary, name)
            return
        # 生成结束：正文保持在 AI 行上（尾部空白由 _stream_append 的写入内容决定，
        # 这里不能 delete+reinsert —— 那会把 mark 挪位、正文拼到操作条上）
        self._trim_ai_tail()   # 压掉气泡尾部多余空行，避免气泡比正文高一截
        self.chat.configure(state="normal")
        self.chat.insert("end", (MODEL_ID + " · " + datetime.datetime.now().strftime("%H:%M")
                                 + (" · 已手动停止" if stopped else "")) + "\n", "meta")
        self.chat.configure(state="disabled")
        # 该条 AI 消息下方固定一行操作条（真实历史索引定位复制/删除）
        hidx = len([m for m in s["messages"] if m["role"] != "system"]) - 1
        # ★★ 助手消息也要有 _edm{hidx} 标记 ✗→✓（与 render_session 语义一致）
        #   语义是"这条消息的**起点**" ✓ 而助手正文是流式写进去的 ✓
        #   它的起点就是 _ai_mark（左重力 ✓ 一直停在开头 ✓）
        #   不补的话：编辑"最近那条用户消息"时找不到 _edm{hidx+1} ✗
        #   → 隐藏范围只能到 end-1c ✗ → 卡片落不到原位 ✓ 还可能圈住流式回复 ✗
        try:
            self.chat.mark_set(f"_edm{hidx}", "_ai_mark")
            self.chat.mark_gravity(f"_edm{hidx}", "left")
        except Exception:
            pass
        self.add_op_bar("ai", hidx)
        # AI 回复写入 md（持久化）
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            append_dialog(name, "assistant", reply, now)
        except Exception:
            pass
        speed = int(len(reply) / max(dt, 0.1))
        total = est_messages_tokens(self._ctx_messages(s))
        self.set_status(f"✅ {dt:.1f}秒 · ~{speed}字/秒 · 估算{total} tokens · 已写入记忆文件")

        # 自动归档判断（按模型上下文估算）
        if not stopped and (s["turns"] >= MAX_TURNS
                                  or total >= min(MAX_EST_TOKENS, int(input_budget() * 0.65))):
            self.after(100, self._do_summary, name)

    def _restore_regen(self, name, s, msg):
        """重新生成失败/被停止：整体还原到原样（含撤销编辑），重写 md 并重绘。"""
        # 撤销编辑：恢复原问题内容（重新思考场景 _regen_edit_orig 为 None，不动）
        if self._regen_edit_orig is not None and s["messages"] and s["messages"][-1]["role"] == "user":
            s["messages"][-1]["content"] = self._regen_edit_orig
        s["messages"].extend(self._regen_backup)
        self._reset_regen()
        try:
            rewrite_dialog_file(name, s["messages"])
        except Exception:
            pass
        self.render_session(name)
        self.set_status(msg)

    def _split_chunks(self, hist, budget=SUMMARY_CHUNK_TOKENS):
        """把待归档对话按估算 token 切块，保证每块单独摘要都不超上下文"""
        chunks, cur, used = [], [], 0
        for m in hist:
            t = est_content_tokens(m.get("content"))
            if cur and used + t > budget:
                chunks.append(cur)
                cur, used = [], 0
            cur.append(m)
            used += t
        if cur:
            chunks.append(cur)
        return chunks

    def _start_sum_timer(self):
        """★ 归档实时秒级计时器（独立于分块循环 ✓ 每秒刷新 ✓ 不会被 api() 阻塞冻住 ✓）"""
        self._sum_timer_id = None
        self._tick_sum_timer()

    def _tick_sum_timer(self):
        """每秒刷新归档进度显示（已耗时 + 当前块 + 预计剩余）"""
        import time as _t
        if not getattr(self, "_sum_t0", 0):
            return   # 归档已结束，停表
        elapsed = _t.time() - self._sum_t0
        cur = getattr(self, "_sum_cur", 0)
        total = getattr(self, "_sum_total", 1)
        label = getattr(self, "_sum_label", "自动")
        if cur > 0:
            avg = elapsed / cur
            remain = max(0, avg * (total - cur))
            self.set_status(f"⏳ 正在{label}提炼归档…（第 {cur}/{total} 段 · 已耗时 {elapsed:.0f} 秒 · 预计还需 {remain:.0f} 秒）")
        else:
            self.set_status(f"⏳ 正在{label}提炼归档…（已耗时 {elapsed:.0f} 秒）")
        self._sum_timer_id = self.after(1000, self._tick_sum_timer)

    def _stop_sum_timer(self):
        """归档结束，停掉秒级计时器"""
        self._sum_t0 = 0
        tid = getattr(self, "_sum_timer_id", None)
        if tid:
            try:
                self.after_cancel(tid)
            except Exception:
                pass
            self._sum_timer_id = None

    def _do_summary(self, name, manual=False):
        global busy
        if busy or name not in sessions:
            if manual:
                self._after_archive = None
                self.set_status("⚠️ 正在忙（生成或归档中），请稍后再试")
            return
        s = sessions[name]
        # 只提炼 ctx_start 之后未归档的对话（之前内容已在长期记忆摘要里，避免重复摘要）
        hist = [m for m in self._ctx_messages(s) if m["role"] != "system"]
        if not hist:
            if manual:
                self._after_archive = None
            return
        self.stop_event.clear()  # 在主线程开始归档时清除旧标志，之后不覆盖强制发送的中断。
        busy = True
        if manual:
            self.set_status("⏳ 正在提炼归档（手动）…")
            self.add_arch("正在提炼归档…")
        else:
            self.set_status("⏳ 上下文快满了，正在自动提炼归档…")
            self.add_arch("正在自动提炼归档…")
        # 关键：分块摘要。之前把整坨上下文一次性发出去做摘要，
        # 上下文一超限摘要请求自己也 400 → 归档永远失败 → ctx_start 不推进 → 死循环
        chunks = self._split_chunks(hist)
        self._sum_t0 = __import__("time").time()      # ★ 归档开始时间
        self._sum_total = len(chunks)                   # ★ 总块数
        self._sum_cur = 0                               # ★ 当前第几块（秒级计时器用）
        self._sum_label = "自动" if not manual else "手动"
        self._start_sum_timer()                         # ★ 启动实时秒级计时器
        # 记下这次要归档的**原文**：摘要写进 .md 之后，原文会完整搬进隐藏区
        pairs = []
        for m in hist:
            c = m["content"]
            if not isinstance(c, str):
                try:
                    c = self._mem_user_segments(c)
                except Exception:
                    c = str(c)
            pairs.append((m["role"], c))
        self._arch_pending[name] = pairs
        threading.Thread(target=self._summary_worker, args=(name, chunks), daemon=True).start()

    def _summary_worker(self, name, chunks):
        """逐块摘要后合并；单块请求必然在上下文预算内"""
        parts, err = [], None
        for i, chunk in enumerate(chunks, 1):
            if self.stop_event.is_set():
                break
            self._sum_cur = i   # ★ 通知秒级计时器当前进度（计时器自己刷新显示 ✓）
            text = "\n".join(
                f"{m['role']}: " + (m["content"] if isinstance(m["content"], str) else "[图片]")
                for m in chunk)
            if len(chunks) > 1:
                text = f"（第 {i}/{len(chunks)} 段）\n" + text
            summary, err = api([
                {"role": "system", "content":
            "你是一个对话归档助手，擅长提炼要点。"
            "**不要记录 AI 对自己身份、名称、底层模型的任何说法或猜测**"
            "（那属于噪音：模型常常顺着用户的话瞎猜，记下来会污染以后的对话）。"
            "用户问这类问题时，只记『用户问了模型身份』这个事实即可，不要记 AI 的回答。"
            "同理，AI 关于自己运行在本机还是云端的说法也不要记。"},
                {"role": "user", "content": text + "\n\n" + SUMMARY_PROMPT},
            ], stop_event=self.stop_event)
            if err or self.stop_event.is_set():
                break
            if summary:
                parts.append(summary)
        self._ui_call(lambda: self._on_summary(name, "\n".join(parts), err))

    def _meta_worker(self, name, over):
        """A. 分层摘要：把「装不进近期预算的最老那批摘要」+ 现有总纲，
        再压缩成一份新的「开篇总纲」，写回 md，并把被吸收的旧块删掉。

        只在这一步多调一次模型（平均十几次归档才发生一次），
        平时每一轮的上下文大小不变，所以不影响对话响应速度。"""
        try:
            old = [block_body(b) for b in extract_arch_blocks(load_mem(name))
                   if is_meta_block(b)]
            merged = "\n\n".join([x for x in old if x] + [block_body(b) for b in over])
            if not merged.strip():
                return
            summary, err = api([
                {"role": "system", "content":
            "你是长对话记忆压缩助手，擅长把摘要再压缩成梗概。"
            "**保持原样地丢弃**关于『AI 自称是什么模型 / 什么身份 / 本地还是云端』的内容，"
            "不要把它带进梗概里。"},
                {"role": "user", "content": merged + "\n\n" + META_PROMPT},
            ])
            if err or not summary or not summary.strip():
                return
            set_meta_block(name, summary, over)
            self._ui_call(lambda: self._after_meta(name, len(over)))
        except Exception:
            pass

    def _after_meta(self, name, absorbed=0):
        """总纲更新完：刷新 system 提示词并把"已压缩"告诉用户"""
        try:
            if name in sessions:
                sessions[name]["messages"][0] = {
                    "role": "system", "content": build_system(name)}
            self.set_status(f"🗜 已把最早的 {absorbed} 段归档压缩进「开篇总纲」（永久保留）")
            self.add_arch(f"🗜 早期 {absorbed} 段归档已压缩成「开篇总纲」，开头的事以后也记得")
        except Exception:
            pass

    def _on_summary(self, name, summary, err):
        self._stop_sum_timer()   # ★ 停掉秒级计时器
        global busy
        stopped = self.stop_event.is_set()
        busy = False
        self._set_busy_ui(False)
        self._pump_queue()
        if stopped:
            self._arch_pending.pop(name, None)
            self._after_archive = None
            self.set_status("⏹ 已停止归档，原对话保留；继续处理排队消息")
            self.add_arch("已停止归档（未写入摘要）")
            return
        if name not in sessions:
            return
        if err:
            self.set_status(f"⚠️ 归档失败：{err}")
            self.add_arch("归档失败")
            return
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        # ① 原文**完整搬进隐藏区**（一行不丢），归档后从聊天区隐掉
        pairs = self._arch_pending.pop(name, []) if hasattr(self, "_arch_pending") else []
        arch_path = None
        if pairs:
            try:
                arch_path = arch_save_batch(name, now, pairs, summary)
            except Exception as e:
                self.set_status(f"⚠️ 归档原文保存失败：{e}")
        # ② 标题里带上**记忆文件名**：归档摘要可能被导出/搬家，带上来源才认得出是谁的
        append_mem(name, f"会话归档 {name} {now}", summary)
        # 不清空**内存**里的历史（聊天区靠 ctx_start 决定显示哪些），只推进模型上下文起点
        s = sessions[name]
        s["messages"][0] = {"role": "system", "content": build_system(name)}
        s["ctx_start"] = len(s["messages"])   # 全部旧对话都已并入摘要 → 模型上下文从末尾重新开始
        s["turns"] = 0
        # ★ 把已归档的逐条对话从 **md 文件**里移除，只留归档摘要 + 还没归档的新对话。
        #   这一步就是「提炼重点」：md 始终只有摘要那么长，上下文永远不会膨胀。
        #   原文刚才已经**完整**存进 memories\_arch_batches\（上面的 arch_save_batch），一个字都丢不了。
        #   rewrite_dialog_file 会自动保留全部 `## 会话归档` 摘要块，所以长期记忆不受影响。
        try:
            rewrite_dialog_file(name, s["messages"][s["ctx_start"]:])
        except Exception as e:
            self.set_status(f"⚠️ 记忆文件精简失败（摘要已保存）：{e}")
        self._loaded_from = 0
        self.render_session(name)             # 已归档部分不再显示，其余历史照常
        extra = "，原文已存入归档区" if arch_path else ""
        self.add_arch(f"✅ 已归档到 {name}（摘要已进长期记忆，原文收进归档区，可继续对话）")
        self.set_status(f"✅ 已归档{extra} —— 到左下角「本地用户 · 归档」查看")
        # ---- A. 分层摘要：近期摘要装不下时，把最老的那批再压缩成「开篇总纲」 ----
        # 在**后台线程**里做，不影响你继续打字；平均十几次归档才触发一次。
        try:
            over = meta_overflow(name)
            if over:
                threading.Thread(target=self._meta_worker,
                                 args=(name, over), daemon=True).start()
        except Exception:
            pass
        # 手动「只保留归档对话」的后续步骤：归档完成后接着清掉逐条对话
        act = getattr(self, "_after_archive", None)
        self._after_archive = None
        if act and act[0] == "clear_dialog" and act[1] == name:
            self._do_clear_dialog(name)

    # ---------- 模型切换 ----------
    def on_model_change(self, event=None):
        global MODEL_ID, busy
        old = MODEL_ID
        new = self.model_var.get()
        if not new:
            return
        # 正在生成时切换模型 → 先停止当前生成
        if busy:
            self.stop_event.set()
            self.set_status("⏹ 正在停止当前生成…")
        MODEL_ID = new
        # 记住本次选择（下次启动默认用它）
        st = load_settings()
        st["model"] = new
        save_settings(st)
        self.set_status(f"📦 已切换模型：{new} · 首次对话会自动加载（可能等待 10-60 秒）")
        self.add_arch(f"模型已切换为 {new}")
        # 释放旧模型：否则它会继续占显存、甚至还在后台生成
        if old and old != new:
            self.set_model_state(f"⏳ 正在释放旧模型：{old} …", "#d08a00")
            threading.Thread(target=self._release_model_async, args=(old,), daemon=True).start()
        else:
            self.refresh_model_state()
        self.rebuild_reason_combo(new)

    # ---------- 推理强度（自动扫描模型模板，不写死） ----------
    def rebuild_reason_combo(self, mid):
        """扫描所选模型的 GGUF chat 模板，按其真实值域重建推理下拉"""
        caps = self._reason_cache.get(mid)
        if caps is None:
            path = self._model_paths.get(mid)
            # ★ path 为 None **也要问适配器** —— 云端/通用引擎本来就没有 GGUF 文件，
            #   但它照样支持 reasoning_effort。原来的 `if path else 不支持` 短路 ✗
            #   导致云端永远显示"推理：不支持"（用户实测反馈）。
            caps = scan_model_reasoning(path)
            self._reason_cache[mid] = caps
        opts = [("推理：默认", {})]
        if caps.get("thinking"):
            opts.append(("推理：关闭思考", {"enable_thinking": False}))
        # 档位名显示成中文，但**发给服务端的仍然是 low/medium/high**（标准字段值）
        _CN = {"minimal": "极低", "low": "低", "medium": "中", "high": "高"}
        for v in caps.get("efforts") or []:
            label = f"推理：{_CN.get(str(v).lower(), v)}"
            if caps.get("default") == v:
                label += "（默认）"
            opts.append((label, {"reasoning_effort": v}))
        self._reason_options = opts
        labels = [o[0] for o in opts]
        self._reason_labels = labels
        if len(opts) <= 1:
            self.reason_var.set("推理：不支持")
        else:
            # 保留上次选择（若该模型仍支持），否则回到默认
            cur = self.reason_var.get()
            self.reason_var.set(cur if cur in labels else labels[0])
        self._apply_reasoning()
        self.refresh_model_pill()

    def refresh_model_pill(self):
        """更新合并控件上的文字：模型名 · 推理项"""
        try:
            mid = self.model_var.get() or "未选择模型"
            rsn = self.reason_var.get().replace("推理：", "")
            self.model_pill.set_text(f"{mid} · {rsn}  ▾")
        except Exception:
            pass

    # ---------- 模型 + 推理 合并设置面板 ----------
    def on_reason_change(self, event=None):
        self._apply_reasoning()
        self.refresh_model_pill()
        self.set_status(f"🧠 {self.reason_var.get()}（下次对话生效）")

    def toggle_settings_panel(self):
        if self._settings_panel is not None:
            self.close_settings_panel()
        else:
            self._build_settings_panel()

    def _on_main_focus_out(self, event=None):
        """主窗口失去焦点 → 自动关闭模型选择面板 ✓

        用户反馈："点 GUI 外面的界面时 GUI 窗口下去了，但模型切换框还显示在页面上面
                  而且点击其他位置不能取消，只有回到 GUI 界面点击里面才能取消"
        根因：面板是 overrideredirect + topmost ✗ 不跟主窗口一起收 ✓
        修法：主窗口 <FocusOut> 时关面板 ✓
        ★ 必须检查 event.widget is self（铁律 0k ✓ 子控件冒泡 ✗）
        ★★ 防误关守卫：刚打开面板时面板自己会抢焦点 → 主窗口瞬间失焦 →
           如果不守卫就会"闪一下就没"（用户实测"目测一帧都不到" ✓）
           所以面板创建后 500ms 内不响应 FocusOut ✓
        """
        try:
            w = getattr(event, "widget", None)
            if w is not None and w is not self:
                return
        except Exception:
            pass
        # ★ 防误关守卫
        import time as _t
        if _t.time() - getattr(self, "_sp_open_time", 0) < 0.3:
            return
        self.close_settings_panel()

    def close_settings_panel(self):
        p = self._settings_panel
        self._settings_panel = None
        if p is not None:
            try:
                p.destroy()
            except Exception:
                pass
        try:
            self.model_pill.set_text(self.model_pill.cget_text().replace("▴", "▾"))
        except Exception:
            pass

    def _build_settings_panel(self):
        """模型选择面板 —— 仿 DeepSeek Harness 的样式。

        ★★ 创建时记时间戳 _sp_open_time —— 防止 FocusOut 把刚打开的面板误关 ✗

        顶部**搜索框** + **按引擎分组**的模型列表 + 悬停高亮 + 勾选当前项。
        ★ 关键改进：**云端和本地的模型同时列出来** —— 不再出现
          "切到云端就看不到本地模型"（用户反馈）。点其它分组的模型会**自动切引擎**。
        """
        self._sp_open_time = __import__("time").time()  # ★ 防误关守卫时间戳
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.configure(bg="#ffffff", highlightthickness=1, highlightbackground="#e3e3e3")
        # ★ 2026-10-10 修：去掉 -topmost ✗
        #   原因：-topmost 让面板**永远浮在所有软件上面** ✗
        #   用户实测："切换对话框还在别的软件上面显示"
        #   修后面板只在 MarkTalk 前面显示 ✓ 切到别的软件它跟在后面 ✓
        #   配合 FocusOut 关面板 → 点外面面板就消失 ✓
        self._settings_panel = top

        W, H = 420, 700

        # ---------- 底部：推理等级 + 云端设置 ----------
        # ★ 必须先建、并用 side="bottom" 钉住 —— Tk 空间不够时会挤掉**最后 pack 的控件**，
        #   footer 放最后就会被切掉（这个面板已经踩过一次，见接续文档坑 #1）。
        foot = tk.Frame(top, bg="#ffffff")
        foot.pack(side="bottom", fill="x", padx=12, pady=(6, 12))
        tk.Frame(foot, bg="#eeeeee", height=1).pack(fill="x", pady=(0, 6))
        rsn_box = tk.Frame(foot, bg="#ffffff")
        rsn_box.pack(fill="x")

        def _goto_cloud():
            self.close_settings_panel()
            self.open_settings_window("models")

        cloud_row = tk.Frame(foot, bg="#ffffff")
        cloud_row.pack(fill="x", pady=(6, 0))
        cl = tk.Label(cloud_row, text="⚙  设置（模型 / 引擎 / 云端提供商）…", bg="#ffffff", fg="#2f6fd0",
                      anchor="w", cursor="hand2",
                      font=("Microsoft YaHei UI", 9))
        cl.pack(fill="x")
        cl.bind("<Button-1>", lambda e: _goto_cloud())

        # ---------- 顶部：搜索框 ----------
        head = tk.Frame(top, bg="#ffffff")
        head.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(head, text="🔍", bg="#ffffff", fg="#9a9a9a",
                 font=("Microsoft YaHei UI", 10)).pack(side="left")
        qv = tk.StringVar()
        qe = tk.Entry(head, textvariable=qv, bd=0, relief="flat", bg="#f4f5f7",
                      fg="#1f1f1f", insertbackground="#1f1f1f",
                      font=("Microsoft YaHei UI", 10))
        qe.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=5)

        # ---------- 中部：可滚动的分组列表 ----------
        mid = tk.Frame(top, bg="#ffffff")
        mid.pack(fill="both", expand=True, padx=12)
        cv = tk.Canvas(mid, bg="#ffffff", highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg="#ffffff")
        _win = cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        inner.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",
                lambda e: cv.itemconfigure(_win, width=e.width))

        def _wheel(e):
            """滚轮滚动。

            ★ 必须**递归**绑到所有子控件上 —— 原来只绑了画布和 inner ✗，
              鼠标停在某个模型名（那一行的 Label）上滚时，事件落在 Label 上 ✗
              而它没有绑定 → **完全滚不动** ✗（用户实测："这个位置的滚动有问题"）
            """
            try:
                cv.yview_scroll(int(-1 * (e.delta / 120)) * 3, "units")
            except Exception:
                pass
            return "break"

        def _bind_wheel(w):
            """把滚轮事件递归绑到整棵子树（每次 render 后都要重绑，因为行会重建）"""
            try:
                w.bind("<MouseWheel>", _wheel)
            except Exception:
                pass
            try:
                for ch in w.winfo_children():
                    _bind_wheel(ch)
            except Exception:
                pass

        for w in (cv, inner, top):
            w.bind("<MouseWheel>", _wheel)

        # ---------- 收集各引擎的模型 ----------
        # ★ cur 用**可变容器** —— 换引擎后要能原地更新（否则只能关掉面板重建 ✗，
        #   那就成了"切一下就退出界面还闪一下"，用户实测反馈的就是这个 ✗）
        cur = {"url": getattr(ENGINE, "base_url", "") or "",
               "kind": ENGINE_KIND}
        cur_url = cur["url"]
        cur_kind = cur["kind"]
        cur_model = self.model_var.get()
        groups = []
        # ★ 分组的展开/折叠状态。**默认只展开「当前引擎」那一组** ——
        #   否则云端几十个模型会把本地那组顶到看不见的地方 ✗
        #   （用户反馈："本地模型的显示为什么还是不能与云端的共存"）
        self._panel_expanded = {}

        def _expanded_of(g):
            k = ("cur:" if (g.get("url") or "") == cur["url"] else "oth:") + (g.get("url") or "")
            return self._panel_expanded.get(k, (g.get("url") or "") == cur["url"])

        def _sync_cur_group():
            """把「当前引擎」那组刷成最新的引擎/模型（换引擎后调）。

            ★ **关键**：覆盖之前，先把"上一个引擎"降级成一个普通分组留着 ✓
              否则切走之后那组就没了 ✗ 想切回去都找不到 ✗
              （实测发现：切到本地后云端那组直接消失）
            """
            if cur_group is None:
                return
            new_url = getattr(ENGINE, "base_url", "") or ""
            old_url = cur_group.get("url") or ""
            old_items = list(cur_group.get("items") or [])
            if old_url and old_items and old_url != new_url:
                if not any((g.get("url") or "") == old_url for g in groups):
                    groups.append({"label": ("☁ 云端 " if _is_cloud_url(old_url) else "💻 ")
                                            + old_url,
                                   "kind": cur_group.get("kind"),
                                   "url": old_url,
                                   "cloud": cur_group.get("cloud"),
                                   "items": old_items})
            cur_group["label"] = self._engine_label()
            cur_group["kind"] = ENGINE_KIND
            cur_group["url"] = cur["url"] = new_url
            cur_group["cloud"] = _is_cloud_url(new_url)
            cur_group["items"] = [(m, "") for m in (self.model_choices or [])
                                   if _is_chat_model(m)]
        self._sp_sync_cur = _sync_cur_group

        def _is_cloud_url(u):
            return bool(u) and ("127.0.0.1" not in u and "localhost" not in u)

        if self.model_choices:
            groups.append({"label": self._engine_label(), "kind": cur_kind,
                           "url": cur_url, "cloud": _is_cloud_url(cur_url),
                           "items": [(m, "") for m in self.model_choices
                                     if _is_chat_model(m)]})
        # ★ 「当前引擎」那一组要**实时**读 model_choices / 标签 / 地址 ——
        #   这样换引擎后只要 render() 一下就正确了，不必关掉面板重建 ✓
        cur_group = groups[0] if groups else None
        def _models_of(url, adapter_cfg=None):
            try:
                ad = EA.OpenAIAdapter(adapter_cfg or {"base_url": url})
                return ad.fetch_models(timeout=3).get("ids") or []
            except Exception:
                return []

        # ★★★ 这里**只做本地数据**，绝不做网络探测 ——
        #   原来在打开时同步跑 discover_engines + 多次 fetch_models，
        #   点一下要卡 1.6~3 秒才弹出来 ✗（用户实测："点击这个按钮就会卡一下"）
        #   现在：立刻用已有数据渲染，网络探测丢到后台线程，回来再补上。
        if not groups:
            groups.append({"label": (self._engine_label() or "当前引擎"),
                           "kind": cur_kind, "url": cur_url, "cloud": _is_cloud_url(cur_url),
                           "items": [(cur_model or "（未指定模型 —— 点下面「✎ 手动输入模型名…」）",
                                      "")]})

        # ---------- 选中一个模型 ----------
        def pick(mid, g):
            try:
                same = (g.get("url") or "") == cur["url"]
            except Exception:
                same = False
            if same:
                # ★ 同一个引擎内换模型 → **面板不关** ✓
                #   用户实测："我要是点击了模型他就直接退出这个页面了，
                #   我还想要可以继续点击，不是一次只能点击一个"
                self._pick_model(mid)
                try:
                    render()          # 勾号跟着移动
                    render_reason()
                    _bind_wheel(inner)
                    _bind_wheel(rsn_box)
                except Exception:
                    pass
                return
            # ===== 换引擎：★ 面板**也不关** ✓ 原地更新 =====
            #   （原来的做法是关掉+80ms 重开 → 用户看到的是"直接退出界面还卡一下"✗）
            if not same:
                # ★ 切到另一个引擎（写回 config.json 前先备份，多代轮转）
                backup_config()
                try:
                    c2 = EA.load_config(CONFIG_FILE)
                    c2["engine"] = g["kind"]
                    if g["kind"] == "llama":
                        c2.setdefault("llama", {})["base_url"] = g["url"]
                        EA.save_config(CONFIG_FILE, c2)
                    elif g["kind"] == "freetoken":
                        c2.setdefault("freetoken", {})["base_url"] = g["url"]
                        EA.save_config(CONFIG_FILE, c2)
                    else:
                        # 通用/云端：只有地址跟配置里一致才落盘，
                        # 否则会把用户配好的云端地址覆盖掉 ✗（扫到的本地引擎属于这种）
                        if (c2.get("openai") or {}).get("base_url") == g["url"]:
                            EA.save_config(CONFIG_FILE, c2)
                        else:
                            self.add_arch("💡 已临时切到 " + g["url"] +
                                          "（本次运行有效）。想永久生效请在"
                                          "「云端 API 设置」里保存地址。")
                    # ★ 记下覆盖，_setup_engine 会用它（否则又会读回 config 里的旧引擎）
                    self._engine_override = (g["kind"], g["url"])
                except Exception:
                    pass
                # ★ 先只做**快**的部分：装适配器（不发网络请求 ✓）
                #   拉模型列表要出网（云端 /v1/models 往返可能 1~2 秒 ✗）
                #   → 那个放到 after 里，先把面板原地更新好，用户不会觉得卡 ✓
                self._setup_engine(silent=True)
            self._pick_model(mid)
            # 原地更新面板：分组标题/勾号/地址都跟着新引擎走 ✓ 全程不关窗口 ✓
            try:
                self._sp_sync_cur()
                render()
                render_reason()
                _bind_wheel(inner)
                _bind_wheel(rsn_box)
                set_stat("✓ 已切到 " + self._engine_label() + "，正在拉取它的模型列表…",
                         "#1a7f37")
            except Exception:
                pass
            # 慢的部分（出网拉模型列表 + 重算状态）延后执行 ✓
            try:
                self.after(80, self._sp_finish_switch)
            except Exception:
                pass

        def show_row(parent, mid, sub, g):
            chosen = (mid == self.model_var.get() and
                      (g.get("url") or "") == cur["url"])
            row = tk.Frame(parent, bg="#f4f5f7" if chosen else "#ffffff")
            row.pack(fill="x", pady=0)          # ★ 行距收紧，一屏多显示几个模型
            name = tk.Label(row, text=mid, bg=row["bg"],
                            fg="#1f1f1f" if chosen else "#2b2b2b", anchor="w",
                            font=("Microsoft YaHei UI", 9))
            name.pack(side="left", padx=(10, 0), pady=3)
            if sub:
                tk.Label(row, text=sub, bg=row["bg"], fg="#9a9a9a",
                         font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(6, 0))
            if chosen:
                tk.Label(row, text="✓", bg=row["bg"], fg="#2f6fd0",
                         font=("Microsoft YaHei UI", 10, "bold")).pack(side="right", padx=(0, 10))

            def on_click(_e, m=mid, gg=g):
                pick(m, gg)
            for w in (row, name) + tuple(row.winfo_children()):
                w.bind("<Button-1>", on_click)

            def enter(_e):
                if not chosen:
                    row.configure(bg="#f0f2f5")
                    for ch in row.winfo_children():
                        try:
                            ch.configure(bg="#f0f2f5")
                        except Exception:
                            pass

            def leave(_e):
                if not chosen:
                    row.configure(bg="#ffffff")
                    for ch in row.winfo_children():
                        try:
                            ch.configure(bg="#ffffff")
                        except Exception:
                            pass
            for w in (row, name):
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
            return row

        # ---------- 渲染（带搜索过滤）----------
        def _toggle_group(g):
            k = ("cur:" if (g.get("url") or "") == cur["url"] else "oth:") + (g.get("url") or "")
            self._panel_expanded[k] = not _expanded_of(g)
            render()

        def render(*_a):
            for w in inner.winfo_children():
                w.destroy()
            q = qv.get().strip().lower()
            n_row = 0
            for g in groups:
                items = [(m, s) for (m, s) in g["items"]
                         if (not q) or (q in str(m).lower())]
                if not items:
                    continue
                # 搜索时全部展开（否则搜了也看不见结果）；平时只有当前引擎展开
                exp = bool(q) or _expanded_of(g)
                gh = tk.Frame(inner, bg="#ffffff")
                gh.pack(fill="x", pady=(7, 2))
                hl = tk.Label(gh, cursor="hand2", bg="#ffffff", anchor="w",
                              text=("▾ " if exp else "▸ ") + g["label"]
                                   + ("" if exp else f"　（{len(items)} 个模型）"),
                              fg="#8a8a8a" if exp else "#3b6ea5",
                              font=("Microsoft YaHei UI", 8, "bold"))
                hl.pack(side="left")
                hl.bind("<Button-1>", lambda e, gg=g: _toggle_group(gg))
                if not exp:
                    continue
                for m, s in items:
                    show_row(inner, m, s, g)
                    n_row += 1
            if not n_row:
                tk.Label(inner, text="（没有匹配的模型）", bg="#ffffff", fg="#b0b0b0",
                         font=("Microsoft YaHei UI", 9)).pack(pady=12)
            try:
                inner.update_idletasks()
                cv.configure(scrollregion=cv.bbox("all"))
            except Exception:
                pass
            # ★ 行是每次 render 重建的 → 必须重新递归绑滚轮，否则滚不动
            _bind_wheel(inner)
            _bind_wheel(rsn_box)

        qv.trace_add("write", lambda *a: render())
        qe.bind("<MouseWheel>", _wheel)

        # ---------- 推理等级（固定在底部）----------
        def render_reason():
            for w in rsn_box.winfo_children():
                w.destroy()
            tk.Label(rsn_box, text="推理等级", bg="#ffffff", fg="#8a8a8a", anchor="w",
                     font=("Microsoft YaHei UI", 8, "bold")).pack(fill="x", pady=(0, 3))
            for label, _kw in (self._reason_options or [("推理：默认", {})]):
                chosen = (label == self.reason_var.get())
                r = tk.Frame(rsn_box, bg="#f4f5f7" if chosen else "#ffffff")
                r.pack(fill="x", pady=1)
                t = tk.Label(r, text=label, bg=r["bg"],
                             fg="#1f1f1f" if chosen else "#2b2b2b", anchor="w",
                             font=("Microsoft YaHei UI", 10))
                t.pack(side="left", padx=10, pady=5)
                if chosen:
                    tk.Label(r, text="✓", bg=r["bg"], fg="#2f6fd0",
                             font=("Microsoft YaHei UI", 10, "bold")).pack(side="right", padx=10)

                def on_pick_r(_e, L=label):
                    self.reason_var.set(L)
                    self.on_reason_change()
                    # ★ 也不关面板 ✓（可以和换模型交替着点 ✓）
                    try:
                        render_reason()
                        _bind_wheel(rsn_box)
                        _bind_wheel(foot)
                    except Exception:
                        pass
                for w in (r, t):
                    w.bind("<Button-1>", on_pick_r)
                    w.configure(cursor="hand2")

                def enter(_e, rr=r):
                    if not chosen:
                        rr.configure(bg="#f0f2f5")
                        for ch in rr.winfo_children():
                            try:
                                ch.configure(bg="#f0f2f5")
                            except Exception:
                                pass

                def leave(_e, rr=r):
                    if not chosen:
                        rr.configure(bg="#ffffff")
                        for ch in rr.winfo_children():
                            try:
                                ch.configure(bg="#ffffff")
                            except Exception:
                                pass
                r.bind("<Enter>", enter)
                r.bind("<Leave>", leave)

        def _sp_load_extras():
            """把"配好的云端 + 其它在跑的引擎"补成分组（可重复调用 ✓）。

            ★ 换引擎后也要再跑一次 —— 否则刚离开的那个引擎那组就没了 ✗
              想切回去都找不到 ✗（双保险，不依赖"保留旧组"那条逻辑）
            """
            extra = []
            try:
                try:
                    cfg_now2 = EA.load_config(CONFIG_FILE)
                except Exception:
                    cfg_now2 = {}
                oa_cfg2 = dict(cfg_now2.get("openai") or {})
                oa_url2 = str(oa_cfg2.get("base_url") or "").strip()
                seen2 = {cur["url"]}
                for g in groups:
                    seen2.add(g.get("url") or "")
                if oa_url2 and oa_url2 not in seen2:
                    ids2 = _models_of(oa_url2, oa_cfg2)
                    if ids2:
                        seen2.add(oa_url2)
                        extra.append({"label": "☁ 云端 " + oa_url2, "kind": "openai",
                                      "url": oa_url2, "cloud": True,
                                      "items": [(m, "") for m in ids2
                                                 if _is_chat_model(m)]})
                try:
                    disc2 = discover_engines_cached(max_age=0)   # 强制刷新 ✓
                except Exception:
                    disc2 = []
                for dd2 in disc2:
                    u2 = dd2.get("url") or ""
                    if not u2 or u2 in seen2:
                        continue
                    ids3 = dd2.get("ids") or _models_of(u2)
                    if not ids3:
                        continue
                    seen2.add(u2)
                    k2 = dd2.get("kind") or "openai"
                    extra.append({"label": f"💻 {k2}  {u2}",
                                  "kind": k2 if k2 in ("llama", "freetoken") else "openai",
                                  "url": u2, "cloud": False,
                                  "items": [(m, "") for m in ids3
                                             if _is_chat_model(m)]})
            except Exception:
                extra = []
            for e in extra:
                if not any((g.get("url") or "") == (e.get("url") or "") for g in groups):
                    groups.append(e)
        self._sp_load_extras = _sp_load_extras

        def _sp_finish_switch():
            """换引擎后**慢**的那部分：拉模型列表、刷新状态、再重画一次面板。

            ★ 放在 after 里跑 —— 直接在点击处理里跑会让界面卡 1~2 秒 ✗
              （云端 /v1/models 是真实网络往返 ✓）
            """
            try:
                self.refresh_model_list()
                self.refresh_model_state()
                self._poll_model_state()
            except Exception:
                pass
            try:
                # ★ 双保险：重新发现一遍，把刚离开的引擎那组补回来 ✓
                #   否则切走之后就想切回去都找不到 ✗
                self._sp_load_extras()
            except Exception:
                pass
            try:
                self._sp_sync_cur()
                render()
                render_reason()
                _bind_wheel(inner)
                _bind_wheel(rsn_box)
                set_stat("✓ 当前引擎：" + self._engine_label(), "#1a7f37")
            except Exception:
                pass
        self._sp_finish_switch = _sp_finish_switch

        render()
        render_reason()
        _bind_wheel(foot)

        # ---------- 后台补：配好的云端 + 其它正在运行的引擎（不卡界面）----------
        def _bg_extra():
            try:
                _sp_load_extras()
            except Exception:
                pass

            def done():
                try:
                    if not top.winfo_exists():
                        return
                except Exception:
                    return
                render()
            try:
                self._ui_call(done)
            except Exception:
                try:
                    self.after(0, done)
                except Exception:
                    pass
        threading.Thread(target=_bg_extra, daemon=True).start()

        # ---------- 定位 + 聚焦搜索框 ----------
        self.update_idletasks()
        px, py = self.model_pill.winfo_rootx(), self.model_pill.winfo_rooty()
        ph = self.model_pill.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(8, min(px + self.model_pill.winfo_width() - W, sw - W - 12))
        y = py - H - 8
        if y < 8:
            y = min(py + ph + 8, sh - H - 8)
        top.geometry(f"{W}x{H}+{x}+{max(8, y)}")
        try:
            top.after(60, lambda: (qe.focus_set(), top.lift()))
        except Exception:
            pass
        try:
            self.model_pill.set_text(self.model_pill.cget_text().replace("▾", "▴"))
        except Exception:
            pass


    def open_cloud_dialog(self):
        """GUI 内配置引擎：引擎模式 / 云端 API / 本地地址 / 扫描发现

        · 云端密钥走 DPAPI 加密存 api_key.dat（**不写进 config.json**）
        · 「扫描到的引擎」可以点选 → 自动把地址填到对应字段并切好模式
          （解决"8080 和 11434 同时开着，auto 永远选 8080"这种选不了的问题）
        """
        dlg = getattr(self, "cloud_dlg", None)
        try:
            if dlg is not None and dlg.winfo_exists():
                dlg.lift(); dlg.focus_set(); return
        except Exception:
            pass
        cfg = EA.load_config(CONFIG_FILE)
        oa = cfg.get("openai") or {}
        cur_engine = str(cfg.get("engine") or "auto")
        llama_url0 = str((cfg.get("llama") or {}).get("base_url") or "")
        ft_url0 = str((cfg.get("freetoken") or {}).get("base_url") or "")

        dlg = tk.Toplevel(self)
        self.cloud_dlg = dlg
        dlg.title("引擎与云端 API 设置")
        dlg.configure(bg="#fafafa")
        try:
            dlg.transient(self)
        except Exception:
            pass
        # ★ 尺寸 + 位置都要钳制：这个对话框挺高，主窗口靠下时容易掉出屏幕，
        #   掉出去 = 底部按钮点不到（实测 y=349 + 820 > 屏高 1152）。
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        W = 640
        H = max(560, min(880, sh - 180))          # 给标题栏/任务栏留位置（880 够放下标签页全部字段）
        try:
            x = self.winfo_rootx() + 200
            y = self.winfo_rooty() + 60
        except Exception:
            x = y = 160
        x = max(8, min(x, sw - W - 12))
        y = max(8, min(y, sh - H - 60))           # ★ 保证整个窗口在屏幕内
        dlg.geometry(f"{W}x{H}+{x}+{y}")
        dlg.minsize(580, 560)
        dlg.protocol("WM_DELETE_WINDOW", lambda: (self.cloud_dlg.destroy(),
                                                  setattr(self, "cloud_dlg", None)))

        tk.Label(dlg, text="⚙ 引擎与云端 API 设置", bg="#fafafa", fg="#1f1f1f",
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(dlg, text="本地引擎自动发现 · 云端 OpenAI 兼容 API（DeepSeek / 豆包方舟 / Qwen 云端 …）",
                 bg="#fafafa", fg="#9a9a9a",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(0, 8))

        # ★ 先把底部（状态行 + 按钮行）用 side="bottom" 钉住 ——
        #   Tk 空间不够时会把**最后 pack 的控件**挤掉（踩过这个坑）。
        btns = tk.Frame(dlg, bg="#fafafa")
        btns.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        stat = tk.Label(dlg, text="", bg="#fafafa", fg="#8a8a8a", justify="left",
                        anchor="w", wraplength=580,
                        font=("Microsoft YaHei UI", 9))
        stat.pack(side="bottom", fill="x", padx=16, pady=(8, 2))

        def set_stat(msg, color="#8a8a8a"):
            try:
                stat.configure(text=msg, fg=color)
            except Exception:
                pass

        # ---- 选择引擎（★ **动态**：自动 + 云端 + 扫到的每个引擎）----
        # 不再写死成 4 个固定项 —— 扫描发现的引擎会"加载"进这个列表，
        # 引擎起了、起在任何端口，都能在这里选到（用户明确要求"不要写死"）。
        box = tk.LabelFrame(dlg, text=" 选择引擎（自动发现本机在运行的 · 点一下就用）",
                            bg="#fafafa", fg="#3b6ea5",
                            font=("Microsoft YaHei UI", 9, "bold"), bd=1, relief="solid")
        box.pack(fill="x", padx=16, pady=(0, 8))
        mode = tk.StringVar(value=cur_engine if cur_engine in
                                 ("auto", "llama", "freetoken", "openai") else "auto")
        top_row = tk.Frame(box, bg="#fafafa")
        top_row.pack(fill="x", padx=8, pady=(4, 2))
        sel_hint = tk.Label(top_row, text="… 正在自动发现本机在运行的引擎 …",
                            bg="#fafafa", fg="#8a8a8a",
                            font=("Microsoft YaHei UI", 8))
        sel_hint.pack(side="left")
        b_rescan = RoundedButton(top_row, text="重新扫描", command=lambda: do_scan(),
                                 width=76, height=24, radius=8, bg="#fafafa",
                                 fill="#ffffff", font=("Microsoft YaHei UI", 8))
        b_rescan.pack(side="right")
        _tip(b_rescan, "重新发现：列出本机所有在监听的端口，逐个探测哪些是 OpenAI 兼容引擎。\n"
                       "引擎起在任何端口都能找到；约 2 秒")
        sel_wrap = tk.Frame(box, bg="#fafafa")
        sel_wrap.pack(fill="x", padx=8, pady=(0, 6))
        e_sel = tk.Listbox(sel_wrap, height=5, bg="#ffffff", fg="#2b2b2b",
                           selectbackground="#e6ecf5", selectforeground="#1f1f1f",
                           relief="flat", activestyle="none", bd=0,
                           highlightthickness=1, highlightbackground="#e5e5e5",
                           font=("Microsoft YaHei UI", 9))
        sel_sb = ttk.Scrollbar(sel_wrap, orient="vertical", command=e_sel.yview)
        e_sel.configure(yscrollcommand=sel_sb.set)
        e_sel.pack(side="left", fill="both", expand=True)
        sel_sb.pack(side="right", fill="y")
        found = []          # [{kind, url, port, ids, n_models}]

        def _sel_items():
            """选择器里每一项 = (显示文本, 点它时干什么)"""
            items = [("自动（推荐）：云端配全就优先云端，否则探本地 / 自动发现",
                      ("auto", None))]
            if (oa.get("base_url") or "").strip():
                items.append(("☁ 云端 API    " + (oa.get("base_url") or ""),
                              ("openai", oa.get("base_url") or "")))
            _local_openai = str((cfg.get("local_openai") or {}).get("base_url") or "").strip()
            if _local_openai:
                items.append(("⌂ 本机通用引擎（保存的）  " + _local_openai,
                              ("local", _local_openai)))
            for dd in found:
                if dd["n_models"]:
                    mm = "、".join(dd["ids"][:2]) + ("…" if dd["n_models"] > 2 else "")
                else:
                    mm = "（引擎在跑，没加载模型）"
                items.append((f"● {dd['kind']:<9} {dd['url']:<26} {mm}",
                              (dd["kind"], dd["url"])))
            items.append(("✎ 手动指定本地地址（在「本地引擎」标签页填）",
                          ("manual", None)))
            return items

        def _render_sel(pick_mode=None, pick_url=None):
            """重画选择器；pick_mode 指定要选中的那项"""
            e_sel.delete(0, "end")
            for txt, _p in _sel_items():
                e_sel.insert("end", txt)
            # 把当前生效的模式选中（找不到就选第 0 项 = 自动）
            want = pick_mode or mode.get()
            wurl = pick_url if pick_url is not None else (oa.get("base_url") or "")
            target = 0
            for i, (txt, (m, u)) in enumerate(_sel_items()):
                if m == "manual":
                    continue
                if m == want and (u is None or not wurl or u == wurl or m != "openai"):
                    target = i
                    if u and wurl and u == wurl:
                        break
                elif m == want and u is None:
                    target = i
            try:
                e_sel.selection_clear(0, "end")
                e_sel.selection_set(target)
                e_sel.see(target)
            except Exception:
                pass

        def do_scan():
            sel_hint.configure(text="… 正在列出本机在监听的所有端口，逐个探测（约 2 秒）",
                               fg="#8a8a8a")
            set_stat("… 正在动态发现引擎（netstat 找在监听的端口 → 逐个探测）", "#8a8a8a")

            def work():
                try:
                    # ★ 动态发现：先 netstat 找出**所有**在监听的本机端口，再并发探测
                    #   哪些是 OpenAI 兼容服务 —— 引擎起在任何端口都能被找到，
                    #   不依赖硬编码端口表。顺手取模型列表：没加载模型的也算。
                    res = EA.discover_engines()
                except Exception:
                    res = []

                def done():
                    found.clear(); found.extend(res)
                    _render_sel()
                    if not res:
                        sel_hint.configure(text="（没发现正在运行的引擎 —— 起一个后点「重新扫描」）",
                                           fg="#b3261e")
                        set_stat("没发现引擎。先用 bat 启动一个（llama / FreeToken），"
                                 "引擎起在别的端口也没关系", "#b3261e")
                    else:
                        n_model = sum(1 for dd in res if dd["n_models"])
                        sel_hint.configure(
                            text=f"发现 {len(res)} 个引擎"
                                 + (f"（{n_model} 个有已加载模型）" if n_model else
                                    "（都没加载模型，但引擎可用）")
                                 + " —— 点一下即选用",
                            fg="#1a7f37")
                        set_stat(f"发现 {len(res)} 个引擎 —— 选中哪一个就用哪一个，"
                                 f"然后点「保存并重连」")
                # ★ 必须走 _ui_call：Tk 不是线程安全的，从后台线程直接调
                #   self.after() 会静默失败（实测：列表永远停在"正在扫描"）
                try:
                    self._ui_call(done)
                except Exception:
                    try:
                        self.after(0, done)
                    except Exception:
                        pass
            threading.Thread(target=work, daemon=True).start()

        def pick_scanned(_e=None):
            sel = e_sel.curselection()
            if not sel:
                return
            items = _sel_items()
            if sel[0] >= len(items):
                return
            m, u = items[sel[0]][1]
            if m == "auto":
                mode.set("auto")
                set_stat("已选「自动」：云端配全就优先云端，否则探本地 / 自动发现", "#1a7f37")
                return
            if m == "manual":
                mode.set("llama"); nb.select(1)
                set_stat("已选「手动指定」—— 请在「本地引擎」标签页填地址，再点「保存并重连」",
                         "#1a7f37")
                return
            if m == "local":
                mode.set("local"); nb.select(1)
                set_stat(f"已选本机通用引擎：{u} —— 点「保存并重连」生效", "#1a7f37")
                return
            if m == "llama":
                e_llama.delete(0, "end"); e_llama.insert(0, u)
                mode.set("llama"); nb.select(1)
            elif m == "freetoken":
                e_ft.delete(0, "end"); e_ft.insert(0, u)
                mode.set("freetoken"); nb.select(1)
            else:
                e_url.delete(0, "end"); e_url.insert(0, u)
                dd = next((x for x in found if x["url"] == u), None)
                if dd and len(dd.get("ids") or []) == 1 and not e_model.get().strip():
                    e_model.delete(0, "end"); e_model.insert(0, dd["ids"][0])
                mode.set("openai"); nb.select(0)
            set_stat(f"已选：{u} —— 点「保存并重连」生效"
                     + ("（本机引擎不用填密钥）" if u and ("127.0.0.1" in u or "localhost" in u)
                        else ""), "#1a7f37")
        e_sel.bind("<<ListboxSelect>>", pick_scanned)
        e_sel.bind("<Double-Button-1>", pick_scanned)


        # ---- 标签页：云端 / 本地 ----
        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        def field(parent, title, hint, value, show=None):
            tk.Label(parent, text=title, bg="#fafafa", fg="#2b2b2b",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=2, pady=(6, 0))
            e = tk.Entry(parent, font=("Microsoft YaHei UI", 10), show=show)
            e.pack(fill="x", padx=2, ipady=3)
            e.insert(0, value or "")
            if hint:
                tk.Label(parent, text=hint, bg="#fafafa", fg="#b0b0b0", justify="left",
                         font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=2)
            return e

        # ===== 标签 1：云端 API =====
        t1 = tk.Frame(nb, bg="#fafafa")
        nb.add(t1, text="  ☁ 云端 API  ")
        e_url = field(t1, "云端地址 base_url（★ 不带 /v1）",
                      "DeepSeek：https://api.deepseek.com\n"
                      "豆包方舟：https://ark.cn-beijing.volces.com/api/v3",
                      oa.get("base_url") or "")
        saved = ""
        try:
            saved = EA.load_api_key(CONFIG_FILE)
        except Exception:
            saved = ""
        e_key = field(t1, "API Key（留空 = 不改动已保存的）",
                      ("当前已保存：" + EA.mask_key(saved) +
                       "（DPAPI 加密存 api_key.dat，不会写进 config.json）") if saved
                      else "尚未保存。粘贴后点「保存并重连」，会用 Windows DPAPI 加密存入 api_key.dat",
                      "", show="●")
        e_model = field(t1, "模型名（可留空；豆包方舟这类 /v1/models 不可用的必须填）",
                        "留空则从 /v1/models 获取", oa.get("model") or "")
        e_ctx = field(t1, "上下文上限 ctx（0 = 自动探测）",
                      "云端上下文通常很大（64K/128K），填对能让自动归档更晚触发",
                      str(oa.get("ctx") or 0))

        # ===== 标签 2：本地引擎 =====
        t2 = tk.Frame(nb, bg="#fafafa")
        nb.add(t2, text="  💻 本地引擎  ")
        tk.Label(t2, text="标准端口不用管（8080 / 8000），程序会自己探测；\n"
                          "引擎起在**别的端口**时，在这里填上就行。",
                 bg="#fafafa", fg="#9a9a9a", justify="left",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=2, pady=(8, 0))
        e_llama = field(t2, "llama.cpp 地址", "默认 http://127.0.0.1:8080", llama_url0)
        e_ft = field(t2, "FreeToken 地址", "默认 http://127.0.0.1:8000", ft_url0)

        # ---- 按钮 ----
        def collect():
            try:
                ctx = int((e_ctx.get() or "0").strip() or 0)
            except Exception:
                ctx = 0
            return (e_url.get().strip(), e_key.get().strip(), e_model.get().strip(),
                    ctx, mode.get(), e_llama.get().strip(), e_ft.get().strip())

        def cloud_save():
            url, key, model, ctx, eng, lurl, furl = collect()
            if eng == "openai" and not url and not lb.curselection():
                set_stat("✗ 选了「云端 API」就必须填地址（或在下面扫描列表里点一个）", "#b3261e"); return
            # ★ 写 config.json 前先备份（多代轮转，防止一次覆盖就找不回来）
            backup_config()
            cfg2 = EA.load_config(CONFIG_FILE)
            cfg2.setdefault("openai", {})
            cfg2.setdefault("local_openai", {})
            # ★★ 关键：**本机地址绝不能写进 openai.base_url** —— 那会把用户配好的
            #    云端地址覆盖掉 ✗（真出过事故：云端 base_url 被冲成 127.0.0.1:xxxxx）。
            #    本地扫到的通用引擎放 local_openai 段，engine 用 "local"。
            _local = (url.startswith("http://127.0.0.1") or url.startswith("http://localhost")
                      or url.startswith("http://[::1]"))
            if eng == "openai" and _local:
                cfg2["local_openai"]["base_url"] = url
                cfg2["engine"] = "local"
            else:
                cfg2["openai"]["base_url"] = url
                cfg2["openai"]["model"] = model
                cfg2["openai"]["ctx"] = ctx
                cfg2["openai"]["api_key"] = ""      # 密钥绝不写进 config.json
            cfg2.setdefault("llama", {})["base_url"] = lurl
            cfg2.setdefault("freetoken", {})["base_url"] = furl
            cfg2["engine"] = eng
            if not EA.save_config(CONFIG_FILE, cfg2):
                set_stat("✗ 写 config.json 失败", "#b3261e"); return
            try:
                self._engine_override = None      # 以刚保存的配置为准
            except Exception:
                pass
            msg = "✓ 已保存"
            if key:
                okk, err = EA.save_api_key(CONFIG_FILE, key)
                if okk:
                    msg += "，密钥已加密存入 api_key.dat（" + EA.mask_key(key) + "）"
                    try:
                        e_key.delete(0, "end")
                    except Exception:
                        pass
                else:
                    set_stat("✗ 配置已存，但密钥保存失败：" + str(err), "#b3261e"); return
            try:
                self._setup_engine(silent=True)
                msg += "；当前引擎：" + self._engine_label()
            except Exception:
                pass
            set_stat(msg, "#1a7f37")

        def cloud_test():
            url, key, model, ctx, eng, lurl, furl = collect()
            if eng == "llama":
                ad = EA.LlamaAdapter({"base_url": lurl})
            elif eng == "freetoken":
                ad = EA.FreeTokenAdapter({"base_url": furl})
            else:
                if not url:
                    set_stat("✗ 请先填云端地址（或在扫描列表里点一个）", "#b3261e"); return
                ad = EA.OpenAIAdapter({"base_url": url, "api_key": key or saved,
                                       "model": model, "ctx": ctx})
            set_stat("… 正在探测 " + (ad.base_url or "（空地址）"), "#8a8a8a")
            try:
                dlg.update()
            except Exception:
                pass
            if not ad.health():
                set_stat("✗ 连不上（检查地址 / 网络 / 该服务是否要求 API Key）", "#b3261e"); return
            info = ad.fetch_models(timeout=8)
            ids = info.get("ids") or []
            set_stat("✓ 连通！" + (f"发现 {len(ids)} 个模型：{', '.join(ids[:4])}"
                                   + ("…" if len(ids) > 4 else "")
                                   if ids else
                                   "但 /v1/models 没返回模型列表 —— 可在「模型名」里手动填写"),
                     "#1a7f37")

        def cloud_clear():
            if not messagebox.askyesno("清除密钥", "确定删除已保存的云端 API 密钥吗？\n\n"
                                                  "（只删 api_key.dat，config.json 不动）"):
                return
            try:
                os.remove(EA.key_file_for(CONFIG_FILE))
                set_stat("✓ 已删除 api_key.dat", "#1a7f37")
            except Exception as e:
                set_stat("✗ 删除失败：" + str(e), "#b3261e")

        b_save = RoundedButton(btns, text="保存并重连", command=cloud_save,
                               width=104, height=30, radius=10, bg="#fafafa",
                               fill="#2f6fd0", fg="#ffffff",
                               font=("Microsoft YaHei UI", 9))
        b_save.pack(side="left")
        b_test = RoundedButton(btns, text="测试连接", command=cloud_test,
                               width=88, height=30, radius=10, bg="#fafafa",
                               fill="#ffffff", font=("Microsoft YaHei UI", 9))
        b_test.pack(side="left", padx=(6, 0))
        b_clr = RoundedButton(btns, text="清除密钥", command=cloud_clear,
                              width=88, height=30, radius=10, bg="#fafafa",
                              fill="#fff1f0", fg="#b3261e",
                              font=("Microsoft YaHei UI", 9))
        b_clr.pack(side="left", padx=(6, 0))
        b_close = RoundedButton(btns, text="关闭",
                                command=lambda: (self.cloud_dlg.destroy(),
                                                 setattr(self, "cloud_dlg", None)),
                                width=64, height=30, radius=10, bg="#fafafa",
                                fill="#ffffff", font=("Microsoft YaHei UI", 9))
        b_close.pack(side="right")
        _tip(b_save, "写进 config.json（先自动备份）＋ 用 DPAPI 加密保存密钥，然后立刻重连")
        _tip(b_test, "只探测不保存：按当前选的模式试一下能不能连上")
        _tip(b_clr, "删除 api_key.dat（config.json 不受影响）")

        if saved:
            set_stat("已保存密钥：" + EA.mask_key(saved) + "　（密钥框留空 = 不改动它）", "#1a7f37")
        else:
            set_stat("提示：本地引擎一般不用配（自动发现）。用云端就填「云端 API」标签页，"
                     "再点「保存并重连」。")
        try:
            dlg.after(120, do_scan)      # 打开后自动扫一次（后台线程，不卡界面）
        except Exception:
            pass

    def _pick_model(self, mid):
        """选中一个模型（同一个引擎内切换）。写进 gui_settings 记住选择。"""
        global MODEL_ID
        if not mid:
            return
        MODEL_ID = mid
        try:
            if mid not in self.model_choices:
                self.model_choices.append(mid)
            self.model_var.set(mid)
            st = load_settings()
            st["model"] = mid
            save_settings(st)
            self.rebuild_reason_combo(mid)
            self.refresh_model_pill()
            self.set_status(f"✅ {self._engine_label()} · 模型：{mid} · 自动归档已开启")
            self.refresh_model_state()
        except Exception as e:
            messagebox.showerror("切换模型失败", str(e))

    # ================= ⚙ 设置窗口（仿 DeepSeek Harness 桌面端）=================
    def open_settings_window(self, page="models"):
        """左侧导航 + 右侧内容的设置窗口。

        · 模型 —— 多提供商管理（卡片 + 编辑/删除 + 添加）
        · 引擎 —— 引擎模式 + 动态发现的引擎
        · 通用 —— 数据目录 / 配置文件 / 记忆说明
        · 关于 —— 版本与快捷键
        """
        w = getattr(self, "_settings_win", None)
        try:
            if w is not None and w.winfo_exists():
                self._sw_page = page
                self._sw_render()
                w.lift(); w.focus_set(); return
        except Exception:
            pass

        w = tk.Toplevel(self)
        self._settings_win = w
        w.title("设置")
        w.configure(bg="#ffffff")
        try:
            w.transient(self)
        except Exception:
            pass
        W, H = 780, 620
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        x = max(8, min(self.winfo_rootx() + 120, sw - W - 12))
        y = max(8, min(self.winfo_rooty() + 60, sh - H - 60))
        w.geometry(f"{W}x{H}+{x}+{y}")
        w.minsize(680, 520)
        w.protocol("WM_DELETE_WINDOW", lambda: (self._settings_win.destroy(),
                                                setattr(self, "_settings_win", None)))

        body = tk.Frame(w, bg="#ffffff")
        body.pack(fill="both", expand=True, padx=14, pady=14)

        # ---- 左导航 ----
        nav = tk.Frame(body, bg="#f6f7f9", width=150)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self._sw_nav_items = {}
        for key, label in (("general", "通用设置"), ("models", "模型"),
                           ("engine", "引擎"), ("about", "关于")):
            item = tk.Frame(nav, bg="#f6f7f9", cursor="hand2")
            item.pack(fill="x", pady=1, padx=6)
            lab = tk.Label(item, text=label, bg="#f6f7f9", fg="#2b2b2b", anchor="w",
                           font=("Microsoft YaHei UI", 10))
            lab.pack(fill="x", padx=12, pady=8)
            for ww in (item, lab):
                ww.bind("<Button-1>", lambda e, k=key: self._sw_goto(k))
            self._sw_nav_items[key] = (item, lab)

        # ---- 右侧内容 ----
        right = tk.Frame(body, bg="#ffffff")
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self._sw_right = right
        self._sw_page = page
        self._sw_render()

    def _sw_goto(self, page):
        self._sw_page = page
        self._sw_render()

    def _sw_render(self):
        """按当前页重画右侧内容"""
        right = getattr(self, "_sw_right", None)
        if right is None:
            return
        for c in right.winfo_children():
            c.destroy()
        cur = getattr(self, "_sw_page", "models")
        # 导航高亮
        for key, (item, lab) in (getattr(self, "_sw_nav_items", None) or {}).items():
            on = (key == cur)
            try:
                item.configure(bg="#e8eef8" if on else "#f6f7f9")
                lab.configure(bg="#e8eef8" if on else "#f6f7f9",
                              fg="#2f6fd0" if on else "#2b2b2b",
                              font=("Microsoft YaHei UI", 10, "bold" if on else "normal"))
            except Exception:
                pass
        if cur == "models":
            self._sw_page_models(right)
        elif cur == "engine":
            self._sw_page_engine(right)
        elif cur == "about":
            self._sw_page_about(right)
        else:
            self._sw_page_general(right)

    # ---------- 小工具：扁平按钮 / 卡片 ----------
    def _sw_title(self, parent, title, sub=""):
        tk.Label(parent, text=title, bg="#ffffff", fg="#1f1f1f", anchor="w",
                 font=("Microsoft YaHei UI", 13, "bold")).pack(fill="x", pady=(0, 2))
        if sub:
            tk.Label(parent, text=sub, bg="#ffffff", fg="#9a9a9a", anchor="w",
                     justify="left", wraplength=540,
                     font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(0, 12))
        else:
            tk.Frame(parent, bg="#ffffff", height=10).pack(fill="x")

    def _sw_flatbtn(self, parent, text, cmd, fg="#2b2b2b", bg="#ffffff",
                    hover="#f0f2f5", font=("Microsoft YaHei UI", 9)):
        b = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2",
                     font=font, padx=10, pady=4, bd=0,
                     highlightthickness=1, highlightbackground="#e3e3e3")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=hover))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    # ---------- 页：模型（多提供商）----------
    def _sw_page_models(self, parent):
        self._sw_title(parent, "模型", "填入各提供商的 API 密钥即可使用其模型。"
                                       "密钥用 Windows DPAPI 加密存在 api_key.dat，不会写进 config.json。")
        cfg = EA.load_config(CONFIG_FILE)
        provs = EA.providers_of(cfg)
        active = str(cfg.get("active_provider") or "")
        try:
            km = EA.load_api_key_map(CONFIG_FILE)
        except Exception:
            km = {}

        box = tk.Frame(parent, bg="#ffffff")
        box.pack(fill="both", expand=True)

        for p in provs:
            pid = str(p.get("id") or "")
            has_key = bool((km.get(pid) or "").strip())
            is_act = (pid == active)
            # 卡片（用浅边框模拟圆角卡片的观感）
            card = tk.Frame(box, bg="#ffffff", highlightthickness=1,
                            highlightbackground="#e3e3e3")
            card.pack(fill="x", pady=4)
            inner = tk.Frame(card, bg="#ffffff")
            inner.pack(fill="x", padx=14, pady=10)

            # ★ 右侧按钮区**必须先 pack** —— 否则左侧（fill/expand）会把宽度抢走，
            #   长地址时「删除」按钮被挤出卡片边界（又是 Tk 的 pack 规则，见坑 #1）
            right = tk.Frame(inner, bg="#ffffff")
            right.pack(side="right", padx=(10, 0))

            left = tk.Frame(inner, bg="#ffffff")
            left.pack(side="left", fill="x", expand=True)
            nm = tk.Label(left, text=str(p.get("name") or "未命名"), bg="#ffffff",
                          fg="#1f1f1f", anchor="w",
                          font=("Microsoft YaHei UI", 10, "bold"))
            nm.pack(anchor="w")
            sub = str(p.get("base_url") or "（未填地址）")
            if p.get("model"):
                sub += "　模型：" + str(p["model"])
            if is_act:
                sub += "　← 当前使用"
            tk.Label(left, text=sub, bg="#ffffff", fg="#9a9a9a", anchor="w",
                     justify="left", wraplength=330,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            dot = "#1a9c3d" if has_key else "#c9c9c9"
            tk.Label(right, text="●", bg="#ffffff", fg=dot,
                     font=("Microsoft YaHei UI", 11)).pack(side="left", padx=(0, 6))
            if not is_act:
                self._sw_flatbtn(right, "设为当前",
                                 lambda pid=pid: self._sw_set_active(pid)
                                 ).pack(side="left", padx=(0, 6))
            self._sw_flatbtn(right, "编辑",
                             lambda pid=pid: self.open_provider_dialog(pid)
                             ).pack(side="left", padx=(0, 6))
            self._sw_flatbtn(right, "删除", lambda pid=pid: self._sw_del_provider(pid),
                             fg="#b3261e", hover="#fff1f0").pack(side="left")

        if not provs:
            tk.Label(box, text="还没有配置任何云端提供商。", bg="#ffffff", fg="#b0b0b0",
                     anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=6)

        add = tk.Label(box, text="＋ 添加模型提供商", bg="#ffffff", fg="#2f6fd0",
                       cursor="hand2", anchor="center", pady=12,
                       font=("Microsoft YaHei UI", 10),
                       highlightthickness=1, highlightbackground="#e3e3e3")
        add.pack(fill="x", pady=(10, 0))
        add.bind("<Button-1>", lambda e: self.open_provider_dialog(None))
        add.bind("<Enter>", lambda e: add.configure(bg="#f6f8fc"))
        add.bind("<Leave>", lambda e: add.configure(bg="#ffffff"))

    def _sw_set_active(self, pid):
        try:
            backup_config()
            cfg = EA.load_config(CONFIG_FILE)
            cfg["active_provider"] = pid
            cfg["engine"] = "openai"
            EA.save_config(CONFIG_FILE, cfg)
            self._engine_override = None
            self.init_connect(silent=True)      # 重探引擎 + 刷新模型列表
            self._sw_render()
            self.set_status(f"✅ 已切到提供商：{self._engine_label()}")
        except Exception as e:
            messagebox.showerror("切换失败", str(e))

    def _sw_del_provider(self, pid):
        try:
            cfg = EA.load_config(CONFIG_FILE)
            p = next((x for x in EA.providers_of(cfg) if x.get("id") == pid), None)
            if p is None:
                return
            if not messagebox.askyesno("删除提供商",
                                       f"确定删除「{p.get('name')}」吗？\n\n"
                                       "（同时删掉它保存的 API 密钥）"):
                return
            backup_config()
            cfg["providers"] = [x for x in EA.providers_of(cfg) if x.get("id") != pid]
            if cfg.get("active_provider") == pid:
                cfg["active_provider"] = (cfg["providers"][0]["id"]
                                          if cfg["providers"] else "")
            EA.save_config(CONFIG_FILE, cfg)
            km = EA.load_api_key_map(CONFIG_FILE)
            km.pop(pid, None)
            EA.save_api_key_map(CONFIG_FILE, km)
            self._sw_render()
        except Exception as e:
            messagebox.showerror("删除失败", str(e))

    # ---------- 页：引擎 ----------
    def _sw_page_engine(self, parent):
        self._sw_title(parent, "引擎", "点一下即选用。程序用 netstat 动态发现本机所有在监听的端口，"
                                       "再逐个探测哪些是 OpenAI 兼容引擎 —— 引擎起在任何端口都能找到。")
        cfg = EA.load_config(CONFIG_FILE)
        cur_engine = str(cfg.get("engine") or "auto")

        def row(text, sub, chosen, cmd):
            r = tk.Frame(parent, bg="#f4f5f7" if chosen else "#ffffff",
                         highlightthickness=1, highlightbackground="#f0f0f0")
            r.pack(fill="x", pady=2)
            lab = tk.Label(r, text=text, bg=r["bg"], fg="#1f1f1f" if chosen else "#2b2b2b",
                           anchor="w", font=("Microsoft YaHei UI", 10))
            lab.pack(side="left", padx=12, pady=7)
            if sub:
                tk.Label(r, text=sub, bg=r["bg"], fg="#9a9a9a", anchor="w",
                         font=("Microsoft YaHei UI", 8)).pack(side="left")
            if chosen:
                tk.Label(r, text="✓", bg=r["bg"], fg="#2f6fd0",
                         font=("Microsoft YaHei UI", 10, "bold")).pack(side="right", padx=12)
            for w in (r, lab):
                w.bind("<Button-1>", lambda e: cmd())
                w.configure(cursor="hand2")

        def set_engine(kind, url=None, label=""):
            try:
                backup_config()
                c = EA.load_config(CONFIG_FILE)
                c["engine"] = kind
                if url:
                    if kind == "llama":
                        c.setdefault("llama", {})["base_url"] = url
                    elif kind == "freetoken":
                        c.setdefault("freetoken", {})["base_url"] = url
                    else:
                        c.setdefault("local_openai", {})["base_url"] = url
                EA.save_config(CONFIG_FILE, c)
                self._engine_override = None
                self.init_connect(silent=True)      # 重探引擎 + 刷新模型列表
                self._sw_render()
                self.set_status(f"✅ 已切到：{self._engine_label()}"
                                + (f"（{label}）" if label else ""))
            except Exception as e:
                messagebox.showerror("切换引擎失败", str(e))

        row("自动（推荐）", "云端配全就优先云端；否则探本地、再自动发现",
            cur_engine == "auto", lambda: set_engine("auto"))
        row("本地 llama.cpp", str((cfg.get("llama") or {}).get("base_url") or ""),
            cur_engine == "llama", lambda: set_engine("llama"))
        row("本地 FreeToken", str((cfg.get("freetoken") or {}).get("base_url") or ""),
            cur_engine == "freetoken", lambda: set_engine("freetoken"))
        row("本机其它通用引擎", str((cfg.get("local_openai") or {}).get("base_url") or "（在模型页/引擎列表里选一个）"),
            cur_engine == "local", lambda: set_engine("local"))

        tk.Label(parent, text="自动发现到的引擎（点一下即切换）", bg="#ffffff", fg="#8a8a8a",
                 anchor="w", font=("Microsoft YaHei UI", 8, "bold")).pack(fill="x", pady=(14, 4))
        holder = tk.Frame(parent, bg="#ffffff")
        holder.pack(fill="both", expand=True)
        tk.Label(holder, text="… 正在发现（约 2 秒）…", bg="#ffffff", fg="#9a9a9a",
                 anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x")

        def work():
            try:
                found = EA.discover_engines()
            except Exception:
                found = []

            def done():
                try:
                    if not holder.winfo_exists():
                        return
                except Exception:
                    return
                for c in holder.winfo_children():
                    c.destroy()
                if not found:
                    tk.Label(holder, text="（没发现正在运行的引擎）", bg="#ffffff",
                             fg="#b0b0b0", anchor="w",
                             font=("Microsoft YaHei UI", 9)).pack(fill="x")
                    return
                for d in found:
                    k, u = d.get("kind"), d.get("url")
                    mm = ("、".join(d["ids"][:3]) + ("…" if d["n_models"] > 3 else "")
                          if d.get("n_models") else "（引擎在跑，没加载模型）")
                    kind2 = k if k in ("llama", "freetoken") else "local"
                    row(f"{k}  {u}", mm, (self._engine_label() or "").find(u) >= 0,
                        lambda kk=kind2, uu=u: set_engine(kk, uu, f"{k} {u}"))
            try:
                self._ui_call(done)
            except Exception:
                try:
                    self.after(0, done)
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

    # ---------- 页：通用 ----------
    def _sw_page_general(self, parent):
        self._sw_title(parent, "通用设置", "数据与控制接口信息；手机 Web 可在下方手动开启。")
        cfg = EA.load_config(CONFIG_FILE)
        try:
            _tok = load_control_token()
        except Exception:
            _tok = ""
        _cs = getattr(self, "control_server", None)
        _curl = (_cs.url if _cs is not None else
                 f"http://127.0.0.1:{CONTROL_DEFAULT_PORT}（未启动）")
        rows = [
            ("控制接口", _curl),
            ("控制 token", _tok or "（未生成）"),
            ("记忆目录", str(MEM_DIR)),
            ("记忆分级", os.path.basename(MEM_DIR) + "（云端模式只看 cloud/ ✓）"),
            ("配置文件", str(CONFIG_FILE)),
            ("密钥文件", EA.key_file_for(CONFIG_FILE) + "（DPAPI 加密，绑当前用户+本机）"),
            ("上下文上限", f"{CTX_LIMIT} tokens"),
            ("自动归档", "开启（上下文占用到阈值后自动摘要）"),
            ("当前引擎", self._engine_label()),
        ]
        for k, v in rows:
            r = tk.Frame(parent, bg="#ffffff")
            r.pack(fill="x", pady=3)
            tk.Label(r, text=k, bg="#ffffff", fg="#8a8a8a", width=10, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(r, text=v, bg="#ffffff", fg="#2b2b2b", anchor="w",
                     justify="left", wraplength=400,
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
        btns = tk.Frame(parent, bg="#ffffff")
        btns.pack(fill="x", pady=(16, 0))
        self._sw_flatbtn(btns, "打开配置文件所在目录",
                         lambda: self._sw_open_dir(CONFIG_FILE)).pack(side="left")
        self._sw_flatbtn(btns, "打开记忆目录",
                         lambda: self._sw_open_dir(MEM_DIR)).pack(side="left", padx=(8, 0))
        self._sw_flatbtn(btns, "复制控制 token",
                         lambda: self._copy_control_token()).pack(side="left", padx=(8, 0))

        web = tk.Frame(parent, bg="#ffffff")
        web.pack(fill="x", pady=(16, 0))
        server = self.web_server
        tk.Label(web, text=f"手机 Web：{'已开启' if server else '未开启'} · 端口 {WEB_PORT} · 同 WiFi 访问",
                 bg="#ffffff", anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x")
        controls = tk.Frame(web, bg="#ffffff")
        controls.pack(fill="x", pady=(8, 0))
        self._sw_flatbtn(controls, "关闭手机 Web" if server else "开启手机 Web",
                         self._toggle_web).pack(side="left")
        self._sw_flatbtn(controls, "复制 Web token",
                         lambda: self._copy_web_access(True)).pack(side="left", padx=8)
        self._sw_flatbtn(controls, "复制手机地址",
                         self._copy_web_access).pack(side="left")

    def _copy_control_token(self):
        """把控制接口 token 复制到剪贴板（本机用，方便 DSH / curl 调用 ✓）"""
        try:
            tok = load_control_token()
            self.clipboard_clear()
            self.clipboard_append(tok)
            self.set_status("🔌 控制 token 已复制到剪贴板")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def _sw_open_dir(self, path):
        try:
            d = path if os.path.isdir(path) else os.path.dirname(path)
            os.startfile(d)                      # noqa: S606 (Windows 专用)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---------- 页：关于 ----------
    def _sw_page_about(self, parent):
        self._sw_title(parent, "关于", "")
        for line in ("MarkTalk GUI —— 多会话 · 自动归档 · 无限上下文",
                     "",
                     "· 本地引擎：llama.cpp（8080）/ FreeToken（8000）/ 任意 OpenAI 兼容引擎",
                     "· 云端：支持配置多个提供商（DeepSeek / 豆包方舟 / Qwen 云端 …）",
                     "· 密钥：Windows DPAPI 加密存 api_key.dat，绑定当前用户 + 本机",
                     "· 记忆：memories\ 下的 Markdown 明文，可直接阅读和编辑",
                     "",
                     "快捷键：Ctrl+Enter 发送　Esc 关闭弹窗　↑/↓ 在列表中移动"):
            tk.Label(parent, text=line, bg="#ffffff", fg="#2b2b2b", anchor="w",
                     justify="left", font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=1)

    # ---------- 提供商 编辑/新建 对话框 ----------
    def open_provider_dialog(self, pid=None):
        cfg = EA.load_config(CONFIG_FILE)
        provs = EA.providers_of(cfg)
        p = next((x for x in provs if str(x.get("id")) == str(pid)), None) if pid else None
        is_new = p is None
        if is_new:
            p = EA.new_provider("", "", "", 0)
        saved_key = ""
        try:
            saved_key = EA.load_api_key(CONFIG_FILE, p.get("id")) or ""
        except Exception:
            saved_key = ""

        dlg = tk.Toplevel(self._settings_win if getattr(self, "_settings_win", None)
                          and self._settings_win.winfo_exists() else self)
        dlg.title(("添加模型提供商" if is_new else "编辑模型提供商"))
        dlg.configure(bg="#fafafa")
        try:
            dlg.transient(self)
        except Exception:
            pass
        DW, DH = 560, 560
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        x = max(8, min(self.winfo_rootx() + 220, sw - DW - 12))
        y = max(8, min(self.winfo_rooty() + 120, sh - DH - 60))
        dlg.geometry(f"{DW}x{DH}+{x}+{y}")
        dlg.minsize(480, 460)

        btns = tk.Frame(dlg, bg="#fafafa")
        btns.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        stat = tk.Label(dlg, text="", bg="#fafafa", fg="#8a8a8a", anchor="w",
                        justify="left", wraplength=500,
                        font=("Microsoft YaHei UI", 9))
        stat.pack(side="bottom", fill="x", padx=16, pady=(8, 2))

        def set_stat(msg, color="#8a8a8a"):
            try:
                stat.configure(text=msg, fg=color)
            except Exception:
                pass

        tk.Label(dlg, text=("添加模型提供商" if is_new else "编辑模型提供商"),
                 bg="#fafafa", fg="#1f1f1f",
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(dlg, text="任何 OpenAI 兼容服务都可以（DeepSeek / 豆包方舟 / Qwen 云端 / 小米 MiMo …）",
                 bg="#fafafa", fg="#9a9a9a",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(0, 8))

        def field(title, hint, value, show=None):
            tk.Label(dlg, text=title, bg="#fafafa", fg="#2b2b2b",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=16, pady=(6, 0))
            e = tk.Entry(dlg, font=("Microsoft YaHei UI", 10), show=show)
            e.pack(fill="x", padx=16, ipady=3)
            e.insert(0, value or "")
            if hint:
                tk.Label(dlg, text=hint, bg="#fafafa", fg="#b0b0b0", anchor="w",
                         justify="left", font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16)
            return e

        e_name = field("名称", "只是显示用，随便起，比如 DeepSeek / xiaomi",
                       p.get("name") or "")
        e_url = field("云端地址 base_url（★ 不带 /v1）",
                      "DeepSeek：https://api.deepseek.com　　"
                      "豆包方舟：https://ark.cn-beijing.volces.com/api/v3",
                      p.get("base_url") or "")
        e_key = field("API Key（留空 = 不改动已保存的）",
                      ("当前已保存：" + EA.mask_key(saved_key) +
                       "（DPAPI 加密，绑定当前 Windows 用户 + 本机）") if saved_key
                      else "会用 Windows DPAPI 加密存进 api_key.dat，不会写进 config.json",
                      "", show="●")
        e_model = field("默认模型名（可留空）",
                        "豆包方舟这类 /v1/models 不可用的服务需要填",
                        p.get("model") or "")
        e_ctx = field("上下文上限 ctx（0 = 自动探测）", "",
                      str(p.get("ctx") or 0))

        def collect():
            try:
                ctx = int((e_ctx.get() or "0").strip() or 0)
            except Exception:
                ctx = 0
            return (e_name.get().strip(), e_url.get().strip(), e_key.get().strip(),
                    e_model.get().strip(), ctx)

        def do_save():
            name, url, key, model, ctx = collect()
            if not url:
                set_stat("✗ 请填 base_url", "#b3261e"); return
            if not name:
                name = url
            backup_config()
            c = EA.load_config(CONFIG_FILE)
            c.setdefault("providers", [])
            c["providers"] = EA.providers_of(c)
            if is_new:
                np = EA.new_provider(name, url, model, ctx)
                c["providers"].append(np)
                target = np
                if not c.get("active_provider"):
                    c["active_provider"] = np["id"]
            else:
                target = next((x for x in c["providers"]
                               if str(x.get("id")) == str(p.get("id"))), None)
                if target is None:
                    set_stat("✗ 找不到这个提供商（可能已被删）", "#b3261e"); return
                target.update({"name": name, "base_url": url, "model": model, "ctx": ctx})
            # ★ 提供商列表里绝不带密钥字段
            for x in c["providers"]:
                x.pop("api_key", None)
            if not EA.save_config(CONFIG_FILE, c):
                set_stat("✗ 写 config.json 失败", "#b3261e"); return
            msg = "✓ 已保存"
            if key:
                okk, err = EA.save_api_key(CONFIG_FILE, key, target.get("id"))
                if not okk:
                    set_stat("✗ 配置已存，但密钥保存失败：" + str(err), "#b3261e"); return
                msg += "，密钥已加密存入 api_key.dat（" + EA.mask_key(key) + "）"
            try:
                self._engine_override = None
                self.init_connect(silent=True)      # 重探引擎 + 刷新模型列表
                msg += "；当前引擎：" + self._engine_label()
            except Exception:
                pass
            set_stat(msg, "#1a7f37")
            try:
                self._sw_render()
            except Exception:
                pass

        def do_test():
            name, url, key, model, ctx = collect()
            if not url:
                set_stat("✗ 请先填 base_url", "#b3261e"); return
            ad = EA.OpenAIAdapter({"base_url": url, "api_key": key or saved_key,
                                   "model": model, "ctx": ctx})
            set_stat("… 正在探测 " + url, "#8a8a8a")
            try:
                dlg.update()
            except Exception:
                pass
            if not ad.health():
                set_stat("✗ 连不上（检查地址 / 网络 / Key；401/403 说明 Key 不对）", "#b3261e")
                return
            info = ad.fetch_models(timeout=8)
            ids = info.get("ids") or []
            set_stat("✓ 连通！" + (f"发现 {len(ids)} 个模型：{', '.join(ids[:5])}"
                                   + ("…" if len(ids) > 5 else "")
                                   if ids else
                                   "但 /v1/models 没返回模型列表 —— 请在「默认模型名」里手动填"),
                     "#1a7f37")

        b_save = RoundedButton(btns, text="保存", command=do_save,
                               width=84, height=30, radius=10, bg="#fafafa",
                               fill="#2f6fd0", fg="#ffffff",
                               font=("Microsoft YaHei UI", 9))
        b_save.pack(side="left")
        b_test = RoundedButton(btns, text="测试连接", command=do_test,
                               width=88, height=30, radius=10, bg="#fafafa",
                               fill="#ffffff", font=("Microsoft YaHei UI", 9))
        b_test.pack(side="left", padx=(6, 0))
        b_close = RoundedButton(btns, text="关闭", command=dlg.destroy,
                                width=64, height=30, radius=10, bg="#fafafa",
                                fill="#ffffff", font=("Microsoft YaHei UI", 9))
        b_close.pack(side="right")
        set_stat("填好后点「保存」。密钥框留空 = 不改动已保存的密钥。")

    def _ask_model_name(self):
        """手动输入模型名 —— 云端 API（如小米 MiMo / 豆包方舟）常常不提供 /v1/models"""
        from tkinter import simpledialog
        cur = self.model_var.get()
        name = simpledialog.askstring(
            "手动输入模型名",
            "云端 API 常常不提供 /v1/models，这里可以直接填服务商文档里的模型名。\n\n"
            "例如：\n"
            "  DeepSeek  → deepseek-chat\n"
            "  豆包方舟  → ep-2024xxxx（接入点 ID）\n"
            "  小米 MiMo → 按服务商文档填\n\n"
            "填好后直接发消息即可，模型名会随每次请求发给服务端。",
            initialvalue=cur if cur not in ("", "（无模型）") else "",
            parent=self)
        if not name or not str(name).strip():
            return
        self._pick_model(str(name).strip())

    def _on_panel_pick(self, key, label):
        """面板里选了模型或推理项"""
        if key == "m":
            if str(label).startswith("✎"):
                self.close_settings_panel()
                self._ask_model_name()
                return
            self.model_var.set(label)
            self.close_settings_panel()
            self.on_model_change()
        else:
            self.reason_var.set(label)
            self.close_settings_panel()
            self.on_reason_change()

    def _on_global_click(self, event):
        """点击面板外部时收起面板"""
        p = self._settings_panel
        if p is None:
            return
        try:
            wx, wy = event.x_root, event.y_root
            px, py = p.winfo_rootx(), p.winfo_rooty()
            pw, ph = p.winfo_width(), p.winfo_height()
            if px <= wx <= px + pw and py <= wy <= py + ph:
                return
            bx, by = self.model_pill.winfo_rootx(), self.model_pill.winfo_rooty()
            bw, bh = self.model_pill.winfo_width(), self.model_pill.winfo_height()
            if bx <= wx <= bx + bw and by <= wy <= by + bh:
                return
        except Exception:
            pass
        self.close_settings_panel()

    def _apply_reasoning(self):
        """把当前推理选择写入全局，供请求体作为 chat_template_kwargs 发送"""
        global REASONING_KWARGS
        label = self.reason_var.get()
        for text, kw in self._reason_options:
            if text == label:
                REASONING_KWARGS = dict(kw)
                return
        REASONING_KWARGS = {}

    # ---------- 模型启停 ----------
    def toggle_model_power(self):
        # ★ 云端引擎不提供启停 —— 模型在服务商那边跑，本机卸载不了 ✗
        #   直接给一句明确说明，而不是让用户点了看到"不支持卸载"的报错 ✓
        try:
            if ENGINE is not None and ENGINE.is_cloud():
                self.set_status("☁ 云端模型不由本机启停 —— 直接在上面的「选择模型」里"
                                "换一个模型名即可（每次请求都会把模型名发给服务端）")
                return
        except Exception:
            pass
        """一键启动/停止当前所选模型（停止=卸载并释放显存）"""
        if self._model_power_busy:
            return
        mid = self.model_var.get()
        if not mid:
            self.set_status("⚠️ 请先选择模型")
            return
        run_id, st, reach = running_model()
        if not reach:
            self.set_status("❌ 连不上引擎服务：请先启动本地引擎（llama.cpp / FreeToken / Ollama / LM Studio 等）")
            return
        self._model_power_busy = True
        if run_id:
            self.set_model_state(f"⏳ 正在停止 {run_id} …", "#d08a00")
            threading.Thread(target=self._model_power_async,
                             args=("unload", run_id), daemon=True).start()
        else:
            self.set_model_state(f"⏳ 正在启动 {mid} …（首次可能 10-60 秒）", "#d08a00")
            threading.Thread(target=self._model_power_async,
                             args=("load", mid), daemon=True).start()

    def _model_power_async(self, action, mid):
        if action == "load":
            ok, err = load_model(mid)
        else:
            ok, err = unload_model(mid)
        # 先复位标志再调度 UI：即使 UI 调度失败，按钮也不会永久卡在「处理中」
        self._model_power_busy = False

        def done():
            self._model_power_busy = False
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            verb = "启动" if action == "load" else "停止"
            if ok:
                self.set_status(f"✅ 已{verb}模型 {mid}")
                self.add_arch(f"已{verb}模型 {mid}" + ("（显存已回收）" if action == "unload" else ""))
            else:
                self.set_status(f"⚠️ {verb} {mid} 失败：{err}")
            self.refresh_model_state()

        self._ui_call(done)

    def set_model_state(self, text, color):
        """更新「正在运行的模型」颜色指示"""
        try:
            self.model_state_var.set(text)
            self.model_state_lb.configure(fg=color)
        except Exception:
            pass

    def refresh_model_state(self):
        """后台查询正在运行的模型（不阻塞 UI）"""
        if self._state_refreshing:
            return
        self._state_refreshing = True

        def work():
            state = running_model()
            self._ui_call(lambda: self._apply_model_state(state))

        threading.Thread(target=work, daemon=True).start()

    def _apply_model_state(self, state):
        self._state_refreshing = False
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        mid, st, reachable = state
        sel = self.model_var.get()
        # ★ 云端 / 通用引擎没有"加载在显存里"这回事 —— 模型名是随每次请求发过去的，
        #   所以措辞用「使用中」，而不是容易误解的「运行中」✗
        try:
            cloud = bool(ENGINE and ENGINE.is_cloud())
        except Exception:
            cloud = False
        # ★ 状态文字要**短** —— 这一行还有模型胶囊 + 启停按钮 + 发送按钮，
        #   原来那句「☁ 使用中：xxx（云端模型，随请求发送）」把整行挤爆了 ✗
        #   （用户反馈："太拥挤太长了"）所以统一用一个短前缀 + 截断的长名字。
        def _short(s, n=20):
            s = str(s or "")
            return s if len(s) <= n else s[:n - 1] + "…"
        if not reachable:
            self.set_model_state("○ 未连接", "#d33")
        elif cloud:
            if sel:
                self.set_model_state(f"☁ 云端：{_short(sel)}", "#1a9c3d")
            else:
                self.set_model_state("○ 未指定模型", "#999999")
        elif st is None:
            self.set_model_state("○ 未加载模型", "#999999")
        elif st == "loading":
            self.set_model_state(f"● 加载中：{_short(mid)}", "#d08a00")
        elif sel and mid != sel:
            # 跑着的不是当前所选 → 下次对话会换模型
            self.set_model_state(f"● {_short(mid)} → {_short(sel)}", "#d08a00")
        else:
            self.set_model_state(f"● 运行中：{_short(mid)}", "#1a9c3d")
        # 同步「启停模型」按钮（RoundedButton：用 set_text / set_outline）
        # ★★ 云端引擎**必须禁用**这个按钮 —— 云端的模型跑在服务商那边，
        #    本机根本没法"卸载"✗（用户实测：点了报"该引擎可能不支持卸载"）
        try:
            if cloud:
                # ★ 云端**直接隐藏**这个按钮 —— 云端的模型跑在服务商那边，
                #   本机根本卸载不了 ✗ 摆一个灰按钮既没用又占地方 ✗
                #   （用户反馈："这个位置太拥挤太长了"）
                try:
                    self.model_power_btn.pack_forget()
                except Exception:
                    pass
            elif self._model_power_busy:
                self.model_power_btn.set_text("⏳ 处理中…", "#999999")
                self.model_power_btn.set_outline("#e3e3e3")
            else:
                # 本地引擎：确保按钮在位（用 before= 保证它回到模型胶囊左边）
                try:
                    if not self.model_power_btn.winfo_ismapped():
                        self.model_power_btn.pack(side="right", padx=(0, 10),
                                                  before=self.model_pill)
                except Exception:
                    pass
                if mid:
                    self.model_power_btn.set_text("■ 停止模型", "#d33")
                    self.model_power_btn.set_outline("#f0b4b4")
                else:
                    self.model_power_btn.set_text("▶ 启动模型", "#2f6fd6")
                    self.model_power_btn.set_outline("#b9d0f5")
        except Exception:
            pass

    def _poll_model_state(self):
        """周期刷新正在运行的模型（颜色指示）"""
        if self._closing:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self.refresh_model_state()
        self._poll_after_id = self.after(MODEL_POLL_MS, self._poll_model_state)

    def _release_model_async(self, old):
        """后台卸载旧模型以释放显存；结束后刷新指示"""
        ok, err = unload_model(old)

        def done():
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            if ok:
                self.set_status(f"✅ 已释放旧模型 {old} · 新模型 {MODEL_ID} 首次对话时加载")
                self.add_arch(f"已释放旧模型 {old}（显存已回收）")
            else:
                # 释放失败不致命：切到新模型时 router 会自动换出
                self.set_status(f"⚠️ 释放 {old} 未成功（{err}）· 切到新模型时会自动换出")
            self.refresh_model_state()

        self._ui_call(done)

    # ---------- 状态 ----------
    def set_status(self, text):
        self.status_var.set(text)

if __name__ == "__main__":
    explicit = any(a == "--data-dir" or a.startswith("--data-dir=") for a in sys.argv[1:])
    instance = _SingleInstance(BASE, default=not explicit)
    try:
        if instance.existing:
            instance.activate_existing()
            sys.exit(0)
        if not os.path.isdir(MEM_DIR):
            os.makedirs(MEM_DIR, exist_ok=True)
        app = ChatApp()
        instance.attach(app)
        app.mainloop()
    finally:
        instance.close()
