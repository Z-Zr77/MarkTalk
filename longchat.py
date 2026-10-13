# -*- coding: utf-8 -*-
"""
本地长对话管理器 v2 · 多记忆 + 随时换模型
========================================
对接本机 llama-server (127.0.0.1:8080)，实现无限长对话：
  1. 正常对话（无感）
  2. 每 N 轮或上下文将满时，自动让模型提炼全部对话
  3. 提炼结果追加写入当前记忆文件（本地 .md）
  4. 清空对话历史，带着记忆继续聊 —— 永不受 8192 限制

命令：
  /save   手动提炼并写入当前记忆文件
  /new    手动开新对话（不清记忆文件）
  /mem    查看当前记忆文件内容
  /use X  切换到记忆文件 X（不存在则新建；切换会开新对话）
  /model  列出模型并切换（切换时**自动释放旧模型**，历史对话保留）
  /free   立刻释放当前模型，把显存还回去
  /exit   退出（**退出时会自动释放模型**）

中断：
  生成过程中按 Ctrl+C 只中断本次回答，**不会退出程序**；
  在提示符 "你 >" 处按 Ctrl+C 才是退出。

用法：先自行启动本地引擎（llama.cpp / FreeToken / Ollama / LM Studio 等），再运行本脚本（程序会自动发现引擎）。
"""
import sys, os, json, datetime, threading, time, shutil, urllib.request, urllib.error, glob

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "http://127.0.0.1:8080/v1/chat/completions"
BASE = os.path.dirname(os.path.abspath(__file__))
# ★★ 记忆分级（与 GUI 保持一致）：memories/cloud/ + memories/private/
#   终端版只连**本地引擎**，所以两个级都能看 ✓（本地读什么都不外泄 ✓）
#   但写入/新建默认落在 private/（安全优先 ✓）
MEM_DIR_BASE = os.path.join(BASE, "memories")
MEM_DIR = os.path.join(MEM_DIR_BASE, "private")   # 当前生效的级（终端版固定 private）


def mem_roots():
    """终端版可见的记忆根：private 优先，其次 cloud"""
    return [os.path.join(MEM_DIR_BASE, "private"),
            os.path.join(MEM_DIR_BASE, "cloud")]
DEFAULT_MEM = "长对话记忆.md"

# 记忆文件里的「来源标记」：终端版写 [终端]，图形版写 [软件]。
# 两个程序共用同一批 memories\*.md，加这个标记就能看出某段是哪儿归档的。
# 标记挂在 "## " 归档标题的尾部，图形版用 startswith("会话归档") 前缀匹配，不受影响。
SRC_TAG = "终端"

MAX_TURNS = 12            # 每 12 轮自动提炼一次
MAX_EST_TOKENS = 5500     # 估算上下文达到 5500 tokens 时提前提炼（留余量）
SUMMARY_PROMPT = (
    "请把以上整个对话提炼成精炼的中文要点摘要。要求：分条列出，保留关键事实、决定、"
    "数字、人名、结论和未完成事项，每条一句话，不要客套话，不要复述寒暄。"
)

current_mem = DEFAULT_MEM   # 当前记忆文件名（全局可变）
MODEL_ID = None             # 当前模型（启动选择，可 /model 切换）

def est_tokens(text):
    """粗略估算 tokens：中文约 1 字 ≈ 1 token，英文按 4 字符 1 token，保守取较大值"""
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = max(0, len(text) - cjk)
    return cjk + other // 4

def mem_path():
    # ★ 跨两级找（终端版两级都能看 ✓）；找不到就落在 private/（新建默认 ✓）
    for root in mem_roots():
        p = os.path.join(root, current_mem)
        if os.path.isfile(p):
            return p
    return os.path.join(MEM_DIR, current_mem)

def list_memories():
    """列出**当前可见**的记忆文件（.md）—— 跨 private/ 和 cloud/ 两级 ✓"""
    names = set()
    for root in mem_roots():
        if os.path.isdir(root):
            names |= {os.path.basename(p)
                      for p in glob.glob(os.path.join(root, "*.md"))}
    return sorted(names)

def get_models():
    """查询 llama-server 当前可用模型列表"""
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []

def api(messages, timeout=600):
    body = json.dumps({
        "messages": messages,
        "model": MODEL_ID,
        "temperature": 0.7,
        "max_tokens": 2048,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip(), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return None, str(e)

def api_stream(messages, on_delta, timeout=600):
    """流式请求：每收到一段就回调 on_delta(str)，实现"边生成边显示"。
    返回 (完整文本, err)。"""
    body = json.dumps({
        "messages": messages,
        "model": MODEL_ID,
        "temperature": 0.7,
        "max_tokens": 2048,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body,
                                 headers={"Content-Type": "application/json"})
    full = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                    delta = j["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                if delta:
                    full.append(delta)
                    on_delta(delta)
        return "".join(full).strip(), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return None, str(e)


# ---------------- 等待回复时的动态提示（转轮 + 读秒） ----------------
_SPIN_UNICODE = "◐◓◑◒"
_SPIN_ASCII = "|/-\\"

def _spin_chars():
    """挑一套当前终端能显示的转轮字符（终端编码不支持 Unicode 时退回 ASCII）"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        _SPIN_UNICODE.encode(enc)
        return _SPIN_UNICODE
    except Exception:
        return _SPIN_ASCII


def _term_cols(default=80):
    """终端可见列数（用来把动态提示裁到一行内）"""
    try:
        return max(20, shutil.get_terminal_size((default, 20)).columns)
    except Exception:
        return default


class WaitHint:
    """在后台线程里刷新「思考中 + 已等待 N 秒」。
    用 \\r 原地覆盖同一行，第一段文字到达或请求结束时清除。
    （终端版原来只 print("AI > ") 然后干等，长时间没有任何反馈）"""

    def __init__(self, label="思考中", interval=0.25):
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._th = None
        self._t0 = 0.0
        self._width = 0        # 上次打印的可见宽度，用于把残字擦干净

    def start(self):
        self._t0 = time.time()
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        return self

    def _loop(self):
        chars = _spin_chars()
        i = 0
        while not self._stop.is_set():
            el = time.time() - self._t0
            txt = f"  {chars[i % len(chars)]} {self.label}… 已等待 {el:.0f} 秒"
            if el >= 8:
                txt += "（首次加载较慢）"
            self._draw(txt)
            i += 1
            self._stop.wait(self.interval)

    def _draw(self, txt):
        try:
            # ★ 必须裁到一行以内。文字超过终端宽度会折成两行，
            #   而 \r 只能回到"当前视觉行"的行首，折出去的那截擦不掉，
            #   收尾清屏后会在界面上留一条残影（实测出现过）。
            cols = _term_cols()
            if len(txt) > cols - 1:
                txt = txt[:max(1, cols - 2)]
            pad = max(0, self._width - len(txt))
            sys.stdout.write("\r" + txt + " " * pad)
            sys.stdout.flush()
            self._width = max(self._width, len(txt))
        except Exception:
            pass

    def stop(self, clear=True):
        self._stop.set()
        if self._th is not None:
            try:
                self._th.join(timeout=1.0)
            except Exception:
                pass
            self._th = None
        if clear and self._width:
            try:
                sys.stdout.write("\r" + " " * (self._width + 2) + "\r")
                sys.stdout.flush()
            except Exception:
                pass
        self._width = 0


def check_server():
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def loaded_model():
    """当前服务端**已加载**（也就是正占着显存的）模型 id；没有则 None"""
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("data", []):
            st = (m.get("status") or {}).get("value")
            if st in ("loaded", "loading"):
                return m.get("id")
    except Exception:
        pass
    return None


def unload_model(mid):
    """让服务端卸载指定模型以释放显存。返回 (ok, err)。
    服务端本来就是 --models-max 1，加载新模型时会自动卸旧的；
    但那是"等下次请求才发生"，这里显式调用可以**立刻**把显存还回来。"""
    if not mid:
        return False, "空模型名"
    try:
        body = json.dumps({"model": mid}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:8080/models/unload", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def free_model(verbose=True):
    """把当前占显存的模型卸掉，并**等它真正释放完**再返回。
    注意：llama.cpp router 的卸载是**异步**的 —— 接口返回 200 之后状态还要过几秒
    才变 unloaded（实测 6300 → 5136 → 778 MiB），所以这里轮询确认，
    免得出现"提示已释放、其实还占着"的假象。"""
    cur = loaded_model()
    if not cur:
        if verbose:
            print("💤 当前没有已加载的模型（显存已空闲）")
        return False
    ok, e = unload_model(cur)
    if not ok:
        if verbose:
            print(f"⚠️ 释放模型 {cur} 失败：{e}")
        return False
    t0 = time.time()
    while time.time() - t0 < 10:          # 最多等 10 秒
        time.sleep(0.5)
        if loaded_model() != cur:
            break
    if verbose:
        if loaded_model() is None:
            print(f"🧹 已释放模型 {cur}，显存已归还")
        else:
            print(f"🧹 已请求释放 {cur}（路由正在异步卸载，几秒后显存就会空出来）")
    return True

def load_memory():
    p = mem_path()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

def append_memory(title, content):
    with open(mem_path(), "a", encoding="utf-8") as f:
        f.write(f"\n## {title} [{SRC_TAG}]\n{content}\n\n---\n")

def do_summary(messages):
    """提炼当前全部对话，写入当前记忆文件，返回新的起始消息"""
    print("\n⏳ 上下文快满了，正在自动提炼并归档，请稍候（可能需要 1-3 分钟）...")
    hist = messages[1:] if messages and messages[0]["role"] == "system" else messages
    if not hist:
        print("（没有可提炼的内容）")
        return messages
    text = "\n".join(f"{m['role']}: {m['content']}" for m in hist)
    try:
        summary, err = api([
            {"role": "system", "content": "你是一个对话归档助手，擅长提炼要点。"},
            {"role": "user", "content": text + "\n\n" + SUMMARY_PROMPT},
        ])
        if err:
            print(f"⚠️ 提炼失败：{err}")
            return messages
    except Exception as e:
        print(f"⚠️ 提炼失败：{e}")
        return messages

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    append_memory(f"会话归档 {current_mem} {now}", summary)
    print(f"✅ 已提炼并写入记忆文件：{mem_path()}")

    mem = load_memory()
    new_messages = [{"role": "system",
                     "content": "以下是此前对话的长期记忆摘要，请基于它继续当前话题：\n" + mem}]
    new_messages.append({"role": "user",
                         "content": "（上一段对话已归档完毕。请基于记忆文件中的摘要继续刚才的话题。）"})
    return new_messages

def build_messages():
    """按当前记忆文件构造起始消息"""
    mem = load_memory()
    system_content = "你是本地部署的中文 AI 助手，请用中文回答。"
    if mem:
        system_content += "\n\n【长期记忆（此前对话摘要，回答相关问题时可参考）】\n" + mem
    return [{"role": "system", "content": system_content}]

def pick_model():
    """让用户选择模型。**切换时显式释放旧模型**，避免两份模型同时占显存。
    返回 model id；失败返回 None"""
    global MODEL_ID
    models = get_models()
    if not models:
        print("\n⚠️ 未能探测到模型列表，当前模型不变。")
        return None
    print("\n可用模型：")
    for i, mid in enumerate(models, 1):
        print(f"  [{i}] {mid}")
    cur_loaded = loaded_model()
    if cur_loaded:
        print(f"  （当前正占显存：{cur_loaded}）")
    choice = input("选择模型（回车不切换）> ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        new = models[int(choice) - 1]
        old = MODEL_ID
        MODEL_ID = new
        print(f"📦 已切换模型：{new}")
        # 显式卸载旧模型：服务端是 --models-max 1（加载新模型时也会自动卸旧的），
        # 但那要等下一次请求才发生；这里立刻释放，显存马上还回来
        if old and old != new:
            ok, e = unload_model(old)
            print(f"   🧹 已释放旧模型 {old}" if ok
                  else f"   ⚠️ 释放旧模型 {old} 失败：{e}")
    return MODEL_ID

def show_help(turns=0):
    """显示可用命令 + 当前状态（忘了命令时输入 /help）"""
    print()
    print("=" * 56)
    print("  可用命令")
    print("=" * 56)
    print("  /help         显示这份帮助")
    print("  /save         把当前对话提炼成摘要，写入记忆文件")
    print("  /new          开新对话（记忆文件保留）")
    print("  /mem          查看当前记忆文件内容")
    print("  /use 名字.md   切换记忆文件（不存在则新建；会开新对话）")
    print("  /model        列出模型并切换（切换时自动释放旧模型）")
    print("  /free         立刻释放当前模型，把显存还回去")
    print("  /exit         退出（会自动释放模型）")
    print("-" * 56)
    print(f"  当前模型 : {MODEL_ID or '（未选）'}")
    print(f"  记忆文件 : {current_mem}")
    print(f"  当前轮数 : {turns} 轮"
          f"（满 {MAX_TURNS} 轮 或 估算超 {MAX_EST_TOKENS} tokens 会自动归档）")
    print("-" * 56)
    print("  直接输入文字      = 跟 AI 聊天")
    print("  生成中按 Ctrl+C   = 只中断本次回答，不退出")
    print("  提示符处 Ctrl+C   = 退出")
    print("=" * 56)
    print()


def main():
    print("=" * 56)
    print("  本地长对话管理器 v2（多记忆 + 随时换模型）")
    print("  对接：http://127.0.0.1:8080（llama-server）")
    print("  ★ 忘记命令？输入 /help 查看全部可用命令")
    print("  常用：/save 归档  /new 新对话  /mem 看记忆")
    print("        /use 名字.md 换记忆  /model 换模型")
    print("        /free 释放显存  /exit 退出（自动释放）")
    print("  中断：生成中按 Ctrl+C 只中断本次回答，不退出")
    print("=" * 56)

    if not check_server():
        print("\n❌ 连不上 llama-server。请先双击「启动.bat」，等提示就绪后再运行本脚本。")
        input("按回车退出...")
        return

    os.makedirs(MEM_DIR, exist_ok=True)   # 确保记忆文件夹存在

    global MODEL_ID, current_mem

    # 第一步：选记忆文件
    mems = list_memories()
    if mems:
        print("\n已有记忆文件：")
        for i, name in enumerate(mems, 1):
            print(f"  [{i}] {name}")
        print("  或者输入一个新名字（如 工作.md）来新建一个记忆")
        c = input(f"选择记忆（回车默认 [{DEFAULT_MEM}]）> ").strip()
        if c.isdigit() and 1 <= int(c) <= len(mems):
            current_mem = mems[int(c) - 1]
        elif c:
            if not c.endswith(".md"):
                c += ".md"
            current_mem = c
        else:
            current_mem = DEFAULT_MEM
    else:
        c = input(f"记忆文件（回车默认 [{DEFAULT_MEM}]，或输入新名字如 工作.md）> ").strip()
        if c:
            current_mem = c if c.endswith(".md") else c + ".md"
        else:
            current_mem = DEFAULT_MEM
    print(f"📄 记忆文件夹：{MEM_DIR}")
    print(f"📄 当前记忆文件：{current_mem}")

    # 第二步：选模型
    pick_model()
    if MODEL_ID is None:
        models = get_models()
        MODEL_ID = models[0] if models else None

    messages = build_messages()
    turns = 0
    print(f"\n✅ 服务已连接 · 当前模型：{MODEL_ID}")
    print(f"   记忆：{current_mem}   输入 /exit 退出")
    print("   生成中按 Ctrl+C 可中断本次回答（不会退出程序）\n")

    while True:
        try:
            user = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（提示符处按 Ctrl+C = 退出）")
            free_model()
            print("再见！记忆已保存在 %s" % mem_path())
            break
        if not user:
            continue
        if user in ("/help", "/?", "/h"):
            show_help(turns)
            continue
        if user == "/exit":
            # 退出时释放模型，把显存还回去（不释放的话 llama-server 会一直占着）
            free_model()
            print("再见！记忆已保存在 %s" % mem_path())
            break
        if user == "/free":
            free_model()
            print()
            continue
        if user == "/mem":
            mem = load_memory()
            print(f"\n--- 当前记忆 [{current_mem}] 内容 ---\n" + (mem if mem else "（空）") + f"\n（文件位置：{mem_path()}）\n-------------------\n")
            continue
        if user == "/new":
            messages = [messages[0]] if messages else []
            turns = 0
            print("已开启新对话（记忆仍保留在文件中）\n")
            continue
        if user == "/save":
            messages = do_summary(messages)
            turns = 0
            continue
        if user == "/model":
            pick_model()
            print("（历史对话保留，后续回复将由新模型生成）\n")
            continue
        if user.startswith("/use"):
            parts = user.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                print("用法：/use 工作.md  （输入要切换的记忆文件名）\n")
                continue
            if not name.endswith(".md"):
                name += ".md"
            current_mem = name
            messages = build_messages()
            turns = 0
            print(f"📄 已切换到记忆：{current_mem}（对话已重置为新话题）\n")
            continue
        if user.startswith("/"):
            # 未知命令：拦下来提示，**不要**当成问题发给模型（既浪费又困惑）
            print(f"⚠️ 未知命令：{user}")
            print("   输入 /help 查看可用命令\n")
            continue

        messages.append({"role": "user", "content": user})
        turns += 1

        # 提示符带上模型名，一眼知道是谁在回答
        print(f"AI[{MODEL_ID}] > ", end="", flush=True)
        # 动态提示：等待时显示「转轮 + 已等待 N 秒」，第一段文字到达就撤掉
        hint = WaitHint().start()
        got = []                      # 已经流式打印出来的片段

        def on_delta(d, _h=hint, _g=got):
            if not _g:
                _h.stop(clear=True)   # 首字到达 → 清掉等待提示
            sys.stdout.write(d)
            sys.stdout.flush()
            _g.append(d)

        stopped = False
        try:
            reply, err = api_stream(messages, on_delta)
        except KeyboardInterrupt:
            # Ctrl+C：**只中断本次生成，不退出程序**（会话与记忆都保留）
            stopped = True
            reply, err = "".join(got), None
        hint.stop(clear=True)
        if err and not got:
            # 流式一个字都没收到 → 退回非流式再试一次（兼容不支持 SSE 的情况）
            hint = WaitHint().start()
            try:
                reply, err = api(messages)
            except KeyboardInterrupt:
                stopped, reply, err = True, None, None
            hint.stop(clear=True)
            if not err and reply:
                sys.stdout.write(reply)
                sys.stdout.flush()
        if stopped:
            print("\n⏹ 已中断本次生成（会话保留，可继续输入；退出请用 /exit）")
            if reply:
                # 已生成的部分保留进上下文，和图形版"手动停止"的语义一致
                messages.append({"role": "assistant", "content": reply})
            continue
        if err:
            print(f"\n⚠️ 调用失败：{err}")
            messages.pop()   # 失败则回滚这条消息
            continue
        print()              # 收尾换行（正文已经流式打出来了）
        messages.append({"role": "assistant", "content": reply})

        # 自动提炼判断：轮数达标 或 上下文估算将满
        hist_tokens = sum(est_tokens(m["content"]) for m in messages[1:])
        if turns >= MAX_TURNS or hist_tokens >= MAX_EST_TOKENS:
            print(f"\n（已聊 {turns} 轮 / 估算 {hist_tokens} tokens）")
            messages = do_summary(messages)
            turns = 0

if __name__ == "__main__":
    main()
