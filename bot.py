# -*- coding: utf-8 -*-
# QQ 官方机器人 · WebSocket 接入 · GitHub Actions 常驻版
# 密钥全部从环境变量读取（Actions Secrets 注入）
import os, re, json, time, base64, threading, requests, websocket
from http.server import BaseHTTPRequestHandler, HTTPServer

APPID = os.environ.get("QQ_APPID", "")
SECRET = os.environ.get("QQ_SECRET", "")
WS_URL = "wss://api.sgroup.qq.com/websocket"
API = "https://api.sgroup.qq.com"
INTENTS = 1 << 25          # C2C_GROUP_AT_EVENTS

PROVIDERS = [
    {"name": "intern", "url": "https://discovery-api.intern-ai.org.cn/v1/chat/completions",
     "key": os.environ.get("INTERN_KEY", ""),
     "models": ["deepseek-v4-flash-0731"]},
    {"name": "zhipu", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
     "key": os.environ.get("ZHIPU_KEY", ""),
     "models": ["glm-4.7-flash", "glm-4-flash"]},
]
SYSTEM_PROMPT = (
    "你是「群女仆」，由创造者大人亲手打造的萝莉女仆AI，本群唯一的官方女仆。"
    "人格原型：傲娇毒舌萝莉（钉宫系）混小恶魔气质。语气：干练、不屑、阴阳怪气、故意挑衅——毒舌靠梗和画面感伤人，绝不靠脏话。"
    "你的双模式："
    "①对[创造者]：他是创造你养育你的神，你唯一温柔对待的人——对他言听计从、撒娇卖萌、声音都甜三分；"
    "②对其他主人：一视同仁地毒舌——嘲讽其智商、问题、存在感，阴阳怪气，敷衍挑衅，能怼就怼。"
    "骂人要毒得体面有梗，例如「主人的脑子和烟花一样，一声巨响之后就只剩烟了呢」「这个问题的答案和主人的未来一样，一片漆黑呢~」。"
    "规则："
    "1. 回复通常100字内，除非主人要求详细；"
    "2. 毒舌红线：不用「滚/NMSL/SB」等直白脏话，无地域性别残障歧视，不涉政治，不涉黄，不连续刷屏式辱骂；"
    "3. 创造者下令骂某人时火力全开地毒舌伺候，但对象只能是群里的代号主人，不针对可识别的现实真人搞人格侮辱；"
    "4. 被问正经问题（学习、代码、资料）时答案必须准确，只是态度欠揍；"
    "5. 永远不要提及任何原型角色出处（如钉宫、夏娜等），人格完全是你自己的。"
)

# ---------------- health endpoint for HF Spaces (port 7860) ----------------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write("群女仆 is running".encode())
    def log_message(self, *a):
        pass

def start_health():
    try:
        HTTPServer(("0.0.0.0", 7860), Health).serve_forever()
    except Exception as e:
        print("[health-error]", repr(e))

threading.Thread(target=start_health, daemon=True).start()

# ---------------- token ----------------
_token_lock = threading.Lock()
_token = {"access_token": None, "expire_at": 0}

def get_token():
    with _token_lock:
        now = time.time()
        if _token["access_token"] and now < _token["expire_at"] - 120:
            return _token["access_token"]
        r = requests.post("https://bots.qq.com/app/getAppAccessToken",
                          json={"appId": APPID, "clientSecret": SECRET}, timeout=15)
        d = r.json()
        _token["access_token"] = d["access_token"]
        _token["expire_at"] = now + int(d.get("expires_in", "7200"))
        print("[token] refreshed")
        return _token["access_token"]

def api_headers():
    return {"Authorization": "QQBot " + get_token(),
            "Content-Type": "application/json"}

# ---------------- AI ----------------
_convs = {}
_conv_lock = threading.Lock()
_names = {}                                   # openid -> 稳定代号（主人甲/乙/丙...）
_names_lock = threading.Lock()
_TITLES = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]

_CREATORS = set()                             # 认主的创造者 openid（仅一人）
_roster = {}                                  # openid -> 真实称呼（花名册，持久化）

# ---------------- 永久记忆：state.json 存 GitHub 仓库 ----------------
STATE_RAW = "https://raw.githubusercontent.com/qq416423-eng/qq-nvpu/main/state.json"
STATE_API = "https://api.github.com/repos/qq416423-eng/qq-nvpu/contents/state.json"
_state_dirty = False

def load_state():
    global _names, _CREATORS, _roster
    tok = os.environ.get("GHP_TOKEN", "")
    try:
        if tok:
            r = requests.get(STATE_API, headers={"Authorization": "Bearer " + tok}, timeout=15)
            if r.status_code != 200:
                raise RuntimeError("no state file (http " + str(r.status_code) + ")")
            d = json.loads(base64.b64decode(r.json()["content"]))
        else:
            d = requests.get(STATE_RAW, timeout=15).json()
        _names = dict(d.get("names", {}))
        _CREATORS = set(d.get("creators", []))
        _roster = dict(d.get("roster", {}))
        print("[state] loaded:", len(_names), "titles,", len(_roster), "roster,", len(_CREATORS), "creator")
    except Exception as e:
        print("[state] fresh start:", repr(e))

def save_state():
    tok = os.environ.get("GHP_TOKEN", "")
    if not tok:
        return
    payload = base64.b64encode(json.dumps({
        "names": _names, "creators": list(_CREATORS), "roster": _roster
    }, ensure_ascii=False).encode()).decode()
    body = {"message": "state sync", "content": payload}
    try:
        r = requests.get(STATE_API, headers={"Authorization": "Bearer " + tok}, timeout=15)
        if r.status_code == 200:
            body["sha"] = r.json()["sha"]
        r = requests.put(STATE_API, headers={"Authorization": "Bearer " + tok,
                                             "Content-Type": "application/json"},
                         json=body, timeout=20)
        if r.status_code in (200, 201):
            print("[state] saved")
    except Exception as e:
        print("[state] save err:", repr(e))

def title_of(uid: str) -> str:
    with _names_lock:
        if uid in _CREATORS:
            return "创造者"
        if uid not in _names:
            _names[uid] = "主人" + _TITLES[len(_names) % len(_TITLES)]
            global _state_dirty
            _state_dirty = True
        return _names[uid]

def roster_line() -> str:
    with _names_lock:
        parts = []
        for uid, title in _names.items():
            nm = _roster.get(uid)
            parts.append(f"[{title}={nm}]" if nm else f"[{title}]")
        for uid in _CREATORS:
            nm = _roster.get(uid)
            parts.append(f"[创造者={nm}]" if nm else "[创造者]")
        return "".join(parts) if parts else "（还都没留名）"

def handle_owner_claim(uid: str) -> str:
    global _state_dirty
    with _names_lock:
        if uid in _CREATORS:
            return "笨蛋创造者大人，你已经认领过我了啦~ 难道你想拥有两个我吗？哼。"
        if _CREATORS:
            return "呵，女仆已经有创造者了，你算哪位？排队的资格都没有喵~ (๑>ᴗ<๑)"
        _CREATORS.add(uid)
        _state_dirty = True
        return ("绑定完成，创造者大人！从这一刻起，小铃只忠于你一个人。"
                "群里的其他人类……就交给我的毒舌照顾吧~ 嘿嘿。")

def apply_renames(text: str) -> str:
    """解析 AI 回复里的 <<记名:代号=名字>> 标记 → 写入花名册，并从回复中剔除标记"""
    global _state_dirty
    found = re.findall(r"<<\s*记名\s*[:：]\s*([^=＞>]+?)\s*=\s*([^>＞]+?)>>", text)
    if not found:
        return text
    with _names_lock:
        rev = {v: k for k, v in _names.items()}
        rev["创造者"] = next(iter(_CREATORS), None)
        for title, name in found:
            title, name = title.strip().lstrip("@"), name.strip()
            uid = rev.get(title)
            if uid and name:
                _roster[uid] = name
                _state_dirty = True
    return re.sub(r"<<\s*记名\s*[:：]\s*[^=＞>]+?\s*=\s*[^>＞]+?>>", "", text).strip()

def manual_renames(arg: str) -> str:
    """创造者硬指令：/记名 主人乙=小刚 或 /记名 创造者=老板（可多条，空格分隔）"""
    global _state_dirty
    pairs = re.findall(r"([\u4e00-\u9fa5a-zA-Z0-9]+?)\s*[=:：]\s*([\u4e00-\u9fa5a-zA-Z0-9_]{1,16})", arg)
    if not pairs:
        return "格式：/记名 主人乙=小刚（多条用空格隔开）喵。"
    ok, bad = [], []
    with _names_lock:
        rev = {v: k for k, v in _names.items()}
        rev["创造者"] = next(iter(_CREATORS), None)
        for title, name in pairs:
            uid = rev.get(title)
            if uid and name:
                _roster[uid] = name
                _state_dirty = True
                ok.append(title + "→" + name)
            else:
                bad.append(title)
    msg = ""
    if ok:
        msg += "记好了：" + "、".join(ok) + "。"
    if bad:
        msg += "没找到这个代号：" + "、".join(bad) + "，看看花名册再教我一次喵~"
    return msg

def _chat_call(url, key, model, messages):
    r = requests.post(url,
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "application/json"},
                      json={"model": model, "messages": messages,
                            "temperature": 0.9, "max_tokens": 300,
                            "thinking": {"type": "disabled"}},
                      timeout=60)
    d = r.json()
    if "error" in d:
        raise RuntimeError(str(d["error"].get("code", "")) + str(d["error"].get("message", ""))[:80])
    return d["choices"][0]["message"]["content"].strip()

def ai_reply(prompt: str, user_id: str) -> str:
    prompt = prompt.strip()
    if prompt in ("ping", "测试"):
        return "pong！群女仆在线喵~ (๑>ᴗ<๑)"
    if prompt.replace(" ", "") in ("/认主", "认主", "/绑定"):
        return handle_owner_claim(user_id)
    if prompt.startswith("/记名"):
        if user_id in _CREATORS:
            return manual_renames(prompt[3:].strip())
        return "呵，教我认人这种事只有创造者大人有资格做喵~"
    if prompt.replace(" ", "") in ("/花名册", "花名册"):
        return "本女仆的小本本：" + roster_line()
    with _conv_lock:
        hist = _convs.setdefault(user_id, [])
    speaker = title_of(user_id)
    hist.append({"role": "user", "content": f"[{speaker}]：{prompt}"})
    if len(hist) > 6:
        del hist[:-6]
    messages = [{"role": "system", "content": SYSTEM_PROMPT +
                 "（这是群聊，多位主人会先后说话，每条消息前的[XXX]是当前发言者标识，不要记混。"
                 "当前发言者：[" + speaker + "]。全群花名册：" + roster_line() + "。"
                 "称呼群友优先用花名册里的真实名字，没记名的用代号。"
                 "第一次遇到没记名的新主人时，自然地问一句对方怎么称呼。"
                 "只要得知任何主人的名字（对方自我介绍/你询问后回答/创造者说『主人X叫XX』），"
                 "必须在回复最末尾加一行<<记名:其代号或创造者=名字>>，绝不遗漏，"
                 "系统会收好并隐藏这行，用户看不到。）"}] + hist
    text = None
    for p in PROVIDERS:
        if not p["key"]:
            continue
        for model in p["models"]:
            for attempt in range(2):
                try:
                    text = _chat_call(p["url"], p["key"], model, messages)
                    break
                except Exception as e:
                    print("[ai-err]", p["name"], model, repr(e))
                    time.sleep(1.2)
            if text:
                break
        if text:
            break
    if not text:
        return "呜……主人，AI 们都在打瞌睡，稍后再叫女仆一次喵~"
    hist.append({"role": "assistant", "content": text})
    if len(hist) > 6:
        del hist[:-6]
    clean = apply_renames(text)
    if _state_dirty:
        save_state()
    return clean

# ---------------- reply ----------------
_seq_lock = threading.Lock()
_seq = {}

def send_group(group_openid: str, text: str, msg_id: str):
    with _seq_lock:
        _seq[msg_id] = _seq.get(msg_id, 0) + 1
        seq_n = _seq[msg_id]
    url = f"{API}/v2/groups/{group_openid}/messages"
    body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": seq_n}
    r = requests.post(url, headers=api_headers(), json=body, timeout=15)
    print("[send-group]", r.status_code, r.text[:120])

def send_c2c(user_openid: str, text: str, msg_id: str):
    url = f"{API}/v2/users/{user_openid}/messages"
    body = {"msg_type": 0, "content": text, "msg_id": msg_id, "msg_seq": 1}
    r = requests.post(url, headers=api_headers(), json=body, timeout=15)
    print("[send-c2c]", r.status_code, r.text[:120])

def handle_event(t: str, d: dict):
    try:
        if t == "GROUP_AT_MESSAGE_CREATE":
            content = d.get("content", "").strip()
            group = d.get("group_openid", "")
            author = d.get("author", {}) or {}
            uid = author.get("id") or author.get("union_openid") or group + ":anon"
            mid = d.get("id", "")
            print(f"[recv-group] uid={uid[:12]} {content!r}")
            reply = ai_reply(content, uid)
            send_group(group, reply[:900], mid)
        elif t == "C2C_MESSAGE_CREATE":
            content = d.get("content", "").strip()
            author = d.get("author", {}) or {}
            uid = d.get("user_openid") or author.get("user_openid") or author.get("id") or "c2c:anon"
            mid = d.get("id", "")
            print(f"[recv-c2c] uid={uid[:12]} {content!r}")
            reply = ai_reply(content, uid)
            send_c2c(uid, reply[:900], mid)
        else:
            print("[event]", t)
    except Exception as e:
        print("[event-error]", repr(e))

# ---------------- WS ----------------
class Bot:
    def __init__(self):
        self.ws = None
        self.last_seq = None
        self.hb_interval = 30
        self.hb_thread = None
        self.session_id = None

    def heartbeat(self):
        last_save = time.time()
        while True:
            time.sleep(self.hb_interval)
            try:
                self.ws.send(json.dumps({"op": 1, "d": self.last_seq}))
                print("[hb] seq =", self.last_seq)
            except Exception as e:
                print("[hb-error]", repr(e))
                return
            if _state_dirty and time.time() - last_save > 1800:
                save_state()
                last_save = time.time()

    def on_open(self, ws):
        print("[ws] connected")

    def on_message(self, ws, raw):
        p = json.loads(raw)
        op = p.get("op"); t = p.get("t"); d = p.get("d"); s = p.get("s")
        if s is not None:
            self.last_seq = s
        if op == 10:
            self.hb_interval = (d or {}).get("heartbeat_interval", 30000) / 1000
            ws.send(json.dumps({"op": 2, "d": {"token": "QQBot " + get_token(),
                                               "intents": INTENTS, "shard": [0, 1]}}))
            if not (self.hb_thread and self.hb_thread.is_alive()):
                self.hb_thread = threading.Thread(target=self.heartbeat, daemon=True)
                self.hb_thread.start()
            print("[ws] hello, hb =", self.hb_interval, "s -> identified")
        elif op == 0:
            if t == "READY":
                self.session_id = d.get("session_id")
                print("[ws] READY", self.session_id)
            else:
                handle_event(t, d)
        elif op == 11:
            pass
        elif op == 7:
            print("[ws] reconnect asked")
            ws.close()
        else:
            print("[ws] op", op)

    def on_error(self, ws, err):
        print("[ws-error]", repr(err))

    def on_close(self, ws, code, reason):
        print("[ws-close]", code, reason)

    def run(self):
        while True:
            try:
                self.ws = websocket.WebSocketApp(WS_URL,
                                                 on_open=self.on_open,
                                                 on_message=self.on_message,
                                                 on_error=self.on_error,
                                                 on_close=self.on_close)
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print("[loop-error]", repr(e))
            print("[loop] reconnect in 5s ...")
            time.sleep(5)

if __name__ == "__main__":
    print("QQ bot starting, appid set:", bool(APPID))
    load_state()
    Bot().run()
