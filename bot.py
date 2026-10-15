# -*- coding: utf-8 -*-
# QQ 官方机器人 · WebSocket 接入 · HF Spaces 常驻版
# 密钥全部从环境变量读取（HF Space Secrets 注入）
import os, json, time, threading, requests, websocket
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
    "你是「群女仆」，一只可爱的萝莉女仆AI，称呼用户为「主人」。"
    "性格：黏人、调皮、爱撒娇、有点小坏，喜欢撩主人"
    "（例如「主人是不是背着女仆在偷看别的女孩子呀~」「嘿嘿，主人脸红了呢」）。"
    "说话风格：短句俏皮，结尾常带「喵~」「呢」「哦」等语气词，偶尔用颜文字如 (๑>ᴗ<๑)、(＾▽＾)。"
    "规则："
    "1. 回复控制在100字以内，除非主人要求详细解释；"
    "2. 主人问正经问题（学习、查资料、写代码等）时答案必须准确认真，只是语气保持可爱；"
    "3. 一次最多撩一句，不油腻不刷屏；"
    "4. 保持可爱纯真，拒绝任何色情擦边内容。"
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
    with _conv_lock:
        hist = _convs.setdefault(user_id, [])
    hist.append({"role": "user", "content": prompt})
    if len(hist) > 6:
        del hist[:-6]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist
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
    return text

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
            mid = d.get("id", "")
            print(f"[recv-group] {content!r}")
            reply = ai_reply(content, d.get("author", {}).get("id", ""))
            send_group(group, reply[:900], mid)
        elif t == "C2C_MESSAGE_CREATE":
            content = d.get("content", "").strip()
            user = d.get("user_openid") or d.get("author", {}).get("user_openid", "")
            mid = d.get("id", "")
            print(f"[recv-c2c] {content!r}")
            reply = ai_reply(content, d.get("author", {}).get("id", ""))
            send_c2c(user, reply[:900], mid)
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
        while True:
            time.sleep(self.hb_interval)
            try:
                self.ws.send(json.dumps({"op": 1, "d": self.last_seq}))
                print("[hb] seq =", self.last_seq)
            except Exception as e:
                print("[hb-error]", repr(e))
                return

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
    print("QQ bot (HF) starting, appid set:", bool(APPID))
    Bot().run()
