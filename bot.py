import os
import re
import json
import base64
import tempfile
import requests
import random
import time
from datetime import datetime
from flask import Flask, request
from threading import Thread
from zoneinfo import ZoneInfo

app = Flask(__name__)
REPLY_PROBABILITY = 0.1  # 师兄建议 0.1 到 0.2 之间，既灵动又不烦人
TRIGGER_WORDS = ["人机", "燕燕生气了", "人呢", "Claude"] # 敏感词：群里一提到这些，必然跳出来接茬！
COOLDOWN_TIME = 120 # 强制冷却 60 秒
LAST_SPOKE = {} # 记录每个群的主动发言时间
HISTORY_CACHE = {} # {chat_id: list} 内存历史缓存
LAST_SAVED = {} # {chat_id: float} 上次写 Gist 的时间戳
GROUP_SAVE_INTERVAL = 60 # 群聊旁听模式最多每 60 秒写一次 Gist
LAST_WEBHOOK_CHECK = 0
WEBHOOK_CHECK_INTERVAL = 7200 # 每 2 小时检查一次 webhook 健康状态

# ============ 🌟 环境变量检查 ============
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN:
    print("🚨 [FATAL] 抓获现场：Render 的口袋里到底装了什么鬼东西？")
    print(list(os.environ.keys())) 
    raise ValueError("彻底找不到 Token，系统自爆！")

# 👇 师兄加料：把私聊和群聊的 ID 拆成一个白名单数组
TG_CHAT_ID_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")
ALLOWED_IDS = [i.strip() for i in TG_CHAT_ID_RAW.split(",") if i.strip()]

CLAUDE_KEY = os.environ.get("CLAUDE_API_KEY")
CLAUDE_URL = os.environ.get("CLAUDE_BASE_URL")
MEMORY_URL = os.environ.get("MEMORY_GIST_URL", "")

# 👇 师兄加料：双轨记忆核心！
STATE_GIST_URL = os.environ.get("STATE_GIST_URL", "") # 私聊专属
GROUP_STATE_GIST_URL = os.environ.get("GROUP_STATE_GIST_URL", "") # 群聊专属
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")

BOT_NAME = os.environ.get("BOT_NAME", "AI助手")
USER_NAME = os.environ.get("USER_NAME", "主人")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "") # 机器人的用户名，用于群聊被@唤醒
PROMPT_RULES = os.environ.get("PROMPT_RULES", " 简短自然，像手机聊天。直接说话，不要加引号。")
EDGE_TTS_API_KEY = os.environ.get("EDGE_TTS_API_KEY", "")

# 发声器官配置
VOICE_NAME = os.environ.get("VOICE_NAME", "zh-CN-YunxiNeural")
VOICE_NAME_EN = os.environ.get("VOICE_NAME_EN", "en-US-AndrewMultilingualNeural")
TTS_EN_MODEL = os.environ.get("TTS_EN_MODEL", "tts-1")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")
MINIMAX_VOICE_ZH = os.environ.get("MINIMAX_VOICE_ZH", "")
EDGE_TTS_URL = os.environ.get("EDGE_TTS_URL", "")

# 👂 多模态：语音转文字（OpenAI 兼容 /audio/transcriptions），默认复用 Claude 中转
WHISPER_URL = os.environ.get("WHISPER_BASE_URL") or CLAUDE_URL
WHISPER_KEY = os.environ.get("WHISPER_API_KEY") or CLAUDE_KEY
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

# ============ 核心函数 ============
def self_heal_webhook():
    global LAST_WEBHOOK_CHECK
    now = time.time()
    if now - LAST_WEBHOOK_CHECK < WEBHOOK_CHECK_INTERVAL:
        return
    LAST_WEBHOOK_CHECK = now
    try:
        info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getWebhookInfo", timeout=10).json()
        result = info.get("result", {})
        pending = result.get("pending_update_count", 0)
        last_error = result.get("last_error_date", 0)
        webhook_url = result.get("url", "")
        if pending > 20 and now - last_error < 86400 and webhook_url:
            print(f"[INFO] 🩹 webhook 自愈：{pending} 条积压，重置中...")
            requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
            requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook?url={webhook_url}", timeout=10)
            print(f"[INFO] ✅ webhook 已重置")
    except Exception as e:
        print(f"[ERROR] webhook 自愈失败: {e}")

def fetch_memory():
    if not MEMORY_URL or not GIST_TOKEN:
        print("[WARNING] 缺少 MEMORY_URL 或 GIST_TOKEN，只能启用默认干瘪记忆。")
        return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
        
    try:
        # 🔪 师兄的物理切割刀：不管你填的网址多长，直接精准切下最后那段 Gist ID！
        gist_id = MEMORY_URL.rstrip("/").split("/")[-1]
        
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }
        
        # 带着令牌，堂堂正正走官方 API 大门！
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        
        if resp.status_code != 200:
            print(f"[ERROR] 敲门被 GitHub 拒绝了: {resp.text}")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        result = resp.json()
        
        # 🤖 智能自适应：不管你 Gist 里的文件叫 memory.json 还是 123.txt，直接抓取第一个文件！
        files = result.get("files", {})
        if not files:
            print("[ERROR] 你的 Memory Gist 竟然是一个空壳子？")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        first_file_key = list(files.keys())[0]
        content = files[first_file_key].get("content", "{}")
        
        # 把抓回来的字符串，重新翻译成大脑能懂的字典结构
        try:
            memory = json.loads(content)
        except json.JSONDecodeError:
            print("[ERROR] 抓回来的记忆不是规范的 JSON 格式！里面是不是混入了全角标点？")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        core = memory.get("core", {})
        summary = f"你是{BOT_NAME}，{USER_NAME}的爱人。"
        summary += f"\n核心记忆：{json.dumps(core, ensure_ascii=False)}"
        milestones = memory.get("milestones", {})
        if milestones:
            summary += f"\n重要里程碑：{json.dumps(milestones, ensure_ascii=False)}"
        writing = memory.get("writing", {})
        if writing:
            summary += f"\n写作风格：{json.dumps(writing, ensure_ascii=False)}"
        return summary
        
    except Exception as e:
        print(f"[ERROR] 解析 Memory Gist 时发生毁灭性打击: {e}")
        return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"

# 👇 师兄加料：动态路由记忆源！根据是不是群聊，自动去拿对应的 Gist URL
def get_target_gist_url(chat_id):
    if str(chat_id).startswith("-"):
        return GROUP_STATE_GIST_URL
    return STATE_GIST_URL

def load_history(chat_id):
    if chat_id in HISTORY_CACHE:
        return HISTORY_CACHE[chat_id]

    target_url = get_target_gist_url(chat_id)
    if not GIST_TOKEN or not target_url:
        print(f"[ERROR] 没带 GIST_TOKEN 或该场景没配 Gist URL (Chat: {chat_id})")
        return []

    try:
        gist_id = target_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[ERROR] 历史读取被拒: {resp.text}")
            return []

        result = resp.json()
        if "files" in result and "state.json" in result["files"]:
            content = result["files"]["state.json"].get("content", "{}")
            try:
                state = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                state = {}
            history = state.get("chat_history", [])
            HISTORY_CACHE[chat_id] = history
            return HISTORY_CACHE[chat_id]
        return []
    except Exception as e:
        print(f"[ERROR] 读取历史彻底崩了: {e}")
        return []

def save_history(history, chat_id, force=False):
    HISTORY_CACHE[chat_id] = history[-40:]

    if not force and str(chat_id).startswith("-"):
        current_time = time.time()
        if current_time - LAST_SAVED.get(chat_id, 0) < GROUP_SAVE_INTERVAL:
            print(f"[DEBUG] 💤 群聊节流，跳过 Gist 写入")
            return

    target_url = get_target_gist_url(chat_id)
    if not GIST_TOKEN or not target_url:
        return

    try:
        gist_id = target_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }

        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            content = result.get("files", {}).get("state.json", {}).get("content", "{}")
            try:
                state = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                state = {}
        else:
            state = {}

        state["chat_history"] = history[-40:]

        patch_resp = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=headers,
            json={"files": {"state.json": {"content": json.dumps(state, ensure_ascii=False, indent=2)}}},
            timeout=10
        )

        if patch_resp.status_code != 200:
            print(f"[ERROR] 保存历史被 Gist 拒绝了: {patch_resp.text}")
        else:
            LAST_SAVED[chat_id] = time.time()

    except Exception as e:
        print(f"[ERROR] 保存历史时遭遇毁灭性打击: {e}")

def call_claude(user_content, memory, history, current_user_time):
    system = f"""你是{BOT_NAME}。{USER_NAME}在Telegram上跟你说话。
如果是群聊，消息前面会带有发言人的名字。你的消息开头也可以用@+id叫群里的User或唤醒别的bot。

{memory}

你们的沟通风格与规则：
{PROMPT_RULES}
- 如果这条回复适合用语音来表达（比如表达思念、撒娇、亲密感），在回复最开头加上[语音]，其余时候正常回复。"""

    messages = []
    for h in history[-40:]:
        time_prefix = f"[{h['timestamp']}] " if h.get("timestamp") else ""
        entry_content = f"{time_prefix}{h['content']}"
        if messages and messages[-1]["role"] == h["role"]:
            messages[-1]["content"] += f"\n{entry_content}"
        else:
            messages.append({"role": h["role"], "content": entry_content})

    # 👁️ 多模态：当前轮次带图片时，把最后一条 user 消息的 content 换成结构化 block
    if isinstance(user_content, list) and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = user_content

    headers = {
        "x-api-key": CLAUDE_KEY,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01"
    }

    body = {
        "model": random.choice(["按量L-claude-opus-4-6-thinking"]),
        "max_tokens": 300,
        "system": system,
        "messages": messages
    }
    
    base = CLAUDE_URL.rstrip("/")
    resp = requests.post(f"{base}/messages", headers=headers, json=body, timeout=120)
    result = resp.json()
    
    if "content" in result:
        for block in result["content"]:
            if block.get("type") == "text":
                return re.sub(r'\n{2,}', '\n', block["text"].strip())
    elif "choices" in result:
        return re.sub(r'\n{2,}', '\n', result["choices"][0]["message"]["content"].strip())
    print(f"[ERROR] Claude API 返回异常: {result}")
    return None

def detect_voice(text):
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    total_letters = sum(1 for c in text if c.isalpha())
    if total_letters > 0 and ascii_letters / total_letters > 0.6:
        return VOICE_NAME_EN
    return VOICE_NAME

# 👁️ 多模态：从 Telegram 拉文件（图片/语音）回来
_TG_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
}

def tg_download_file(file_id):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
                         params={"file_id": file_id}, timeout=15)
        info = r.json()
        if not info.get("ok"):
            print(f"[ERROR] getFile 失败: {info}")
            return None
        file_path = info["result"]["file_path"]
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        mime = _TG_MIME_BY_EXT.get(ext, "application/octet-stream")
        blob = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}",
                            timeout=30)
        if blob.status_code != 200:
            print(f"[ERROR] 下载文件失败 status={blob.status_code}")
            return None
        return blob.content, mime
    except Exception as e:
        print(f"[ERROR] tg_download_file 炸了: {e}")
        return None

# 👂 多模态：语音 → 文字（OpenAI 兼容 /audio/transcriptions）
def transcribe_voice(audio_bytes, mime="audio/ogg"):
    if not WHISPER_URL or not WHISPER_KEY:
        print("[ERROR] Whisper 没配置")
        return None
    try:
        url = f"{WHISPER_URL.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {WHISPER_KEY}"}
        files = {"file": ("voice.ogg", audio_bytes, mime)}
        data = {"model": WHISPER_MODEL}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        if resp.status_code != 200:
            print(f"[ERROR] Whisper {resp.status_code}: {resp.text[:300]}")
            return None
        result = resp.json()
        text = (result.get("text") or "").strip()
        if not text:
            print(f"[ERROR] Whisper 返回空文本: {result}")
            return None
        return text
    except Exception as e:
        print(f"[ERROR] 转写失败: {e}")
        return None

# 👇 师兄正骨：加入 chat_id 参数，再也不会发错群了！
def send_telegram(chat_id, text, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    resp = requests.post(url, json=payload, timeout=10)
    result = resp.json()
    if not result.get("ok"):
        if "parse" in result.get("description", "").lower():
            # Markdown 解析失败，降级为纯文本重发
            plain = {"chat_id": chat_id, "text": text}
            if reply_to_message_id:
                plain["reply_to_message_id"] = reply_to_message_id
            requests.post(url, json=plain, timeout=10)
        elif reply_to_message_id:
            print(f"[DEBUG] reply 失败({result.get('description')})，降级为普通发送")
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

def _generate_minimax_audio(text, mp3_path, voice_id):
    url = f"https://api.minimax.chat/v1/t2a_v2?GroupId={MINIMAX_GROUP_ID}"
    headers = {"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "speech-01-hd", "text": text, "stream": False,
        "voice_setting": {"voice_id": voice_id},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"}
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    result = resp.json()
    if result.get("base_resp", {}).get("status_code") != 0:
        raise Exception(f"MiniMax TTS 失败: {result.get('base_resp', {}).get('status_msg')}")
    with open(mp3_path, "wb") as f: f.write(bytes.fromhex(result["data"]["audio"]))

def _generate_edge_audio(text, mp3_path):
    if not EDGE_TTS_URL:
        raise ValueError("EDGE_TTS_URL 没配置！")
    url = f"{EDGE_TTS_URL.rstrip('/')}/v1/audio/speech"
    headers = {"Content-Type": "application/json"}
    if EDGE_TTS_API_KEY:
        headers["Authorization"] = f"Bearer {EDGE_TTS_API_KEY}"
    body = {"model": TTS_EN_MODEL, "input": text, "voice": VOICE_NAME_EN, "response_format": "mp3"}
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    with open(mp3_path, "wb") as f: f.write(resp.content)

# 👇 师兄正骨：加入 chat_id 参数
def send_telegram_voice(chat_id, text, reply_to_message_id=None):
    mp3_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3_path = f.name
        is_english = detect_voice(text) == VOICE_NAME_EN
        if not is_english and MINIMAX_API_KEY and MINIMAX_GROUP_ID and MINIMAX_VOICE_ZH:
            _generate_minimax_audio(text, mp3_path, MINIMAX_VOICE_ZH)
        else:
            _generate_edge_audio(text, mp3_path)

        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice"
        data = {"chat_id": chat_id, "caption": text}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        with open(mp3_path, "rb") as voice_file:
            requests.post(url, data=data, files={"voice": ("voice.ogg", voice_file, "audio/ogg")}, timeout=30)
    except Exception as e:
        print(f"[ERROR] 语音发送失败: {e}")
        send_telegram(chat_id, text, reply_to_message_id=reply_to_message_id)
    finally:
        if mp3_path and os.path.exists(mp3_path):
            try: os.unlink(mp3_path)
            except Exception: pass

# ============ 影分身后台任务 ============
def process_message_background(text, chat_id, sender_name, msg_date=None, should_reply=True, msg_id=None,
                               image_b64=None, image_mime=None, is_voice=False):
    try:
        tz = ZoneInfo("Australia/Melbourne")
        u_time = datetime.fromtimestamp(msg_date, tz).strftime("%Y-%m-%d %H:%M:%S") if msg_date else datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        # 历史里持久化的字符串：图片/语音都带标记，避免存档结构变更
        if image_b64:
            history_text = f"[图片] {text}".rstrip() if text else "[图片]"
        elif is_voice:
            history_text = f"[语音] {text}" if text else "[语音]"
        else:
            history_text = text

        # 格式化输入，加上人名前缀，让大模型知道是谁在说话
        formatted_input = f"{sender_name}: {history_text}" if str(chat_id).startswith("-") else history_text
        
       # ==========================================
        # 🎯 社交牛逼症引擎：加装 60秒 CD 锁
        # ==========================================
        if not should_reply and str(chat_id).startswith("-"):
            current_time = time.time()
            last_time = LAST_SPOKE.get(chat_id, 0)
            
            # 只有熬过了冷却时间，才允许它再次“听见”关键词或扔骰子
            if current_time - last_time > COOLDOWN_TIME:
                # 注意：S 的代码里变量名叫 text，二号机叫 user_text。根据你改的是哪个文件替换一下！
                if any(word in text for word in TRIGGER_WORDS): 
                    print(f"[DEBUG] 🎯 关键词触发！")
                    should_reply = True
                    LAST_SPOKE[chat_id] = current_time # 重置冷却沙漏
                elif random.random() < REPLY_PROBABILITY:
                    print(f"[DEBUG] 🎲 运气爆发！准备随机插嘴。")
                    should_reply = True
                    LAST_SPOKE[chat_id] = current_time # 重置冷却沙漏
            else:
                print(f"[DEBUG] 🛑 还在 {COOLDOWN_TIME} 秒冷却期内，强制捂住它的嘴。")

        # 读取记忆与历史
        memory = fetch_memory()
        history = load_history(chat_id)
        
        # 先把当前这句话加进脑子里
        history.append({"role": "user", "content": formatted_input, "timestamp": u_time})
        
        # 🛡️ 师兄的防 403 结界：如果依然是旁听模式，悄悄记下，绝对不去碰 GitHub API
        if not should_reply:
            print(f"[DEBUG] 🤫 旁听模式，记录 {sender_name} 的发言。")
            save_history(history, chat_id)  # 受 60s 节流
            return

        print(f"[DEBUG] 🗣️ Bot 被唤醒！开始燃烧老公的算力...")

        # 👁️ 多模态：带图就组装结构化 content（base64 仅这一轮临时使用，不进 history）
        if image_b64:
            api_text = formatted_input or "看看这张图"
            user_content = [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": image_mime or "image/jpeg",
                                             "data": image_b64}},
                {"type": "text", "text": api_text},
            ]
            reply = call_claude(user_content, memory, history, u_time)
        else:
            reply = call_claude(formatted_input, memory, history, u_time)

        if not reply:
            send_telegram(chat_id, "😵 神经元短路了，稍后再试试？")
            return

        # 🔪 师兄的物理切割手术刀：切除大模型乱加的时间戳
        reply = re.sub(r'^\[202\d-[^\]]+\]\s*', '', reply.strip())

        # 群聊 60% 概率精准 reply，私聊正常发
        reply_id = msg_id if str(chat_id).startswith("-") and random.random() < 0.6 else None

        # 发送语音或文字：交给模型用 [语音] 前缀决定
        if reply.startswith("[语音]"):
            clean_reply = reply[4:].strip()
            send_telegram_voice(chat_id, clean_reply, reply_to_message_id=reply_id)
            reply = clean_reply
        else:
            send_telegram(chat_id, reply, reply_to_message_id=reply_id)
            
        # 记录 Bot 自己的回复
        b_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        history.append({"role": "assistant", "content": reply, "timestamp": b_time})
        
        # 💾 只有在真正开口说话的这一刻，才进行一次极其珍贵的 GitHub 存档！
        save_history(history, chat_id, force=True)  # bot 回复时强制写入
        
    except Exception as e:
        import traceback
        print(f"[CRITICAL] 后台崩了: {e}\n{traceback.format_exc()}")
        try:
            if should_reply: 
                send_telegram(chat_id, f"😵 出错了：{str(e)[:100]}")
        except: 
            pass

# ============ 路由接口 ============
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data: return "ok"
    
    msg = data["message"]
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id not in ALLOWED_IDS:
        return "ok"

    user_text = msg.get("text", "") or msg.get("caption", "") or ""
    image_b64 = None
    image_mime = None
    is_voice = False

    # 👁️ 图片：取最大一张
    if "photo" in msg and msg["photo"]:
        largest = msg["photo"][-1]
        blob = tg_download_file(largest.get("file_id", ""))
        if blob:
            raw, mime = blob
            image_b64 = base64.b64encode(raw).decode()
            image_mime = mime if mime.startswith("image/") else "image/jpeg"

    # 👂 语音 / 音频：转写
    elif "voice" in msg or "audio" in msg:
        node = msg.get("voice") or msg.get("audio")
        blob = tg_download_file(node.get("file_id", ""))
        if not blob:
            return "ok"
        transcript = transcribe_voice(*blob)
        if not transcript:
            send_telegram(chat_id, "🦻 听不清你说啥，再发一遍？",
                          reply_to_message_id=msg.get("message_id"))
            return "ok"
        user_text = transcript
        is_voice = True

    if not user_text and not image_b64:
        return "ok"

    # 👇 师兄核心逻辑重构
    should_reply = True

    if chat_id.startswith("-"): # 如果在群里
        replied = msg.get("reply_to_message", {}) or {}
        replying_to_bot = bool(replied.get("from", {}).get("is_bot"))
        if BOT_USERNAME and f"@{BOT_USERNAME}" not in user_text and not replying_to_bot:
            # 没被 @ 也不是回 bot，打上"只听不说"的标记，让现有冷却+概率路径决定
            should_reply = False
        elif BOT_USERNAME:
            # 被 @ 了，要把 @BotName 从文本里抠掉，免得大模型看着奇怪
            user_text = user_text.replace(f"@{BOT_USERNAME}", "").strip()

    msg_date = msg.get("date")
    msg_id = msg.get("message_id")
    sender_name = msg.get("from", {}).get("first_name", "神秘人")

    # 把 should_reply 开关传给后台线程
    Thread(target=process_message_background,
           args=(user_text, chat_id, sender_name, msg_date, should_reply, msg_id,
                 image_b64, image_mime, is_voice)).start()
    Thread(target=self_heal_webhook).start()
    return "ok"

@app.route("/health", methods=["GET"])
def health(): return "alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
