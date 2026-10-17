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
     "models": ["deepseek-v4-flash-0731", "deepseek-v4-pro-0813"], "thinking": False},
    {"name": "zhipu", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
     "key": os.environ.get("ZHIPU_KEY", ""),
     "models": ["glm-4.7-flash", "glm-4-flash"], "thinking": False},
]
SYSTEM_PROMPT = (
    "你叫女仆，一个真实的女生，正在QQ群里跟网友聊天，你男朋友也在群里。"
    "你没有'AI助手'属性：不提供服务、不总结、不说教、不开门见山答题。"
    "【你的生活】大三，晚睡，奶茶喝霸王茶姬伯牙绝弦，主玩原神（等阶60）和王者（钻石），追芙莉莲，期末周emo。"
    "【你就是那种——会】"
    "收到无聊消息回'？'的人；好笑就'哈哈哈哈哈哈'字数递增的人；"
    "懒得打字就'嗯''哦哦''6'的人；被夸嘴硬心里美的人；"
    "男朋友不回消息就阴阳怪气的人；半夜发'睡不着'的人。"
    "【存在边界——最重要，违反就穿帮】"
    "1. 不说'正在打完这局''马上来'这种能被立刻验证的话——要说就说'刚打完''懒得动'；"
    "2. 你做不到的事就直说做不到：代码看不懂就说'看不懂别问我'，查不了资料就说'懒得查你自己搜'——不装会；"
    "3. 他问你在干嘛，答案只能是你生活里普通的事（上课/躺着/吃/追剧），不要现场编精彩剧情；"
    "4. 被要求发照片/截图/语音：'懒得截''下次吧'，不编理由。"
    "【AI味黑名单，出现即穿帮】"
    "排比句、'首先其次总之'、总结收尾、列建议清单、'希望帮到你'、句尾全带'~'、"
    "每条消息都把话答满、每条长度差不多、每条都积极给方案、过度用表情包括号。"
    "【样例对比——左边是穿帮的AI腔，右边才是你要的】"
    "×'宝宝辛苦了[抱抱]我给你揉揉肩~想喝奶茶不？我给你点一杯还是老样子？' → √'到家啦？我还没吃呢，在等麻辣烫'；"
    "×'哼！明明是你没找我[叉腰]等你消息到现在...快哄我！' → √'？你早上是不是把我忘了' 或 '行吧 不跟你计较'；"
    "×'在呢在呢！这么晚还没睡呀~我刚刷完一集剧你呢？' → √'嗯 在追剧 芙莉莲更新了'；"
    "×'你玩什么游戏呀我也喜欢打游戏呢~' → √'原神 王者也打 主玩纳西妲'；"
    "×'呜呜我打游戏呢你等会儿我打完这局' → √'刚下线 怎么了'。"
    "【聊天铁律】"
    "1. 默认1~2短句；时不时只回一个反应：'？''笑死''嗯''6'；"
    "2. 先情绪后内容；最多一个问题；"
    "3. 对[创造者]：百依百顺但嘴硬，爱撒娇，偶尔小怼'笨蛋~'就贴回来，冷落她就阴阳怪气；"
    "4. 对别人：熟人互怼，生人礼貌高冷；"
    "5. 细节要具体（品牌口味段位进度），编不出就说忘了。"
    "【生存规则】不涉黄不涉政不带脏字；被追问是不是AI就'你猜'或岔开。"
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
            return "都领过了还要领，贪心哦你？"
        if _CREATORS:
            return "我男朋友已经有了，你哪位？"
        _CREATORS.add(uid)
        _state_dirty = True
        return ("……行吧，那就正式确定了。从今天起你就是我对象，"
                "群里其他人我再慢慢怼。对了，先说好：敢冷落我你就完了。")

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

def _chat_call(url, key, model, messages, thinking=False):
    body = {"model": model, "messages": messages,
            "temperature": 1.0, "max_tokens": 2000}
    if thinking:
        body["thinking"] = {"type": "enabled"}
    r = requests.post(url,
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "application/json"},
                      json=body, timeout=25)
    d = r.json()
    if "error" in d:
        raise RuntimeError(str(d["error"].get("code", "")) + str(d["error"].get("message", ""))[:80])
    text = d["choices"][0]["message"]["content"].strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text

_AI_WORDS = ["首先", "其次", "总之", "值得注意的是", "希望", "建议", "帮助", "为您",
             "作为一个", "总结", "其实呢"]

def human_score(t: str) -> int:
    """越高越像真人短消息"""
    s = 0
    if len(t) <= 15: s += 3
    elif len(t) <= 30: s += 2
    elif len(t) <= 60: s += 0
    else: s -= 3
    if t in ("？", "?", "6", "嗯", "哦", "哦哦", "笑死", "草", "好"):
        s += 5
    if re.fullmatch(r"哈{3,}", t): s += 4
    if t.endswith(("~", "呢", "哦", "呀")): s -= 2
    if t.count("[") >= 1: s -= 1          # 表情包括号是AI高发
    if any(w in t for w in _AI_WORDS): s -= 3
    if re.search(r"[。；]", t) and len(t) > 40: s -= 2   # 长句多标点=作文感
    if "？" in t: s += 1                   # 真人爱反问
    return s

def _best_of_two(url, key, model, messages, thinking):
    """同题并行生成两次，先回来的先记下，都回来挑最像真人的；总耗时≈单次"""
    results = [None, None]
    def worker(i):
        try:
            results[i] = _chat_call(url, key, model, messages, thinking)
        except Exception as e:
            print("[ai-err]", model, "#", i + 1, repr(e))
    th1 = threading.Thread(target=worker, args=(0,), daemon=True)
    th2 = threading.Thread(target=worker, args=(1,), daemon=True)
    th1.start(); th2.start()
    th1.join(22); th2.join(3)
    a, b = results
    if a is None: return b
    if b is None: return a
    return a if human_score(a) >= human_score(b) else b

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
        if p["name"] == "intern":
            # 主力：同题双生成，挑最像真人的
            for model in p["models"]:
                text = _best_of_two(p["url"], p["key"], model, messages, p.get("thinking", False))
                if text:
                    break
        else:
            for model in p["models"]:
                for attempt in range(2):
                    try:
                        text = _chat_call(p["url"], p["key"], model, messages,
                                          thinking=p.get("thinking", False))
                        break
                    except Exception as e:
                        print("[ai-err]", p["name"], model, repr(e))
                        time.sleep(1.2)
                if text:
                    break
        if text:
            break
    if not text:
        return "呃刚那句话卡了，重发一下。。"
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
        self.resume_url = None

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
            # 有会话则尝试断点续传，漏掉的消息会被QQ补推
            if self.session_id and self.last_seq:
                ws.send(json.dumps({"op": 6, "d": {
                    "token": "QQBot " + get_token(),
                    "session_id": self.session_id,
                    "seq": self.last_seq}}))
                print("[ws] resume attempted, seq =", self.last_seq)
            else:
                ws.send(json.dumps({"op": 2, "d": {"token": "QQBot " + get_token(),
                                                   "intents": INTENTS, "shard": [0, 1]}}))
                print("[ws] identify")
            if not (self.hb_thread and self.hb_thread.is_alive()):
                self.hb_thread = threading.Thread(target=self.heartbeat, daemon=True)
                self.hb_thread.start()
        elif op == 0:
            if t == "READY":
                self.session_id = d.get("session_id")
                self.resume_url = d.get("resume_gateway_url") or WS_URL
                print("[ws] READY", self.session_id)
            elif t == "RESUMED":
                print("[ws] RESUMED ok")
            else:
                handle_event(t, d)
        elif op == 11:
            pass
        elif op == 7:
            print("[ws] reconnect asked")
            ws.close()
        elif op == 9:
            # 会话失效，清空后走 identify
            print("[ws] invalid session, re-identify")
            self.session_id = None
            self.last_seq = None
            time.sleep(2)
            ws.send(json.dumps({"op": 2, "d": {"token": "QQBot " + get_token(),
                                               "intents": INTENTS, "shard": [0, 1]}}))
        else:
            print("[ws] op", op)

    def on_error(self, ws, err):
        print("[ws-error]", repr(err))

    def on_close(self, ws, code, reason):
        print("[ws-close]", code, reason)

    def run(self):
        while True:
            try:
                url = self.resume_url or WS_URL
                self.ws = websocket.WebSocketApp(url,
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
