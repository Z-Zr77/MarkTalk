# -*- coding: utf-8 -*-
"""多引擎适配器 —— 让「MarkTalk」同时支持

    · llama.cpp 引擎（默认 8080，router 模式）
    · FreeToken 引擎（默认 8000，OpenAI 兼容）
    · 任意 OpenAI 兼容引擎（自动发现：Ollama / LM Studio / vLLM / text-gen-webui …）
    · 云端 OpenAI 兼容 API（DeepSeek / 豆包方舟 / Qwen 云端 …，需在 config.json 填 key）

设计原则
--------
1. GUI 只依赖 `EngineAdapter` 这个抽象，不直接碰端口 / 私有端点。
2. `LlamaAdapter` 是**原 llama.cpp 逻辑的等价迁移**，行为零变化（回归安全）。
3. `FreeTokenAdapter` 把「切换模型」实现为「重启 ft serve 进程」（它没有热加载接口）。
4. 所有适配器**共用** `caps_from_template()` 与基类的 `chat()/chat_stream()`，
   所以对话协议与推理能力识别逻辑都只有一份。
5. 自动发现是**动态**的：`discover_engines()` 先用 netstat 找出本机**所有在监听**的
   端口，再并发探测哪些是 OpenAI 兼容服务 —— 引擎起在任何端口都能被找到
   （`SCAN_PORTS` 只用于给已知端口起个展示名，**不再是发现的唯一来源**）。
   扫到就用，**不写回 config.json**。

对外只暴露：
    load_config / save_config
    probe / scan_ports / detect_engine / detect_engine_ex
    make_adapter
    LLAMA / FREETOKEN / OPENAI
"""
import json
import contextlib
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

LLAMA = "llama"
FREETOKEN = "freetoken"
OPENAI = "openai"          # 通用 OpenAI 兼容引擎（扫描发现时用）
AUTO = "auto"

DEFAULT_CONFIG = {
    "engine": AUTO,
    "llama": {
        "base_url": "http://127.0.0.1:8080",      # localhost，通用
        "models_dir": "",                          # 空 = 未配置（llama 的路径来自服务端 /v1/models）
    },
    "freetoken": {
        "base_url": "http://127.0.0.1:8000",      # localhost，通用
        "models_dir": "",                          # 空 = 未配置，模型列表为空
        "ft_exe": "",                              # 空 = 未配置，load_model 会明确报"未配置"
        # moe-strategy：offload 省内存（内存小的机器更稳），hybrid 更快但更吃内存
        "serve_args": ["--host", "127.0.0.1", "--port", "8000",
                       "--moe-strategy", "offload", "--mm-encoder-weights", "host"],
        "load_timeout": 300,
    },
    # ---- 云端 OpenAI 兼容 API（DeepSeek / 豆包方舟 / Qwen 云端 …）----
    # 填了 base_url + api_key 且 engine=auto 时，**优先走云端**（想用本地请把 engine 设成
    # "llama"/"freetoken"）。api_key 只存在本地 config.json（已被 .gitignore 排除）。
    # ★ 本机扫到的「通用 OpenAI 兼容引擎」（Ollama / LM Studio …）。
    #   单独一段，**绝不能写进 openai 段** —— 那会把用户配好的云端地址覆盖掉 ✗
    #   （真出过这个事故：云端 base_url 被冲成了 127.0.0.1:xxxxx）
    "local_openai": {
        "base_url": ""
    },
    # 本地控制接口（C · 让 agent 能操控 MarkTalk）
    #   · 只绑 127.0.0.1 ✓ token 存 control_token.dat（DPAPI 加密，已 gitignore）✓
    #   · 想关掉就设 "enabled": false ✓
    "control": {
        "enabled": True,
        "port": 8765
    },
    "openai": {
        "base_url": "",        # ★ 填**不含 /v1** 的地址，例如 "https://api.deepseek.com"
        "api_key": "",         # API Key（本地明文；别把 config.json 发给别人）
        "model": "",           # 可选：固定模型名；空则尝试 /v1/models
                               #（豆包方舟的 /v1/models 常不可用，这里必须填）
        "ctx": 0,              # 可选：上下文上限；0/空 = 自动探测，探测不到用 GUI 默认 8192
    },
}


# 自动发现要扫的端口：(引擎名, 端口, base_url)
# ★ 端口和引擎**不是一一对应**（8000 既是 FreeToken 也是 vLLM；5000 既是 text-gen-webui
#   也是 TabbyAPI）。所以扫描只认「这个端口上有没有活的 OpenAI 兼容服务」，
#   引擎名仅用于状态栏展示标注，对话能力全部走标准 OpenAI 协议。
#   顺序 = 优先级（扫到多个时取靠前的那个）。
SCAN_PORTS = [
    ("llama",     8080, "http://127.0.0.1:8080"),
    ("freetoken", 8000, "http://127.0.0.1:8000"),
    ("ollama",    11434, "http://127.0.0.1:11434"),
    ("lmstudio",  1234, "http://127.0.0.1:1234"),
    ("vllm",      8000, "http://127.0.0.1:8000"),      # vLLM 常见 8000
    ("textgen",   5000, "http://127.0.0.1:5000"),
    ("tabbyapi",  5000, "http://127.0.0.1:5000"),      # TabbyAPI 常见 5000
]


# ---------------------------------------------------------------- 配置

def load_config(path):
    """读 config.json；不存在/损坏则返回默认值（并补全缺失字段）。

    ★ 云端密钥解析顺序（**加密优先**）：
        1. api_key.dat 里 DPAPI 解出来的（推荐，也是设置脚本写入的位置）
        2. config.json 里的明文 "api_key"（仅为兼容手填；会有泄露风险，会给出警告）
      解出来的密钥只放在内存的 cfg 里，**绝不写回 config.json**（save_config 会强制清空）。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))       # 深拷贝
    plaintext_used = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        if str((user.get("openai") or {}).get("api_key") or "").strip():
            plaintext_used = True
    except Exception:
        pass
    # ── 提供商：旧配置自动迁移 ──
    if not isinstance(cfg.get("providers"), list):
        cfg["providers"] = []
    if not providers_of(cfg):
        oa0 = cfg.get("openai") or {}
        if str(oa0.get("base_url") or "").strip():
            p = new_provider("默认云端", oa0.get("base_url"),
                             oa0.get("model"), oa0.get("ctx"))
            cfg["providers"] = [p]
            cfg["active_provider"] = p["id"]
    act = active_provider(cfg)

    # ── 合成 openai 段（★ 关键：下游适配器代码完全不用改）──
    keymap = load_api_key_map(path)
    cfg.setdefault("openai", {})
    if act is not None:
        cfg["openai"]["base_url"] = act.get("base_url") or ""
        cfg["openai"]["model"] = act.get("model") or ""
        try:
            cfg["openai"]["ctx"] = int(act.get("ctx") or 0)
        except Exception:
            cfg["openai"]["ctx"] = 0
        cfg["openai"]["api_key"] = (keymap.get(act.get("id")) or "").strip()
        cfg["_active_provider_name"] = act.get("name") or ""
    else:
        # 没有提供商 → 沿用旧式单个密钥（加密的那份优先）
        key = load_api_key(path)
        if key:
            cfg["openai"]["api_key"] = key
    cfg["_api_key_plaintext_in_config"] = plaintext_used     # 供 GUI 提示"建议改用加密"
    return cfg


def save_config(path, cfg):
    """写 config.json。

    ★ 双保险：**永远不把 openai.api_key 写进文件**（置空后再写）。
      哪怕调用方传进来的 cfg 里带着解密后的明文，也不会落盘。
    """
    try:
        out = json.loads(json.dumps(cfg or {}))
        for k in list(out.keys()):
            if k.startswith("_"):                # 内部字段（如 _api_key_plaintext_in_config）
                out.pop(k, None)
        if isinstance(out.get("openai"), dict):
            out["openai"]["api_key"] = ""
        # 提供商列表里的密钥字段也绝不落盘
        for p in (out.get("providers") or []):
            if isinstance(p, dict):
                p.pop("api_key", None)
        # ★ 原子写：先写临时文件再替换 —— 中途崩溃也不会把原 config.json 写坏
        #   （用户以前出过一次 config.json 被覆盖的事故，这里做双保险）
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 能力识别（两引擎共用）

def caps_from_template(tpl):
    """从对话模板文本推断推理能力。GGUF 与 HF 的 chat 模板格式一致，共用这一份逻辑。
       返回 {"thinking": bool, "efforts": [...], "default": 默认强度 or None}"""
    caps = {"thinking": False, "efforts": [], "default": None}
    if not tpl:
        return caps
    if "enable_thinking" in tpl:
        caps["thinking"] = True
    mdef = re.search(r"reasoning_effort\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]", tpl)
    if mdef:
        caps["default"] = mdef.group(1)
    if "reasoning_effort" in tpl:
        m = re.search(r"reasoning_effort[\s\S]{0,200}?not\s+in\s*\(([^)]*)\)", tpl)
        if m:
            raw = m.group(1)
            caps["efforts"] = [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", raw)]
    return caps


def _read_gguf_chat_template(path, max_mb=200):
    """分块扫描 GGUF，定位 tokenizer.chat_template（找不到返回 None）"""
    if not path or not os.path.isfile(path):
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


def _read_hf_chat_template(model_dir):
    """从 HF 目录读 chat 模板：优先 chat_template.jinja，其次 config.json 的 chat_template"""
    if not model_dir or not os.path.isdir(model_dir):
        return None
    p = os.path.join(model_dir, "chat_template.jinja")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            pass
    try:
        with open(os.path.join(model_dir, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        tpl = cfg.get("chat_template")
        if isinstance(tpl, list):          # 有些模型给的是列表
            tpl = "\n".join(str(x) for x in tpl)
        return tpl or None
    except Exception:
        return None


# ---------------------------------------------------------------- 云端密钥安全存储
# ★ 设计要点（用户明确要求"密钥一定要加密保存好、不要泄露"）：
#   · 用 Windows 自带的 **DPAPI**（CryptProtectData）—— 真加密，且**零新依赖**
#     （只用 ctypes 调 crypt32.dll，不引入 cryptography/keyring 等第三方包）
#   · 密文绑**当前 Windows 用户 + 本机**：别人拿到文件解不开，拷到别的电脑也解不开
#   · 密钥**单独存 api_key.dat**，不放 config.json —— config.json 用户经常发给别人求助，
#     密文不在里面就从根本上不会跟着泄露
#   · config.json 里那个 "api_key" 字段**永远不写**（save_config 会强制清空，双保险）
#   · 界面/日志里只显示掩码（如 sk-…1a2b），绝不回显完整密钥

KEY_FILE_NAME = "api_key.dat"

# 密钥表里的特殊键
LEGACY_KEY_ID = "__legacy__"      # 旧格式（单个密钥字符串）迁移过来的
ACTIVE_KEY = "__active__"         # 未指定提供商时的兜底


def new_provider(name="", base_url="", model="", ctx=0):
    """造一个提供商配置。id 用短随机串（稳定、可作为密钥表的键）"""
    import uuid
    return {"id": "p" + uuid.uuid4().hex[:8],
            "name": (name or "").strip() or "未命名",
            "base_url": (base_url or "").strip(),
            "model": (model or "").strip(),
            "ctx": int(ctx or 0),
            "enabled": True}


def providers_of(cfg):
    """取提供商列表（一定是 list）"""
    ps = (cfg or {}).get("providers")
    return [p for p in ps if isinstance(p, dict)] if isinstance(ps, list) else []


def active_provider(cfg):
    """取当前生效的提供商 dict（没有就返回 None）"""
    ps = providers_of(cfg)
    if not ps:
        return None
    pid = str((cfg or {}).get("active_provider") or "")
    for p in ps:
        if p.get("id") == pid:
            return p
    return ps[0]

try:
    import ctypes
    from ctypes import wintypes as _wt

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", _wt.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi(protect, data):
        """protect=True 加密 / False 解密。失败抛 OSError。"""
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(bytes(data), len(data) + 1)
        blob_in = _DATA_BLOB(len(data),
                             ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if protect:
            ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
                    ctypes.byref(blob_out))
        else:
            ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
                    ctypes.byref(blob_out))
        if not ok:
            raise OSError(f"DPAPI {'加密' if protect else '解密'}失败"
                          f"（错误码 {ctypes.get_last_error() or kernel32.GetLastError()}）")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            try:
                kernel32.LocalFree(blob_out.pbData)
            except Exception:
                pass

    def dpapi_encrypt(s):
        """str → DPAPI 密文 bytes（绑当前用户 + 本机）"""
        return _dpapi(True, s.encode("utf-8"))

    def dpapi_decrypt(blob):
        """DPAPI 密文 bytes → str；解不开（换用户/换机器/文件损坏）抛 OSError"""
        return _dpapi(False, blob).decode("utf-8", "replace")

    DPAPI_OK = True
except Exception:                                   # 非 Windows / ctypes 不可用
    DPAPI_OK = False

    def dpapi_encrypt(s):
        raise OSError("当前系统不支持 DPAPI 加密（仅 Windows）")

    def dpapi_decrypt(blob):
        raise OSError("当前系统不支持 DPAPI 解密（仅 Windows）")


def key_file_for(config_path):
    """密钥文件与 config.json 同目录（都被 .gitignore 排除）"""
    d = os.path.dirname(os.path.abspath(config_path)) if config_path else "."
    return os.path.join(d, KEY_FILE_NAME)


def _write_key_blob(config_path, text):
    """把明文（JSON 或旧式单串）DPAPI 加密写进 api_key.dat"""
    if not DPAPI_OK:
        return False, "当前系统不支持 DPAPI（本程序仅支持 Windows 加密存储）"
    try:
        blob = dpapi_encrypt(text)
        p = key_file_for(config_path)
        with open(p, "wb") as f:
            f.write(blob)
        try:                                        # 尽量收紧权限（失败不影响功能）
            os.chmod(p, 0o600)
        except Exception:
            pass
        return True, None
    except Exception as e:
        return False, str(e)


def load_api_key_map(config_path):
    """读回**所有**提供商的密钥：{provider_id: key}

    ★ 兼容旧格式：以前 api_key.dat 里只存**一个密钥字符串**（单个云端时代）。
      先试 JSON 字典；不是 JSON 就当成"唯一那个密钥"，归到 LEGACY_KEY_ID 下。
    """
    p = key_file_for(config_path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "rb") as f:
            raw = (dpapi_decrypt(f.read()) or "").strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k): str(v or "") for k, v in d.items()}
    except Exception:
        pass
    return {LEGACY_KEY_ID: raw}


def save_api_key_map(config_path, keymap):
    """整表加密写入。返回 (ok, err)"""
    try:
        return _write_key_blob(config_path,
                               json.dumps(keymap or {}, ensure_ascii=False))
    except Exception as e:
        return False, str(e)


def save_api_key(config_path, key, provider_id=None):
    """保存某个提供商的密钥（provider_id=None 时写"活动提供商"/旧的唯一密钥）。

    返回 (ok, err)
    """
    key = (key or "").strip()
    if not key:
        return False, "空密钥"
    m = load_api_key_map(config_path)
    if provider_id:
        m[provider_id] = key
    else:
        m[LEGACY_KEY_ID] = key
    ok, err = save_api_key_map(config_path, m)
    if not ok:
        return False, err
    # 顺手把旧的 openai.api_key（若有）清掉，避免明文残留
    return True, None


def load_api_key(config_path, provider_id=None):
    """读回某个提供商的密钥；没有/解不开返回空串（绝不抛异常打断启动）"""
    m = load_api_key_map(config_path)
    if provider_id:
        return (m.get(provider_id) or "").strip()
    if ACTIVE_KEY in m:
        return (m.get(ACTIVE_KEY) or "").strip()
    if LEGACY_KEY_ID in m:
        return (m.get(LEGACY_KEY_ID) or "").strip()
    if len(m) == 1:
        return list(m.values())[0].strip()
    return ""


def mask_key(key):
    """只用于显示：sk-abcdef…1234 → "sk-a…1234"。绝不返回完整密钥。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "…" + k[-2:] if len(k) > 2 else "…"
    return k[:4] + "…" + k[-4:]


# ---------------------------------------------------------------- HTTP 小工具

def _post_json(url, payload, timeout, headers=None):
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url, timeout, headers=None):
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _kill_port(port):
    """结束监听指定端口的进程（FreeToken 没有卸载接口，只能杀进程）。
    返回被杀的 PID 列表。"""
    pids = []
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return pids
    key = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") \
                and parts[1].endswith(key) and parts[3].upper() == "LISTENING":
            pid = parts[4]
            if pid.isdigit() and pid not in pids:
                pids.append(pid)
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    return pids


# ---------------------------------------------------------------- 基类

# ==================== 落盘日志（排查用）====================
# ★★ 隐私红线（本项目自己定的规矩）：
#   ✗ **绝不记录对话正文**（用户说了什么 / 模型答了什么）
#   ✓ 只记**元数据**：角色序列 / 条数 / 字符数 / token 估算 / 模型名 / 端点
#   ✓ 错误时记**完整错误体**（截断到 LOG_ERR_CAP）—— 这是排查的关键 ✓
#     真机教训：图一那个 500，屏幕上只显示 200 字符（"…first %)"）→ 查了 20 分钟 ✗
#     而完整错误明明写着 `Jinja Exception: System message must be at the beginning.` ✓
#   ✓ 日志放 logs/，已 gitignore ✓ 按天清理（只删自己目录里匹配的 ✓ 不用通配符 ✓）
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "marktalk.log"
LOG_KEEP_DAYS = 7
LOG_ERR_CAP = 2000          # 错误体最多记这么多字符


def log_dir():
    """日志目录（跟程序同级的 logs/ ✓ 打包成 exe 后是 exe 旁边 ✓）"""
    base = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    return os.path.join(base, LOG_DIR_NAME)


def log(msg, level="I"):
    """写一行日志。★ 整个函数被 try 包住 —— 日志失败**绝不能**影响主流程 ✓"""
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(d, LOG_FILE_NAME), "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {msg}\n")
    except Exception:
        pass


def log_trim(days=LOG_KEEP_DAYS):
    """按天滚动：删掉 logs/ 里超过 N 天的**归档日志**（只删本程序自己生成的 ✓）"""
    try:
        import glob as _glob
        d = log_dir()
        if not os.path.isdir(d):
            return
        cutoff = time.time() - days * 86400
        for p in _glob.glob(os.path.join(d, "marktalk*.log*")):
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def brief_messages(messages):
    """请求摘要 —— ★ 只有元数据，没有正文 ✓（这是排查"角色序列"的关键）"""
    try:
        roles = [m.get("role", "?") for m in (messages or [])]
        total = 0
        for m in (messages or []):
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                total += sum(len(str(s)) for s in c)
        sys_cnt = roles.count("system")
        flag = "  ★★ 有 %d 条 system（有的模型模板会因此 500）" % sys_cnt if sys_cnt > 1 else ""
        return f"messages={len(roles)} roles={roles} chars={total}{flag}"
    except Exception:
        return "messages=?"


def log_error(where, code, body):
    """记**完整**错误体（排查的关键 ✓ —— 屏幕上只显示截断版）"""
    try:
        b = (body or "")
        if len(b) > LOG_ERR_CAP:
            b = b[:LOG_ERR_CAP] + f"…（截断，共 {len(body)} 字符）"
        log(f"{where} → HTTP {code}  响应体：\n{b}", "E")
    except Exception:
        pass


def normalize_system_messages(messages):
    """把所有 system 消息**合并到第一条** ✗→✓，并保证 system 只出现在最前面。

    ★ 为什么必须做（真机踩到）：
      有的模型 chat template 强制要求 system 只能在最前面，模板里直接
      `raise_exception('System message must be at the beginning.')`
      → 服务端返回 **HTTP 500**，而报错信息只给个模板行号，极难定位 ✓
      （实测：Qwen3.8-27B-Uncensored 模板第 106 行就是这个 ✓
        触发条件：MarkTalk 的「归档检索回忆」原来会注入第 2 条 system ✗）

    ★ 为什么放在适配层：
      这样**任何调用方**（GUI 聊天 / 流式 / 摘要 / 终端版 / 控制接口）
      都不可能再发出"杂散 system" ✗ → 治一整类 bug ✓ 而不是补一个点 ✓

    合并后语义不变（system 本来就是给模型的指令，合起来等价 ✓）
    """
    if not messages:
        return messages
    systems, rest = [], []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            systems.append(c if isinstance(c, str) else str(c or ""))
        else:
            rest.append(m)
    if len(systems) <= 1:
        return messages                      # 本来就正常 → 原样返回（不复制 ✓）
    head = "\n\n".join(s for s in systems if s)
    # ★ 记一笔：**有杂散 system 被合并了** ✓
    #   这条日志本身就是排查线索 —— 一眼看出"是谁注入了第 2 条 system"✓
    #   （用户图一那个 500，如果有这条日志，10 秒就能定位 ✗→✓）
    try:
        log("归一化：合并了 %d 条 system → 1 条  原始 roles=%s"
            % (len(systems), [m.get("role", "?") for m in messages]), "W")
    except Exception:
        pass
    return [{"role": "system", "content": head}] + rest


class EngineAdapter:
    """统一接口。GUI 只依赖这些方法。"""
    kind = "?"

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.base_url = str(self.cfg.get("base_url") or "").rstrip("/")
        # 云端 API 用；本地引擎留空 → 不发 Authorization，行为与以前完全一致
        self.api_key = str(self.cfg.get("api_key") or "").strip()

    def _headers(self):
        """请求头。api_key 为空时只带 Content-Type（本地引擎不受任何影响）。"""
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def is_cloud(self):
        """当前连的是不是**云端**（用于状态栏「☁ 云端」标记与隐私提示）。

        判据：填了 api_key，或地址不是回环地址 —— 两者任一成立即视为云端。
        """
        if self.api_key:
            return True
        u = (self.base_url or "").lower()
        if not u:
            return False
        return not any(h in u for h in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))

    # --- 必须实现 ---
    def health(self):
        raise NotImplementedError

    def fetch_models(self, timeout=6):
        raise NotImplementedError

    def load_model(self, mid, timeout=300):
        raise NotImplementedError

    def unload_model(self, mid):
        raise NotImplementedError

    def scan_reasoning(self, path):
        raise NotImplementedError

    def chat(self, messages, model_id, kwargs, timeout=900, stop_event=None):
        raise NotImplementedError

    def chat_stream(self, messages, model_id, kwargs, stop_event, on_delta, timeout=900):
        raise NotImplementedError

    # --- 通用实现（基于 fetch_models / 自身端点）---
    def model_status(self):
        """[(id, status), ...]；服务不可达返回 None（区别于「可达但没有模型」的 []）"""
        info = self.fetch_models(timeout=3)
        if not info.get("ok"):
            return None
        return [(i, "loaded" if i == info.get("running") else "unloaded")
                for i in info.get("ids") or []]

    def running_model(self):
        """(id, status, 服务可达)"""
        info = self.fetch_models(timeout=3)
        if not info.get("ok"):
            return None, None, False
        return info.get("running"), info.get("running_state"), True

    def ctx_limit(self, mid=None):
        return None

    def label(self):
        """状态栏显示用"""
        return f"{self.kind} {self.base_url}"

    # --- 请求体 ---
    def _body(self, messages, model_id, kwargs, stream):
        # ★ 兜底归一化：system 只允许出现在最前面 ✗
        #   （有的模型模板会因"system 不在首位"直接 500 ✓ 见函数注释）
        messages = normalize_system_messages(messages)
        body = {
            "messages": messages,
            "model": model_id,
            "temperature": 0.7,
            "max_tokens": 2048,
            "stream": stream,
        }
        if kwargs:
            body["chat_template_kwargs"] = dict(kwargs)
        return body

    def _chat_url(self):
        """拼出 /chat/completions 的完整 URL。

        ★ 2026-10-10 修复：智能识别版本段，不再硬拼 /v1 ✗
          问题：原来固定 return base_url + "/v1/chat/completions"
                豆包方舟 base_url = .../api/v3 → 拼出 .../api/v3/v1/... → 404 ✗
          修法：base_url 已含版本段（结尾是 /v1 或 /api/v3 等）→ 只拼 /chat/completions
                否则照旧拼 /v1/chat/completions（向后兼容 ✓ 老配置不用改 ✓）
          正则结尾锚定 $：/(v\d+|api/v\d+)$  只在 URL 末尾才算命中 ✓
        """
        base = self.base_url.rstrip("/")
        if re.search(r"/(v\d+|api/v\d+)$", base):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    # --- OpenAI 标准的对话实现（所有兼容引擎共用这一份）---
    # 说明：原先 LlamaAdapter / FreeTokenAdapter 各自抄了一份**逐字相同**的实现，
    #       这里上移到基类去重。行为完全一致（回归安全）。
    def chat(self, messages, model_id, kwargs, timeout=900, stop_event=None):
        body = json.dumps(self._body(messages, model_id, kwargs, False)).encode("utf-8")
        req = urllib.request.Request(self._chat_url(), data=body,
                                     headers=self._headers())
        log(f"POST {self._chat_url()}  model={model_id!r}  {brief_messages(messages)}")
        try:
            with _interruptible_response(req, timeout, stop_event) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return (content or "").strip(), None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            log_error("chat", e.code, raw)          # ★ 日志里存**全量** ✓
            return None, f"HTTP {e.code}: {raw[:200]}"   # 屏幕上仍短提示 ✓
        except Exception as e:
            if stop_event is not None and stop_event.is_set():
                return None, "已手动停止"
            log(f"chat 异常：{type(e).__name__}: {e}", "E")
            return None, str(e)

    def chat_stream(self, messages, model_id, kwargs, stop_event, on_delta, timeout=900):
        return _stream_impl(self._chat_url(), self._body(messages, model_id, kwargs, True),
                            stop_event, on_delta, timeout, headers=self._headers())


# ---------------------------------------------------------------- llama.cpp

class LlamaAdapter(EngineAdapter):
    """原 llama.cpp 逻辑的等价迁移 —— 行为必须与改造前完全一致。"""
    kind = LLAMA

    def health(self):
        try:
            req = urllib.request.Request(self.base_url + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def fetch_models(self, timeout=6):
        """一次请求 /v1/models 拿全：模型列表 + GGUF 路径 + 上下文上限 + 正在运行的模型"""
        info = {"ids": [], "paths": {}, "ctx": None, "running": None,
                "running_state": None, "ok": False}
        try:
            data = _get_json(self.base_url + "/v1/models", timeout)
        except Exception:
            return info
        info["ok"] = True
        ctxs = []
        for m in data.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            info["ids"].append(mid)
            args = (m.get("status") or {}).get("args") or []
            for i, a in enumerate(args):
                if a == "--model" and i + 1 < len(args):
                    info["paths"][mid] = args[i + 1]
            st = (m.get("status") or {}).get("value") or "unknown"
            if st in ("loaded", "loading") and info["running"] is None:
                info["running"], info["running_state"] = mid, st
            n = (m.get("meta") or {}).get("n_ctx")
            if n:
                ctxs.append(n)
        if ctxs:
            info["ctx"] = min(ctxs)
        return info

    def load_model(self, mid, timeout=300):
        if not mid:
            return False, "空模型名"
        try:
            _post_json(self.base_url + "/models/load", {"model": mid}, timeout)
            return True, None
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    def unload_model(self, mid):
        if not mid:
            return False, "空模型名"
        try:
            _post_json(self.base_url + "/models/unload", {"model": mid}, 30)
            return True, None
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    def scan_reasoning(self, path):
        return caps_from_template(_read_gguf_chat_template(path))

    def ctx_limit(self, mid=None):
        return self.fetch_models(timeout=4).get("ctx")


# ---------------------------------------------------------------- FreeToken

class FreeTokenAdapter(EngineAdapter):
    """FreeToken（OpenAI 兼容）。模型切换 = 重启 ft serve 进程。"""
    kind = FREETOKEN

    def __init__(self, cfg):
        super().__init__(cfg)
        self.models_dir = self.cfg.get("models_dir") or ""
        self.ft_exe = self.cfg.get("ft_exe") or ""
        self.serve_args = list(self.cfg.get("serve_args") or [])
        self.load_timeout = int(self.cfg.get("load_timeout") or 300)
        self.port = self._port()

    def _port(self):
        try:
            return int(self.base_url.rsplit(":", 1)[1].split("/")[0])
        except Exception:
            return 8000

    # --- 模型目录扫描（HF 格式：子目录里含 config.json）---
    def local_models(self):
        out = {}
        if not self.models_dir or not os.path.isdir(self.models_dir):
            return out
        for name in sorted(os.listdir(self.models_dir)):
            d = os.path.join(self.models_dir, name)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "config.json")):
                out[name] = d
        return out

    def health(self):
        """FreeToken 没有 /health，用 /v1/models 代替"""
        try:
            _get_json(self.base_url + "/v1/models", 2)
            return True
        except Exception:
            return False

    def _loaded_id(self, timeout=2):
        try:
            data = _get_json(self.base_url + "/v1/models", timeout)
        except Exception:
            return None
        for m in data.get("data", []):
            if m.get("id"):
                return m["id"]
        return None

    def fetch_models(self, timeout=6):
        """ids 来自本地目录扫描（FreeToken 的 /v1/models 只返回**已加载**的那个），
        running 来自 /v1/models，ctx 读 config.json。"""
        info = {"ids": [], "paths": {}, "ctx": None, "running": None,
                "running_state": None, "ok": False}
        local = self.local_models()
        if not local:
            # 目录没配好：退化为只看服务端返回了什么
            mid = self._loaded_id(timeout)
            if mid is None:
                return info
            info.update(ok=True, ids=[mid], running=mid, running_state="loaded")
            return info
        info["ok"] = True
        info["ids"] = list(local.keys())
        info["paths"] = dict(local)
        loaded = self._loaded_id(timeout)
        if loaded:
            info["running"] = loaded
            info["running_state"] = "loaded"
        # 上下文上限：优先取正在跑的模型，否则取任意一个
        for mid in ([loaded] if loaded else []) + list(local.keys()):
            n = self._ctx_from_config(local.get(mid))
            if n:
                info["ctx"] = n
                break
        return info

    def _ctx_from_config(self, model_dir):
        """读模型上下文上限。
        注意：多模态/新版 transformers 的 config.json 常把长度放在嵌套的
        `text_config` 里（实测 Qwen3.6-35B-A3B 是 text_config.max_position_embeddings
        = 262144），所以顶层找不到时要往里层找。"""
        if not model_dir:
            return None
        try:
            with open(os.path.join(model_dir, "config.json"), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return None
        scopes = [cfg]
        for key in ("text_config", "llm_config", "language_config", "decoder"):
            sub = cfg.get(key)
            if isinstance(sub, dict):
                scopes.append(sub)
        for scope in scopes:
            for k in ("max_position_embeddings", "max_seq_len", "seq_length",
                      "n_positions", "model_max_length", "max_context_length"):
                v = scope.get(k)
                if isinstance(v, int) and v > 0:
                    return v
        return None

    def ctx_limit(self, mid=None):
        info = self.fetch_models(timeout=3)
        return info.get("ctx")

    def scan_reasoning(self, path):
        """path 是 HF 模型**目录**"""
        return caps_from_template(_read_hf_chat_template(path))

    def unload_model(self, mid=None):
        """杀掉监听本端口的进程（不碰桌面版：桌面版 API 在 1919）"""
        pids = _kill_port(self.port)
        return True, None

    def load_model(self, mid, timeout=None):
        """切模型 = 先停旧进程，再起新进程，然后轮询直到服务可用"""
        if not mid:
            return False, "空模型名"
        local = self.local_models()
        d = local.get(mid)
        if not d:
            # models_dir 未配置时 local_models() 返回 {}，也会走到这里
            return False, (f'FreeToken model "{mid}" not found. '
                           f'Check "freetoken.models_dir" in config.json.')
        if not self.ft_exe or not os.path.isfile(self.ft_exe):
            return False, 'FreeToken engine not configured. Set "freetoken.ft_exe" in config.json.'
        # ① 停掉旧进程
        _kill_port(self.port)
        time.sleep(1.5)
        # ② 起新进程
        args = [self.ft_exe, "serve", "--model-path", d] + self.serve_args \
               + ["--served-model-name", mid]
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(args, creationflags=flags,
                             cwd=os.path.dirname(self.ft_exe))
        except Exception as e:
            return False, f"启动 ft serve 失败：{e}"
        # ③ 轮询直到可用
        wait = int(timeout or self.load_timeout)
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(2)
            if self.health():
                return True, None
        return False, f"等待服务超时（{wait} 秒）"


# ---------------------------------------------------------------- 通用 OpenAI 兼容引擎

class OpenAIAdapter(EngineAdapter):
    """任意 OpenAI 兼容引擎（Ollama / LM Studio / vLLM / text-gen-webui / TabbyAPI …）。

    只依赖标准 OpenAI 协议，所以**对话能力与 llama/FreeToken 完全一致**；
    引擎专属能力（GGUF 扫描、重启进程、模型目录）一律不做 —— 那不是通用协议的一部分。
    热切换模型「尽力而为」：多数这类引擎不支持 POST /v1/models/load，失败就明确告知，
    绝不抛异常（GUI 会把它显示成一句提示）。

    ★ 这个类同时服务两种场景，靠「配置里有没有 api_key / model / ctx」区分：
      · **云端 API**（DeepSeek / 豆包 / Qwen 云端 …）：带 Authorization、不发
        llama.cpp 专属的 chat_template_kwargs、模型名与上下文可由配置直接指定。
      · **扫描发现的本地引擎**（Ollama / LM Studio …）：make_adapter 会传一个
        **干净的 cfg**（只有 base_url），所以不会继承云端的 key/model/ctx。
    """
    kind = OPENAI

    def __init__(self, cfg):
        super().__init__(cfg)
        self.model_name = str(self.cfg.get("model") or "").strip()
        try:
            self.cfg_ctx = int(self.cfg.get("ctx") or 0)
        except Exception:
            self.cfg_ctx = 0

    def _body(self, messages, model_id, kwargs, stream):
        """覆盖基类：**不发 chat_template_kwargs**，改发 OpenAI 标准的 `reasoning_effort`。

        那个 `chat_template_kwargs` 是 llama.cpp 专属的推理参数载体 ✗ 云端不认识它
        （轻则忽略，重则 400）✗

        ★ 但**不能把推理强度整个丢掉** —— 那用户就完全没法调思考强度了 ✗
        （用户实测："为什么无法扫描显示推理强度？"）
        OpenAI 协议本身就有 **`reasoning_effort`** 字段（o 系列 / DeepSeek / Qwen 云端都认），
        所以这里把 GUI 选的强度映射过去。`enable_thinking` 之类模板参数云端不认，丢掉。
        model 为空时回退到配置里的 model（豆包方舟这类 /v1/models 不可用的服务靠它）。
        """
        # ★ 兜底归一化：system 只允许出现在最前面 ✗
        #   （有的模型模板会因"system 不在首位"直接 500 ✓ 见函数注释）
        messages = normalize_system_messages(messages)
        body = {
            "messages": messages,
            "model": model_id or self.model_name,
            "temperature": 0.7,
            "max_tokens": 2048,
            "stream": stream,
        }
        kw = dict(kwargs or {})
        effort = kw.get("reasoning_effort")
        if effort:
            body["reasoning_effort"] = effort
        return body

    def health(self):
        """★ 不能只看 `/v1/models` 能不能 200 —— **很多云端 API 根本不实现它**。

        实测：小米等云端对 /v1/models 返回 404 ✗ 用原来的写法会被误判成
        "服务未连接"，于是 GUI 显示不出引擎、模型也切换不了（用户报的就是这个现象）。

        判定：
          · 连上了（任何 HTTP 响应，除 401/403）→ **算正常** ✓
            —— 云端常见 404/405（没实现 /v1/models），服务本身是好的
          · **401 / 403** → 服务在，但 **Key 不对** ✗ 如实报错（否则用户白折腾）
          · 连不上（超时 / 拒绝 / DNS）→ ✗
        """
        code = _http_status(self.base_url + "/v1/models", 3, headers=self._headers())
        if code is None:
            return False
        return code not in (401, 403)

    def fetch_models(self, timeout=6):
        """ids 来自 /v1/models；paths 留空（没有 GGUF 路径可解析）。

        ★ 三种兜底（都是云端才需要的）：
          1. `/v1/models` 不可用或返回空 + 配置里填了 `model` → 返回 `ids=[model]`，不崩
             （豆包方舟的 /v1/models 常不可用，必须靠这一条）
          2. `ctx` 优先用配置里的 `ctx`（用户显式指定最可信）
          3. 否则从 `meta.n_ctx` / `meta.context_length` 取；都拿不到返回 None
             （GUI 有 8192 兜底）
        注意 GUI 读的是**这个函数的 "ctx" 字段**（不是 ctx_limit()），所以配置值必须写在这里。
        """
        info = {"ids": [], "paths": {}, "ctx": None, "running": None,
                "running_state": None, "ok": False}
        try:
            data = _get_json(self.base_url + "/v1/models", timeout,
                             headers=self._headers())
        except Exception:
            data = None
        if data is None or not (data.get("data") or []):
            # 回退：用配置里写死的模型名（云端常见）
            if self.model_name:
                info.update(ok=True, ids=[self.model_name],
                            running=self.model_name, running_state="loaded")
                if self.cfg_ctx > 0:
                    info["ctx"] = self.cfg_ctx
            return info
        info["ok"] = True
        ctxs = []
        for m in data.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            info["ids"].append(mid)
            st = (m.get("status") or {}).get("value")
            if st in ("loaded", "loading") and info["running"] is None:
                info["running"], info["running_state"] = mid, st
            n = (m.get("meta") or {}).get("n_ctx")                 or (m.get("meta") or {}).get("context_length")
            if isinstance(n, int) and n > 0:
                ctxs.append(n)
        # 只有一个模型时，就认为它是当前在用的（Ollama / LM Studio 常见形态）
        if info["running"] is None and len(info["ids"]) == 1:
            info["running"], info["running_state"] = info["ids"][0], "loaded"
        # 配置里显式写了 ctx 就用配置的（最可信），否则用探测到的最小值
        info["ctx"] = self.cfg_ctx if self.cfg_ctx > 0 else (min(ctxs) if ctxs else None)
        return info

    def ctx_limit(self, mid=None):
        if self.cfg_ctx > 0:
            return self.cfg_ctx
        return self.fetch_models(timeout=4).get("ctx")

    def load_model(self, mid, timeout=300):
        """★ 通用 / 云端引擎**不需要"加载"** —— OpenAI 协议里模型名就是请求里的一个参数，
        换个名字就是换模型。所以这里**直接成功**，不再报"该引擎不支持热切换"。
        （原来去调 POST /v1/models/load —— 那是 llama.cpp 专属接口，云端必然 404，
         于是切换模型全部失败 ✗ 这是真 bug。）

        少数引擎确实有 /v1/models/load，就顺手调一次；失败也**不影响**切换，
        因为真正的切换发生在下一次请求的 body.model 里。
        """
        if not mid:
            return False, "空模型名"
        mid = str(mid).strip()
        if not mid:
            return False, "空模型名"
        try:
            _post_json(self.base_url + "/v1/models/load", {"model": mid}, 8,
                       headers=self._headers())
        except Exception:
            pass                     # 不支持就算了，不影响使用
        self.model_name = mid        # ★ 记住它，之后请求都用这个名字
        return True, None

    def running_model(self):
        """★ 云端 / 通用引擎没有"正在运行的模型"这个概念 —— 模型是按请求传的。

        所以只要服务可达，就把当前选定的 model 当作"就绪"返回，
        这样状态栏显示的是「● 运行中：<模型>」而不是误导性的「○ 未加载模型」。
        """
        try:
            if not self.health():
                return None, None, False
        except Exception:
            return None, None, False
        # 服务端愿意报就用它报的（有些引擎会报）
        try:
            info = self.fetch_models(timeout=3)
            if info.get("running"):
                return info["running"], info.get("running_state") or "loaded", True
        except Exception:
            pass
        return (self.model_name or None), ("loaded" if self.model_name else None), True

    def unload_model(self, mid=None):
        """★ 本机的"通用"引擎**也可能就是 llama.cpp**（只是起在非标准端口，
        所以被归类成通用 ✗）。它其实**支持卸载** ✓ 所以这里去试一下
        llama.cpp 的 `POST /models/unload`，成功就返回成功。

        用户实测："用这个端口检测出来的本地模型无法点击暂停模型" ✗
        云端则**没有卸载这回事** → 交给 GUI 隐藏按钮（见 is_cloud）✓
        """
        if not mid:
            return False, "空模型名"
        if self.is_cloud():
            return False, "云端模型跑在服务商那边，本机无法卸载（直接换个模型名即可）"
        last = None
        # llama.cpp 的两种常见卸载入口都试一下
        for path in ("/models/unload", "/v1/models/unload"):
            try:
                _post_json(self.base_url + path, {"model": mid}, 30,
                           headers=self._headers())
                return True, None
            except Exception as e:
                last = e
        return False, ("该引擎不支持在本机卸载模型（"
                       + (str(last)[:60] if last else "接口不可用")
                       + "）—— 请在引擎侧操作")

    def scan_reasoning(self, path):
        """推理能力识别 —— **必须区分"本机"和"云端"** ✓

        ★ 原来无条件返回 low/medium/high ✗ —— 这是**写死**的：
          本机那个走通用协议的引擎（比如非标准端口的 llama.cpp）明明**不支持**
          推理档位，界面上却也列出「推理：低/中/高」✗
          （用户实测："这个 8B 的模型不支持推理等级，但是他还是会有推理等级，
            是不是你写死了，不是自动检测的？" —— 对，就是写死的 ✗）

        正确做法：
          · **本机引擎** → 有 GGUF 路径就**真扫模板** ✓；扫不出来就如实说"不支持" ✓
            （绝不编造档位 ✓）
          · **云端引擎** → 没有模板文件可扫 ✗，但 OpenAI 协议本身就有
            `reasoning_effort` 字段 ✓ 所以给标准档位；不认它的服务商会忽略（无害）✓
        """
        if not self.is_cloud():
            if path:
                try:
                    caps = caps_from_template(_read_gguf_chat_template(path))
                    if isinstance(caps, dict):
                        return caps
                except Exception:
                    pass
            # 本机 + 拿不到模板 → **如实说不知道**，不编 ✗
            return {"thinking": False, "efforts": [], "default": None}
        return {"thinking": True,
                "efforts": ["low", "medium", "high"],
                "default": "medium"}


# ---------------------------------------------------------------- 流式实现（所有引擎共用）

def _http_status(url, timeout, headers=None):
    """只取 HTTP 状态码；连不上返回 None。**不抛异常**。

    用途：健康检查。有些服务对 /v1/models 返回 404，但那不代表服务不可用 ——
    需要区分"连不上"和"连上了但没这个接口"。
    """
    try:
        req = urllib.request.Request(url, method="GET", headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        return int(getattr(e, "code", 0) or 0)
    except Exception:
        return None


@contextlib.contextmanager
def _interruptible_response(req, timeout, stop_event):
    """摘要复用 stop_event 关闭连接，连等待响应头/正文时也能退出。"""
    if stop_event is None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            yield resp
        return
    sockets = []
    finished = threading.Event()  # 仅用于回收本次请求的监视线程，不是 GUI 状态。

    class Handler(urllib.request.HTTPHandler):
        def do_open(self, connection_class, request, **kwargs):
            def connect(*args, **kw):
                connection = connection_class(*args, **kw)
                original_connect = connection.connect
                def remember_socket():
                    original_connect()
                    sockets.append(connection.sock)
                    if stop_event.is_set():
                        connection.sock.shutdown(socket.SHUT_RDWR)
                        raise InterruptedError("已手动停止")
                connection.connect = remember_socket
                return connection
            return urllib.request.AbstractHTTPHandler.do_open(self, connect, request, **kwargs)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        do_open = Handler.do_open

    def cancel():
        while not finished.wait(.05):
            if stop_event.is_set():
                for sock in sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
    watcher = threading.Thread(target=cancel, daemon=True)
    watcher.start()
    try:
        if stop_event.is_set():
            raise InterruptedError("已手动停止")
        with urllib.request.build_opener(Handler(), HTTPSHandler()).open(req, timeout=timeout) as resp:
            yield resp
    finally:
        finished.set()
        watcher.join()


def _stream_impl(url, body_dict, stop_event, on_delta, timeout, headers=None):
    """SSE 流式请求：逐字回调 on_delta(str)；stop_event 置位则中断。
    返回 (完整文本, err)。llama.cpp / FreeToken / 云端的 SSE 格式一致，共用。

    headers：云端需要 Authorization，由调用方传 self._headers()；不传则只带 Content-Type。
    """
    # ★ 请求摘要（只有元数据 ✓ 不记正文）—— 排查"角色序列"就靠它 ✓
    try:
        log(f"STREAM {url}  {brief_messages((body_dict or {}).get('messages'))}")
    except Exception:
        pass
    body = json.dumps(body_dict).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    full = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if stop_event.is_set():
                    break
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
                if stop_event.is_set():
                    break
                if delta:
                    full.append(delta)
                    on_delta(delta)
        return "".join(full).strip(), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        log_error("stream", e.code, raw)          # ★ 日志里存**全量** ✓
        return None, f"HTTP {e.code}: {raw[:200]}"   # 屏幕上仍短提示 ✓
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------- 探测 / 工厂

# 自动发现时单个端口的探测超时。
# 实测：本机连"没人监听的端口"不会立刻被拒绝，而是一路挂到超时（正好 ~1011ms），
# 所以这个值直接决定启动时最长要等多久。1.5s 足够真引擎响应（真实引擎是毫秒级），
# 又不会让"一个都没开"的情况等太久。
SCAN_TIMEOUT = 1.5


def probe(base_url, timeout=2.0):
    """该 base_url 上有没有活的 OpenAI 兼容服务"""
    if not base_url:
        return False
    try:
        urllib.request.urlopen(
            urllib.request.Request(base_url.rstrip("/") + "/v1/models", method="GET"),
            timeout=timeout).close()
        return True
    except Exception:
        return False


def probe_openai(base_url, timeout=None):
    """探测是不是 OpenAI 兼容服务。返回 (ok, ids)。

    ★ 判定标准比 probe() 严：**必须能解析出 JSON，且带 "data" 字段**（`object` 是 list 也行）。
      只判 HTTP 200 会把随便一个本地网页服务（甚至 DSH 自己的 Web 界面）误判成引擎 ✗
      （实测本机 19 个监听端口里就有一个假阳性）。
    ★ **空的 data 也算活着** —— "引擎在跑、但一个模型都没加载"正是要能识别出来的情况 ✓

    ids 顺带返回，省一次请求。
    """
    t = SCAN_TIMEOUT if timeout is None else timeout
    try:
        data = _get_json(base_url.rstrip("/") + "/v1/models", t)
    except Exception:
        return False, []
    if not isinstance(data, dict):
        return False, []
    if "data" not in data and "list" not in str(data.get("object") or "").lower():
        return False, []
    ids = []
    for m in (data.get("data") or []):
        if isinstance(m, dict) and m.get("id"):
            ids.append(m["id"])
        elif isinstance(m, str):
            ids.append(m)
    return True, ids


def scan_ports(timeout=1.0):
    """扫 SCAN_PORTS 里的常见端口，返回活着的 [(kind, base_url), ...]（按表顺序）。

    ★ 并发探测：串行的话 7 个端口 × 1s 超时 = 启动白等 8 秒（实测就是 8.05s）。
      每个端口起一个线程后，最坏耗时 ≈ 一个 timeout。
    ★ 同一个端口在表里可能出现多次（8000 是 FreeToken 也是 vLLM），只探一次，
      以表里靠前的那个名字为准。
    ★ 返回顺序固定为**表顺序**（= 优先级），与探测完成的先后无关。
    """
    uniq, seen = [], set()
    for kind, port, url in SCAN_PORTS:
        if port in seen:
            continue
        seen.add(port)
        uniq.append((kind, port, url))
    results = {}

    def _one(kind, port, url):
        try:
            results[port] = probe(url, timeout=timeout)
        except Exception:
            results[port] = False

    threads = [threading.Thread(target=_one, args=item, daemon=True) for item in uniq]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 0.8)          # 留点余量，绝不无限等
    return [(kind, url) for kind, port, url in uniq if results.get(port)]


# netstat 里这些端口明显不是 AI 引擎，探测它们纯属浪费时间
_PORT_SKIP = {135, 139, 445, 593, 1900, 2869, 3389, 5040, 5353, 5357, 7680}


def local_listening_ports():
    """本机正在 LISTENING 的 TCP 端口（只看回环 / 通配地址）。

    ★ 用 netstat **动态**发现，不依赖任何硬编码端口表 ——
      引擎起在 8081、9999 还是 50000 都能被找到，这正是用户要的"自动扫描运行的引擎"。
    """
    ports = set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             ).stdout or ""
    except Exception:
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].upper().startswith("TCP"):
            continue
        # 末两列是状态 / PID；状态必须是 LISTENING
        if parts[-2].upper() != "LISTENING":
            continue
        addr = parts[1]
        if not (addr.startswith("127.0.0.1:") or addr.startswith("0.0.0.0:")
                or addr.startswith("[::1]:") or addr.startswith("[::]:")):
            continue                        # 只听局域网的不管
        try:
            p = int(addr.rsplit(":", 1)[1])
        except Exception:
            continue
        if 0 < p <= 65535 and p not in _PORT_SKIP:
            ports.add(p)
    return sorted(ports)


def _kind_for_port(port):
    """按已知端口表猜引擎名 —— **只影响状态栏的展示标注**，对话一律走 OpenAI 协议。

    所以即使猜成 "openai" 也能正常对话；猜不出来也不影响功能。
    """
    for kind, p, _u in SCAN_PORTS:
        if p == port:
            return kind
    return "openai"


def discover_engines(timeout=None, with_models=True):
    """★ 动态发现本机在跑的 OpenAI 兼容引擎。

    步骤：
      1. netstat 找出**所有** LISTENING 的本机端口
      2. 并上 SCAN_PORTS 的常见端口（兜底：有些服务只绑在别的接口上）
      3. **并发**探测每个端口是不是 OpenAI 兼容服务（GET /v1/models）
      4. 是的话顺手把它的模型列表也取回来 ——
         **即使这个引擎一个模型都没加载，它本身也会被列出来**（ids 为空数组）

    返回 [{kind, url, port, ids, n_models}]，排序：已知端口优先，其次端口号升序。
    """
    t = SCAN_TIMEOUT if timeout is None else timeout
    ports = set(local_listening_ports())
    for _k, p, _u in SCAN_PORTS:
        ports.add(p)
    ports = sorted(ports)
    res = {}

    def _one(p):
        url = f"http://127.0.0.1:{p}"
        try:
            # probe_openai 一次请求就同时判"是不是引擎" + 拿到模型列表（省一次往返）
            okk, ids = probe_openai(url, timeout=t)
            if not okk:
                res[p] = None
                return
            res[p] = {"kind": _kind_for_port(p), "url": url, "port": p,
                      "ids": list(ids), "n_models": len(ids)}
        except Exception:
            res[p] = None

    threads = [threading.Thread(target=_one, args=(p,), daemon=True) for p in ports]
    for th in threads:
        th.start()
    for th in threads:
        th.join(t + 1.2)

    out = [res[p] for p in ports if res.get(p)]

    def rank(d):
        for i, (_k, p, _u) in enumerate(SCAN_PORTS):
            if p == d["port"]:
                return (0, i, d["port"])
        return (1, 0, d["port"])
    out.sort(key=rank)
    return out


def detect_engine_ex(cfg):
    """自动发现引擎。返回 **(kind, base_url)**：

        kind     : "llama" / "freetoken" / "openai" / "both" / None
        base_url : 扫描发现时给出真实地址；用配置里的则返回 None

    决策顺序：
      1. 手动指定 llama / freetoken / openai → 用配置地址，**不扫描**
      2. auto + 云端（openai）配全了（base_url **且** api_key）→ **直接用云端**，不探本地
         （优先级 A：填了云端就是明确意图；想用本地就把 engine 设成 llama/freetoken）
      3. auto + 配置端口有人活 → 沿用配置（两个都活返回 both 让 GUI 问用户）
      4. auto + 配置端口全死 → 扫常见端口，取表里第一个活的
      5. 扫不到 → (None, None)

    ★★ base_url 返回值的语义（**很重要，别改错**）：
        非 None **只表示"这是扫描发现的地址"**；用户自己配置的地址一律返回 None，
        由 make_adapter 去读 cfg。
        这样 make_adapter 才能安全地区分"扫描到的本地引擎"和"用户配的云端" ——
        否则会把云端 api_key 连带发给本机进程（安全 + 模型名串号，见改造报告冲突 1/2）。

    ★ 性能：所有端口**一次并发探完**。串行做的话，本机对"没人监听的端口"不会立刻
      拒绝，而是一路挂到 timeout（实测每个死端口正好 1011ms）——两个配置端口串行
      就要 4 秒，加扫描接近 5 秒，启动明显卡。并发后最坏 ≈ 一个 timeout（1.5 秒）。
    """
    want = str(cfg.get("engine") or AUTO).lower()
    if want in (LLAMA, FREETOKEN):
        return want, None
    # ① 手动指定 openai：用配置，不扫描
    if want == OPENAI:
        return OPENAI, None
    # ①b 手动指定「本机扫到的通用引擎」：地址在 local_openai 段
    if want == "local":
        lu = str((cfg.get("local_openai") or {}).get("base_url") or "").strip()
        return (OPENAI, lu) if lu else (None, None)
    # ② auto + 云端配全了 → 优先云端（不探本地，省一次超时等待）
    oa = cfg.get("openai") or {}
    if str(oa.get("base_url") or "").strip() and str(oa.get("api_key") or "").strip():
        return OPENAI, None

    # ── 组装探测目标：配置里的两个端口（最高优先）+ 常见端口 ──
    targets = []                       # [(key, url, kind)]
    for kind, key in ((LLAMA, "llama"), (FREETOKEN, "freetoken")):
        u = str((cfg.get(key) or {}).get("base_url") or "").rstrip("/")
        if u:
            targets.append(("cfg:" + kind, u, kind))
    scan_list, seen = [], set()
    for kind, port, url in SCAN_PORTS:
        if port in seen:
            continue
        seen.add(port)
        scan_list.append((kind, url))
        targets.append(("scan:" + kind, url, kind))

    results = {}

    def _one(key, url, kind):
        try:
            results[key] = probe(url, timeout=SCAN_TIMEOUT)
        except Exception:
            results[key] = False

    threads = [threading.Thread(target=_one, args=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SCAN_TIMEOUT + 0.8)     # 留余量，绝不无限等

    # ── 决策 1：配置里的端口 ──
    hits = [k for k in (LLAMA, FREETOKEN) if results.get("cfg:" + k)]
    if len(hits) >= 2:
        return "both", None
    if hits:
        return hits[0], None

    # ── 决策 2：自动发现 ──
    # ★ 用 discover_engines() 而不是只扫硬编码表：它会先 netstat 找出**所有在监听
    #   的本机端口**再并发探测，所以引擎起在任何端口都能被找到（用户要的"动态发现"）。
    #   如果配置里的两个端口这次没探到（比如只绑了别的接口），这里也一并覆盖。
    for d in discover_engines(with_models=False):
        kind, url = d["kind"], d["url"]
        key = "cfg:" + kind
        if results.get(key) or results.get("scan:" + kind):
            continue                     # 上面已经判过配置里那两个
        if kind == LLAMA:
            return LLAMA, url
        if kind == FREETOKEN:
            return FREETOKEN, url
        return OPENAI, url
    return None, None


def detect_engine(cfg):
    """旧接口（终端版/外部脚本可能在用）：只返回 kind，保持向后兼容。

    新代码请用 detect_engine_ex()，它还会告诉你是哪个 base_url 被扫到的。
    """
    return detect_engine_ex(cfg)[0]


def _cli_key(argv=None):
    """命令行工具（供 bat 调用）：
        python engine_adapter.py --get-key-config <config.json>   → 打印密钥文件路径
        python engine_adapter.py --check-key <config.json>        → 打印是否有密钥（掩码）
        python engine_adapter.py --clear-key <config.json>        → 删除密钥文件
        没有参数时：交互式输入（隐藏回显）并加密保存
    """
    import getpass
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if argv and argv[0] in ("--get-key-config", "--check-key", "--clear-key"):
        if len(argv) > 1:
            cfg_path = argv[1]
        if argv[0] == "--get-key-config":
            print(key_file_for(cfg_path))
            return 0
        if argv[0] == "--check-key":
            k = load_api_key(cfg_path)
            if k:
                print("已保存密钥：" + mask_key(k))
                return 0
            print("尚未保存密钥（或密文无法解密）")
            return 1
        try:
            os.remove(key_file_for(cfg_path))
            print("已删除密钥文件")
            return 0
        except Exception as e:
            print("删除失败：" + str(e))
            return 1
    print("=" * 56)
    print("  MarkTalk - save cloud API key (encrypted with DPAPI)")
    print("  The key is encrypted for THIS Windows user on THIS PC.")
    print("  Stored in: " + key_file_for(cfg_path))
    print("=" * 56)
    if not DPAPI_OK:
        print("[ERROR] DPAPI unavailable (Windows only).")
        return 1
    try:
        k1 = getpass.getpass("  Paste API key (input hidden): ").strip()
        if not k1:
            print("[CANCELLED] empty input")
            return 1
        k2 = getpass.getpass("  Repeat to confirm: ").strip()
        if k1 != k2:
            print("[ERROR] the two inputs differ")
            return 1
        ok, err = save_api_key(cfg_path, k1)
        if not ok:
            print("[ERROR] " + str(err))
            return 1
        print("[OK] saved and encrypted: " + mask_key(k1))
        print("     Now you can delete the plaintext \"api_key\" line in config.json.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n[CANCELLED]")
        return 1


if __name__ == "__main__":
    sys.exit(_cli_key())


def make_adapter(kind, cfg, base_url_override=None):
    """按 kind 造适配器。

    base_url_override：自动扫描发现时传真实地址；None 表示用 config.json 里配的。
    """
    if kind == LLAMA:
        sec = dict(cfg.get("llama") or {})
        if base_url_override:
            sec["base_url"] = base_url_override
        return LlamaAdapter(sec)
    if kind == FREETOKEN:
        sec = dict(cfg.get("freetoken") or {})
        if base_url_override:
            sec["base_url"] = base_url_override
        return FreeTokenAdapter(sec)
    if kind == OPENAI:
        # ★★ base_url_override 非空 = 这个地址是**扫描发现的本地引擎**，
        #    绝不能继承 cfg.openai 里的 api_key / model / ctx：
        #      · 继承 api_key → 把你的云端 Key 发给本机进程（安全问题）
        #      · 继承 model   → 请求带着云端模型名打给 Ollama，直接 404（功能问题）
        #    所以这里造一个只含 base_url 的干净配置。
        if base_url_override:
            return OpenAIAdapter({"base_url": base_url_override})
        return OpenAIAdapter(dict(cfg.get("openai") or {}))
    return None
